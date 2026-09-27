import logging

import httpx

from astrorainprotect.notify import TAGS, Notifier


def make(handler, **kw):
    return Notifier(
        httpx.Client(transport=httpx.MockTransport(handler)), "https://ntfy.example.net/rain", **kw
    )


def test_send_headers_with_token():
    seen = {}

    def handler(request):
        seen.update(headers=dict(request.headers), body=request.content, url=str(request.url))
        return httpx.Response(200, json={"id": "x"})

    assert make(handler, token="tk_abc", priority="urgent").send("Rain incoming", "hello") is True
    assert seen["url"] == "https://ntfy.example.net/rain"
    assert seen["body"] == b"hello"
    assert seen["headers"]["title"] == "Rain incoming"
    assert seen["headers"]["priority"] == "urgent"
    assert seen["headers"]["tags"] == TAGS == "loud_sound,bell"
    assert seen["headers"]["authorization"] == "Bearer tk_abc"


def test_send_without_token_has_no_auth_header():
    seen = {}

    def handler(request):
        seen.update(headers=dict(request.headers))
        return httpx.Response(200)

    make(handler).send("t", "m")
    assert "authorization" not in seen["headers"]
    assert seen["headers"]["priority"] == "high"


def test_send_failure_logs_status_and_body(caplog):
    caplog.set_level(logging.ERROR)
    n = make(lambda r: httpx.Response(403, text="forbidden " + "x" * 500))
    assert n.send("t", "m") is False
    rec = caplog.records[-1].getMessage()
    assert "403" in rec and "forbidden" in rec and len(rec) < 450


def test_send_network_error(caplog):
    caplog.set_level(logging.ERROR)

    def boom(request):
        raise httpx.ConnectError("refused")

    assert make(boom).send("t", "m") is False
    assert "refused" in caplog.records[-1].getMessage()


def test_send_priority_override():
    seen = {}

    def handler(request):
        seen.update(headers=dict(request.headers))
        return httpx.Response(200)

    make(handler, priority="urgent").send("t", "m", priority="default")
    assert seen["headers"]["priority"] == "default"


def test_send_with_attachment_uses_put_and_headers():
    seen = {}

    def handler(request):
        seen.update(method=request.method, headers=dict(request.headers), body=request.content)
        return httpx.Response(200, json={"id": "x"})

    n = make(handler, token="tk_abc")
    assert n.send("Rain incoming", "12 km SW", attachment=(b"\x89PNG...", "radar.png")) is True
    assert seen["method"] == "PUT"
    assert seen["body"] == b"\x89PNG..."
    assert seen["headers"]["filename"] == "radar.png"
    assert seen["headers"]["title"] == "Rain incoming"
    assert seen["headers"]["message"] == "12 km SW"
    assert seen["headers"]["authorization"] == "Bearer tk_abc"
    assert seen["headers"]["tags"] == TAGS


def test_attachment_failure_falls_back_to_text_post(caplog):
    caplog.set_level(logging.WARNING)
    calls = []

    def handler(request):
        calls.append((request.method, request.content))
        if request.method == "PUT":
            return httpx.Response(400, text="attachments disabled")
        return httpx.Response(200)

    assert make(handler).send("t", "m", attachment=(b"img", "radar.png")) is True
    assert [c[0] for c in calls] == ["PUT", "POST"]
    assert calls[1][1] == b"m"
    assert "attachment" in caplog.records[0].getMessage()


def test_attachment_network_error_falls_back(caplog):
    caplog.set_level(logging.WARNING)
    calls = []

    def handler(request):
        calls.append(request.method)
        if request.method == "PUT":
            raise httpx.ConnectError("refused")
        return httpx.Response(200)

    assert make(handler).send("t", "m", attachment=(b"img", "radar.png")) is True
    assert calls == ["PUT", "POST"]
