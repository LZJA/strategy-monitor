from __future__ import annotations

from typing import Any, Callable

import numpy as np
import pandas as pd


def build_ultra_short_hot_template(
    *,
    pattern_template_cls: Any,
    env_int: Callable[[str, int], int],
    env_float: Callable[[str, float], float],
) -> Any:
    return pattern_template_cls(
        name="超短期热门",
        window_days=env_int("ULTRA_SHORT_WINDOW_DAYS", 50),
        window_days_max=0,
        b_window_days=0,
        recent_low_window_days=0,
        max_ab_gap_ratio=env_float("ULTRA_SHORT_MAX_AB_GAP_RATIO", 0.15),
        low_ratio_threshold=0.0,
        local_extrema_neighbor_days=env_int("ULTRA_SHORT_LOCAL_EXTREMA_NEIGHBOR_DAYS", 2),
        min_ac_amplitude_ratio=env_float("ULTRA_SHORT_MIN_AC_AMPLITUDE_RATIO", 0.10),
        min_bd_amplitude_ratio=env_float("ULTRA_SHORT_MIN_BD_AMPLITUDE_RATIO", 0.08),
        min_b_peak_prominence_ratio=env_float("ULTRA_SHORT_MIN_B_PEAK_PROMINENCE_RATIO", 0.02),
        post_d_peak_neighbor_days=env_int("LONG_LOCAL_EXTREMA_NEIGHBOR_DAYS", 2),
        min_breakout_over_d_ratio=env_float("ULTRA_SHORT_MIN_BREAKOUT_OVER_D_RATIO", 0.03),
        amount_rank_min=env_int("ULTRA_SHORT_AMOUNT_RANK_MIN", 200),
        amount_rank_max=env_int("ULTRA_SHORT_AMOUNT_RANK_MAX", 300),
    )


def _build_pattern_detail(
    frame: pd.DataFrame,
    pattern_name: str,
    a_idx: int,
    b_idx: int,
    c_idx: int,
    d_idx: int,
    latest_idx: int,
    neckline_price: float,
    pattern_detail_cls: Any,
) -> Any:
    highs = frame["High"]
    lows = frame["Low"]
    closes = frame["Close"]
    return pattern_detail_cls(
        pattern_name=pattern_name,
        point_a_date=pd.Timestamp(frame.index[a_idx]).strftime("%Y-%m-%d"),
        point_b_date=pd.Timestamp(frame.index[b_idx]).strftime("%Y-%m-%d"),
        point_c_date=pd.Timestamp(frame.index[c_idx]).strftime("%Y-%m-%d"),
        point_d_date=pd.Timestamp(frame.index[d_idx]).strftime("%Y-%m-%d"),
        point_a_price=float(highs.iloc[a_idx]),
        point_b_price=float(highs.iloc[b_idx]),
        point_c_price=float(lows.iloc[c_idx]),
        point_d_price=float(lows.iloc[d_idx]),
        breakout_price=round(float(neckline_price), 4),
        e_close=float(closes.iloc[latest_idx]),
        e_high=float(highs.iloc[latest_idx]),
    )


def _candidate_pattern_is_valid(
    highs: pd.Series,
    lows: pd.Series,
    a_idx: int,
    b_idx: int,
    c_idx: int,
    d_idx: int,
    max_ab_gap_ratio: float,
    min_ac_amplitude_ratio: float,
    min_bd_amplitude_ratio: float,
) -> bool:
    point_a_price = float(highs.iloc[a_idx])
    point_b_price = float(highs.iloc[b_idx])
    point_c_price = float(lows.iloc[c_idx])
    point_d_price = float(lows.iloc[d_idx])
    if not (a_idx < c_idx < b_idx < d_idx):
        return False
    if point_b_price <= 0 or point_c_price <= 0 or point_d_price <= 0:
        return False
    if point_a_price < point_b_price:
        return False
    if max_ab_gap_ratio > 0 and (point_a_price / point_b_price - 1.0) > max_ab_gap_ratio:
        return False
    if min_ac_amplitude_ratio > 0 and (point_a_price - point_c_price) / point_a_price < min_ac_amplitude_ratio:
        return False
    if min_bd_amplitude_ratio > 0 and (point_b_price - point_d_price) / point_b_price < min_bd_amplitude_ratio:
        return False
    return True


