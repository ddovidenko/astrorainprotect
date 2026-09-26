"""The alert state machine, ported from legacy/rain-check.sh. Pure."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class Action(Enum):
    SEND = auto()
    REPEAT = auto()
    SKIP = auto()
    REARM = auto()
    NONE = auto()


@dataclass(frozen=True)
class Trigger:
    source: str            # "radar" or "pirate weather"
    eta_min: float | None  # minutes until rain, when known
    detail: str            # human text for the message body


@dataclass(frozen=True)
class AlarmInputs:
    triggers: tuple[Trigger, ...]
    latched: bool
    latch_age_sec: float | None
    repeat_min: int


@dataclass(frozen=True)
class Decision:
    action: Action
    title: str = ""
    message: str = ""


TITLE_FIRST = "Rain incoming"
TITLE_REPEAT = "Rain incoming (still)"
TITLE_RAINING = "Currently raining"
TITLE_RAINING_REPEAT = "Currently raining (still)"
HOUSE = "house"  # Trigger.source for rain at the house; detail is the rate, e.g. "0.8 mm/h"


def _raining_at_house(triggers: tuple[Trigger, ...]) -> Trigger | None:
    return next((t for t in triggers if t.source == HOUSE), None)


def compose_message(triggers: tuple[Trigger, ...]) -> str:
    house = _raining_at_house(triggers)
    others = tuple(t for t in triggers if t.source != HOUSE)
    sources = "; ".join(f"{t.source}: {t.detail}" for t in others)
    if house is not None:
        head = f"Currently raining at the house ({house.detail})"
        return f"{head}; {sources}" if sources else head
    etas = [t.eta_min for t in others if t.eta_min is not None]
    if etas:
        return f"Rain expected in about {round(min(etas))} min ({sources})"
    return f"Rain nearby: {sources}"


def decide(inputs: AlarmInputs) -> Decision:
    # Rain at the house arrives as a "house" trigger with ETA 0, so it alerts like any other
    # trigger. The latch clears only once no trigger remains.
    active = inputs.triggers
    if active:
        raining = _raining_at_house(active) is not None
        if not inputs.latched:
            title = TITLE_RAINING if raining else TITLE_FIRST
            return Decision(Action.SEND, title, compose_message(active))
        age = inputs.latch_age_sec
        if inputs.repeat_min > 0 and age is not None and age >= inputs.repeat_min * 60:
            title = TITLE_RAINING_REPEAT if raining else TITLE_REPEAT
            return Decision(Action.REPEAT, title, compose_message(active))
        return Decision(Action.SKIP)
    if inputs.latched:
        return Decision(Action.REARM)
    return Decision(Action.NONE)
