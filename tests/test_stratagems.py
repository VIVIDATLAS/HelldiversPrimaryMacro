from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
import threading
import tomllib
import unittest

from helldivers_macro import app
from helldivers_macro.config import ConfigError, load_config, parse_config
from helldivers_macro.input_backend import (
    INPUT_MARKER,
    KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP,
    KEYEVENTF_SCANCODE,
    InputApiError,
    SendInputBackend,
)
from helldivers_macro.input_hooks import (
    HookPolicy,
    LLKHF_INJECTED,
    VK_1,
    VK_F23,
    VK_F24,
    WM_KEYDOWN,
    WM_KEYUP,
)
from helldivers_macro.macro_engine import MacroEngine
from helldivers_macro.models import ControlEventKind, MacroState, WorkerKind, WorkerResult
from helldivers_macro.simulation import FakeClock, SimulationHarness
from helldivers_macro.stratagems import (
    Direction,
    FOUR_TARGET_SEQUENCES,
    SUPPORT_SEQUENCES,
    sequence_duration_ms,
)
from tests.test_input_backend import FakeUser32


ROOT = Path(__file__).resolve().parent.parent
CONFIG = load_config(ROOT / "config.toml")


class StratagemBackend:
    def __init__(self, clock: FakeClock, fail_on: str | None = None) -> None:
        self.clock = clock
        self.fail_on = fail_on
        self.events: list[tuple[str, int, int]] = []
        self.owner: int | None = None
        self.scan: tuple[int, bool] | None = None
        self.ctrl = False
        self.mouse = False
        self.release_calls = 0

    def _record(self, name: str, token: int) -> None:
        self.events.append((name, token, round(self.clock() * 1000)))
        if name == self.fail_on:
            raise InputApiError(name)

    def stratagem_key_down(
        self, token: int, scan_code: int, *, extended: bool, ctrl: bool = False
    ) -> None:
        self.owner = token
        self._record("CTRL_DOWN" if ctrl else f"{scan_code:02X}_DOWN_EXT", token)
        if ctrl:
            self.ctrl = True
        else:
            self.scan = (scan_code, extended)

    def stratagem_key_up(
        self, token: int, scan_code: int, *, extended: bool, ctrl: bool = False
    ) -> None:
        if self.owner != token:
            return
        self._record("CTRL_UP" if ctrl else f"{scan_code:02X}_UP_EXT", token)
        if ctrl:
            self.ctrl = False
        else:
            self.scan = None

    def stratagem_mouse_down(self, token: int) -> None:
        self.owner = token
        self._record("MB1_DOWN", token)
        self.mouse = True

    def stratagem_mouse_up(self, token: int) -> None:
        if self.owner == token and self.mouse:
            self._record("MB1_UP", token)
            self.mouse = False

    def release_stratagem(self, token: int) -> None:
        self.release_calls += 1
        if self.owner != token:
            return
        if self.scan is not None:
            scan, extended = self.scan
            self.stratagem_key_up(token, scan, extended=extended)
        if self.ctrl:
            self.stratagem_key_up(token, 0x1D, extended=False, ctrl=True)
        self.stratagem_mouse_up(token)
        self.owner = None


