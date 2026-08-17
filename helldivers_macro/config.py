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
    shift_cancels_aim_natively: bool


@dataclass(frozen=True)
class OutputConfig:
    fire_device: str
    fire_scan_code: int | None


@dataclass(frozen=True)
class WeaponsConfig:
    reload_on_select: bool
    switch_settle_ms: int


@dataclass(frozen=True)
class BehaviorConfig:
    start_policy: str


@dataclass(frozen=True)
class DiagnosticsConfig:
    ctrl_bypass_logging: bool
    state_tracing: bool


@dataclass(frozen=True)
class StratagemsConfig:
    enabled: bool
    four_target_trigger: str
    support_trigger: str
    key_press_ms: int
    key_gap_ms: int
    ctrl_settle_ms: int
    action_press_ms: int
    action_delay_ms: int


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
    fire_mode: str
    shots_per_cycle: int | None
    shot_period_ms: int | None
    fire_press_ms: int | None
    automatic_hold_ms: int | None
    post_fire_reload_delay_ms: int
    reload_press_ms: int
    reload_wait_ms: int


@dataclass(frozen=True)
class SecondaryConfig:
    fire_mode: str
    shots_per_cycle: int | None
    shot_period_ms: int | None
    fire_press_ms: int | None
    automatic_hold_ms: int | None
    post_fire_reload_delay_ms: int
    reload_press_ms: int
    reload_wait_ms: int


@dataclass(frozen=True)
class AppConfig:
    target: TargetConfig
    controls: ControlsConfig
    output: OutputConfig
    weapons: WeaponsConfig
    behavior: BehaviorConfig
    diagnostics: DiagnosticsConfig
    stratagems: StratagemsConfig
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


def _optional_int(
    table: Mapping[str, Any], table_name: str, name: str
) -> int | None:
    if name not in table:
        return None
    return _value(table, table_name, name, int)


def _weapon_config(
    table: Mapping[str, Any], table_name: str, config_type: type
) -> Any:
    fire_mode = _value(table, table_name, "fire_mode", str)
    automatic_hold_ms = _optional_int(
        table, table_name, "automatic_hold_ms"
    )
    post_fire_reload_delay_ms = _optional_int(
        table, table_name, "post_fire_reload_delay_ms"
    )
    if fire_mode == "automatic_hold":
        if automatic_hold_ms is None:
            raise ConfigError(
                f"missing configuration value: {table_name}.automatic_hold_ms"
            )
        if post_fire_reload_delay_ms is None:
            raise ConfigError(
                "missing configuration value: "
                f"{table_name}.post_fire_reload_delay_ms"
            )
    return config_type(
        fire_mode=fire_mode,
        shots_per_cycle=_optional_int(table, table_name, "shots_per_cycle"),
        shot_period_ms=_optional_int(table, table_name, "shot_period_ms"),
        fire_press_ms=_optional_int(table, table_name, "fire_press_ms"),
        automatic_hold_ms=automatic_hold_ms,
        post_fire_reload_delay_ms=(
            0
            if post_fire_reload_delay_ms is None
            else post_fire_reload_delay_ms
        ),
        reload_press_ms=_value(table, table_name, "reload_press_ms", int),
        reload_wait_ms=_value(table, table_name, "reload_wait_ms", int),
    )


