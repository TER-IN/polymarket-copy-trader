from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable, Mapping


class ShadowRegimePath(StrEnum):
    FOLLOW = "follow_shadow"
    INVERT = "invert_shadow"


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
        }


def calculate_shadow_regime(
    rows: Iterable[Mapping[str, object]],
    window_size: int = 50,
    confirmation_required: int = 10,
) -> ShadowRegimeSnapshot:
    results = [
        1 if float(row["shadow_payout_per_share"]) > 0.5 else 0
        for row in rows
    ]
    active: ShadowRegimePath | None = None
    pending: ShadowRegimePath | None = None
    confirmation_count = 0
    switch_count = 0
    desired: ShadowRegimePath | None = None
    last_wins = 0
    last_rate: float | None = None

    for end in range(window_size, len(results) + 1):
        window = results[end - window_size : end]
        last_wins = sum(window)
        last_rate = last_wins / window_size
        desired = _desired_path(last_rate)

        if active is None:
            if desired is not None:
                active = desired
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
    )


def _desired_path(win_rate: float) -> ShadowRegimePath | None:
    if win_rate > 0.5:
        return ShadowRegimePath.FOLLOW
    if win_rate < 0.5:
        return ShadowRegimePath.INVERT
    return None
