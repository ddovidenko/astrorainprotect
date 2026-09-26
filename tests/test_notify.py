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
