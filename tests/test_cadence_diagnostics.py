from __future__ import annotations

import ctypes
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import threading
import unittest

from helldivers_macro.cadence_diagnostics import (
    MAX_ANOMALIES,
    MAX_INJECTED_MOUSE_RECORDS,
    CadenceDiagnostics,
)
from helldivers_macro.config import load_config
from helldivers_macro.input_backend import (
    INPUT,
    INPUT_KEYBOARD,
    INPUT_MARKER,
    KEYEVENTF_KEYUP,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    SendInputBackend,
    VK_R,
)
from helldivers_macro.input_hooks import (
    HookPolicy,
    LLKHF_INJECTED,
    LLMHF_INJECTED,
    LLMHF_LOWER_IL_INJECTED,
    WM_KEYDOWN,
    WM_KEYUP,
    WM_LBUTTONDOWN,
    WM_LBUTTONUP,
)
from helldivers_macro.macro_engine import MacroEngine
from helldivers_macro.models import WeaponMode, WorkerProgress


ROOT = Path(__file__).resolve().parent.parent
CONFIG = load_config(ROOT / "config.toml")


class Function:
    def __init__(self, implementation):
        self.implementation = implementation
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.implementation(*args)


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def clock_ns(self) -> int:
        return round(self.now * 1_000_000_000)

    def wait(self, event: threading.Event, seconds: float) -> bool:
        self.now += seconds
        return event.is_set()


class HookedUser32:
    def __init__(self, policy: HookPolicy) -> None:
        self.policy = policy
        self.MapVirtualKeyW = Function(lambda _vk, _map_type: 0x13)
        self.SendInput = Function(self._send)

    def _send(self, _count, pointer, _size) -> int:
        value = INPUT.from_buffer_copy(ctypes.string_at(pointer, ctypes.sizeof(INPUT)))
        if value.type == INPUT_KEYBOARD:
            message = WM_KEYUP if value.ki.dwFlags & KEYEVENTF_KEYUP else WM_KEYDOWN
            self.policy.keyboard(
                message,
                VK_R,
                LLKHF_INJECTED,
                value.ki.wScan,
                value.ki.dwExtraInfo,
            )
        else:
            message = {
                MOUSEEVENTF_LEFTDOWN: WM_LBUTTONDOWN,
                MOUSEEVENTF_LEFTUP: WM_LBUTTONUP,
            }.get(value.mi.dwFlags)
            if message is not None:
                self.policy.mouse(
                    message,
                    LLMHF_INJECTED,
                    value.mi.dwExtraInfo,
                )
        return 1


