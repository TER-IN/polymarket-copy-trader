from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable, Mapping


class ShadowRegimePath(StrEnum):
    FOLLOW = "follow_shadow"
    INVERT = "invert_shadow"


class ShadowRegimeInitialPath(StrEnum):
    WARMUP = "warmup"
    FOLLOW = ShadowRegimePath.FOLLOW
    INVERT = ShadowRegimePath.INVERT


class ShadowRegimeOverride(StrEnum):
    AUTO = "auto"
    FOLLOW = ShadowRegimePath.FOLLOW
    INVERT = ShadowRegimePath.INVERT


def effective_shadow_regime_path(
    snapshot: ShadowRegimeSnapshot,
    initial_path: ShadowRegimeInitialPath,
    override: ShadowRegimeOverride,
) -> ShadowRegimePath | None:
    if override == ShadowRegimeOverride.FOLLOW:
        return ShadowRegimePath.FOLLOW
    if override == ShadowRegimeOverride.INVERT:
        return ShadowRegimePath.INVERT
    if snapshot.active_path is not None:
        return snapshot.active_path
    if initial_path == ShadowRegimeInitialPath.FOLLOW:
        return ShadowRegimePath.FOLLOW
    if initial_path == ShadowRegimeInitialPath.INVERT:
        return ShadowRegimePath.INVERT
    return None


@dataclass(frozen=True)
class ShadowRegimeSnapshot:
    resolved_markets: int
    window_size: int
    shadow_wins: int
    shadow_win_rate: float | None
    active_path: ShadowRegimePath | None
    desired_path: ShadowRegimePath | None
    pending_path: ShadowRegimePath | None
    confirmation_count: int
    confirmation_required: int
    switch_count: int
    last_transition_at: str | None
    last_transition_reason: str | None

    @property
    def ready(self) -> bool:
        return self.active_path is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "resolved_markets": self.resolved_markets,
            "window_size": self.window_size,
            "shadow_wins": self.shadow_wins,
            "shadow_win_rate": self.shadow_win_rate,
            "active_path": self.active_path.value if self.active_path else None,
            "desired_path": self.desired_path.value if self.desired_path else None,
            "pending_path": self.pending_path.value if self.pending_path else None,
            "confirmation_count": self.confirmation_count,
            "confirmation_required": self.confirmation_required,
            "switch_count": self.switch_count,
            "last_transition_at": self.last_transition_at,
            "last_transition_reason": self.last_transition_reason,
        }


def calculate_shadow_regime(
    rows: Iterable[Mapping[str, object]],
    window_size: int = 50,
    confirmation_required: int = 10,
) -> ShadowRegimeSnapshot:
    row_list = list(rows)
    results = [
        1 if float(row["shadow_payout_per_share"]) > 0.5 else 0
        for row in row_list
    ]
    active: ShadowRegimePath | None = None
    pending: ShadowRegimePath | None = None
    confirmation_count = 0
    switch_count = 0
    desired: ShadowRegimePath | None = None
    last_wins = 0
    last_rate: float | None = None
    last_transition_at: str | None = None
    last_transition_reason: str | None = None

    for end in range(window_size, len(results) + 1):
        window = results[end - window_size : end]
        last_wins = sum(window)
        last_rate = last_wins / window_size
        desired = _desired_path(last_rate)

        if active is None:
            if desired is not None:
                active = desired
                last_transition_at = _resolved_at(row_list, end - 1)
                last_transition_reason = (
                    f"initial {window_size}-market window win rate "
                    f"{last_rate:.2%} selected {active.value}"
                )
            continue

        if desired is None or desired == active:
            pending = None
            confirmation_count = 0
            continue

        if pending == desired:
            confirmation_count += 1
        else:
            pending = desired
            confirmation_count = 1

        if confirmation_count >= confirmation_required:
            active = desired
            pending = None
            confirmation_count = 0
            switch_count += 1
            last_transition_at = _resolved_at(row_list, end - 1)
            last_transition_reason = (
                f"rolling {window_size}-market win rate {last_rate:.2%} "
                f"confirmed {active.value} for {confirmation_required} markets"
            )

    if len(results) < window_size:
        recent = results[-window_size:]
        last_wins = sum(recent)
        last_rate = (last_wins / len(recent)) if recent else None
        desired = None

    return ShadowRegimeSnapshot(
        resolved_markets=len(results),
        window_size=window_size,
        shadow_wins=last_wins,
        shadow_win_rate=last_rate,
        active_path=active,
        desired_path=desired,
        pending_path=pending,
        confirmation_count=confirmation_count,
        confirmation_required=confirmation_required,
        switch_count=switch_count,
        last_transition_at=last_transition_at,
        last_transition_reason=last_transition_reason,
    )


def _desired_path(win_rate: float) -> ShadowRegimePath | None:
    if win_rate > 0.5:
        return ShadowRegimePath.FOLLOW
    if win_rate < 0.5:
        return ShadowRegimePath.INVERT
    return None


def _resolved_at(
    rows: list[Mapping[str, object]],
    index: int,
) -> str | None:
    value = dict(rows[index]).get("resolved_at")
    return str(value) if value is not None else None
