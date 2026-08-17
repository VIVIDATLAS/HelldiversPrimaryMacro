from __future__ import annotations

import argparse
import logging
from pathlib import Path
import queue
import sys
import threading
import time
from typing import Sequence

from .audio import AudioNotifier
from .cadence_diagnostics import CadenceDiagnostics
from .config import AppConfig, ConfigError, load_config
from .foreground import ForegroundCache, ForegroundMonitor, WindowsForegroundInspector
from .input_backend import INPUT_MARKER, InputCoordination, SendInputBackend
from .input_hooks import HookPolicy, VK_F23, VK_F24, WindowsHookThread
from .macro_engine import (
    MacroEngine,
    MacroWorker,
    primary_cycle_steps,
    secondary_cycle_steps,
)
from .models import (
    ControlEvent,
    ControlEventKind,
    CycleStep,
    EventSource,
    OutputAction,
    WorkerProgressUpdate,
    WorkerRequest,
    WorkerResult,
)
from .state_machine import MacroStateMachine
from .stratagems import (
    FOUR_TARGET_SEQUENCES,
    LEFT_CTRL_SCAN_CODE,
    SUPPORT_SEQUENCES,
)
from .timer_resolution import TimerResolutionError, WindowsTimerResolution


LOGGER = logging.getLogger(__name__)
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.toml"


def ensure_windows_11_pro() -> None:
    if sys.platform != "win32":
        raise RuntimeError("this project supports Windows 11 Pro only")
    version = sys.getwindowsversion()
    if version.major != 10 or version.build < 22000:
        raise RuntimeError(
            f"Windows 11 is required (detected build {version.build})"
        )
    try:
        import winreg

        key_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
            edition_id = str(winreg.QueryValueEx(key, "EditionID")[0])
    except OSError as exc:
        raise RuntimeError(f"could not confirm Windows edition: {exc}") from exc
    if not edition_id.casefold().startswith("professional"):
        raise RuntimeError(
            f"Windows 11 Pro is required (detected EditionID {edition_id!r})"
        )


def _worker_factory(
    engine: MacroEngine,
    shutdown_event: threading.Event,
    event_queue: queue.Queue[ControlEvent],
    coordination: InputCoordination | None = None,
):
    def create(token: int, request: WorkerRequest) -> MacroWorker:
        def complete(worker_token: int, result: WorkerResult) -> None:
            event_queue.put_nowait(
                ControlEvent(
                    ControlEventKind.WORKER_STOPPED,
                    detail=result,
                    worker_token=worker_token,
                    source=EventSource.WORKER,
                )
            )

        def progress(worker_token: int, update: WorkerProgressUpdate) -> None:
            event_queue.put_nowait(
                ControlEvent(
                    ControlEventKind.WORKER_PROGRESS,
                    detail=update,
                    worker_token=worker_token,
                    source=EventSource.WORKER,
                )
            )

        return MacroWorker(
            token,
            request,
            engine,
            shutdown_event,
            complete,
            progress,
            coordination,
        )

    return create


def run_live(config: AppConfig, *, cadence_diagnostics: bool = False) -> int:
    """The only command path authorized to install hooks or generate input."""
    ensure_windows_11_pro()
    timer_resolution: WindowsTimerResolution | None = None
    try:
        try:
            candidate = WindowsTimerResolution(1)
            candidate.acquire()
            timer_resolution = candidate
        except (OSError, TimerResolutionError, ValueError) as exc:
            print(
                "Warning: 1 ms Windows timer resolution unavailable; "
                f"using the default wait resolution ({exc}).",
                file=sys.stderr,
            )
        return _run_live_session(
            config,
            cadence_diagnostics=cadence_diagnostics,
        )
    finally:
        if timer_resolution is not None:
            try:
                timer_resolution.release()
            except TimerResolutionError as exc:
                print(
                    f"Warning: could not release 1 ms Windows timer resolution ({exc}).",
                    file=sys.stderr,
                )


