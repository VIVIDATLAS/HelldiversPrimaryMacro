from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Any


class WeaponMode(Enum):
    PRIMARY = "PRIMARY"
    SECONDARY = "SECONDARY"


class MagazineState(Enum):
    FULL = "FULL"
    UNKNOWN = "UNKNOWN"


class PreparationLifecycle(Enum):
    IDLE_UNKNOWN = auto()
    PREPARING = auto()
    IDLE_FULL_ARMED = auto()
    PREPARATION_FAILED = auto()


class Mb1PairDecision(Enum):
    SUPPRESS_TOGGLE = auto()
    PASS_THROUGH = auto()
    DEFERRED_BYPASS = auto()


class WorkerKind(Enum):
    MACRO = auto()
    PREPARATION = auto()
    BYPASS = auto()


class WorkerProgress(Enum):
    SHOT_BEGAN = auto()
    RELOAD_COMPLETE = auto()


class MacroState(Enum):
    IDLE_PRIMARY = auto()
    IDLE_SECONDARY = auto()
    RUNNING_PRIMARY = auto()
    RUNNING_SECONDARY = auto()
    PREPARING_PRIMARY = auto()
    PREPARING_SECONDARY = auto()
    FORWARDING_BYPASS = auto()
    STOPPING = auto()
    SHUTTING_DOWN = auto()


class ControlEventKind(Enum):
    PHYSICAL_MB1_DOWN = auto()
    PHYSICAL_MB1_UP = auto()
    MANUAL_BYPASS_DOWN = auto()
    DEFERRED_BYPASS_DOWN = auto()
    DEFERRED_BYPASS_UP = auto()
    SELECT_PRIMARY = auto()
    SELECT_SECONDARY = auto()
    CTRL_DOWN = auto()
    CTRL_UP = auto()
    FOREGROUND_LOST = auto()
    FOREGROUND_UNCERTAIN = auto()
    HOOK_FAILURE = auto()
    WORKER_STOPPED = auto()
    WORKER_PROGRESS = auto()
    DIAGNOSTIC = auto()
    SHUTDOWN = auto()


class EventSource(Enum):
    PHYSICAL = "physical"
    INJECTED_OWNED = "injected-owned"
    INJECTED_BYPASS = "injected-bypass"
    WORKER = "worker"
    FOREGROUND = "foreground"
    SHUTDOWN = "shutdown"


@dataclass(frozen=True)
class ControlEvent:
    kind: ControlEventKind
    detail: Any = None
    worker_token: int | None = None
    source: EventSource = EventSource.PHYSICAL


@dataclass(frozen=True)
class WorkerRequest:
    kind: WorkerKind
    mode: WeaponMode
    switch_settle_ms: int = 0
    bypass_release: Any = None
    bypass_click_ms: int = 0
    preparation_generation: int = 0
    generation: int = 0


@dataclass(frozen=True)
class WorkerResult:
    success: bool
    canceled: bool = False
    error: BaseException | None = None


class OutputAction(Enum):
    MB1_DOWN = "MB1_DOWN"
    MB1_UP = "MB1_UP"
    R_DOWN = "R_DOWN"
    R_UP = "R_UP"
    WAIT = "WAIT"


@dataclass(frozen=True)
class CycleStep:
    action: OutputAction
    duration_ms: int = 0


@dataclass(frozen=True)
class ForegroundObservation:
    active: bool
    certain: bool
    timestamp: float
    pid: int | None = None
    executable: str | None = None
    error: str | None = None
