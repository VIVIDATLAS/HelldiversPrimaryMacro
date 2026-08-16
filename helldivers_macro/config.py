from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import Any, Mapping


class ConfigError(ValueError):
    """Raised before any live subsystem starts when configuration is invalid."""


@dataclass(frozen=True)
class TargetConfig:
    executable: str
    foreground_poll_ms: int
    foreground_cache_max_age_ms: int


@dataclass(frozen=True)
class ControlsConfig:
    fire_toggle_button: str
    normal_fire_modifier: str
    primary_select_key: str
    secondary_select_key: str
    reload_key: str
    poll_ms: int
    toggle_debounce_ms: int
    deferred_bypass_click_ms: int


@dataclass(frozen=True)
class WeaponsConfig:
    reload_on_select: bool
    reload_before_start_if_unknown: bool
    switch_settle_ms: int


@dataclass(frozen=True)
class DiagnosticsConfig:
    ctrl_bypass_logging: bool
    state_tracing: bool


@dataclass(frozen=True)
class ToneConfig:
    frequency_hz: int
    duration_ms: int


@dataclass(frozen=True)
class AudioConfig:
    on: ToneConfig
    off: ToneConfig


@dataclass(frozen=True)
class PrimaryConfig:
    shots_per_cycle: int
    fire_hold_ms: int
    inter_shot_ms: int
    post_last_shot_ms: int
    reload_press_ms: int
    reload_wait_ms: int


@dataclass(frozen=True)
class SecondaryConfig:
    shots_per_cycle: int
    shot_period_ms: int
    fire_press_ms: int
    reload_press_ms: int
    reload_wait_ms: int


@dataclass(frozen=True)
class AppConfig:
    target: TargetConfig
    controls: ControlsConfig
    weapons: WeaponsConfig
    diagnostics: DiagnosticsConfig
    audio: AudioConfig
    primary: PrimaryConfig
    secondary: SecondaryConfig


