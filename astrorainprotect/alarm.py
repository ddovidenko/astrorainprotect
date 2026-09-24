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
    raining_now: bool
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


def compose_message(triggers: tuple[Trigger, ...]) -> str:
    etas = [t.eta_min for t in triggers if t.eta_min is not None]
    if len(triggers) == 1:
        detail = triggers[0].detail
        if etas:
            return f"Rain expected in about {round(min(etas))} min ({detail})"
        return f"Rain nearby: {detail}"
    sources = "; ".join(f"{t.source}: {t.detail}" for t in triggers)
    if etas:
        return f"Rain expected in about {round(min(etas))} min ({sources})"
    return f"Rain nearby: {sources}"


def decide(inputs: AlarmInputs) -> Decision:
    # Already raining: nothing left to warn about. Clearing the latch lets the next cell alert.
    active = () if inputs.raining_now else inputs.triggers
    if active:
        if not inputs.latched:
            return Decision(Action.SEND, TITLE_FIRST, compose_message(active))
        age = inputs.latch_age_sec
        if inputs.repeat_min > 0 and age is not None and age >= inputs.repeat_min * 60:
            return Decision(Action.REPEAT, TITLE_REPEAT, compose_message(active))
        return Decision(Action.SKIP)
    if inputs.latched:
        return Decision(Action.REARM)
    return Decision(Action.NONE)
