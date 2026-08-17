from __future__ import annotations

from collections import Counter, deque
from contextlib import contextmanager
from dataclasses import dataclass
import math
import statistics
import threading
import time
from typing import Iterator


SUPPORTED_ACTIONS = (
    "MB1_DOWN",
    "MB1_UP",
    "P_DOWN",
    "P_UP",
    "R_DOWN",
    "R_UP",
)
MAX_ANOMALIES = 32
MAX_INJECTED_MOUSE_RECORDS = 32


def _device_for(action: str) -> str:
    return "mouse" if action.startswith("MB1_") else "keyboard"


@dataclass
class _BackendEvent:
    sequence: int
    action: str
    before_ns: int
    after_ns: int | None = None
    accepted: int | None = None
    last_error: int = 0
    observed: bool = False

    @property
    def device(self) -> str:
        return _device_for(self.action)


class CadenceDiagnostics:
    """Bounded, opt-in accounting for one PRIMARY firing phase.

    The recorder arms on the first generated PRIMARY fire-down, captures one
    configured firing phase and its first R pair, then freezes. Hook callbacks
    perform only bounded in-memory bookkeeping; they never wait, log, call a
    backend, or acquire the backend output lock.
    """

    def __init__(
        self,
        *,
        primary_shots_per_cycle: int = 45,
        primary_fire_mode: str = "tap",
        ownership_marker: int | None = None,
        fire_device: str = "mouse",
        clock_ns=time.perf_counter_ns,
    ) -> None:
        if (
            isinstance(primary_shots_per_cycle, bool)
            or not isinstance(primary_shots_per_cycle, int)
            or primary_shots_per_cycle < 1
        ):
            raise ValueError("primary_shots_per_cycle must be a positive integer")
        self._primary_shots = primary_shots_per_cycle
        if primary_fire_mode not in {"tap", "automatic_hold"}:
            raise ValueError("primary_fire_mode must be 'tap' or 'automatic_hold'")
        self._primary_fire_mode = primary_fire_mode
        if fire_device not in {"keyboard", "mouse"}:
            raise ValueError("fire_device must be 'keyboard' or 'mouse'")
        self._fire_device = fire_device
        self._fire_down_action = "P_DOWN" if fire_device == "keyboard" else "MB1_DOWN"
        self._fire_up_action = "P_UP" if fire_device == "keyboard" else "MB1_UP"
        self._tracked_actions = (
            self._fire_down_action,
            self._fire_up_action,
            "R_DOWN",
            "R_UP",
        )
        self._mouse_actions = (
            ("MB1_DOWN", "MB1_UP") if fire_device == "mouse" else ()
        )
        self._keyboard_actions = (
            ("P_DOWN", "P_UP", "R_DOWN", "R_UP")
            if fire_device == "keyboard"
            else ("R_DOWN", "R_UP")
        )
        self._ownership_marker = ownership_marker
        self._clock_ns = clock_ns
        self._started_ns = clock_ns()
        self._capture_started_ns: int | None = None
        self._capture_state = "ARMED"
        self._capture_complete = False
        self._captured_primary_cycles = 0
        self._extra_events_ignored_after_capture = 0
        self._local = threading.local()
        self._next_sequence = 1
        self._intended: Counter[str] = Counter()
        self._send_requested: Counter[str] = Counter()
        self._send_accepted: Counter[str] = Counter()
        self._send_failures = 0
        self._send_last_errors: deque[str] = deque(maxlen=MAX_ANOMALIES)
        self._backend_events: list[_BackendEvent] = []
        self._pending: dict[str, deque[_BackendEvent]] = {
            action: deque() for action in self._tracked_actions
        }
        self._hook_observed: Counter[str] = Counter()
        self._hook_passed: Counter[str] = Counter()
        self._hook_suppressed: Counter[str] = Counter()
        self._hook_routed: Counter[str] = Counter()
        self._unmatched_observed: Counter[str] = Counter()
        self._last_observed_sequence = {"mouse": 0, "keyboard": 0}
        self._backend_down_intervals_ns: list[int] = []
        self._backend_down_durations_ns: list[int] = []
        self._previous_backend_down_ns: int | None = None
        self._active_backend_down_ns: int | None = None
        self._last_backend_fire_up_ns: int | None = None
        self._final_backend_fire_up_ns: int | None = None
        self._backend_reload_down_ns: int | None = None
        self._mouse_injected_callbacks = 0
        self._mouse_lower_il_callbacks = 0
        self._mouse_marker_matches = 0
        self._mouse_marker_mismatches = 0
        self._injected_mouse_records: deque[dict[str, object]] = deque(
            maxlen=MAX_INJECTED_MOUSE_RECORDS
        )
        self._keyboard_injected_callbacks = 0
        self._keyboard_marker_matches = 0
        self._keyboard_marker_mismatches = 0
        self._injected_keyboard_records: deque[dict[str, object]] = deque(
            maxlen=MAX_INJECTED_MOUSE_RECORDS
        )
        self._cleanup_releases: Counter[str] = Counter()
        self._cleanup_records: deque[str] = deque(maxlen=MAX_ANOMALIES)
        self._anomaly_count = 0
        self._anomalies: deque[str] = deque(maxlen=MAX_ANOMALIES)

    def macro_worker_started(self, mode: str) -> None:
        self._local.macro_scope = True
        self._local.mode = mode
        self._local.cleanup_reason = f"{mode} worker exit"

    def macro_worker_stopped(self) -> None:
        if (
            getattr(self._local, "mode", None) == "PRIMARY"
            and self._capture_state == "CAPTURING"
        ):
            self._capture_state = "FROZEN"
        self._local.action = None
        self._local.mode = None
        self._local.cleanup_reason = None
        self._local.macro_scope = False

    @contextmanager
    def macro_cleanup_scope(self, reason: str) -> Iterator[None]:
        previous_scope = getattr(self._local, "macro_scope", False)
        previous_reason = getattr(self._local, "cleanup_reason", None)
        self._local.macro_scope = True
        self._local.cleanup_reason = reason
        try:
            yield
        finally:
            self._local.cleanup_reason = previous_reason
            self._local.macro_scope = previous_scope

    @contextmanager
    def macro_action(self, action: str, *, intended: bool = True) -> Iterator[None]:
        previous = getattr(self._local, "action", None)
        accepted = intended and self._accept_macro_action(action)
        self._local.action = action if accepted else None
        try:
            yield
        finally:
            self._local.action = previous

    def _accept_macro_action(self, action: str) -> bool:
        if (
            not getattr(self._local, "macro_scope", False)
            or getattr(self._local, "mode", None) != "PRIMARY"
            or action not in self._tracked_actions
        ):
            return False
        if self._capture_state == "FROZEN":
            self._extra_events_ignored_after_capture += 1
            return False
        if self._capture_state == "ARMED":
            if action != self._fire_down_action:
                return False
            self._capture_state = "CAPTURING"
            self._capture_started_ns = self._clock_ns()

        down = self._intended[self._fire_down_action]
        up = self._intended[self._fire_up_action]
        r_down = self._intended["R_DOWN"]
        r_up = self._intended["R_UP"]
        valid = (
            (
                action == self._fire_down_action
                and down < self._primary_shots
                and down == up
            )
            or (action == self._fire_up_action and up < down)
            or (
                action == "R_DOWN"
                and down == up == self._primary_shots
                and r_down == 0
            )
            or (action == "R_UP" and r_down == 1 and r_up == 0)
        )
        if not valid:
            self._anomaly(f"unexpected captured PRIMARY action {action}")
            return False
        self._intended[action] += 1
        return True

    def send_requested(self) -> _BackendEvent | None:
        action = getattr(self._local, "action", None)
        if action not in self._tracked_actions:
            return None
        before_ns = self._clock_ns()
        expected = _BackendEvent(self._next_sequence, action, before_ns)
        self._next_sequence += 1
        self._send_requested[action] += 1
        self._backend_events.append(expected)
        self._pending[action].append(expected)
        self._record_backend_dispatch(action, before_ns)
        return expected

    def send_completed(
        self,
        expected: _BackendEvent | None,
        accepted: int,
        last_error: int,
    ) -> None:
        if expected is None:
            return
        expected.after_ns = self._clock_ns()
        expected.accepted = accepted
        expected.last_error = last_error
        self._send_accepted[expected.action] += accepted
        if accepted != 1:
            self._send_failures += 1
            self._send_last_errors.append(
                f"seq={expected.sequence} action={expected.action} "
                f"accepted={accepted}/1 WinError={last_error}"
            )
            if not expected.observed:
                try:
                    self._pending[expected.action].remove(expected)
                except ValueError:
                    pass
        if expected.action == "R_UP":
            self._capture_state = "FROZEN"
            self._capture_complete = self._has_complete_capture()
            self._captured_primary_cycles = int(self._capture_complete)

    def _record_backend_dispatch(self, action: str, occurred_ns: int) -> None:
        if action == self._fire_down_action:
            if self._previous_backend_down_ns is not None:
                self._backend_down_intervals_ns.append(
                    occurred_ns - self._previous_backend_down_ns
                )
            if self._active_backend_down_ns is not None:
                self._anomaly("backend fire-down requested while prior fire was down")
            self._previous_backend_down_ns = occurred_ns
            self._active_backend_down_ns = occurred_ns
        elif action == self._fire_up_action:
            if self._active_backend_down_ns is None:
                self._anomaly("backend fire-up requested without matching fire-down")
            else:
                self._backend_down_durations_ns.append(
                    occurred_ns - self._active_backend_down_ns
                )
            self._active_backend_down_ns = None
            self._last_backend_fire_up_ns = occurred_ns
        elif action == "R_DOWN":
            self._final_backend_fire_up_ns = self._last_backend_fire_up_ns
            self._backend_reload_down_ns = occurred_ns

    def observe_injected_keyboard_event(
        self,
        action: str,
        flags: int,
        extra_info: int,
        *,
        marker_matches: bool,
        injected_flag: int,
    ) -> None:
        if self._capture_state != "CAPTURING" or action not in self._keyboard_actions:
            return
        if flags & injected_flag:
            self._keyboard_injected_callbacks += 1
        if marker_matches:
            self._keyboard_marker_matches += 1
        else:
            self._keyboard_marker_mismatches += 1
            self._unmatched_observed["keyboard:MARKER_MISMATCH"] += 1
        self._injected_keyboard_records.append(
            {
                "action": action,
                "flags": f"0x{flags:x}",
                "dwExtraInfo": f"0x{int(extra_info):x}",
                "marker_matches": marker_matches,
            }
        )

    def observe_injected_mouse_event(
        self,
        message: int,
        flags: int,
        extra_info: int,
        *,
        marker_matches: bool,
        injected_flag: int,
        lower_il_flag: int,
    ) -> None:
        if self._capture_state != "CAPTURING":
            return
        if flags & injected_flag:
            self._mouse_injected_callbacks += 1
        if flags & lower_il_flag:
            self._mouse_lower_il_callbacks += 1
        if marker_matches:
            self._mouse_marker_matches += 1
        else:
            self._mouse_marker_mismatches += 1
            self._unmatched_observed["mouse:MARKER_MISMATCH"] += 1
        self._injected_mouse_records.append(
            {
                "message": self._mouse_message_name(message),
                "flags": f"0x{flags:x}",
                "dwExtraInfo": f"0x{int(extra_info):x}",
                "marker_matches": marker_matches,
                "llmhf_injected": bool(flags & injected_flag),
                "llmhf_lower_il_injected": bool(flags & lower_il_flag),
            }
        )

    def observe_owned_hook_event(
        self,
        action: str,
        *,
        passed: bool,
        suppressed: bool,
        routed: bool,
    ) -> None:
        if action not in self._tracked_actions or self._capture_state == "FROZEN":
            return
        queue = self._pending[action]
        if not queue:
            if self._capture_state == "CAPTURING":
                self._unmatched_observed[f"{_device_for(action)}:{action}"] += 1
                self._anomaly(f"owned hook observed unmatched {action}")
            return
        expected = queue.popleft()
        expected.observed = True
        device = expected.device
        previous_sequence = self._last_observed_sequence[device]
        if expected.sequence <= previous_sequence:
            self._anomaly(
                f"{device} hook order regressed from seq={previous_sequence} "
                f"to seq={expected.sequence}"
            )
        self._last_observed_sequence[device] = expected.sequence
        self._hook_observed[action] += 1
        if passed:
            self._hook_passed[action] += 1
        if suppressed:
            self._hook_suppressed[action] += 1
        if routed:
            self._hook_routed[action] += 1

    def record_cleanup_release(self, action: str) -> None:
        if not getattr(self._local, "macro_scope", False):
            return
        reason = getattr(self._local, "cleanup_reason", None) or "macro cleanup"
        self._cleanup_releases[action] += 1
        self._cleanup_records.append(f"{action}: {reason}")

    def _has_complete_capture(self) -> bool:
        return (
            self._intended[self._fire_down_action] == self._primary_shots
            and self._intended[self._fire_up_action] == self._primary_shots
            and self._intended["R_DOWN"] == 1
            and self._intended["R_UP"] == 1
        )

    @staticmethod
    def _mouse_message_name(message: int) -> str:
        return {
            0x0200: "WM_MOUSEMOVE",
            0x0201: "WM_LBUTTONDOWN",
            0x0202: "WM_LBUTTONUP",
            0x0204: "WM_RBUTTONDOWN",
            0x0205: "WM_RBUTTONUP",
        }.get(message, f"0x{message:x}")

    def _anomaly(self, message: str) -> None:
        self._anomaly_count += 1
        self._anomalies.append(message)

    def _counter_snapshot(self, counter: Counter[str]) -> dict[str, int]:
        return {action: counter[action] for action in self._tracked_actions}

    def _grouped_pending(self) -> dict[str, dict[str, int]]:
        return {
            "mouse": {action: len(self._pending[action]) for action in self._mouse_actions},
            "keyboard": {
                action: len(self._pending[action]) for action in self._keyboard_actions
            },
        }

    def _grouped_unmatched_observed(self) -> dict[str, dict[str, int]]:
        return {
            "mouse": {
                action: self._unmatched_observed[f"mouse:{action}"]
                for action in (*self._mouse_actions, "MARKER_MISMATCH")
            },
            "keyboard": {
                action: self._unmatched_observed[f"keyboard:{action}"]
                for action in (*self._keyboard_actions, "MARKER_MISMATCH")
            },
        }

    def _mouse_visibility(self) -> str:
        if not self._injected_mouse_records:
            return "callback never observed"
        if self._mouse_marker_matches == 0:
            return "callback observed with marker mismatch"
        if sum(self._hook_suppressed[action] for action in self._mouse_actions):
            return "callback observed and owned; suppressed"
        if sum(self._hook_routed[action] for action in self._mouse_actions):
            return "callback observed and owned; routed"
        return "callback observed and owned; passed"

    def snapshot(self) -> dict[str, object]:
        pending = self._grouped_pending()
        final_up_ms = self._relative_capture_ms(self._final_backend_fire_up_ns)
        reload_down_ms = self._relative_capture_ms(self._backend_reload_down_ns)
        final_gap_ms = (
            None
            if self._final_backend_fire_up_ns is None
            or self._backend_reload_down_ns is None
            else (
                self._backend_reload_down_ns - self._final_backend_fire_up_ns
            )
            / 1_000_000.0
        )
        backend_records = [
            {
                "sequence": event.sequence,
                "device": event.device,
                "action": event.action,
                "requested": 1,
                "accepted": event.accepted,
                "before_call_ms": self._relative_capture_ms(event.before_ns),
                "after_call_ms": self._relative_capture_ms(event.after_ns),
                "call_duration_ms": (
                    None
                    if event.after_ns is None
                    else (event.after_ns - event.before_ns) / 1_000_000.0
                ),
                "last_error": event.last_error,
                "hook_observed": event.observed,
            }
            for event in self._backend_events
        ]
        return {
            "capture_state": self._capture_state,
            "fire_device": self._fire_device,
            "primary_fire_mode": self._primary_fire_mode,
            "fire_down_action": self._fire_down_action,
            "fire_up_action": self._fire_up_action,
            "captured_primary_cycles": self._captured_primary_cycles,
            "capture_complete": self._capture_complete,
            "extra_events_ignored_after_capture": (
                self._extra_events_ignored_after_capture
            ),
            "intended": self._counter_snapshot(self._intended),
            "send_requested": self._counter_snapshot(self._send_requested),
            "send_accepted": self._counter_snapshot(self._send_accepted),
            "send_failures": self._send_failures,
            "send_last_errors": list(self._send_last_errors),
            "backend_events": backend_records,
            "backend_down_intervals_ms": [
                value / 1_000_000.0 for value in self._backend_down_intervals_ns
            ],
            "backend_down_durations_ms": [
                value / 1_000_000.0 for value in self._backend_down_durations_ns
            ],
            "hook_observed": self._counter_snapshot(self._hook_observed),
            "hook_passed": self._counter_snapshot(self._hook_passed),
            "hook_suppressed": self._counter_snapshot(self._hook_suppressed),
            "hook_routed": self._counter_snapshot(self._hook_routed),
            "mouse_hook_injected_callbacks": self._mouse_injected_callbacks,
            "mouse_hook_lower_il_callbacks": self._mouse_lower_il_callbacks,
            "mouse_hook_marker_matches": self._mouse_marker_matches,
            "mouse_hook_marker_mismatches": self._mouse_marker_mismatches,
            "mouse_hook_visibility": self._mouse_visibility(),
            "keyboard_hook_injected_callbacks": self._keyboard_injected_callbacks,
            "keyboard_hook_marker_matches": self._keyboard_marker_matches,
            "keyboard_hook_marker_mismatches": self._keyboard_marker_mismatches,
            "expected_ownership_marker_hex": (
                "not supplied"
                if self._ownership_marker is None
                else f"0x{self._ownership_marker:x}"
            ),
            "injected_mouse_records": list(self._injected_mouse_records),
            "injected_keyboard_records": list(self._injected_keyboard_records),
            "cleanup_releases": dict(self._cleanup_releases),
            "cleanup_records": list(self._cleanup_records),
            "final_fire_up_ms": final_up_ms,
            "final_mb1_up_ms": final_up_ms if self._fire_device == "mouse" else None,
            "reload_down_ms": reload_down_ms,
            "final_up_to_reload_down_ms": final_gap_ms,
            "pending_expected_hook_events": pending,
            "unmatched_expected_hook_events": pending,
            "pending_hook_event_count": sum(
                count for values in pending.values() for count in values.values()
            ),
            "unmatched_observed_hook_events": self._grouped_unmatched_observed(),
            "anomaly_count": self._anomaly_count,
            "anomalies": list(self._anomalies),
        }

    def _relative_capture_ms(self, timestamp_ns: int | None) -> float | None:
        if timestamp_ns is None:
            return None
        origin = self._capture_started_ns or self._started_ns
        return (timestamp_ns - origin) / 1_000_000.0

    def _format_counter(self, values: dict[str, int]) -> str:
        return " ".join(
            f"{action}={values[action]}" for action in self._tracked_actions
        )

    @staticmethod
    def _format_stats(values: list[float]) -> str:
        if not values:
            return "n=0 min=n/a median=n/a average=n/a p95=n/a max=n/a"
        ordered = sorted(values)
        p95_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
        return (
            f"n={len(values)} min={ordered[0]:.3f} "
            f"median={statistics.median(ordered):.3f} "
            f"average={statistics.fmean(ordered):.3f} "
            f"p95={ordered[p95_index]:.3f} max={ordered[-1]:.3f}"
        )

    @staticmethod
    def _format_optional(value: float | None) -> str:
        return "not observed" if value is None else f"{value:.3f}"

    @staticmethod
    def _format_grouped(values: dict[str, dict[str, int]]) -> str:
        return " ".join(
            f"{device}["
            + " ".join(f"{action}={count}" for action, count in actions.items())
            + "]"
            for device, actions in values.items()
        )

    def format_summary(self) -> str:
        data = self.snapshot()
        lines = ["CADENCE DIAGNOSTICS SUMMARY"]
        lines.append(f"fire_device: {data['fire_device']}")
        lines.append(f"primary_fire_mode: {data['primary_fire_mode']}")
        lines.append(f"captured_primary_cycles: {data['captured_primary_cycles']}")
        lines.append(f"capture_complete: {str(data['capture_complete']).lower()}")
        lines.append(
            "extra_events_ignored_after_capture: "
            f"{data['extra_events_ignored_after_capture']}"
        )
        lines.append(f"intended: {self._format_counter(data['intended'])}")
        lines.append(
            f"SendInput requested: {self._format_counter(data['send_requested'])}"
        )
        lines.append(
            f"SendInput accepted: {self._format_counter(data['send_accepted'])}"
        )
        lines.append(f"SendInput failures: {data['send_failures']}")
        errors = data["send_last_errors"]
        lines.append("SendInput last errors: " + ("; ".join(errors) if errors else "none"))
        if data["primary_fire_mode"] == "automatic_hold":
            lines.append(
                "automatic MB1 hold duration ms: "
                + self._format_stats(data["backend_down_durations_ms"])
            )
        else:
            lines.append(
                "backend-dispatch down-to-down ms: "
                + self._format_stats(data["backend_down_intervals_ms"])
            )
            lines.append(
                "backend-dispatch down-duration ms: "
                + self._format_stats(data["backend_down_durations_ms"])
            )
        lines.append("backend SendInput call records:")
        for record in data["backend_events"]:
            lines.append(
                "  seq={sequence} device={device} action={action} requested=1 "
                "accepted={accepted} before_ms={before_call_ms:.3f} "
                "after_ms={after_call_ms:.3f} duration_ms={call_duration_ms:.3f} "
                "last_error={last_error} hook_observed={hook_observed}".format(
                    **record
                )
            )
        lines.append(f"owned hook observed: {self._format_counter(data['hook_observed'])}")
        lines.append(f"owned hook passed: {self._format_counter(data['hook_passed'])}")
        lines.append(
            f"owned hook suppressed: {self._format_counter(data['hook_suppressed'])}"
        )
        lines.append(f"owned hook routed: {self._format_counter(data['hook_routed'])}")
        lines.append(
            "mouse hook injected callbacks: "
            f"{data['mouse_hook_injected_callbacks']}"
        )
        lines.append(
            "mouse hook lower-integrity injected callbacks: "
            f"{data['mouse_hook_lower_il_callbacks']}"
        )
        lines.append(
            f"mouse hook marker matches: {data['mouse_hook_marker_matches']}"
        )
        lines.append(
            f"mouse hook marker mismatches: {data['mouse_hook_marker_mismatches']}"
        )
        lines.append(f"mouse hook visibility: {data['mouse_hook_visibility']}")
        lines.append(
            "keyboard hook injected callbacks: "
            f"{data['keyboard_hook_injected_callbacks']}"
        )
        lines.append(
            "keyboard hook marker matches: "
            f"{data['keyboard_hook_marker_matches']}"
        )
        lines.append(
            "keyboard hook marker mismatches: "
            f"{data['keyboard_hook_marker_mismatches']}"
        )
        lines.append(
            "backend ownership marker: "
            f"{data['expected_ownership_marker_hex']}"
        )
        lines.append(
            "bounded injected mouse records: "
            + (
                "; ".join(
                    "message={message} flags={flags} dwExtraInfo={dwExtraInfo} "
                    "marker_matches={marker_matches}".format(**record)
                    for record in data["injected_mouse_records"]
                )
                if data["injected_mouse_records"]
                else "none"
            )
        )
        lines.append(
            "bounded injected keyboard records: "
            + (
                "; ".join(
                    "action={action} flags={flags} dwExtraInfo={dwExtraInfo} "
                    "marker_matches={marker_matches}".format(**record)
                    for record in data["injected_keyboard_records"]
                )
                if data["injected_keyboard_records"]
                else "none"
            )
        )
        lines.append(
            "pending expected hook events: "
            + self._format_grouped(data["pending_expected_hook_events"])
        )
        lines.append(
            "unmatched expected hook events: "
            + self._format_grouped(data["unmatched_expected_hook_events"])
        )
        lines.append(
            "unmatched observed hook events: "
            + self._format_grouped(data["unmatched_observed_hook_events"])
        )
        lines.append(
            "cleanup releases: "
            + (str(data["cleanup_releases"]) if data["cleanup_releases"] else "none")
        )
        lines.append(
            f"final {data['fire_up_action']} ms: "
            + self._format_optional(data["final_fire_up_ms"])
        )
        lines.append(
            "reload R-down ms: " + self._format_optional(data["reload_down_ms"])
        )
        lines.append(
            f"final {data['fire_up_action']} -> R-down ms: "
            + self._format_optional(data["final_up_to_reload_down_ms"])
        )
        lines.append(f"anomalies: {data['anomaly_count']}")
        anomaly_records = data["anomalies"]
        lines.append(
            "bounded anomaly records: "
            + ("; ".join(anomaly_records) if anomaly_records else "none")
        )
        cleanup_records = data["cleanup_records"]
        lines.append(
            "bounded cleanup records: "
            + ("; ".join(cleanup_records) if cleanup_records else "none")
        )
        return "\n".join(lines)
