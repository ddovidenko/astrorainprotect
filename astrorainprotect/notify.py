"""ntfy client. Same headers as legacy/rain-check.sh."""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

TAGS = "loud_sound,bell"


class Notifier:
    def __init__(
        self, client: httpx.Client, url: str, token: str = "", priority: str = "high"
    ) -> None:
        self._client, self._url, self._token, self._priority = client, url, token, priority

    def send(self, title: str, message: str, priority: str | None = None,
             attachment: tuple[bytes, str] | None = None) -> bool:
        """POST the text; with an attachment, PUT the bytes with the text in headers.

        If the attachment PUT fails for any reason, retry as the plain text POST so an image
        problem never costs an alert.
        """
        headers = {"Title": title, "Priority": priority or self._priority, "Tags": TAGS}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if attachment is not None:
            data, filename = attachment
            put_headers = {**headers, "Filename": filename,
                           "Message": message.encode("ascii", "replace").decode()}
            try:
                r = self._client.put(self._url, content=data, headers=put_headers, timeout=30.0)
                if r.status_code == 200:
                    return True
                log.warning("ntfy attachment send returned HTTP %d: %s; sending text only",
                            r.status_code, r.text[:300])
            except httpx.HTTPError as exc:
                log.warning("ntfy attachment send failed (%s); sending text only", exc)
        try:
            r = self._client.post(
                self._url, content=message.encode(), headers=headers, timeout=15.0
            )
        except httpx.HTTPError as exc:
            log.error("ntfy send failed (%s): %s", self._url, exc)
            return False
        if r.status_code != 200:
            log.error("ntfy returned HTTP %d: %s", r.status_code, r.text[:300])
            return False
        return True