class CadenceDiagnosticsTests(unittest.TestCase):
    def test_full_primary_cycle_correlates_owned_sendinput_and_hook_delivery(self) -> None:
        fake_time = FakeTime()
        diagnostics = CadenceDiagnostics(
            primary_shots_per_cycle=1,
            primary_fire_mode="automatic_hold",
            ownership_marker=INPUT_MARKER,
            fire_device="mouse",
            clock_ns=fake_time.clock_ns,
        )
        controller_events = []
        policy = HookPolicy(
            lambda: (True, True),
            controller_events.append,
            cadence_diagnostics=diagnostics,
            fire_device="mouse",
        )
        backend = SendInputBackend(
            user32=HookedUser32(policy),
            cadence_diagnostics=diagnostics,
            output=CONFIG.output,
        )
        cancel = threading.Event()

        def progress(update) -> None:
            if update.phase is WorkerProgress.RELOAD_COMPLETED:
                cancel.set()

        result = MacroEngine(
            CONFIG,
            backend,
            lambda: True,
            clock=fake_time.clock,
            wait=fake_time.wait,
            cadence_diagnostics=diagnostics,
        ).run_macro(
            WeaponMode.PRIMARY,
            cancel,
            threading.Event(),
            progress,
        )

        self.assertTrue(result.canceled)
        data = diagnostics.snapshot()
        shots = 1
        self.assertEqual(data["captured_primary_cycles"], 1)
        self.assertTrue(data["capture_complete"])
        self.assertEqual(data["extra_events_ignored_after_capture"], 0)
        self.assertEqual(data["intended"]["MB1_DOWN"], shots)
        self.assertEqual(data["intended"]["MB1_UP"], shots)
        self.assertEqual(data["intended"]["R_DOWN"], 1)
        self.assertEqual(data["intended"]["R_UP"], 1)
        self.assertEqual(data["send_requested"]["MB1_DOWN"], shots)
        self.assertEqual(data["send_requested"]["MB1_UP"], shots)
        self.assertEqual(data["send_accepted"]["MB1_DOWN"], shots)
        self.assertEqual(data["send_accepted"]["MB1_UP"], shots)
        self.assertEqual(data["hook_observed"]["MB1_DOWN"], shots)
        self.assertEqual(data["hook_observed"]["MB1_UP"], shots)
        self.assertEqual(data["hook_passed"], data["hook_observed"])
        self.assertEqual(sum(data["hook_suppressed"].values()), 0)
        self.assertEqual(sum(data["hook_routed"].values()), 0)
        self.assertEqual(controller_events, [])
        self.assertEqual(
            data["backend_down_intervals_ms"],
            [],
        )
        self.assertEqual(
            data["backend_down_durations_ms"],
            [CONFIG.primary.automatic_hold_ms],
        )
        self.assertEqual(data["final_up_to_reload_down_ms"], 0.0)
        self.assertEqual(data["pending_hook_event_count"], 0)
        self.assertEqual(data["mouse_hook_injected_callbacks"], 2)
        self.assertEqual(data["mouse_hook_marker_matches"], 2)
        self.assertEqual(data["mouse_hook_marker_mismatches"], 0)
        self.assertEqual(data["keyboard_hook_injected_callbacks"], 2)
        self.assertEqual(data["keyboard_hook_marker_matches"], 2)
        self.assertEqual(data["keyboard_hook_marker_mismatches"], 0)
        self.assertEqual(
            data["expected_ownership_marker_hex"], f"0x{INPUT_MARKER:x}"
        )
        self.assertEqual(data["mouse_hook_visibility"], "callback observed and owned; passed")
        self.assertEqual(len(data["backend_events"]), shots * 2 + 2)
        for record in data["backend_events"]:
            self.assertEqual(record["requested"], 1)
            self.assertEqual(record["accepted"], 1)
            self.assertIsNotNone(record["before_call_ms"])
            self.assertIsNotNone(record["after_call_ms"])
            self.assertIsNotNone(record["call_duration_ms"])

    def test_foreign_injected_mouse_is_visibility_only_and_never_routed(self) -> None:
        diagnostics = CadenceDiagnostics(clock_ns=lambda: 0)
        controller_events = []
        policy = HookPolicy(
            lambda: (True, True),
            controller_events.append,
            cadence_diagnostics=diagnostics,
        )
        diagnostics.macro_worker_started("PRIMARY")
        with diagnostics.macro_action("MB1_DOWN"):
            expected = diagnostics.send_requested()

        foreign_marker = 0x12345678
        self.assertFalse(
            policy.mouse(WM_LBUTTONDOWN, LLMHF_INJECTED, foreign_marker)
        )
        self.assertFalse(
            policy.mouse(
                WM_LBUTTONUP,
                LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED,
                foreign_marker,
            )
        )
        self.assertFalse(policy.mouse(0x0200, 0, 0))
        diagnostics.send_completed(expected, 1, 0)
        self.assertEqual(controller_events, [])
        data = diagnostics.snapshot()
        self.assertEqual(sum(data["hook_observed"].values()), 0)
        self.assertEqual(sum(data["hook_routed"].values()), 0)
        self.assertEqual(data["mouse_hook_injected_callbacks"], 2)
        self.assertEqual(data["mouse_hook_lower_il_callbacks"], 1)
        self.assertEqual(data["mouse_hook_marker_matches"], 0)
        self.assertEqual(data["mouse_hook_marker_mismatches"], 2)
        self.assertEqual(
            data["mouse_hook_visibility"],
            "callback observed with marker mismatch",
        )
        for record in data["injected_mouse_records"]:
            self.assertEqual(record["dwExtraInfo"], "0x12345678")
            self.assertNotIn("position", record)
            self.assertNotIn("pt", record)
            self.assertNotIn("x", record)
            self.assertNotIn("y", record)

    def test_records_are_bounded_and_recording_is_silent(self) -> None:
        tick = 0

        def clock_ns() -> int:
            nonlocal tick
            tick += 1_000_000
            return tick

        diagnostics = CadenceDiagnostics(primary_shots_per_cycle=2, clock_ns=clock_ns)
        with redirect_stdout(StringIO()) as output:
            diagnostics.macro_worker_started("PRIMARY")
            with diagnostics.macro_action("MB1_DOWN"):
                expected = diagnostics.send_requested()
            diagnostics.send_completed(expected, 1, 0)
            for _ in range(MAX_INJECTED_MOUSE_RECORDS + 20):
                diagnostics.observe_injected_mouse_event(
                    WM_LBUTTONDOWN,
                    LLMHF_INJECTED,
                    0,
                    marker_matches=False,
                    injected_flag=LLMHF_INJECTED,
                    lower_il_flag=0x02,
                )
            for _ in range(MAX_ANOMALIES + 5):
                diagnostics.observe_owned_hook_event(
                    "R_DOWN", passed=True, suppressed=False, routed=False
                )
                diagnostics.record_cleanup_release("MB1_UP")
            diagnostics.macro_worker_stopped()

        self.assertEqual(output.getvalue(), "")
        data = diagnostics.snapshot()
        self.assertEqual(
            len(data["injected_mouse_records"]), MAX_INJECTED_MOUSE_RECORDS
        )
        self.assertEqual(len(data["anomalies"]), MAX_ANOMALIES)
        self.assertEqual(len(data["cleanup_records"]), MAX_ANOMALIES)
        self.assertIn("CADENCE DIAGNOSTICS SUMMARY", diagnostics.format_summary())

    def test_capture_freezes_after_one_cycle_and_ignores_later_cycles(self) -> None:
        diagnostics = CadenceDiagnostics(primary_shots_per_cycle=2, clock_ns=lambda: 0)
        diagnostics.macro_worker_started("PRIMARY")

        for _cycle in range(2):
            for _shot in range(2):
                with diagnostics.macro_action("MB1_DOWN"):
                    down = diagnostics.send_requested()
                diagnostics.send_completed(down, 1, 0)
                with diagnostics.macro_action("MB1_UP"):
                    up = diagnostics.send_requested()
                diagnostics.send_completed(up, 1, 0)
            with diagnostics.macro_action("R_DOWN"):
                r_down = diagnostics.send_requested()
            diagnostics.send_completed(r_down, 1, 0)
            with diagnostics.macro_action("R_UP"):
                r_up = diagnostics.send_requested()
            diagnostics.send_completed(r_up, 1, 0)
        diagnostics.macro_worker_stopped()

        data = diagnostics.snapshot()
        self.assertEqual(data["captured_primary_cycles"], 1)
        self.assertTrue(data["capture_complete"])
        self.assertEqual(data["send_requested"]["MB1_DOWN"], 2)
        self.assertEqual(data["send_requested"]["MB1_UP"], 2)
        self.assertEqual(data["send_requested"]["R_DOWN"], 1)
        self.assertEqual(data["send_requested"]["R_UP"], 1)
        self.assertEqual(data["extra_events_ignored_after_capture"], 6)
        self.assertEqual(len(data["backend_events"]), 6)

    def test_secondary_activity_does_not_arm_or_contaminate_primary_capture(self) -> None:
        diagnostics = CadenceDiagnostics(primary_shots_per_cycle=1, clock_ns=lambda: 0)
        diagnostics.macro_worker_started("SECONDARY")
        for action in ("MB1_DOWN", "MB1_UP", "R_DOWN", "R_UP"):
            with diagnostics.macro_action(action):
                event = diagnostics.send_requested()
            self.assertIsNone(event)
        diagnostics.macro_worker_stopped()
        self.assertEqual(diagnostics.snapshot()["capture_state"], "ARMED")

        diagnostics.macro_worker_started("PRIMARY")
        for action in ("MB1_DOWN", "MB1_UP", "R_DOWN", "R_UP"):
            with diagnostics.macro_action(action):
                event = diagnostics.send_requested()
            diagnostics.send_completed(event, 1, 0)
        diagnostics.macro_worker_stopped()
        data = diagnostics.snapshot()
        self.assertTrue(data["capture_complete"])
        self.assertEqual(len(data["backend_events"]), 4)

    def test_canceled_automatic_hold_freezes_incomplete_without_reload(self) -> None:
        diagnostics = CadenceDiagnostics(
            primary_shots_per_cycle=1,
            primary_fire_mode="automatic_hold",
            clock_ns=lambda: 0,
        )
        diagnostics.macro_worker_started("PRIMARY")
        with diagnostics.macro_action("MB1_DOWN"):
            down = diagnostics.send_requested()
        diagnostics.send_completed(down, 1, 0)
        diagnostics.record_cleanup_release("MB1_UP")
        diagnostics.macro_worker_stopped()

        data = diagnostics.snapshot()
        self.assertEqual(data["capture_state"], "FROZEN")
        self.assertFalse(data["capture_complete"])
        self.assertEqual(data["captured_primary_cycles"], 0)
        self.assertEqual(data["intended"]["MB1_DOWN"], 1)
        self.assertEqual(data["intended"]["MB1_UP"], 0)
        self.assertEqual(data["intended"]["R_DOWN"], 0)
        self.assertEqual(data["cleanup_releases"], {"MB1_UP": 1})

    def test_backend_cadence_and_r_correlation_do_not_require_mouse_hooks(self) -> None:
        fake_time = FakeTime()
        diagnostics = CadenceDiagnostics(
            primary_shots_per_cycle=2,
            clock_ns=fake_time.clock_ns,
        )
        diagnostics.macro_worker_started("PRIMARY")
        for _shot in range(2):
            with diagnostics.macro_action("MB1_DOWN"):
                down = diagnostics.send_requested()
            diagnostics.send_completed(down, 1, 0)
            fake_time.now += 0.035
            with diagnostics.macro_action("MB1_UP"):
                up = diagnostics.send_requested()
            diagnostics.send_completed(up, 1, 0)
            fake_time.now += 0.015
        with diagnostics.macro_action("R_DOWN"):
            r_down = diagnostics.send_requested()
        diagnostics.observe_owned_hook_event(
            "R_DOWN", passed=True, suppressed=False, routed=False
        )
        diagnostics.send_completed(r_down, 1, 0)
        fake_time.now += 0.025
        with diagnostics.macro_action("R_UP"):
            r_up = diagnostics.send_requested()
        diagnostics.observe_owned_hook_event(
            "R_UP", passed=True, suppressed=False, routed=False
        )
        diagnostics.send_completed(r_up, 1, 0)

        data = diagnostics.snapshot()
        self.assertEqual(data["backend_down_intervals_ms"], [50.0])
        self.assertEqual(data["backend_down_durations_ms"], [35.0, 35.0])
        self.assertEqual(data["hook_observed"]["MB1_DOWN"], 0)
        self.assertEqual(data["hook_observed"]["MB1_UP"], 0)
        self.assertEqual(data["hook_observed"]["R_DOWN"], 1)
        self.assertEqual(data["hook_observed"]["R_UP"], 1)
        self.assertEqual(
            data["pending_expected_hook_events"]["mouse"],
            {"MB1_DOWN": 2, "MB1_UP": 2},
        )
        self.assertEqual(
            data["pending_expected_hook_events"]["keyboard"],
            {"R_DOWN": 0, "R_UP": 0},
        )
        self.assertEqual(data["anomaly_count"], 0)
        self.assertEqual(data["mouse_hook_visibility"], "callback never observed")

    def test_marker_owned_event_can_be_observed_before_sendinput_returns(self) -> None:
        diagnostics = CadenceDiagnostics(clock_ns=lambda: 1)
        diagnostics.macro_worker_started("PRIMARY")
        with diagnostics.macro_action("MB1_DOWN"):
            expected = diagnostics.send_requested()
        diagnostics.observe_owned_hook_event(
            "MB1_DOWN", passed=True, suppressed=False, routed=False
        )
        diagnostics.send_completed(expected, 1, 0)
        diagnostics.macro_worker_stopped()
        data = diagnostics.snapshot()
        self.assertEqual(data["hook_observed"]["MB1_DOWN"], 1)
        self.assertEqual(data["pending_hook_event_count"], 0)


if __name__ == "__main__":
    unittest.main()
