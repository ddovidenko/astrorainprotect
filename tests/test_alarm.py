import pytest

from astrorainprotect.alarm import Action, AlarmInputs, Decision, Trigger, compose_message, decide

RADAR = Trigger(source="radar", eta_min=None, detail="reflectivity 41 dBZ, 8.2 km to the SW")
RADAR_ETA = Trigger(source="radar", eta_min=12.0, detail="reflectivity 41 dBZ, 8.2 km to the SW")
PW = Trigger(source="pirate weather", eta_min=25.0, detail="prob 60%")


RAINING = Trigger(source="house", eta_min=0.0, detail="0.3 mm/h")


def inputs(triggers=(), latched=False, latch_age_sec=None, repeat_min=0):
    return AlarmInputs(triggers=tuple(triggers), latched=latched,
                       latch_age_sec=latch_age_sec, repeat_min=repeat_min)


def test_first_alert():
    d = decide(inputs([RADAR]))
    assert d.action is Action.SEND
    assert d.title == "Rain incoming"
    assert "8.2 km to the SW" in d.message


def test_latched_skips():
    d = decide(inputs([RADAR], latched=True, latch_age_sec=60, repeat_min=0))
    assert d.action is Action.SKIP


def test_repeat_after_interval():
    d = decide(inputs([RADAR_ETA], latched=True, latch_age_sec=601, repeat_min=10))
    assert d.action is Action.REPEAT
    assert d.title == "Rain incoming (still)"
    assert "12 min" in d.message


def test_repeat_exactly_at_interval():
    d = decide(inputs([RADAR], latched=True, latch_age_sec=600, repeat_min=10))
    assert d.action is Action.REPEAT


def test_no_repeat_before_interval():
    d = decide(inputs([RADAR], latched=True, latch_age_sec=599, repeat_min=10))
    assert d.action is Action.SKIP


def test_no_repeat_when_disabled():
    d = decide(inputs([RADAR], latched=True, latch_age_sec=99999, repeat_min=0))
    assert d.action is Action.SKIP


def test_rearm_when_no_trigger():
    assert decide(inputs([], latched=True, latch_age_sec=5)).action is Action.REARM


def test_raining_trigger_sends_when_unlatched():
    d = decide(inputs([RAINING]))
    assert d.action is Action.SEND
    assert d.title == "Currently raining"
    assert d.message == "Currently raining at the house (0.3 mm/h)"


def test_raining_with_nearby_storm_lists_both():
    d = decide(inputs([RADAR, RAINING]))
    assert d.title == "Currently raining"
    assert d.message == (
        "Currently raining at the house (0.3 mm/h); radar: reflectivity 41 dBZ, 8.2 km to the SW"
    )


def test_raining_repeat_title():
    d = decide(inputs([RAINING], latched=True, latch_age_sec=601, repeat_min=10))
    assert d.action is Action.REPEAT
    assert d.title == "Currently raining (still)"


def test_raining_trigger_latched_skips():
    d = decide(inputs([RAINING], latched=True, latch_age_sec=5))
    assert d.action is Action.SKIP


def test_nothing_to_do():
    assert decide(inputs([])).action is Action.NONE


@pytest.mark.parametrize("age", [None, 0.0])
def test_latched_with_unknown_or_zero_age_never_repeats(age):
    d = decide(inputs([RADAR], latched=True, latch_age_sec=age, repeat_min=1))
    assert d.action is Action.SKIP


def test_message_uses_smallest_eta_and_lists_sources():
    msg = compose_message((PW, RADAR_ETA))
    assert msg.startswith("Rain expected in about 12 min")
    assert "radar: reflectivity 41 dBZ, 8.2 km to the SW" in msg
    assert "pirate weather: prob 60%" in msg


def test_message_without_eta_uses_detail():
    msg = compose_message((RADAR,))
    assert msg.startswith("Rain nearby: radar: reflectivity 41 dBZ, 8.2 km to the SW")


def test_decision_is_frozen():
    d = Decision(Action.NONE)
    with pytest.raises(AttributeError):
        d.title = "x"  # type: ignore[misc]


def test_eta_under_half_minute_reads_arriving_now():
    """Issue #19: a projected ETA that rounds to 0 must not read 'about 0 min'."""
    t = Trigger(source="radar", eta_min=0.2, detail="reflectivity 41 dBZ, 3.0 km to the W")
    d = decide(inputs([t]))
    assert d.title == "Rain incoming"
    assert d.message == "Rain arriving now (radar: reflectivity 41 dBZ, 3.0 km to the W)"