def _select_ab_indices(
    highs: pd.Series,
    peak_indices: list[int],
    neighbor_days: int,
    min_peak_prominence_ratio: float,
) -> tuple[int, int] | None:
    if len(peak_indices) < 2:
        return None

    # Pick the nearest peak as B, then walk further back for the first A with A >= B.
    sorted_desc = sorted(peak_indices, reverse=True)
    for b_idx in sorted_desc:
        if not _passes_peak_prominence_filter(
            highs=highs,
            idx=b_idx,
            neighbor_days=neighbor_days,
            min_peak_prominence_ratio=min_peak_prominence_ratio,
        ):
            continue
        b_price = float(highs.iloc[b_idx])
        earlier_peaks = [idx for idx in sorted_desc if idx < b_idx]
        for a_idx in earlier_peaks:
            if not _passes_peak_prominence_filter(
                highs=highs,
                idx=a_idx,
                neighbor_days=neighbor_days,
                min_peak_prominence_ratio=min_peak_prominence_ratio,
            ):
                continue
            if float(highs.iloc[a_idx]) >= b_price:
                return a_idx, b_idx
    return None


def _passes_peak_prominence_filter(
    highs: pd.Series,
    idx: int,
    neighbor_days: int,
    min_peak_prominence_ratio: float,
) -> bool:
    if min_peak_prominence_ratio <= 0:
        return True
    if idx - neighbor_days < 0 or idx + neighbor_days >= len(highs):
        return False
    center = pd.to_numeric(pd.Series([highs.iloc[idx]]), errors="coerce").iloc[0]
    if pd.isna(center) or float(center) <= 0:
        return False
    left = pd.to_numeric(highs.iloc[idx - neighbor_days : idx], errors="coerce")
    right = pd.to_numeric(highs.iloc[idx + 1 : idx + neighbor_days + 1], errors="coerce")
    neighbors = pd.concat([left, right]).dropna()
    if len(neighbors) != neighbor_days * 2:
        return False
    reference_high = float(neighbors.max())
    prominence_ratio = (float(center) - reference_high) / float(center)
    return prominence_ratio >= float(min_peak_prominence_ratio)


def _select_lowest_trough_index(
    trough_indices: list[int],
    lows: pd.Series,
    start_exclusive: int,
    end_exclusive: int,
) -> int | None:
    eligible = [
        idx
        for idx in trough_indices
        if start_exclusive < idx < end_exclusive
        and pd.notna(pd.to_numeric(lows.iloc[idx], errors="coerce"))
    ]
    if not eligible:
        return None
    return min(eligible, key=lambda idx: (float(lows.iloc[idx]), -idx))


def _has_post_d_major_peak(
    highs: pd.Series,
    d_idx: int,
    latest_idx: int,
    neighbor_days: int,
    min_peak_prominence_ratio: float,
    *,
    is_local_peak: Callable[[pd.Series, int, int], bool],
) -> bool:
    check_neighbor_days = max(int(neighbor_days), 0)
    if check_neighbor_days <= 0 or d_idx >= latest_idx:
        return False
    for idx in range(d_idx + 1, latest_idx):
        if not is_local_peak(highs, idx, check_neighbor_days):
            continue
        if _passes_peak_prominence_filter(
            highs=highs,
            idx=idx,
            neighbor_days=check_neighbor_days,
            min_peak_prominence_ratio=min_peak_prominence_ratio,
        ):
            return True
    return False


def _is_two_lowest_points_in_window(
    lows: pd.Series,
    c_idx: int,
    d_idx: int,
    latest_idx: int,
) -> bool:
    eligible = [
        (float(lows.iloc[idx]), idx)
        for idx in range(latest_idx)
        if pd.notna(pd.to_numeric(lows.iloc[idx], errors="coerce"))
    ]
    if len(eligible) < 2:
        return False
    two_lowest = sorted(eligible, key=lambda item: (item[0], item[1]))[:2]
    return {c_idx, d_idx} == {idx for _, idx in two_lowest}


