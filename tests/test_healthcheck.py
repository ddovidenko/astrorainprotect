from astrorainprotect.healthcheck import main
from astrorainprotect.state import State

ENV = {"POLL_SEC": "180"}


def ok_state(tmp_path, now):
    st = State(tmp_path)
    st.heartbeat(now)
    st.contact(now)
    return {**ENV, "STATE_DIR": str(tmp_path)}


def test_healthy(tmp_path):
    env = ok_state(tmp_path, 1000.0)
    assert main(env, now=1000.0 + 539) == 0


def test_unhealthy_when_heartbeat_stale(tmp_path):
    env = ok_state(tmp_path, 1000.0)
    assert main(env, now=1000.0 + 541) == 1


def test_unhealthy_when_missing(tmp_path):
    assert main({"STATE_DIR": str(tmp_path)}, now=5.0) == 1


def test_unhealthy_when_nothing_external_answered_for_six_polls(tmp_path):
    """#62: the loop ran for 13 hours with no network while docker ps said healthy."""
    st = State(tmp_path)
    st.contact(1000.0)
    st.heartbeat(1000.0 + 6 * 180 + 1)
    env = {**ENV, "STATE_DIR": str(tmp_path)}
    assert main(env, now=1000.0 + 6 * 180 + 1) == 1


def test_healthy_within_six_polls_of_last_contact(tmp_path):
    st = State(tmp_path)
    st.contact(1000.0)
    st.heartbeat(1000.0 + 6 * 180 - 1)
    env = {**ENV, "STATE_DIR": str(tmp_path)}
    assert main(env, now=1000.0 + 6 * 180 - 1) == 0


def test_unhealthy_without_any_contact(tmp_path):
    State(tmp_path).heartbeat(1000.0)
    assert main({**ENV, "STATE_DIR": str(tmp_path)}, now=1000.0) == 1
