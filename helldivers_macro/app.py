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
from .config import AppConfig, ConfigError, load_config
from .foreground import ForegroundCache, ForegroundMonitor, WindowsForegroundInspector
from .input_backend import InputCoordination, SendInputBackend
from .input_hooks import HookPolicy, WindowsHookThread
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
    WorkerProgress,
    WorkerRequest,
    WorkerResult,
)
from .state_machine import MacroStateMachine


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

        def progress(worker_token: int, update: WorkerProgress) -> None:
            event_queue.put_nowait(
                ControlEvent(
                    ControlEventKind.WORKER_PROGRESS,
                    detail=update,
                    worker_token=worker_token,
                    source=EventSource.WORKER,
                )
            )

        return MacroWorker(
            token, request, engine, shutdown_event, complete, progress
        )

    return create


def run_live(config: AppConfig) -> int:
    """The only command path authorized to install hooks or generate input."""
    ensure_windows_11_pro()
    event_queue: queue.Queue[ControlEvent] = queue.Queue()
    shutdown_event = threading.Event()
    audio = AudioNotifier(config.audio)
    cache = ForegroundCache(config.target.foreground_cache_max_age_ms)
    inspector = WindowsForegroundInspector(config.target.executable)
    backend = SendInputBackend()
    coordination = InputCoordination()
    engine = MacroEngine(config, backend, cache.is_confirmed_active)
    machine = MacroStateMachine(
        config,
        cache.is_confirmed_active,
        audio,
        _worker_factory(engine, shutdown_event, event_queue),
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

    monitor = ForegroundMonitor(
        inspector,
        cache,
        shutdown_event,
        config.target.foreground_poll_ms,
        inactive,
    )
    policy = HookPolicy(
        cache.status,
        event_queue.put_nowait,
        backend.mouse_owned_snapshot,
        coordination,
        config.diagnostics.ctrl_bypass_logging,
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


def _format_dry_run(name: str, steps: list[CycleStep]) -> str:
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
            lines.append(f"{index:02d}. {step.action.value} (elapsed {elapsed} ms)")
    lines.append(f"Cycle duration: {elapsed} ms")
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
    modes.add_argument("--simulate-session", action="store_true")
    modes.add_argument("--test-audio", action="store_true")
    modes.add_argument("--live", action="store_true")
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
    if not any(
        (
            args.check_config,
            args.identify_foreground,
            args.dry_run_primary_cycle,
            args.dry_run_secondary_cycle,
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
            print(_format_dry_run("PRIMARY", primary_cycle_steps(config)))
            return 0
        if args.dry_run_secondary_cycle:
            print(_format_dry_run("SECONDARY", secondary_cycle_steps(config)))
            return 0
        if args.simulate_session:
            return simulate_session(config)
        if args.test_audio:
            return test_audio(config)
        if args.live:
            return run_live(config)
    except (ConfigError, OSError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 0