def _finalize_breakout_or_watch(
    frame: pd.DataFrame,
    detail: Any,
    d_idx: int,
    prebreakout_gap_ratio: float,
    watchlist_min_d_age_ratio: float,
    watchlist_min_rebound_position_ratio: float,
    watchlist_max_rebound_position_ratio: float,
    pullback_confirm_lookback_days: int,
    *,
    passes_neckline_breakout: Callable[..., bool],
    detect_neckline_pullback: Callable[..., Any],
    build_scan_outcome: Callable[..., Any],
) -> Any:
    closes = frame["Close"]
    latest_idx = len(frame) - 1
    point_d_price = detail.point_d_price
    confirmed_breakout = passes_neckline_breakout(
        closes=closes,
        d_idx=d_idx,
        point_d_price=point_d_price,
        latest_idx=latest_idx,
        latest_e_price=detail.e_close,
        breakout_price=detail.breakout_price,
    )
    if confirmed_breakout:
        pullback = detect_neckline_pullback(
            frame=frame,
            breakout_price=detail.breakout_price,
            d_idx=d_idx,
            latest_idx=latest_idx,
            lookback_days=pullback_confirm_lookback_days,
        )
        if pullback is not None:
            detail.pullback_confirmed = True
            detail.pullback_date = pullback["date"]
            detail.pullback_low = pullback["low"]
            detail.pullback_close = pullback["close"]
        return build_scan_outcome(matched=detail, watch=None)

    d_age_days = latest_idx - d_idx
    min_d_age_days = max(int(np.ceil(len(frame) * max(float(watchlist_min_d_age_ratio), 0.0))), 1)
    bd_span = detail.point_b_price - detail.point_d_price
    rebound_position_ratio = np.inf
    if bd_span > 0:
        rebound_position_ratio = (detail.e_close - detail.point_d_price) / bd_span

    if (
        detail.close_gap_ratio >= 0
        and detail.close_gap_ratio <= prebreakout_gap_ratio
        and d_age_days >= min_d_age_days
        and detail.e_close > detail.point_d_price
        and rebound_position_ratio >= float(watchlist_min_rebound_position_ratio)
        and rebound_position_ratio <= float(watchlist_max_rebound_position_ratio)
    ):
        return build_scan_outcome(matched=None, watch=detail)
    return build_scan_outcome(matched=None, watch=None)


