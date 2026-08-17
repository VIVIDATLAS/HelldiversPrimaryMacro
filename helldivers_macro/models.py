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


class AimState(Enum):
    AIM_OFF = auto()
    AIM_ON = auto()
    AIM_OFF_PENDING = auto()
    UNKNOWN = auto()


class PreparationLifecycle(Enum):
    IDLE_UNKNOWN = auto()
    PREPARING = auto()
    IDLE_FULL_ARMED = auto()
    PREPARATION_FAILED = auto()


class Mb1PairDecision(Enum):
    SUPPRESS_TOGGLE = auto()
    PASS_THROUGH = auto()
    DEFERRED_BYPASS = auto()


class Mb2PairDecision(Enum):
    PASS_THROUGH = auto()
    DEFERRED_AIM_OFF = auto()


class WorkerKind(Enum):
    MACRO = auto()
    PREPARATION = auto()
    RELOAD_ONLY = auto()
    SHIFT_TRANSACTION = auto()
    AIM_OFF_TRANSACTION = auto()
    BYPASS = auto()


class WorkerProgress(Enum):
    SHOT_BEGAN = auto()
    FINAL_SHOT_DOWN = auto()
    FINAL_SHOT_UP = auto()
    RELOAD_KEY_DOWN = auto()
    RELOAD_KEY_UP = auto()
    RELOAD_WAIT_STARTED = auto()
    RELOAD_COMPLETED = auto()
    RELOAD_FAILED = auto()
    AIM_OFF_SENT = auto()
    AIM_OFF_REPLAY_DOWN = auto()
    AIM_OFF_REPLAY_UP = auto()
    SHIFT_REPLAY_DOWN = auto()
    SHIFT_REPLAY_UP = auto()
    # Compatibility alias for existing callers while trace output uses the
    # approved RELOAD_COMPLETED diagnostic name.
    RELOAD_COMPLETE = RELOAD_COMPLETED


@dataclass(frozen=True)
class WorkerProgressUpdate:
    phase: WorkerProgress
    occurred_at: float
    reason: str


class MacroState(Enum):
    IDLE_PRIMARY = auto()
    IDLE_SECONDARY = auto()
    RUNNING_PRIMARY = auto()
    RUNNING_SECONDARY = auto()
    PREPARING_PRIMARY = auto()
    PREPARING_SECONDARY = auto()
    RELOADING_PRIMARY = auto()
    RELOADING_SECONDARY = auto()
    FORWARDING_BYPASS = auto()
    STOPPING = auto()
    SHUTTING_DOWN = auto()


class ControlEventKind(Enum):
    PHYSICAL_MB1_DOWN = auto()
    PHYSICAL_MB1_UP = auto()
    PHYSICAL_MB2_DOWN = auto()
    PHYSICAL_MB2_UP = auto()
    DEFERRED_AIM_OFF = auto()
    MANUAL_BYPASS_DOWN = auto()
    DEFERRED_BYPASS_DOWN = auto()
    DEFERRED_BYPASS_UP = auto()
    SELECT_PRIMARY = auto()
    SELECT_SECONDARY = auto()
    CTRL_DOWN = auto()
    CTRL_UP = auto()
    SHIFT_DOWN = auto()
    SHIFT_UP = auto()
    FOREGROUND_ACTIVE = auto()
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
class ShiftStroke:
    vk_code: int
    scan_code: int


@dataclass(frozen=True)
class WorkerRequest:
    kind: WorkerKind
    mode: WeaponMode
    switch_settle_ms: int = 0
    bypass_release: Any = None
    bypass_click_ms: int = 0
    preparation_generation: int = 0
    generation: int = 0
    shift_vk_code: int = 0
    shift_scan_code: int = 0
    cancel_aim: bool = False
    native_aim_cancel: bool = False
    normalize_unknown_aim: bool = False


@dataclass(frozen=True)
class WorkerResult:
    success: bool
    canceled: bool = False
    error: BaseException | None = None


class OutputAction(Enum):
    FIRE_DOWN = "FIRE_DOWN"
    FIRE_UP = "FIRE_UP"
    R_DOWN = "R_DOWN"
    R_UP = "R_UP"
    WAIT = "WAIT"
    # Compatibility aliases for older fake-cycle callers. Runtime cycle
    # construction and execution use the device-neutral FIRE names.
    MB1_DOWN = FIRE_DOWN
    MB1_UP = FIRE_UP


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