def parse_config(data: Mapping[str, Any]) -> AppConfig:
    target_t = _table(data, "target")
    controls_t = _table(data, "controls")
    output_t = _table(data, "output")
    weapons_t = _table(data, "weapons")
    behavior_t = _table(data, "behavior")
    diagnostics_t = _table(data, "diagnostics")
    stratagems_t = _table(data, "stratagems")
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
        shift_cancels_aim_natively=_value(
            controls_t, "controls", "shift_cancels_aim_natively", bool
        ),
    )
    fire_device = _value(output_t, "output", "fire_device", str)
    fire_scan_code = output_t.get("fire_scan_code")
    if fire_scan_code is not None and (
        not isinstance(fire_scan_code, int) or isinstance(fire_scan_code, bool)
    ):
        raise ConfigError("output.fire_scan_code must be an integer")
    output = OutputConfig(
        fire_device=fire_device,
        fire_scan_code=fire_scan_code,
    )
    weapons = WeaponsConfig(
        reload_on_select=_value(
            weapons_t, "weapons", "reload_on_select", bool
        ),
        switch_settle_ms=_value(weapons_t, "weapons", "switch_settle_ms", int),
    )
    behavior = BehaviorConfig(
        start_policy=_value(
            behavior_t, "behavior", "start_policy", str
        ).casefold(),
    )
    diagnostics = DiagnosticsConfig(
        ctrl_bypass_logging=_value(
            diagnostics_t, "diagnostics", "ctrl_bypass_logging", bool
        ),
        state_tracing=_value(
            diagnostics_t, "diagnostics", "state_tracing", bool
        ),
    )
    stratagems = StratagemsConfig(
        enabled=_value(stratagems_t, "stratagems", "enabled", bool),
        four_target_trigger=_value(
            stratagems_t, "stratagems", "four_target_trigger", str
        ).upper(),
        support_trigger=_value(
            stratagems_t, "stratagems", "support_trigger", str
        ).upper(),
        key_press_ms=_value(stratagems_t, "stratagems", "key_press_ms", int),
        key_gap_ms=_value(stratagems_t, "stratagems", "key_gap_ms", int),
        ctrl_settle_ms=_value(stratagems_t, "stratagems", "ctrl_settle_ms", int),
        action_press_ms=_value(stratagems_t, "stratagems", "action_press_ms", int),
        action_delay_ms=_value(stratagems_t, "stratagems", "action_delay_ms", int),
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
    primary = _weapon_config(primary_t, "primary", PrimaryConfig)
    secondary = _weapon_config(secondary_t, "secondary", SecondaryConfig)
    config = AppConfig(
        target,
        controls,
        output,
        weapons,
        behavior,
        diagnostics,
        stratagems,
        audio,
        primary,
        secondary,
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
    if config.output.fire_device not in {"keyboard", "mouse"}:
        raise ConfigError(
            "output.fire_device must be exactly 'keyboard' or 'mouse'"
        )
    if config.output.fire_device == "keyboard":
        if (
            type(config.output.fire_scan_code) is not int
            or not 1 <= config.output.fire_scan_code <= 0xFF
        ):
            raise ConfigError(
                "output.fire_scan_code must be an integer from 1 through 255 "
                "for keyboard output"
            )
    elif config.output.fire_scan_code is not None and (
        type(config.output.fire_scan_code) is not int
        or not 1 <= config.output.fire_scan_code <= 0xFF
    ):
        raise ConfigError(
            "output.fire_scan_code must be an integer from 1 through 255 when set"
        )
    _nonnegative("weapons.switch_settle_ms", config.weapons.switch_settle_ms)
    if config.behavior.start_policy != "immediate":
        raise ConfigError("behavior.start_policy supports only 'immediate'")

    for name, trigger in (
        ("four_target_trigger", config.stratagems.four_target_trigger),
        ("support_trigger", config.stratagems.support_trigger),
    ):
        if trigger not in {"F23", "F24"}:
            raise ConfigError(f"stratagems.{name} supports only 'F23' or 'F24'")
    if config.stratagems.four_target_trigger == config.stratagems.support_trigger:
        raise ConfigError("stratagem trigger keys must be distinct")
    for name in (
        "key_press_ms", "key_gap_ms", "ctrl_settle_ms", "action_press_ms"
    ):
        value = getattr(config.stratagems, name)
        if not 1 <= value <= 10_000:
            raise ConfigError(f"stratagems.{name} must be from 1 through 10000 ms")
    if not 0 <= config.stratagems.action_delay_ms <= 60_000:
        raise ConfigError(
            "stratagems.action_delay_ms must be from 0 through 60000 ms"
        )

    for name, tone in (("audio.on", config.audio.on), ("audio.off", config.audio.off)):
        if not 37 <= tone.frequency_hz <= 32767:
            raise ConfigError(
                f"{name}.frequency_hz must be in the winsound.Beep range 37..32767"
            )
        _nonnegative(f"{name}.duration_ms", tone.duration_ms)

    for name, weapon in (
        ("primary", config.primary),
        ("secondary", config.secondary),
    ):
        if weapon.fire_mode not in {"tap", "automatic_hold"}:
            raise ConfigError(
                f"{name}.fire_mode must be exactly 'tap' or 'automatic_hold'"
            )
        _nonnegative(f"{name}.reload_press_ms", weapon.reload_press_ms)
        _nonnegative(f"{name}.reload_wait_ms", weapon.reload_wait_ms)
        _nonnegative(
            f"{name}.post_fire_reload_delay_ms",
            weapon.post_fire_reload_delay_ms,
        )
        if weapon.fire_mode == "automatic_hold":
            if (
                type(weapon.automatic_hold_ms) is not int
                or weapon.automatic_hold_ms <= 0
            ):
                raise ConfigError(
                    f"{name}.automatic_hold_ms must be a positive integer"
                )
            continue
        for field in ("shots_per_cycle", "shot_period_ms", "fire_press_ms"):
            if getattr(weapon, field) is None:
                raise ConfigError(f"missing configuration value: {name}.{field}")
        if name == "primary":
            if (
                type(weapon.shots_per_cycle) is not int
                or not 1 <= weapon.shots_per_cycle <= 1000
            ):
                raise ConfigError(
                    "primary.shots_per_cycle must be an integer from 1 through 1000"
                )
        elif weapon.shots_per_cycle != 13:
            raise ConfigError("secondary.shots_per_cycle must be exactly 13")
        _nonnegative(f"{name}.shot_period_ms", weapon.shot_period_ms)
        _nonnegative(f"{name}.fire_press_ms", weapon.fire_press_ms)
        if weapon.fire_press_ms > weapon.shot_period_ms:
            raise ConfigError(
                f"{name}.fire_press_ms must not exceed {name}.shot_period_ms"
            )


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot load {config_path}: {exc}") from exc
    return parse_config(raw)
