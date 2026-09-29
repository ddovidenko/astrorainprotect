import socket
import threading

from astrorainprotect.scope import online_hosts, parse_hosts, probe_hosts


def test_parse_hosts():
    assert parse_hosts("") == []
    assert parse_hosts("10.0.0.5") == [("10.0.0.5", 4700)]
    assert parse_hosts("seestar.lan:5555, 10.0.0.6 ,,") == [
        ("seestar.lan", 5555),
        ("10.0.0.6", 4700),
    ]


def test_parse_hosts_bad_port_falls_back_to_default():
    assert parse_hosts("host:abc") == [("host", 4700)]


def test_online_hosts_detects_listening_socket():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def accept_loop():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
                c.close()
            except TimeoutError:
                pass

    t = threading.Thread(target=accept_loop, daemon=True)
    t.start()
    try:
        assert online_hosts([("127.0.0.1", port)], timeout=1.0) == ["127.0.0.1"]
    finally:
        stop.set()
        t.join()
        srv.close()


def test_online_hosts_closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                     # nothing listens here now
    assert online_hosts([("127.0.0.1", port)], timeout=0.5) == []


def test_online_hosts_unresolvable():
    assert online_hosts([("no-such-host.invalid", 4700)], timeout=0.5) == []


def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_probe_retries_once_before_giving_up():
    """A scope in Wi-Fi power save can miss one connect; one miss must not read as offline."""
    pauses = []
    result = probe_hosts([("127.0.0.1", closed_port())], timeout=0.5, retries=1,
                         retry_pause=1.5, sleep=pauses.append)
    assert result == {"127.0.0.1": None}
    assert pauses == [1.5]


def test_probe_second_attempt_succeeds(monkeypatch):
    calls = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_connect(addr, timeout):
        calls.append((addr, timeout))
        if len(calls) == 1:
            raise TimeoutError("timed out")
        return Conn()

    monkeypatch.setattr("astrorainprotect.scope.socket.create_connection", fake_connect)
    result = probe_hosts([("10.0.0.5", 4700)], timeout=5.0, retries=1, sleep=lambda s: None)
    assert list(result) == ["10.0.0.5"] and result["10.0.0.5"] is not None
    assert result["10.0.0.5"] >= 0.0
    assert calls == [(("10.0.0.5", 4700), 5.0)] * 2


def test_default_timeout_is_five_seconds(monkeypatch):
    seen = []

    def fake_connect(addr, timeout):
        seen.append(timeout)
        raise ConnectionRefusedError

    monkeypatch.setattr("astrorainprotect.scope.socket.create_connection", fake_connect)
    monkeypatch.setattr("astrorainprotect.scope.time.sleep", lambda s: None)
    assert online_hosts([("10.0.0.5", 4700)]) == []
    assert seen == [5.0, 5.0]