def _table(parent: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = parent.get(name)
    if not isinstance(value, dict):
        raise ConfigError(f"missing or invalid [{name}] table")
    return value


def _value(table: Mapping[str, Any], table_name: str, name: str, kind: type) -> Any:
    if name not in table:
        raise ConfigError(f"missing configuration value: {table_name}.{name}")
    value = table[name]
    if kind is int and (not isinstance(value, int) or isinstance(value, bool)):
        raise ConfigError(f"{table_name}.{name} must be an integer")
    if kind is str and not isinstance(value, str):
        raise ConfigError(f"{table_name}.{name} must be a string")
    if kind is bool and not isinstance(value, bool):
        raise ConfigError(f"{table_name}.{name} must be a boolean")
    return value


def _nonnegative(name: str, value: int) -> None:
    if value < 0:
        raise ConfigError(f"{name} must be non-negative")


def parse_config(data: Mapping[str, Any]) -> AppConfig:
    target_t = _table(data, "target")
    controls_t = _table(data, "controls")
    weapons_t = _table(data, "weapons")
    diagnostics_t = _table(data, "diagnostics")
    audio_t = _table(data, "audio")
    audio_on_t = _table(audio_t, "on")
    audio_off_t = _table(audio_t, "off")
    primary_t = _table(data, "primary")
    secondary_t = _table(data, "secondary")

    target = TargetConfig(
        executable=_value(target_t, "target", "executable", str).strip(),
        foreground_poll_ms=_value(target_t, "target", "foreground_poll_ms", int),
        foreground_cache_max_age_ms=_value(
            target_t, "target", "foreground_cache_max_age_ms", int
        ),
    )
    controls = ControlsConfig(
        fire_toggle_button=_value(
            controls_t, "controls", "fire_toggle_button", str
        ).upper(),
        normal_fire_modifier=_value(
            controls_t, "controls", "normal_fire_modifier", str
        ).upper(),
        primary_select_key=_value(
            controls_t, "controls", "primary_select_key", str
        ),
        secondary_select_key=_value(
            controls_t, "controls", "secondary_select_key", str
        ),
        reload_key=_value(controls_t, "controls", "reload_key", str).upper(),
        poll_ms=_value(controls_t, "controls", "poll_ms", int),
        toggle_debounce_ms=_value(
            controls_t, "controls", "toggle_debounce_ms", int
        ),
        deferred_bypass_click_ms=_value(
            controls_t, "controls", "deferred_bypass_click_ms", int
        ),
    )
    weapons = WeaponsConfig(
        reload_on_select=_value(
            weapons_t, "weapons", "reload_on_select", bool
        ),
        reload_before_start_if_unknown=_value(
            weapons_t, "weapons", "reload_before_start_if_unknown", bool
        ),
        switch_settle_ms=_value(weapons_t, "weapons", "switch_settle_ms", int),
    )
    diagnostics = DiagnosticsConfig(
        ctrl_bypass_logging=_value(
            diagnostics_t, "diagnostics", "ctrl_bypass_logging", bool
        ),
        state_tracing=_value(
            diagnostics_t, "diagnostics", "state_tracing", bool
        ),
    )
    audio = AudioConfig(
        on=ToneConfig(
            frequency_hz=_value(audio_on_t, "audio.on", "frequency_hz", int),
            duration_ms=_value(audio_on_t, "audio.on", "duration_ms", int),
        ),
        off=ToneConfig(
            frequency_hz=_value(audio_off_t, "audio.off", "frequency_hz", int),
            duration_ms=_value(audio_off_t, "audio.off", "duration_ms", int),
        ),
    )
    primary = PrimaryConfig(
        shots_per_cycle=_value(primary_t, "primary", "shots_per_cycle", int),
        fire_hold_ms=_value(primary_t, "primary", "fire_hold_ms", int),
        inter_shot_ms=_value(primary_t, "primary", "inter_shot_ms", int),
        post_last_shot_ms=_value(
            primary_t, "primary", "post_last_shot_ms", int
        ),
        reload_press_ms=_value(primary_t, "primary", "reload_press_ms", int),
        reload_wait_ms=_value(primary_t, "primary", "reload_wait_ms", int),
    )
    secondary = SecondaryConfig(
        shots_per_cycle=_value(
            secondary_t, "secondary", "shots_per_cycle", int
        ),
        shot_period_ms=_value(
            secondary_t, "secondary", "shot_period_ms", int
        ),
        fire_press_ms=_value(
            secondary_t, "secondary", "fire_press_ms", int
        ),
        reload_press_ms=_value(
            secondary_t, "secondary", "reload_press_ms", int
        ),
        reload_wait_ms=_value(
            secondary_t, "secondary", "reload_wait_ms", int
        ),
    )
    config = AppConfig(
        target, controls, weapons, diagnostics, audio, primary, secondary
    )
    validate_config(config)
    return config


def validate_config(config: AppConfig) -> None:
    if not config.target.executable:
        raise ConfigError("target.executable must be nonempty")
    if Path(config.target.executable).name in {"", ".", ".."}:
        raise ConfigError("target.executable must name an executable")
    if config.target.foreground_poll_ms != 5:
        raise ConfigError("target.foreground_poll_ms must be 5 for this safety profile")
    if config.target.foreground_cache_max_age_ms <= 0:
        raise ConfigError("target.foreground_cache_max_age_ms must be positive")
    if config.target.foreground_cache_max_age_ms < config.target.foreground_poll_ms:
        raise ConfigError(
            "target.foreground_cache_max_age_ms must be at least foreground_poll_ms"
        )

    supported = {
        "controls.fire_toggle_button": (config.controls.fire_toggle_button, "MB1"),
        "controls.normal_fire_modifier": (config.controls.normal_fire_modifier, "CTRL"),
        "controls.primary_select_key": (config.controls.primary_select_key, "1"),
        "controls.secondary_select_key": (config.controls.secondary_select_key, "2"),
        "controls.reload_key": (config.controls.reload_key, "R"),
    }
    for name, (actual, expected) in supported.items():
        if actual != expected:
            raise ConfigError(f"{name} supports only {expected!r}")
    if config.controls.poll_ms != 5:
        raise ConfigError("controls.poll_ms must be 5 for cancellation safety")
    _nonnegative("controls.toggle_debounce_ms", config.controls.toggle_debounce_ms)
    if config.controls.deferred_bypass_click_ms <= 0:
        raise ConfigError("controls.deferred_bypass_click_ms must be positive")
    _nonnegative("weapons.switch_settle_ms", config.weapons.switch_settle_ms)

    for name, tone in (("audio.on", config.audio.on), ("audio.off", config.audio.off)):
        if not 37 <= tone.frequency_hz <= 32767:
            raise ConfigError(
                f"{name}.frequency_hz must be in the winsound.Beep range 37..32767"
            )
        _nonnegative(f"{name}.duration_ms", tone.duration_ms)

    if config.primary.shots_per_cycle != 3:
        raise ConfigError("primary.shots_per_cycle must be exactly 3")
    if config.secondary.shots_per_cycle != 13:
        raise ConfigError("secondary.shots_per_cycle must be exactly 13")
    for name, value in (
        ("primary.fire_hold_ms", config.primary.fire_hold_ms),
        ("primary.inter_shot_ms", config.primary.inter_shot_ms),
        ("primary.post_last_shot_ms", config.primary.post_last_shot_ms),
        ("primary.reload_press_ms", config.primary.reload_press_ms),
        ("primary.reload_wait_ms", config.primary.reload_wait_ms),
        ("secondary.shot_period_ms", config.secondary.shot_period_ms),
        ("secondary.fire_press_ms", config.secondary.fire_press_ms),
        ("secondary.reload_press_ms", config.secondary.reload_press_ms),
        ("secondary.reload_wait_ms", config.secondary.reload_wait_ms),
    ):
        _nonnegative(name, value)
    if config.primary.shots_per_cycle <= 0 or config.secondary.shots_per_cycle <= 0:
        raise ConfigError("shot counts must be positive")
    if config.secondary.fire_press_ms > config.secondary.shot_period_ms:
        raise ConfigError(
            "secondary.fire_press_ms must not exceed secondary.shot_period_ms"
        )


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot load {config_path}: {exc}") from exc
    return parse_config(raw)
