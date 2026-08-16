from __future__ import annotations

import copy
from pathlib import Path
import tomllib
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from helldivers_macro import app
from helldivers_macro.config import ConfigError, load_config, parse_config
from helldivers_macro.models import ForegroundObservation


ROOT = Path(__file__).resolve().parent.parent


def raw_config() -> dict:
    with (ROOT / "config.toml").open("rb") as handle:
        return tomllib.load(handle)


class ConfigTests(unittest.TestCase):
    def test_default_config_loads(self) -> None:
        config = load_config(ROOT / "config.toml")
        self.assertEqual(config.target.executable, "helldivers2.exe")
        self.assertEqual(config.primary.shots_per_cycle, 3)
        self.assertEqual(config.secondary.shots_per_cycle, 13)
        self.assertEqual(config.controls.deferred_bypass_click_ms, 20)
        self.assertTrue(config.weapons.reload_on_select)
        self.assertEqual(config.weapons.switch_settle_ms, 500)
        self.assertEqual(config.behavior.start_policy, "immediate")
        self.assertFalse(config.diagnostics.ctrl_bypass_logging)
        self.assertFalse(config.diagnostics.state_tracing)

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
        raw["secondary"]["fire_press_ms"] = 181
        with self.assertRaisesRegex(ConfigError, "must not exceed"):
            parse_config(raw)

    def test_exact_shot_counts_are_enforced(self) -> None:
        for section, count in (("primary", 4), ("secondary", 12)):
            with self.subTest(section=section):
                raw = copy.deepcopy(raw_config())
                raw[section]["shots_per_cycle"] = count
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
        ), patch.object(app, "AudioNotifier", side_effect=forbidden):
            for mode in modes:
                with self.subTest(mode=mode), redirect_stdout(StringIO()):
                    self.assertEqual(
                        app.main([mode, "--config", str(ROOT / "config.toml")]), 0
                    )

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
