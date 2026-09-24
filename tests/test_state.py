from astrorainprotect.state import State


def test_latch_lifecycle(tmp_path):
    s = State(tmp_path / "state", tmp_path / "tmp")
    assert not s.latched()
    assert s.latch_age_sec(1000.0) is None
    s.set_latch(1000.0)
    assert s.latched()
    assert s.latch_age_sec(1060.0) == 60.0
    s.set_latch(1100.0)                          # re-touch resets the timer
    assert s.latch_age_sec(1130.0) == 30.0
    s.clear_latch()
    assert not s.latched()
    s.clear_latch()                              # idempotent


def test_latch_persists_across_instances(tmp_path):
    State(tmp_path / "state").set_latch(5.0)
    assert State(tmp_path / "state").latched()


def test_state_dir_created(tmp_path):
    s = State(tmp_path / "missing" / "state")
    s.set_latch(1.0)
    assert (tmp_path / "missing" / "state" / "alerted").exists()


def test_test_marker(tmp_path):
    s = State(tmp_path / "state", tmp_path / "tmp")
    assert not s.test_sent()
    s.mark_test_sent()
    assert s.test_sent()


def test_clear_test_marker_on_start(tmp_path):
    s = State(tmp_path / "state", tmp_path / "tmp")
    s.mark_test_sent()
    s.clear_test_marker()
    assert not s.test_sent()
    assert State(tmp_path / "state", tmp_path / "tmp").latched() is False


def test_heartbeat(tmp_path):
    s = State(tmp_path / "state", tmp_path / "tmp")
    assert s.heartbeat_age_sec(10.0) is None
    s.heartbeat(10.0)
    assert s.heartbeat_age_sec(25.0) == 15.0