class StratagemTests(unittest.TestCase):
    def test_configuration_defaults_normalization_and_strict_validation(self) -> None:
        self.assertEqual(CONFIG.stratagems.four_target_trigger, "F23")
        self.assertEqual(CONFIG.stratagems.support_trigger, "F24")
        self.assertEqual(
            (
                CONFIG.stratagems.key_press_ms,
                CONFIG.stratagems.key_gap_ms,
                CONFIG.stratagems.ctrl_settle_ms,
                CONFIG.stratagems.action_press_ms,
                CONFIG.stratagems.action_delay_ms,
            ),
            (20, 20, 20, 20, 800),
        )
        with (ROOT / "config.toml").open("rb") as handle:
            raw = tomllib.load(handle)
        for field in ("key_press_ms", "key_gap_ms", "ctrl_settle_ms", "action_press_ms"):
            for value in (True, 1.5, "20", 0, -1, 10_001):
                changed = copy.deepcopy(raw)
                changed["stratagems"][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ConfigError):
                    parse_config(changed)
        for value in (True, 1.5, "800", -1, 60_001):
            changed = copy.deepcopy(raw)
            changed["stratagems"]["action_delay_ms"] = value
            with self.subTest(action_delay=value), self.assertRaises(ConfigError):
                parse_config(changed)
        changed = copy.deepcopy(raw)
        changed["stratagems"]["action_delay_ms"] = 0
        self.assertEqual(parse_config(changed).stratagems.action_delay_ms, 0)
        for triggers in (("f22", "f24"), ("f23", "F23")):
            changed = copy.deepcopy(raw)
            changed["stratagems"]["four_target_trigger"] = triggers[0]
            changed["stratagems"]["support_trigger"] = triggers[1]
            with self.assertRaises(ConfigError):
                parse_config(changed)

    def test_exact_immutable_recovered_sequences_and_durations(self) -> None:
        self.assertEqual(
            FOUR_TARGET_SEQUENCES,
            (
                (Direction.DOWN, Direction.UP, Direction.RIGHT, Direction.RIGHT, Direction.UP),
                (Direction.DOWN, Direction.UP, Direction.RIGHT, Direction.UP, Direction.LEFT, Direction.UP),
                (Direction.DOWN, Direction.UP, Direction.RIGHT, Direction.RIGHT, Direction.LEFT),
                (Direction.DOWN, Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT),
            ),
        )
        self.assertEqual(
            SUPPORT_SEQUENCES,
            (
                (Direction.DOWN, Direction.DOWN, Direction.UP, Direction.RIGHT),
                (Direction.UP, Direction.DOWN, Direction.RIGHT, Direction.LEFT, Direction.UP),
            ),
        )
        self.assertEqual(sequence_duration_ms(FOUR_TARGET_SEQUENCES, CONFIG.stratagems), 4200)
        self.assertEqual(sequence_duration_ms(SUPPORT_SEQUENCES, CONFIG.stratagems), 2040)

    def test_f23_f24_pair_latch_repeat_and_marker_filtering(self) -> None:
        events = []
        status = [True, True]
        policy = HookPolicy(
            lambda: tuple(status),
            events.append,
            stratagem_triggers={
                VK_F23: ControlEventKind.STRATAGEM_FOUR,
                VK_F24: ControlEventKind.STRATAGEM_SUPPORT,
            },
        )
        self.assertEqual((VK_F23, VK_F24), (0x86, 0x87))
        self.assertTrue(policy.keyboard(WM_KEYDOWN, VK_F23, 0))
        self.assertTrue(policy.keyboard(WM_KEYDOWN, VK_F23, 0))
        status[:] = [False, True]
        self.assertTrue(policy.keyboard(WM_KEYUP, VK_F23, 0))
        self.assertEqual([event.kind for event in events], [ControlEventKind.STRATAGEM_FOUR])
        self.assertFalse(policy.keyboard(WM_KEYDOWN, VK_F24, 0))
        self.assertFalse(policy.keyboard(WM_KEYUP, VK_F24, 0))
        status[:] = [True, True]
        self.assertFalse(policy.keyboard(WM_KEYDOWN, VK_F24, LLKHF_INJECTED, extra_info=INPUT_MARKER))
        self.assertFalse(policy.keyboard(WM_KEYUP, VK_F24, LLKHF_INJECTED, extra_info=INPUT_MARKER))
        self.assertEqual(len(events), 1)

    def test_backend_scan_flags_marker_acceptance_and_stale_ownership(self) -> None:
        user = FakeUser32()
        backend = SendInputBackend(user32=user)
        backend.stratagem_key_down(7, 0x1D, extended=False, ctrl=True)
        backend.stratagem_key_down(7, 0x48, extended=True)
        backend.stratagem_key_up(7, 0x48, extended=True)
        backend.stratagem_key_up(7, 0x1D, extended=False, ctrl=True)
        backend.stratagem_mouse_down(7)
        backend.stratagem_mouse_up(7)
        backend.release_stratagem(7)
        flags = [item.ki.dwFlags for item in user.inputs[:4]]
        self.assertEqual(flags, [
            KEYEVENTF_SCANCODE,
            KEYEVENTF_SCANCODE | KEYEVENTF_EXTENDEDKEY,
            KEYEVENTF_SCANCODE | KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP,
            KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP,
        ])
        self.assertTrue(all(item.ki.dwExtraInfo == INPUT_MARKER for item in user.inputs[:4]))
        before = len(user.inputs)
        backend.release_stratagem(6)
        self.assertEqual(len(user.inputs), before)
        failing = SendInputBackend(user32=FakeUser32([0], last_error=9))
        with self.assertRaisesRegex(InputApiError, "accepted 0/1"):
            failing.stratagem_key_down(1, 0x1D, extended=False, ctrl=True)
        retry_user = FakeUser32([1, 0, 1])
        retry = SendInputBackend(user32=retry_user)
        retry.stratagem_key_down(2, 0x1D, extended=False, ctrl=True)
        with self.assertRaisesRegex(InputApiError, "accepted 0/1"):
            retry.stratagem_key_up(2, 0x1D, extended=False, ctrl=True)
        retry.release_stratagem(2)
        self.assertEqual(
            [item.ki.dwFlags for item in retry_user.inputs],
            [
                KEYEVENTF_SCANCODE,
                KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP,
                KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP,
            ],
        )

    def test_engine_exact_output_timing_no_p_and_waits_outside_lock(self) -> None:
        clock = FakeClock()
        backend = StratagemBackend(clock)
        held = [False]

        class Lock:
            def __enter__(self):
                held[0] = True
            def __exit__(self, *_args):
                held[0] = False

        def wait(event: threading.Event, seconds: float) -> bool:
            self.assertFalse(held[0])
            return clock.wait(event, seconds)

        result = MacroEngine(
            CONFIG, backend, lambda: True, clock=clock, wait=wait, io_lock=Lock()
        ).run_stratagem(12, FOUR_TARGET_SEQUENCES, threading.Event(), threading.Event())
        self.assertTrue(result.success)
        self.assertEqual(round((clock() - 100.0) * 1000), 4200)
        names = [name for name, _token, _at in backend.events]
        self.assertEqual(names.count("CTRL_DOWN"), 4)
        self.assertEqual(names.count("MB1_DOWN"), 4)
        self.assertFalse(any(name.startswith("P") or name.startswith("R") for name in names))
        self.assertTrue(all(token == 12 for _name, token, _at in backend.events))

    def test_cancellation_cleans_each_wait_category_and_every_arrow_position(self) -> None:
        cancel_points = [5, 25, 45, 65, 205, 225, 245]
        cancel_points.extend(20 + position * 40 + 5 for position in range(21))
        for cancel_at in cancel_points:
            clock = FakeClock()
            backend = StratagemBackend(clock)
            cancel = threading.Event()

            def wait(event: threading.Event, seconds: float) -> bool:
                result = clock.wait(event, seconds)
                if round((clock() - 100.0) * 1000) >= cancel_at:
                    cancel.set()
                return result

            result = MacroEngine(
                CONFIG, backend, lambda: True, clock=clock, wait=wait
            ).run_stratagem(4, FOUR_TARGET_SEQUENCES, cancel, threading.Event())
            with self.subTest(cancel_at=cancel_at):
                self.assertTrue(result.canceled)
                self.assertIsNone(backend.owner)
                self.assertFalse(backend.ctrl or backend.mouse or backend.scan)

    def test_controller_exclusivity_no_deferred_activation_or_audio(self) -> None:
        h = SimulationHarness(CONFIG, auto_complete_stratagem=False)
        self.assertEqual(h.key_press(VK_F23, repeats=3), (True, True, True, True, True))
        self.assertTrue(h.machine.stratagem_active)
        worker = h.machine.worker
        self.assertIsNotNone(worker)
        before_mode = h.machine.selected_mode
        before_workers = len(h.workers)
        self.assertEqual(h.key_press(VK_F24), (True, True))
        h.key_press(VK_1)
        self.assertEqual(h.click(), (True, True))
        self.assertEqual(len(h.workers), before_workers)
        self.assertIs(h.machine.selected_mode, before_mode)
        self.assertEqual(h.audio.events, [])
        worker.finish(WorkerResult(True))
        h.drain()
        self.assertIs(h.machine.state, MacroState.IDLE_PRIMARY)
        self.assertFalse(h.machine.enabled)
        self.assertEqual(h.audio.events, [])

    def test_busy_weapon_and_preparation_reject_without_output(self) -> None:
        firing = SimulationHarness(CONFIG)
        firing.start(firing.machine.selected_mode)
        before = list(firing.backend.events)
        firing.key_press(VK_F23)
        self.assertEqual(firing.backend.events, before)
        self.assertIs(firing.machine.worker.request.kind, WorkerKind.MACRO)

        preparing = SimulationHarness(CONFIG, auto_complete_preparation=False)
        preparing.key_press(0x32)
        before = list(preparing.backend.events)
        preparing.key_press(VK_F24)
        self.assertEqual(preparing.backend.events, before)
        self.assertIs(preparing.machine.worker.request.kind, WorkerKind.PREPARATION)

    def test_dry_run_commands_have_exact_totals_without_live_components(self) -> None:
        for flag, total in (
            ("--dry-run-stratagem-four", 4200),
            ("--dry-run-stratagem-support", 2040),
        ):
            with self.subTest(flag=flag):
                from contextlib import redirect_stdout
                from io import StringIO
                from unittest.mock import patch
                output = StringIO()
                forbidden = AssertionError("live boundary")
                with patch.object(app, "WindowsHookThread", side_effect=forbidden), patch.object(
                    app, "SendInputBackend", side_effect=forbidden
                ), redirect_stdout(output):
                    self.assertEqual(app.main([flag, "--config", str(ROOT / "config.toml")]), 0)
                text = output.getvalue()
                self.assertIn(f"Total duration: {total} ms", text)
                self.assertIn("SCANCODE|EXTENDEDKEY", text)


if __name__ == "__main__":
    unittest.main()
