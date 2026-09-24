from astrorainprotect.healthcheck import main
from astrorainprotect.state import State


def test_healthy(tmp_path):
    State(tmp_path).heartbeat(1000.0)
    assert main({"STATE_DIR": str(tmp_path), "POLL_SEC": "180"}, now=1000.0 + 539) == 0


def test_unhealthy_when_stale(tmp_path):
    State(tmp_path).heartbeat(1000.0)
    assert main({"STATE_DIR": str(tmp_path), "POLL_SEC": "180"}, now=1000.0 + 541) == 1


def test_unhealthy_when_missing(tmp_path):
    assert main({"STATE_DIR": str(tmp_path)}, now=5.0) == 1
