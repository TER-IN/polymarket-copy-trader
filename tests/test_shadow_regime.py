from shadow_regime import (
    ShadowRegimeInitialPath,
    ShadowRegimeOverride,
    ShadowRegimePath,
    calculate_shadow_regime,
    effective_shadow_regime_path,
)


def rows(results: list[int]) -> list[dict[str, float]]:
    return [{"shadow_payout_per_share": float(result)} for result in results]


def test_shadow_regime_waits_for_full_window() -> None:
    snapshot = calculate_shadow_regime(rows([1] * 49), window_size=50)

    assert not snapshot.ready
    assert snapshot.resolved_markets == 49
    assert snapshot.active_path is None


def test_shadow_regime_initializes_from_first_full_window() -> None:
    follow = calculate_shadow_regime(rows([1] * 26 + [0] * 24), window_size=50)
    invert = calculate_shadow_regime(rows([1] * 24 + [0] * 26), window_size=50)
    tie = calculate_shadow_regime(rows([1] * 25 + [0] * 25), window_size=50)

    assert follow.active_path == ShadowRegimePath.FOLLOW
    assert invert.active_path == ShadowRegimePath.INVERT
    assert tie.active_path is None


def test_shadow_regime_requires_consecutive_confirmation_before_switch() -> None:
    initial = [1] * 26 + [0] * 24
    nine_below = initial + [0] * 10
    ten_below = initial + [0] * 11

    pending = calculate_shadow_regime(
        rows(nine_below),
        window_size=50,
        confirmation_required=10,
    )
    switched = calculate_shadow_regime(
        rows(ten_below),
        window_size=50,
        confirmation_required=10,
    )

    assert pending.active_path == ShadowRegimePath.FOLLOW
    assert pending.pending_path == ShadowRegimePath.INVERT
    assert pending.confirmation_count == 9
    assert switched.active_path == ShadowRegimePath.INVERT
    assert switched.confirmation_count == 0
    assert switched.switch_count == 1


def test_shadow_regime_tie_does_not_start_pending_switch() -> None:
    initial = [1] * 26 + [0] * 24
    results = initial + [0]

    snapshot = calculate_shadow_regime(
        rows(results),
        window_size=50,
        confirmation_required=10,
    )

    assert snapshot.active_path == ShadowRegimePath.FOLLOW
    assert snapshot.pending_path is None
    assert snapshot.confirmation_count == 0


def test_effective_path_precedence_is_override_then_calculated_then_initial() -> None:
    warmup = calculate_shadow_regime(rows([1]), window_size=2)
    calculated = calculate_shadow_regime(rows([1, 1]), window_size=2)

    assert (
        effective_shadow_regime_path(
            warmup,
            ShadowRegimeInitialPath.FOLLOW,
            ShadowRegimeOverride.AUTO,
        )
        == ShadowRegimePath.FOLLOW
    )
    assert (
        effective_shadow_regime_path(
            calculated,
            ShadowRegimeInitialPath.INVERT,
            ShadowRegimeOverride.AUTO,
        )
        == ShadowRegimePath.FOLLOW
    )
    assert (
        effective_shadow_regime_path(
            calculated,
            ShadowRegimeInitialPath.FOLLOW,
            ShadowRegimeOverride.INVERT,
        )
        == ShadowRegimePath.INVERT
    )
