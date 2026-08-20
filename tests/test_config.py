from __future__ import annotations

import copy
from pathlib import Path
import re
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

from helldivers_macro import app
from helldivers_macro.config import ConfigError, load_config, parse_config
from helldivers_macro.models import ForegroundObservation


ROOT = Path(__file__).resolve().parent.parent


def raw_config() -> dict:
    with (ROOT / "config.toml").open("rb") as handle:
        return tomllib.load(handle)


def tap_primary_fixture(shots_per_cycle=45) -> dict:
    raw = raw_config()
    raw["primary"] = {
        "fire_mode": "tap",
        "shots_per_cycle": shots_per_cycle,
        "shot_period_ms": 85,
        "fire_press_ms": 35,
        "reload_press_ms": 25,
        "reload_wait_ms": 2000,
    }
    return raw


class ConfigTests(unittest.TestCase):
    def test_default_config_loads(self) -> None:
        config = load_config(ROOT / "config.toml")
        self.assertEqual(config.target.executable, "helldivers2.exe")
        self.assertEqual(config.primary.fire_mode, "automatic_hold")
        self.assertEqual(config.primary.automatic_hold_ms, 4450)
        self.assertEqual(config.primary.post_fire_reload_delay_ms, 0)
        self.assertEqual(config.primary.reload_press_ms, 25)
        self.assertEqual(config.primary.reload_wait_ms, 2000)
        self.assertEqual(config.secondary.shots_per_cycle, 13)
        self.assertEqual(config.secondary.fire_press_ms, 35)
        self.assertEqual(config.secondary.shot_period_ms, 120)
        self.assertEqual(config.secondary.reload_press_ms, 25)
        self.assertEqual(config.secondary.reload_wait_ms, 2000)
        self.assertEqual(config.controls.deferred_bypass_click_ms, 20)
        self.assertEqual(config.controls.aim_mode, "hold")
        self.assertEqual(config.output.fire_device, "mouse")
        self.assertIsNone(config.output.fire_scan_code)
        self.assertTrue(config.weapons.reload_on_select)
        self.assertEqual(config.weapons.switch_settle_ms, 500)
        self.assertEqual(config.behavior.start_policy, "immediate")
        self.assertFalse(config.diagnostics.ctrl_bypass_logging)
        self.assertFalse(config.diagnostics.state_tracing)

    def test_ar2_reference_profile_retains_live_tested_configuration(self) -> None:
        profile = load_config(ROOT / "profiles" / "ar2-coyote.toml")
        self.assertEqual(profile.primary.fire_mode, "automatic_hold")
        self.assertEqual(profile.primary.automatic_hold_ms, 4450)
        self.assertEqual(profile.primary.post_fire_reload_delay_ms, 0)
        self.assertEqual(profile.primary.reload_press_ms, 25)
        self.assertEqual(profile.primary.reload_wait_ms, 2000)
        self.assertEqual(profile.secondary, load_config(ROOT / "config.toml").secondary)

    def test_empty_executable_is_rejected(self) -> None:
        raw = raw_config()
        raw["target"]["executable"] = "  "
        with self.assertRaisesRegex(ConfigError, "nonempty"):
            parse_config(raw)

    def test_unsupported_control_is_rejected(self) -> None:
        raw = raw_config()
        raw["controls"]["primary_select_key"] = "NUMPAD1"
        with self.assertRaisesRegex(ConfigError, "supports only"):
            parse_config(raw)

    def test_winsound_frequency_range_is_validated(self) -> None:
        raw = raw_config()
        raw["audio"]["on"]["frequency_hz"] = 36
        with self.assertRaisesRegex(ConfigError, "37..32767"):
            parse_config(raw)

    def test_negative_duration_is_rejected(self) -> None:
        raw = raw_config()
        raw["primary"]["reload_wait_ms"] = -1
        with self.assertRaisesRegex(ConfigError, "non-negative"):
            parse_config(raw)

    def test_secondary_press_cannot_exceed_period(self) -> None:
        raw = raw_config()
        raw["secondary"]["fire_press_ms"] = 121
        with self.assertRaisesRegex(ConfigError, "must not exceed"):
            parse_config(raw)

    def test_output_accepts_keyboard_p_and_mouse_fallback(self) -> None:
        raw = raw_config()
        raw["output"] = {"fire_device": "keyboard", "fire_scan_code": 25}
        keyboard = parse_config(raw)
        self.assertEqual((keyboard.output.fire_device, keyboard.output.fire_scan_code), ("keyboard", 25))
        raw["output"] = {"fire_device": "mouse"}
        mouse = parse_config(raw)
        self.assertEqual((mouse.output.fire_device, mouse.output.fire_scan_code), ("mouse", None))

    def test_output_rejects_invalid_device_and_scan_code(self) -> None:
        for device in ("Keyboard", "key", "", 1, True):
            with self.subTest(device=device):
                raw = raw_config()
                raw["output"]["fire_device"] = device
                with self.assertRaisesRegex(ConfigError, "fire_device"):
                    parse_config(raw)
        for scan_code in (True, 1.5, "25", 0, -1, 256):
            with self.subTest(scan_code=scan_code):
                raw = raw_config()
                raw["output"]["fire_scan_code"] = scan_code
                with self.assertRaisesRegex(ConfigError, "fire_scan_code"):
                    parse_config(raw)
        raw = raw_config()
        raw["output"] = {"fire_device": "keyboard"}
        with self.assertRaisesRegex(ConfigError, "fire_scan_code"):
            parse_config(raw)

    def test_primary_press_cannot_exceed_period(self) -> None:
        raw = tap_primary_fixture()
        raw["primary"]["fire_press_ms"] = 86
        with self.assertRaisesRegex(ConfigError, "must not exceed"):
            parse_config(raw)

    def test_primary_shot_count_accepts_integer_range_endpoints(self) -> None:
        for count in (1, 45, 1000):
            with self.subTest(count=count):
                self.assertEqual(
                    parse_config(tap_primary_fixture(count)).primary.shots_per_cycle,
                    count,
                )

    def test_primary_shot_count_rejects_invalid_types_and_range(self) -> None:
        for value in (True, 1.5, "45", 0, -1, 1001):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ConfigError, "integer|1 through 1000"):
                    parse_config(tap_primary_fixture(value))

    def test_firing_modes_and_automatic_values_are_strict(self) -> None:
        for value in (None, "burst", 1, True):
            with self.subTest(fire_mode=value):
                raw = raw_config()
                if value is None:
                    del raw["primary"]["fire_mode"]
                else:
                    raw["primary"]["fire_mode"] = value
                with self.assertRaisesRegex(ConfigError, "primary.fire_mode"):
                    parse_config(raw)
        for value in (True, 1.5, "4450", 0, -1):
            with self.subTest(automatic_hold_ms=value):
                raw = raw_config()
                raw["primary"]["automatic_hold_ms"] = value
                with self.assertRaisesRegex(ConfigError, "automatic_hold_ms"):
                    parse_config(raw)
        raw = raw_config()
        del raw["primary"]["automatic_hold_ms"]
        with self.assertRaisesRegex(ConfigError, "primary.automatic_hold_ms"):
            parse_config(raw)
        for value in (True, 1.5, "0", -1):
            with self.subTest(post_fire_reload_delay_ms=value):
                raw = raw_config()
                raw["primary"]["post_fire_reload_delay_ms"] = value
                with self.assertRaisesRegex(ConfigError, "post_fire_reload_delay_ms"):
                    parse_config(raw)

    def test_secondary_exact_shot_count_remains_enforced(self) -> None:
        raw = copy.deepcopy(raw_config())
        raw["secondary"]["shots_per_cycle"] = 12
        with self.assertRaisesRegex(ConfigError, "exactly"):
            parse_config(raw)

    def test_five_millisecond_safety_polls_are_enforced(self) -> None:
        raw = raw_config()
        raw["controls"]["poll_ms"] = 10
        with self.assertRaisesRegex(ConfigError, "must be 5"):
            parse_config(raw)

    def test_deferred_click_duration_must_be_positive(self) -> None:
        raw = raw_config()
        raw["controls"]["deferred_bypass_click_ms"] = 0
        with self.assertRaisesRegex(ConfigError, "must be positive"):
            parse_config(raw)

    def test_aim_mode_supports_only_hold(self) -> None:
        raw = raw_config()
        raw["controls"]["aim_mode"] = "toggle"
        with self.assertRaisesRegex(
            ConfigError, "controls.aim_mode supports only 'hold'"
        ):
            parse_config(raw)

    def test_switch_settle_must_be_nonnegative(self) -> None:
        raw = raw_config()
        raw["weapons"]["switch_settle_ms"] = -1
        with self.assertRaisesRegex(ConfigError, "non-negative"):
            parse_config(raw)

    def test_only_immediate_start_policy_is_supported(self) -> None:
        raw = raw_config()
        raw["behavior"]["start_policy"] = "prepared"
        with self.assertRaisesRegex(ConfigError, "supports only 'immediate'"):
            parse_config(raw)

    def test_missing_value_has_clear_path(self) -> None:
        raw = raw_config()
        del raw["controls"]["reload_key"]
        with self.assertRaisesRegex(ConfigError, "controls.reload_key"):
            parse_config(raw)

    def test_no_mode_prints_help_without_loading_live_components(self) -> None:
        with patch.object(app, "load_config", side_effect=AssertionError("loaded")):
            with redirect_stdout(StringIO()) as output:
                self.assertEqual(app.main([]), 0)
        self.assertIn("--live", output.getvalue())

    def test_check_and_dry_runs_never_construct_live_components(self) -> None:
        forbidden = AssertionError("live component constructed")
        modes = (
            "--check-config",
            "--dry-run-primary-cycle",
            "--dry-run-secondary-cycle",
            "--simulate-session",
        )
        with patch.object(app, "WindowsHookThread", side_effect=forbidden), patch.object(
            app, "SendInputBackend", side_effect=forbidden
        ), patch.object(app, "AudioNotifier", side_effect=forbidden), patch.object(
            app, "WindowsTimerResolution", side_effect=forbidden
        ):
            for mode in modes:
                with self.subTest(mode=mode), redirect_stdout(StringIO()):
                    self.assertEqual(
                        app.main([mode, "--config", str(ROOT / "config.toml")]), 0
                    )

    def test_primary_and_secondary_dry_run_durations(self) -> None:
        config = load_config(ROOT / "config.toml")
        primary_duration = (
            config.primary.automatic_hold_ms
            + config.primary.post_fire_reload_delay_ms
            + config.primary.reload_press_ms
            + config.primary.reload_wait_ms
        )
        secondary_duration = (
            (config.secondary.shots_per_cycle - 1) * config.secondary.shot_period_ms
            + config.secondary.fire_press_ms
            + config.secondary.reload_press_ms
            + config.secondary.reload_wait_ms
        )
        for mode, duration in (
            ("--dry-run-primary-cycle", primary_duration),
            ("--dry-run-secondary-cycle", secondary_duration),
        ):
            with self.subTest(mode=mode), redirect_stdout(StringIO()) as output:
                self.assertEqual(
                    app.main([mode, "--config", str(ROOT / "config.toml")]), 0
                )
            self.assertIn(f"Cycle duration: {duration} ms", output.getvalue())
            self.assertIn("MB1_DOWN", output.getvalue())
            self.assertIn("MB1_UP", output.getvalue())
            self.assertNotIn("P_DOWN", output.getvalue())

    def test_cadence_diagnostics_is_opt_in_and_requires_live_mode(self) -> None:
        parser = app.build_parser()
        self.assertFalse(parser.parse_args(["--live"]).cadence_diagnostics)
        self.assertTrue(
            parser.parse_args(
                ["--live", "--cadence-diagnostics"]
            ).cadence_diagnostics
        )
        with redirect_stdout(StringIO()), redirect_stderr(
            StringIO()
        ), self.assertRaises(SystemExit):
            app.main(["--cadence-diagnostics"])

    def test_normal_live_construction_does_not_create_diagnostic_recorder(self) -> None:
        config = load_config(ROOT / "config.toml")
        stop_before_live_side_effects = RuntimeError("stop after construction check")
        with patch.object(app, "ensure_windows_11_pro"), patch.object(
            app, "AudioNotifier"
        ), patch.object(app, "WindowsForegroundInspector"), patch.object(
            app, "CadenceDiagnostics"
        ) as recorder, patch.object(app, "WindowsTimerResolution") as timer, patch.object(
            app,
            "SendInputBackend",
            side_effect=stop_before_live_side_effects,
        ):
            with self.assertRaisesRegex(RuntimeError, "construction check"):
                app.run_live(config)
        recorder.assert_not_called()
        timer.return_value.acquire.assert_called_once_with()
        timer.return_value.release.assert_called_once_with()

    def test_diagnostic_live_construction_uses_active_primary_capture_size(self) -> None:
        config = load_config(ROOT / "config.toml")
        stop_before_live_side_effects = RuntimeError("stop after construction check")
        with patch.object(app, "ensure_windows_11_pro"), patch.object(
            app, "AudioNotifier"
        ), patch.object(app, "WindowsForegroundInspector"), patch.object(
            app, "CadenceDiagnostics"
        ) as recorder, patch.object(app, "WindowsTimerResolution") as timer, patch.object(
            app,
            "SendInputBackend",
            side_effect=stop_before_live_side_effects,
        ):
            with self.assertRaisesRegex(RuntimeError, "construction check"):
                app.run_live(config, cadence_diagnostics=True)
        recorder.assert_called_once_with(
            primary_shots_per_cycle=1,
            primary_fire_mode="automatic_hold",
            ownership_marker=app.INPUT_MARKER,
            fire_device=config.output.fire_device,
        )
        timer.return_value.acquire.assert_called_once_with()
        timer.return_value.release.assert_called_once_with()

    def test_live_timer_resolution_releases_after_hook_start_failure(self) -> None:
        config = load_config(ROOT / "config.toml")
        with patch.object(app, "ensure_windows_11_pro"), patch.object(
            app, "WindowsTimerResolution"
        ) as timer, patch.object(app, "AudioNotifier"), patch.object(
            app, "WindowsForegroundInspector"
        ), patch.object(app, "SendInputBackend"), patch.object(
            app, "MacroEngine"
        ), patch.object(app, "MacroStateMachine"), patch.object(
            app, "ForegroundMonitor"
        ), patch.object(app, "WindowsHookThread") as hook:
            hook.return_value.start.side_effect = RuntimeError("hook startup failed")
            with self.assertRaisesRegex(RuntimeError, "hook startup failed"):
                app.run_live(config)

        timer.return_value.acquire.assert_called_once_with()
        timer.return_value.release.assert_called_once_with()

    def test_live_timer_resolution_failure_reports_default_wait_fallback(self) -> None:
        config = load_config(ROOT / "config.toml")
        stop_before_live_side_effects = RuntimeError("stop after fallback check")
        with patch.object(app, "ensure_windows_11_pro"), patch.object(
            app, "WindowsTimerResolution"
        ) as timer, patch.object(app, "AudioNotifier"), patch.object(
            app, "WindowsForegroundInspector"
        ), patch.object(
            app,
            "SendInputBackend",
            side_effect=stop_before_live_side_effects,
        ), redirect_stderr(StringIO()) as errors:
            timer.return_value.acquire.side_effect = app.TimerResolutionError(
                "unsupported"
            )
            with self.assertRaisesRegex(RuntimeError, "fallback check"):
                app.run_live(config)

        self.assertIn("using the default wait resolution", errors.getvalue())
        timer.return_value.release.assert_not_called()

    def test_foreground_diagnostic_is_inspection_only(self) -> None:
        class Inspector:
            def __init__(self, target):
                self.target = target

            def inspect(self):
                return ForegroundObservation(
                    False,
                    True,
                    1.0,
                    pid=10,
                    executable=r"C:\Windows\explorer.exe",
                )

        forbidden = AssertionError("input/audio/hook component constructed")
        with patch.object(app, "ensure_windows_11_pro"), patch.object(
            app, "WindowsForegroundInspector", Inspector
        ), patch.object(app, "WindowsHookThread", side_effect=forbidden), patch.object(
            app, "SendInputBackend", side_effect=forbidden
        ), patch.object(app, "AudioNotifier", side_effect=forbidden), redirect_stdout(
            StringIO()
        ):
            self.assertEqual(
                app.main(
                    [
                        "--identify-foreground",
                        "--delay",
                        "0",
                        "--config",
                        str(ROOT / "config.toml"),
                    ]
                ),
                0,
            )


if __name__ == "__main__":
    unittest.main()