def _run_live_session(
    config: AppConfig,
    *,
    cadence_diagnostics: bool = False,
) -> int:
    event_queue: queue.Queue[ControlEvent] = queue.Queue()
    shutdown_event = threading.Event()
    audio = AudioNotifier(config.audio)
    cache = ForegroundCache(config.target.foreground_cache_max_age_ms)
    inspector = WindowsForegroundInspector(config.target.executable)
    diagnostics = (
        CadenceDiagnostics(
            primary_shots_per_cycle=(
                1
                if config.primary.fire_mode == "automatic_hold"
                else config.primary.shots_per_cycle
            ),
            primary_fire_mode=config.primary.fire_mode,
            ownership_marker=INPUT_MARKER,
            fire_device=config.output.fire_device,
        )
        if cadence_diagnostics
        else None
    )
    backend = SendInputBackend(
        cadence_diagnostics=diagnostics,
        output=config.output,
    )
    coordination = InputCoordination()
    engine = MacroEngine(
        config,
        backend,
        cache.is_confirmed_active,
        cadence_diagnostics=diagnostics,
    )
    machine = MacroStateMachine(
        config,
        cache.is_confirmed_active,
        audio,
        _worker_factory(engine, shutdown_event, event_queue, coordination),
        coordination=coordination,
        foreground_status=cache.status,
    )

    def inactive(uncertain: bool) -> None:
        event_queue.put_nowait(
            ControlEvent(
                ControlEventKind.FOREGROUND_UNCERTAIN
                if uncertain
                else ControlEventKind.FOREGROUND_LOST,
                source=EventSource.FOREGROUND,
            )
        )

    def active() -> None:
        event_queue.put_nowait(
            ControlEvent(
                ControlEventKind.FOREGROUND_ACTIVE,
                source=EventSource.FOREGROUND,
            )
        )

    monitor = ForegroundMonitor(
        inspector,
        cache,
        shutdown_event,
        config.target.foreground_poll_ms,
        inactive,
        active,
    )
    policy = HookPolicy(
        cache.status,
        event_queue.put_nowait,
        backend.mouse_owned_snapshot,
        coordination,
        config.diagnostics.ctrl_bypass_logging,
        cadence_diagnostics=diagnostics,
        fire_device=config.output.fire_device,
        fire_scan_code=config.output.fire_scan_code,
        stratagem_triggers=(
            {
                (VK_F23 if config.stratagems.four_target_trigger == "F23" else VK_F24):
                    ControlEventKind.STRATAGEM_FOUR,
                (VK_F23 if config.stratagems.support_trigger == "F23" else VK_F24):
                    ControlEventKind.STRATAGEM_SUPPORT,
            }
            if config.stratagems.enabled
            else {}
        ),
    )
    hooks = WindowsHookThread(policy, event_queue.put_nowait)
    audio_started = False
    monitor_started = False
    hooks_started = False
    try:
        audio.start()
        audio_started = True
        monitor.start()
        monitor_started = True
        hooks.start()
        hooks_started = True
        print(
            f"Live mode active; target={config.target.executable!r}, "
            "selected weapon mode: PRIMARY. Press Ctrl+C to exit."
        )
        while machine.fatal_error is None:
            try:
                event = event_queue.get(timeout=config.controls.poll_ms / 1000.0)
            except queue.Empty:
                continue
            machine.handle(event)
        return 1
    except KeyboardInterrupt:
        print("Ctrl+C received; stopping safely.")
        return 0
    finally:
        try:
            # Stop generation first. The worker's cancel-and-release lock prevents a
            # new generated down from racing after this release operation.
            if audio_started:
                machine.shutdown()
            shutdown_event.set()
            if hooks_started:
                hooks.stop()
            if monitor_started:
                monitor.join(2.0)
            try:
                backend.release_all()
            except Exception:
                LOGGER.exception("final generated-input release failed")
            if audio_started:
                audio.stop(drain=True)
        finally:
            if diagnostics is not None:
                print(diagnostics.format_summary())


def identify_foreground(config: AppConfig, delay: float) -> int:
    ensure_windows_11_pro()
    if delay < 0:
        raise ValueError("--delay must be non-negative")
    if delay:
        print(f"Waiting {delay:g} seconds before read-only foreground inspection...")
        time.sleep(delay)
    observation = WindowsForegroundInspector(config.target.executable).inspect()
    print(f"Configured target basename: {Path(config.target.executable).name}")
    print(f"Foreground PID: {observation.pid if observation.pid is not None else 'unknown'}")
    print(f"Foreground executable: {observation.executable or 'unknown'}")
    print(f"Inspection certain: {'yes' if observation.certain else 'no'}")
    print(f"Target matches: {'yes' if observation.active else 'no'}")
    if observation.error:
        print(f"Diagnostic error: {observation.error}")
    return 0 if observation.certain else 1


def _format_dry_run(
    name: str,
    steps: list[CycleStep],
    *,
    fire_device: str,
) -> str:
    lines = [f"{name} dry-run (no hooks, input, suppression, or audio):"]
    elapsed = 0
    for index, step in enumerate(steps, start=1):
        if step.action is OutputAction.WAIT:
            lines.append(
                f"{index:02d}. WAIT {step.duration_ms} ms "
                f"(elapsed {elapsed + step.duration_ms} ms)"
            )
            elapsed += step.duration_ms
        else:
            label = step.action.value
            if fire_device == "keyboard":
                if step.action is OutputAction.FIRE_DOWN:
                    label = "P_DOWN"
                elif step.action is OutputAction.FIRE_UP:
                    label = "P_UP"
            elif fire_device == "mouse":
                if step.action is OutputAction.FIRE_DOWN:
                    label = "MB1_DOWN"
                elif step.action is OutputAction.FIRE_UP:
                    label = "MB1_UP"
            lines.append(f"{index:02d}. {label} (elapsed {elapsed} ms)")
    lines.append(f"Cycle duration: {elapsed} ms")
    return "\n".join(lines)


