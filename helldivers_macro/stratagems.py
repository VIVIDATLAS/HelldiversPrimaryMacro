from __future__ import annotations

from enum import Enum

from .config import StratagemsConfig


class Direction(Enum):
    UP = (0x48, True)
    LEFT = (0x4B, True)
    RIGHT = (0x4D, True)
    DOWN = (0x50, True)

    @property
    def scan_code(self) -> int:
        return self.value[0]


LEFT_CTRL_SCAN_CODE = 0x1D

FOUR_TARGET_SEQUENCES: tuple[tuple[Direction, ...], ...] = (
    (Direction.DOWN, Direction.UP, Direction.RIGHT, Direction.RIGHT, Direction.UP),
    (Direction.DOWN, Direction.UP, Direction.RIGHT, Direction.LEFT),
    (Direction.DOWN, Direction.UP, Direction.RIGHT, Direction.UP, Direction.LEFT, Direction.UP),
    (Direction.DOWN, Direction.UP, Direction.RIGHT, Direction.RIGHT, Direction.LEFT),
)

SUPPORT_SEQUENCES: tuple[tuple[Direction, ...], ...] = (
    (Direction.DOWN, Direction.DOWN, Direction.UP, Direction.RIGHT),
    (Direction.UP, Direction.DOWN, Direction.RIGHT, Direction.LEFT, Direction.UP),
)


def sequence_duration_ms(
    sequences: tuple[tuple[Direction, ...], ...], config: StratagemsConfig
) -> int:
    return sum(
        config.ctrl_settle_ms
        + len(sequence) * (config.key_press_ms + config.key_gap_ms)
        + config.action_press_ms
        + config.action_delay_ms
        for sequence in sequences
    )
