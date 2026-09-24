import socket
import threading

from astrorainprotect.scope import online_hosts, parse_hosts


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