def _format_stratagem_dry_run(
    name: str, sequences: tuple[tuple[object, ...], ...], config: AppConfig
) -> str:
    lines = [f"{name} dry-run (no hooks, input, suppression, or audio):"]
    elapsed = 0
    operation = 0

    def event(label: str) -> None:
        nonlocal operation
        operation += 1
        lines.append(f"{operation:02d}. {label} (elapsed {elapsed} ms)")

    def wait(label: str, duration_ms: int) -> None:
        nonlocal operation, elapsed
        operation += 1
        elapsed += duration_ms
        lines.append(
            f"{operation:02d}. WAIT {label} {duration_ms} ms (elapsed {elapsed} ms)"
        )

    timing = config.stratagems
    for entry, sequence in enumerate(sequences, start=1):
        event(f"CTRL_DOWN scan=0x{LEFT_CTRL_SCAN_CODE:02X} SCANCODE")
        wait("CTRL_SETTLE", timing.ctrl_settle_ms)
        for direction in sequence:
            event(
                f"{direction.name}_DOWN scan=0x{direction.scan_code:02X} "
                "SCANCODE|EXTENDEDKEY"
            )
            wait("KEY_PRESS", timing.key_press_ms)
            event(
                f"{direction.name}_UP scan=0x{direction.scan_code:02X} "
                "SCANCODE|EXTENDEDKEY|KEYUP"
            )
            wait("KEY_GAP", timing.key_gap_ms)
        event(f"CTRL_UP scan=0x{LEFT_CTRL_SCAN_CODE:02X} SCANCODE|KEYUP")
        event("MB1_DOWN tagged")
        wait("ACTION_PRESS", timing.action_press_ms)
        event("MB1_UP tagged")
        wait(f"ACTION_DELAY entry={entry}", timing.action_delay_ms)
    lines.append(f"Total duration: {elapsed} ms")
    return "\n".join(lines)


def test_audio(config: AppConfig) -> int:
    ensure_windows_11_pro()
    notifier = AudioNotifier(config.audio)
    notifier.start()
    try:
        print("Playing ON signal...")
        notifier.notify_on()
        print("Playing OFF signal...")
        notifier.notify_off()
    finally:
        notifier.close(drain=True, raise_errors=True)
    print("Audio test complete.")
    return 0


def simulate_session(config: AppConfig) -> int:
    from .simulation import run_simulated_session

    return run_simulated_session(config)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Foreground-restricted Helldivers primary/secondary macro",
        add_help=True,
    )
    parser.add_argument(
        "--config", default=str(DEFAULT_CONFIG_PATH), help="path to config.toml"
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check-config", action="store_true")
    modes.add_argument("--identify-foreground", action="store_true")
    modes.add_argument("--dry-run-primary-cycle", action="store_true")
    modes.add_argument("--dry-run-secondary-cycle", action="store_true")
    modes.add_argument("--dry-run-stratagem-four", action="store_true")
    modes.add_argument("--dry-run-stratagem-support", action="store_true")
    modes.add_argument("--simulate-session", action="store_true")
    modes.add_argument("--test-audio", action="store_true")
    modes.add_argument("--live", action="store_true")
    parser.add_argument(
        "--cadence-diagnostics",
        action="store_true",
        help="collect bounded owned fire/R delivery data for --live",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=5.0,
        help="seconds before --identify-foreground inspects (default: 5)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cadence_diagnostics and not args.live:
        parser.error("--cadence-diagnostics requires --live")
    if not any(
        (
            args.check_config,
            args.identify_foreground,
            args.dry_run_primary_cycle,
            args.dry_run_secondary_cycle,
            args.dry_run_stratagem_four,
            args.dry_run_stratagem_support,
            args.simulate_session,
            args.test_audio,
            args.live,
        )
    ):
        parser.print_help()
        return 0
    try:
        config = load_config(args.config)
        if args.check_config:
            print(f"Configuration valid: {Path(args.config).resolve()}")
            print(f"Target executable: {config.target.executable}")
            return 0
        if args.identify_foreground:
            return identify_foreground(config, args.delay)
        if args.dry_run_primary_cycle:
            print(
                _format_dry_run(
                    "PRIMARY",
                    primary_cycle_steps(config),
                    fire_device=config.output.fire_device,
                )
            )
            return 0
        if args.dry_run_secondary_cycle:
            print(
                _format_dry_run(
                    "SECONDARY",
                    secondary_cycle_steps(config),
                    fire_device=config.output.fire_device,
                )
            )
            return 0
        if args.dry_run_stratagem_four:
            print(_format_stratagem_dry_run(
                "FOUR-TARGET", FOUR_TARGET_SEQUENCES, config
            ))
            return 0
        if args.dry_run_stratagem_support:
            print(_format_stratagem_dry_run(
                "RESUPPLY + REINFORCE", SUPPORT_SEQUENCES, config
            ))
            return 0
        if args.simulate_session:
            return simulate_session(config)
        if args.test_audio:
            return test_audio(config)
        if args.live:
            return run_live(config, cadence_diagnostics=args.cadence_diagnostics)
    except (ConfigError, OSError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 0
