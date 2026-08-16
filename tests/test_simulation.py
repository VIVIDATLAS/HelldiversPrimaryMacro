from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import unittest
from unittest.mock import patch

from helldivers_macro import app
from helldivers_macro.config import load_config
from helldivers_macro.models import ControlEventKind, WeaponMode
from helldivers_macro.simulation import simulate_weapon_session


ROOT = Path(__file__).resolve().parent.parent
CONFIG = load_config(ROOT / "config.toml")


class SimulatedSessionTests(unittest.TestCase):
    def assert_session(self, mode: WeaponMode) -> None:
        result = simulate_weapon_session(CONFIG, mode)
        self.assertTrue(result.passed)
        selection = (
            ControlEventKind.SELECT_PRIMARY
            if mode is WeaponMode.PRIMARY
            else ControlEventKind.SELECT_SECONDARY
        )
        self.assertEqual(result.logical_selections, 1)
        self.assertEqual(result.hook_events.count(selection), 1)
        self.assertEqual(
            result.hook_events.count(ControlEventKind.PHYSICAL_MB1_DOWN), 2
        )
        self.assertEqual(
            result.hook_events.count(ControlEventKind.PHYSICAL_MB1_UP), 2
        )
        self.assertEqual(result.audio_events, ("ON", "OFF"))
        running = f"RUNNING_{mode.value}"
        mb1_down = [event for event in result.input_events if event[0] == "MB1_DOWN"]
        self.assertEqual(mb1_down, [("MB1_DOWN", running)])
        self.assertIn(("MB1_UP", "STOPPING"), result.input_events)

    def test_primary_session_uses_real_controller_wiring_with_fakes(self) -> None:
        self.assert_session(WeaponMode.PRIMARY)

    def test_secondary_session_uses_real_controller_wiring_with_fakes(self) -> None:
        self.assert_session(WeaponMode.SECONDARY)

    def test_simulate_session_cli_reaches_pass_without_live_boundaries(self) -> None:
        forbidden = AssertionError("live boundary constructed")
        with patch.object(app, "WindowsHookThread", side_effect=forbidden), patch.object(
            app, "SendInputBackend", side_effect=forbidden
        ), patch.object(app, "AudioNotifier", side_effect=forbidden), redirect_stdout(
            StringIO()
        ) as output:
            result = app.main(
                ["--simulate-session", "--config", str(ROOT / "config.toml")]
            )
        self.assertEqual(result, 0)
        text = output.getvalue()
        for label in "ABCDEFGHIJKLMN":
            self.assertIn(f"SCENARIO {label}: PASS", text)
        self.assertIn("DETERMINISTIC CONTROL SIMULATION: PASS", text)


if __name__ == "__main__":
    unittest.main()