def match_ultra_short_hot_breakout(
    frame: pd.DataFrame,
    pattern_name: str,
    max_ab_gap_ratio: float,
    low_ratio_threshold: float,
    min_breakout_over_d_ratio: float,
    prebreakout_gap_ratio: float,
    watchlist_min_d_age_ratio: float,
    watchlist_min_rebound_position_ratio: float,
    watchlist_max_rebound_position_ratio: float,
    local_extrema_neighbor_days: int = 1,
    min_ac_amplitude_ratio: float = 0.0,
    min_bd_amplitude_ratio: float = 0.0,
    min_b_peak_prominence_ratio: float = 0.0,
    post_d_peak_neighbor_days: int = 0,
    pullback_confirm_lookback_days: int = 10,
    *,
    pattern_detail_cls: Any,
    build_scan_outcome: Callable[..., Any],
    is_local_peak: Callable[[pd.Series, int, int], bool],
    is_local_trough: Callable[[pd.Series, int, int], bool],
    calc_neckline_price: Callable[..., float],
    passes_neckline_breakout: Callable[..., bool],
    detect_neckline_pullback: Callable[..., Any],
) -> Any:
    if frame is None or frame.empty or len(frame) < 10:
        return build_scan_outcome(matched=None, watch=None)

    closes = frame["Close"]
    highs = frame["High"]
    lows = frame["Low"]
    latest_idx = len(frame) - 1
    if float(closes.iloc[latest_idx]) <= 0:
        return build_scan_outcome(matched=None, watch=None)

    neighbor_days = max(int(local_extrema_neighbor_days), 1)
    peak_indices = [
        idx
        for idx in range(len(frame))
        if idx < latest_idx and is_local_peak(highs, idx, neighbor_days)
    ]
    trough_indices = [
        idx
        for idx in range(len(frame))
        if idx < latest_idx and is_local_trough(lows, idx, neighbor_days)
    ]
    if not peak_indices or not trough_indices:
        return build_scan_outcome(matched=None, watch=None)

    _ = low_ratio_threshold
    ab_indices = _select_ab_indices(
        highs=highs,
        peak_indices=peak_indices,
        neighbor_days=neighbor_days,
        min_peak_prominence_ratio=min_b_peak_prominence_ratio,
    )
    if ab_indices is None:
        return build_scan_outcome(matched=None, watch=None)

    a_idx, b_idx = ab_indices
    c_idx = _select_lowest_trough_index(
        trough_indices=trough_indices,
        lows=lows,
        start_exclusive=a_idx,
        end_exclusive=b_idx,
    )
    d_idx = _select_lowest_trough_index(
        trough_indices=trough_indices,
        lows=lows,
        start_exclusive=b_idx,
        end_exclusive=latest_idx,
    )
    if c_idx is None or d_idx is None:
        return build_scan_outcome(matched=None, watch=None)

    if _candidate_pattern_is_valid(
        highs=highs,
        lows=lows,
        a_idx=a_idx,
        b_idx=b_idx,
        c_idx=c_idx,
        d_idx=d_idx,
        max_ab_gap_ratio=max_ab_gap_ratio,
        min_ac_amplitude_ratio=min_ac_amplitude_ratio,
        min_bd_amplitude_ratio=min_bd_amplitude_ratio,
    ):
        neckline_price = calc_neckline_price(
            point_a_price=float(highs.iloc[a_idx]),
            point_b_price=float(highs.iloc[b_idx]),
            a_idx=a_idx,
            b_idx=b_idx,
            latest_idx=latest_idx,
        )
        point_d_price = float(lows.iloc[d_idx])
        if neckline_price <= point_d_price:
            return build_scan_outcome(matched=None, watch=None)
        if min_breakout_over_d_ratio > 0 and neckline_price / point_d_price - 1.0 < min_breakout_over_d_ratio:
            return build_scan_outcome(matched=None, watch=None)
        if _has_post_d_major_peak(
            highs=highs,
            d_idx=d_idx,
            latest_idx=latest_idx,
            neighbor_days=post_d_peak_neighbor_days,
            min_peak_prominence_ratio=min_b_peak_prominence_ratio,
            is_local_peak=is_local_peak,
        ):
            return build_scan_outcome(matched=None, watch=None)
        detail = _build_pattern_detail(
            frame=frame,
            pattern_name=pattern_name,
            a_idx=a_idx,
            b_idx=b_idx,
            c_idx=c_idx,
            d_idx=d_idx,
            latest_idx=latest_idx,
            neckline_price=neckline_price,
            pattern_detail_cls=pattern_detail_cls,
        )
        outcome = _finalize_breakout_or_watch(
            frame=frame,
            detail=detail,
            d_idx=d_idx,
            prebreakout_gap_ratio=prebreakout_gap_ratio,
            watchlist_min_d_age_ratio=watchlist_min_d_age_ratio,
            watchlist_min_rebound_position_ratio=watchlist_min_rebound_position_ratio,
            watchlist_max_rebound_position_ratio=watchlist_max_rebound_position_ratio,
            pullback_confirm_lookback_days=pullback_confirm_lookback_days,
            passes_neckline_breakout=passes_neckline_breakout,
            detect_neckline_pullback=detect_neckline_pullback,
            build_scan_outcome=build_scan_outcome,
        )
        if outcome.matched is not None or outcome.watch is not None:
            return outcome

    return build_scan_outcome(matched=None, watch=None)
