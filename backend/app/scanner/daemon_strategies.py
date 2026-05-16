from __future__ import annotations

import csv
import json
import math
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from app.scanner.pattern_scan_tool import load_env


_ENV = load_env()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(_ENV.get(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    raw = str(_ENV.get(name, "")).strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = str(_ENV.get(name, "")).strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class DaemonStrategyConfig:
    observation_lookback_days: int = _env_int("OBSERVATION_LOOKBACK_DAYS", 50)
    observation_min_score: float = _env_float("OBSERVATION_MIN_SCORE", 50)
    observation_surge_volume_ratio: float = _env_float("OBSERVATION_SURGE_VOLUME_RATIO", 1.8)
    observation_rally_lookback_days: int = _env_int("OBSERVATION_RALLY_LOOKBACK_DAYS", 20)
    observation_rally_min_return_pct: float = _env_float("OBSERVATION_RALLY_MIN_RETURN_PCT", 0.30)
    observation_rally_min_up_days: int = _env_int("OBSERVATION_RALLY_MIN_UP_DAYS", 3)
    observation_decline_min_pct: float = _env_float("OBSERVATION_DECLINE_MIN_PCT", 0.05)
    observation_low_price_buffer_pct: float = _env_float("OBSERVATION_LOW_PRICE_BUFFER_PCT", 0.05)
    observation_recent_volume_ratio: float = _env_float("OBSERVATION_RECENT_VOLUME_RATIO", 0.50)
    observation_extreme_volume_quantile: float = _env_float("OBSERVATION_EXTREME_VOLUME_QUANTILE", 0.15)
    observation_exhaustion_max_abs_return_pct: float = _env_float("OBSERVATION_EXHAUSTION_MAX_ABS_RETURN_PCT", 0.025)
    selection_filters_enabled: bool = _env_bool("SELECTION_FILTERS_ENABLED", True)
    selection_amount_top_n: int = _env_int("SELECTION_AMOUNT_TOP_N", 1200)
    confirmation_min_score: float = _env_float("CONFIRMATION_MIN_SCORE", 32)
    confirmation_surge_return_pct: float = _env_float("CONFIRMATION_SURGE_RETURN_PCT", 0.054)
    confirmation_breakout_volume_ratio: float = _env_float("CONFIRMATION_BREAKOUT_VOLUME_RATIO", 0.95)
    confirmation_max_breakout_volume_ratio: float = _env_float("CONFIRMATION_MAX_BREAKOUT_VOLUME_RATIO", 4.50)
    adaptive_exit_enabled: bool = _env_bool("ADAPTIVE_EXIT_ENABLED", True)
    adaptive_exit_stop_atr_mult: float = _env_float("ADAPTIVE_EXIT_STOP_ATR_MULT", 1.8)
    adaptive_exit_confirm_low_atr_buffer: float = _env_float("ADAPTIVE_EXIT_CONFIRM_LOW_ATR_BUFFER", 0.5)
    adaptive_exit_max_stop_loss_pct: float = _env_float("ADAPTIVE_EXIT_MAX_STOP_LOSS_PCT", 0.06)
    adaptive_exit_take_profit_r: float = _env_float("ADAPTIVE_EXIT_TAKE_PROFIT_R", 1.0)
    reversal_hold_days: int = _env_int("REVERSAL_HOLD_DAYS", 10)
    reversal_stop_loss_pct: float = _env_float("REVERSAL_STOP_LOSS_PCT", 0.04)
    reversal_trail_trigger_pct: float = _env_float("REVERSAL_TRAIL_TRIGGER_PCT", 0.08)
    reversal_trail_stop_pct: float = _env_float("REVERSAL_TRAIL_STOP_PCT", 0.05)
    trend_hold_days: int = _env_int("TREND_HOLD_DAYS", 8)
    trend_stop_loss_pct: float = _env_float("TREND_STOP_LOSS_PCT", 0.04)
    trend_trail_trigger_pct: float = _env_float("TREND_TRAIL_TRIGGER_PCT", 0.05)
    trend_trail_stop_pct: float = _env_float("TREND_TRAIL_STOP_PCT", 0.05)


def _code_to_symbol(code: str) -> str:
    return f"{code}.SS" if code.startswith(("600", "601", "603", "605", "688")) else f"{code}.SZ"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
        if math.isfinite(out):
            return out
    except (TypeError, ValueError):
        pass
    return default


def _load_name_map(root_dir: Path) -> dict[str, str]:
    path = root_dir / "data" / "universe_cache" / "a_share_code_list.csv"
    names: dict[str, str] = {}
    if not path.exists():
        return names
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            code = str(row.get("code") or row.get("代码") or "").strip().zfill(6)
            name = str(row.get("name") or row.get("名称") or "").strip()
            if code and name:
                names[code] = name
    return names


def _read_kline(path: Path, target_date: date) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype={"Date": str, "date": str})
    raw_date = frame.get("Date", frame.get("date"))
    if raw_date is None:
        return pd.DataFrame()
    frame.index = pd.to_datetime(raw_date, errors="coerce")
    frame = frame[frame.index.notna()]
    frame = frame[frame.index <= pd.Timestamp(target_date)]
    keep = [col for col in ("Open", "High", "Low", "Close", "Volume", "Amount", "Turnover") if col in frame.columns]
    if not {"Open", "High", "Low", "Close"}.issubset(set(keep)):
        return pd.DataFrame()
    out = frame[keep].copy()
    for col in keep:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    if "Amount" not in out.columns:
        out["Amount"] = 0.0
    if "Volume" not in out.columns:
        out["Volume"] = 0.0
    out = out.dropna(subset=["Open", "High", "Low", "Close"])
    out = out[(out["Open"] > 0) & (out["Close"] > 0) & (out["High"] >= out[["Open", "Close"]].max(axis=1))]
    out = out[out["Low"] <= out[["Open", "Close"]].min(axis=1)]
    return out.sort_index()


def _load_market_data(root_dir: Path, target_date: date) -> dict[str, pd.DataFrame]:
    cache_dir = root_dir / "data" / "kline_cache"
    market_data: dict[str, pd.DataFrame] = {}
    for path in sorted(cache_dir.glob("*.csv")):
        code = path.stem.zfill(6)
        frame = _read_kline(path, target_date)
        if not frame.empty and pd.Timestamp(frame.index[-1]).date() == target_date:
            market_data[_code_to_symbol(code)] = frame
    return market_data


def _avg_amount20(amount: pd.Series) -> float:
    values = pd.to_numeric(amount, errors="coerce").tail(20)
    if len(values) < 20:
        return 0.0
    return float(values.fillna(0.0).sum() / 20.0)


def _atr14(data: pd.DataFrame) -> float:
    high = pd.to_numeric(data["High"], errors="coerce")
    low = pd.to_numeric(data["Low"], errors="coerce")
    close = pd.to_numeric(data["Close"], errors="coerce")
    prev_close = close.shift(1)
    true_range = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr = true_range.rolling(14, min_periods=5).mean().iloc[-1]
    return float(atr) if pd.notna(atr) and float(atr) > 0 else 0.0


def _adaptive_exit_plan(cfg: DaemonStrategyConfig, data: pd.DataFrame, signal_row: pd.Series, entry_price: float) -> tuple[float, float, float]:
    atr = _atr14(data)
    if not cfg.adaptive_exit_enabled or atr <= 0 or entry_price <= 0:
        return entry_price * 1.04, entry_price * 0.98, atr
    signal_low = _safe_float(signal_row.get("Low"), entry_price)
    atr_stop = entry_price - cfg.adaptive_exit_stop_atr_mult * atr
    structure_stop = signal_low - cfg.adaptive_exit_confirm_low_atr_buffer * atr
    stop_loss = max(value for value in (atr_stop, structure_stop) if value > 0)
    if stop_loss >= entry_price:
        stop_loss = entry_price - max(atr, entry_price * 0.02)
    if cfg.adaptive_exit_max_stop_loss_pct > 0:
        stop_loss = max(stop_loss, entry_price * (1.0 - cfg.adaptive_exit_max_stop_loss_pct))
    risk = max(entry_price - stop_loss, entry_price * 0.005)
    return entry_price + cfg.adaptive_exit_take_profit_r * risk, stop_loss, atr


def _low_volume_candidate(cfg: DaemonStrategyConfig, symbol: str, frame: pd.DataFrame, amount_rank: int, name: str) -> Optional[dict[str, Any]]:
    if len(frame) < max(60, cfg.observation_lookback_days + 10) or "ST" in name.upper():
        return None
    window = frame.tail(cfg.observation_lookback_days).copy()
    latest_row = window.iloc[-1]
    current_price = _safe_float(latest_row.get("Close"))
    latest_return = float(window["Close"].pct_change().iloc[-1])
    anchor_pos = -2 if latest_return >= cfg.confirmation_surge_return_pct and len(window) >= 2 else -1
    anchor_row = window.iloc[anchor_pos]
    anchor_idx = pd.Timestamp(window.index[anchor_pos])
    anchor_price = _safe_float(anchor_row.get("Close"))
    pre_anchor = window.iloc[: anchor_pos if anchor_pos < 0 else anchor_pos + 1]
    if len(pre_anchor) < 20:
        return None
    high_idx = pd.Timestamp(pre_anchor["Close"].idxmax())
    high_pos = list(pre_anchor.index).index(high_idx)
    if high_pos < cfg.observation_rally_min_up_days:
        return None
    rally_start_pos = max(0, high_pos - cfg.observation_rally_lookback_days)
    rally_window = pre_anchor.iloc[rally_start_pos : high_pos + 1]
    if len(rally_window) < cfg.observation_rally_min_up_days + 1:
        return None
    low_idx = pd.Timestamp(rally_window["Close"].idxmin())
    low_pos = list(pre_anchor.index).index(low_idx)
    if low_pos >= high_pos:
        return None
    rally_leg = pre_anchor.iloc[low_pos : high_pos + 1]
    rally_low = float(rally_leg["Close"].min())
    rally_high = float(pre_anchor.loc[high_idx, "Close"])
    surge_return = rally_high / max(rally_low, 1e-9) - 1.0
    up_days = int((rally_leg["Close"].pct_change() > 0).sum())
    try:
        data_low_pos = int(frame.index.get_loc(low_idx))
    except Exception:
        data_low_pos = low_pos
    pre_volume = frame.iloc[max(0, data_low_pos - len(rally_leg)) : data_low_pos]["Volume"]
    if pre_volume.empty:
        pre_volume = pre_anchor.iloc[max(0, low_pos - len(rally_leg)) : low_pos]["Volume"]
    base_volume = float(pre_volume.mean()) if not pre_volume.empty else float(pre_anchor["Volume"].rolling(10, min_periods=3).mean().iloc[max(low_pos - 1, 0)])
    rally_volume = float(rally_leg["Volume"].mean())
    surge_volume_ratio = rally_volume / max(base_volume, 1e-9)
    if surge_return < cfg.observation_rally_min_return_pct or up_days < cfg.observation_rally_min_up_days or surge_volume_ratio < cfg.observation_surge_volume_ratio:
        return None
    pullback = pre_anchor.loc[pre_anchor.index >= high_idx]
    if len(pullback) < 4:
        return None
    lowest_price = float(pullback["Close"].min())
    price_low_buffer = anchor_price / max(lowest_price, 1e-9) - 1.0
    decline_from_surge = 1.0 - anchor_price / max(rally_high, 1e-9)
    recent_volume = float(pullback.loc[:anchor_idx, "Volume"].tail(5).mean())
    recent_volume_ratio = recent_volume / max(rally_volume, 1e-9)
    volume_quantile = float((pre_anchor["Volume"].astype(float) <= float(anchor_row["Volume"])).mean())
    recent_returns = pullback["Close"].pct_change().tail(3).dropna()
    exhaustion_abs_return = float(recent_returns.abs().mean()) if not recent_returns.empty else 1.0
    if (
        decline_from_surge < cfg.observation_decline_min_pct
        or price_low_buffer > cfg.observation_low_price_buffer_pct
        or recent_volume_ratio > cfg.observation_recent_volume_ratio
        or volume_quantile > cfg.observation_extreme_volume_quantile
        or exhaustion_abs_return > cfg.observation_exhaustion_max_abs_return_pct
    ):
        return None
    if cfg.selection_filters_enabled and amount_rank > cfg.selection_amount_top_n:
        return None
    surge_score = min(max((surge_return - cfg.observation_rally_min_return_pct) / 0.80, 0.0), 1.0)
    surge_vol_score = min(max((surge_volume_ratio - cfg.observation_surge_volume_ratio) / 4.0, 0.0), 1.0)
    decline_score = min(max((decline_from_surge - cfg.observation_decline_min_pct) / 0.45, 0.0), 1.0)
    low_price_score = min(max((cfg.observation_low_price_buffer_pct - price_low_buffer) / max(cfg.observation_low_price_buffer_pct, 1e-9), 0.0), 1.0)
    shrink_score = min(max((cfg.observation_recent_volume_ratio - recent_volume_ratio) / max(cfg.observation_recent_volume_ratio, 1e-9), 0.0), 1.0)
    ground_volume_score = min(max((cfg.observation_extreme_volume_quantile - volume_quantile) / max(cfg.observation_extreme_volume_quantile, 1e-9), 0.0), 1.0)
    exhaustion_score = min(max((cfg.observation_exhaustion_max_abs_return_pct - exhaustion_abs_return) / max(cfg.observation_exhaustion_max_abs_return_pct, 1e-9), 0.0), 1.0)
    score = max(0.0, min(94.0, 18 * surge_score + 12 * surge_vol_score + 16 * decline_score + 16 * low_price_score + 18 * shrink_score + 10 * ground_volume_score + 10 * exhaustion_score))
    prior_volume_avg = float(pre_anchor["Volume"].tail(5).mean())
    breakout_volume_ratio = float(latest_row["Volume"]) / max(prior_volume_avg, 1e-9)
    confirmed = (
        anchor_pos == -2
        and score >= cfg.confirmation_min_score
        and latest_return >= cfg.confirmation_surge_return_pct
        and float(latest_row["Close"]) > float(latest_row["Open"])
        and breakout_volume_ratio >= cfg.confirmation_breakout_volume_ratio
        and (cfg.confirmation_max_breakout_volume_ratio <= 0 or breakout_volume_ratio <= cfg.confirmation_max_breakout_volume_ratio)
    )
    if confirmed:
        score = min(98.0, score + 4.0)
    min_score = cfg.confirmation_min_score if confirmed else cfg.observation_min_score
    if score < min_score:
        return None
    take_profit, stop_loss, atr14 = _adaptive_exit_plan(cfg, frame, latest_row, current_price)
    return {
        "strategy_name": "地量地价观察",
        "signal_type": "confirmed" if confirmed else "watch",
        "symbol": symbol.split(".")[0],
        "name": name,
        "close_price": current_price,
        "high_price": _safe_float(latest_row.get("High")),
        "take_profit_price": round(take_profit, 4),
        "stop_loss_price": round(stop_loss, 4),
        "amount_rank": amount_rank,
        "score": round(score, 2),
        "surge_date": high_idx.date().isoformat(),
        "surge_return": surge_return,
        "decline_from_surge": decline_from_surge,
        "recent_volume_ratio": recent_volume_ratio,
        "volume_quantile": volume_quantile,
        "breakout_volume_ratio": breakout_volume_ratio,
        "atr14": atr14,
    }


def _best_reversal_candidate(cfg: DaemonStrategyConfig, symbol: str, frame: pd.DataFrame, amount_rank: int, name: str) -> Optional[dict[str, Any]]:
    if len(frame) < 80 or "ST" in name.upper():
        return None
    row = frame.iloc[-1]
    prev_close = _safe_float(frame["Close"].iloc[-2]) if len(frame) >= 2 else 0.0
    open_px, high_px, low_px, close_px = (_safe_float(row.get(col)) for col in ("Open", "High", "Low", "Close"))
    if min(open_px, high_px, low_px, close_px) <= 0 or close_px < 2.0:
        return None
    low60 = float(frame["Low"].tail(60).min())
    high60 = float(frame["High"].tail(60).max())
    if high60 <= low60:
        return None
    day_range = high_px - low_px
    lower_shadow = max(min(open_px, close_px) - low_px, 0.0)
    pos60 = (close_px - low60) / (high60 - low60)
    lower_range = lower_shadow / day_range if day_range > 0 else 0.0
    lower_pct = lower_shadow / close_px
    body_range = abs(close_px - open_px) / day_range if day_range > 0 else 1.0
    avg_amount20 = _avg_amount20(frame["Amount"])
    ret5 = close_px / float(frame["Close"].iloc[-6]) - 1.0 if len(frame) >= 6 and float(frame["Close"].iloc[-6]) > 0 else 0.0
    pct = close_px / prev_close - 1.0 if prev_close > 0 else 0.0
    if not (avg_amount20 >= 80_000_000 and pos60 <= 0.30 and lower_range >= 0.50 and lower_pct >= 0.02 and ret5 <= -0.05 and pct >= 0.02 and body_range <= 0.25):
        return None
    return {
        "strategy_name": "低位长下影 + 急跌反抽",
        "signal_type": "matched",
        "symbol": symbol.split(".")[0],
        "name": name,
        "close_price": close_px,
        "high_price": high_px,
        "take_profit_price": round(close_px * (1.0 + cfg.reversal_trail_trigger_pct), 4),
        "stop_loss_price": round(close_px * (1.0 - cfg.reversal_stop_loss_pct), 4),
        "amount_rank": amount_rank,
        "pct": pct,
        "ret5": ret5,
        "pos60": pos60,
        "lower_pct": lower_pct,
        "lower_range": lower_range,
        "body_range": body_range,
        "avg_amount20": avg_amount20,
        "hold_days": cfg.reversal_hold_days,
        "trail_stop_pct": cfg.reversal_trail_stop_pct,
    }


def _trend_continuation_candidate(cfg: DaemonStrategyConfig, symbol: str, frame: pd.DataFrame, amount_rank: int, name: str) -> Optional[dict[str, Any]]:
    if len(frame) < 90 or "ST" in name.upper():
        return None
    row = frame.iloc[-1]
    prev_close = _safe_float(frame["Close"].iloc[-2]) if len(frame) >= 2 else 0.0
    open_px, high_px, low_px, close_px = (_safe_float(row.get(col)) for col in ("Open", "High", "Low", "Close"))
    if min(open_px, high_px, low_px, close_px) <= 0 or close_px < 2.0:
        return None
    close, high, low, amount = frame["Close"], frame["High"], frame["Low"], frame["Amount"]
    ma5, ma10, ma20, ma60 = (float(close.tail(n).mean()) for n in (5, 10, 20, 60))
    ma20_prev5 = float(close.iloc[-25:-5].mean()) if len(close) >= 25 else 0.0
    low60, high60, high20 = float(low.tail(60).min()), float(high.tail(60).max()), float(high.tail(20).max())
    if min(ma5, ma10, ma20, ma60, ma20_prev5) <= 0 or high60 <= low60:
        return None
    ret3 = close_px / float(close.iloc[-4]) - 1.0 if len(close) >= 4 and float(close.iloc[-4]) > 0 else 0.0
    ret5 = close_px / float(close.iloc[-6]) - 1.0 if len(close) >= 6 and float(close.iloc[-6]) > 0 else 0.0
    ret20 = close_px / float(close.iloc[-21]) - 1.0 if len(close) >= 21 and float(close.iloc[-21]) > 0 else 0.0
    ret60 = close_px / float(close.iloc[-61]) - 1.0 if len(close) >= 61 and float(close.iloc[-61]) > 0 else 0.0
    pct = close_px / prev_close - 1.0 if prev_close > 0 else 0.0
    pos60 = (close_px - low60) / (high60 - low60)
    avg_amount20 = _avg_amount20(amount)
    amount_today = _safe_float(row.get("Amount"))
    amount_ratio20 = amount_today / avg_amount20 if avg_amount20 > 0 else 0.0
    recent = frame.tail(6).copy()
    recent_prev_close = recent["Close"].shift(1)
    recent_down_amount = recent.loc[recent["Close"] < recent_prev_close, "Amount"].tail(5)
    recent_up_amount = recent.loc[recent["Close"] > recent_prev_close, "Amount"].tail(5)
    down_vs_up_amount = float(recent_down_amount.mean()) / float(recent_up_amount.mean()) if not recent_down_amount.empty and not recent_up_amount.empty and float(recent_up_amount.mean()) > 0 else 0.0
    up_days10 = int((close.diff().tail(10) > 0).sum())
    ma20_slope = ma20 / ma20_prev5 - 1.0 if ma20_prev5 > 0 else 0.0
    ma20_gap = close_px / ma20 - 1.0 if ma20 > 0 else 0.0
    body_range = abs(close_px - open_px) / (high_px - low_px) if high_px > low_px else 1.0
    if not (avg_amount20 >= 80_000_000 and ma5 > ma10 > ma20 > ma60 and ma20_slope > 0 and ret20 >= 0.10 and ret60 >= 0.20 and pos60 >= 0.70 and -0.08 <= ret5 <= 0.02 and ret3 <= 0 and close_px <= high20 * 0.98 and 1.2 <= amount_ratio20 <= 3.0 and up_days10 >= 6 and low_px >= ma10 * 0.98 and close_px >= ma5 and body_range <= 0.60 and (down_vs_up_amount <= 1.0 or down_vs_up_amount == 0.0)):
        return None
    return {
        "strategy_name": "趋势上涨 + 回踩放量延续",
        "signal_type": "matched",
        "symbol": symbol.split(".")[0],
        "name": name,
        "close_price": close_px,
        "high_price": high_px,
        "take_profit_price": round(close_px * (1.0 + cfg.trend_trail_trigger_pct), 4),
        "stop_loss_price": round(close_px * (1.0 - cfg.trend_stop_loss_pct), 4),
        "amount_rank": amount_rank,
        "pct": pct,
        "ret3": ret3,
        "ret5": ret5,
        "ret20": ret20,
        "ret60": ret60,
        "pos60": pos60,
        "ma20_gap": ma20_gap,
        "body_range": body_range,
        "down_vs_up_amount": down_vs_up_amount,
        "amount_ratio20": amount_ratio20,
        "avg_amount20": avg_amount20,
        "up_days10": up_days10,
        "hold_days": cfg.trend_hold_days,
        "trail_stop_pct": cfg.trend_trail_stop_pct,
    }


def _amount_ranks(market_data: dict[str, pd.DataFrame]) -> dict[str, int]:
    rows = [(symbol, _safe_float(frame.iloc[-1].get("Amount"))) for symbol, frame in market_data.items() if not frame.empty]
    rows.sort(key=lambda item: item[1], reverse=True)
    return {symbol: index for index, (symbol, _amount) in enumerate(rows, start=1)}


def run_daemon_strategy_scan(root_dir: Path, signal_date: date) -> list[Path]:
    cfg = DaemonStrategyConfig()
    market_data = _load_market_data(root_dir, signal_date)
    if not market_data:
        return []
    names = _load_name_map(root_dir)
    ranks = _amount_ranks(market_data)
    rows: list[dict[str, Any]] = []
    for symbol, frame in market_data.items():
        code = symbol.split(".")[0]
        name = names.get(code, code)
        amount_rank = ranks.get(symbol, 0)
        for candidate in (
            _low_volume_candidate(cfg, symbol, frame, amount_rank, name),
            _best_reversal_candidate(cfg, symbol, frame, amount_rank, name),
            _trend_continuation_candidate(cfg, symbol, frame, amount_rank, name),
        ):
            if candidate is not None:
                rows.append(candidate)
    rows.sort(key=lambda item: (str(item["strategy_name"]), int(item.get("amount_rank") or 0), str(item["symbol"])))
    output_dir = root_dir / "data" / "pattern_scan_cache" / signal_date.isoformat()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "daemon_strategies_matched.csv"
    columns = [
        "symbol",
        "name",
        "strategy_name",
        "signal_type",
        "close_price",
        "high_price",
        "breakout_price",
        "take_profit_price",
        "stop_loss_price",
        "amount_rank",
        "payload_json",
    ]
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            payload = {key: value for key, value in row.items() if key not in columns}
            writer.writerow(
                {
                    "symbol": row.get("symbol"),
                    "name": row.get("name"),
                    "strategy_name": row.get("strategy_name"),
                    "signal_type": row.get("signal_type"),
                    "close_price": row.get("close_price"),
                    "high_price": row.get("high_price"),
                    "breakout_price": row.get("breakout_price", ""),
                    "take_profit_price": row.get("take_profit_price"),
                    "stop_loss_price": row.get("stop_loss_price"),
                    "amount_rank": row.get("amount_rank"),
                    "payload_json": json.dumps(payload, ensure_ascii=False),
                }
            )
    return [output_path]
