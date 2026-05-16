from __future__ import annotations

import argparse
import contextlib
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
import time
import re
import socket
import subprocess
import threading
from typing import Dict, List, Optional, Tuple, Any


def load_env(file_path: str = ".env") -> Dict[str, str]:
    """Manually parse scanner config first, then allow the project .env to override it."""
    env_vars = {}
    candidate_paths = []
    if file_path == ".env":
        candidate_paths.append(os.path.join(os.path.dirname(__file__), "scanner.env"))
    candidate_paths.append(file_path)

    for path in candidate_paths:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        key, value = line.split("=", 1)
                        env_vars[key.strip()] = value.strip()
        except Exception:
            pass
    return env_vars


_ENV = load_env()

import numpy as np
import pandas as pd
import requests

try:
    from strategies.ultra_short_hot import build_ultra_short_hot_template, match_ultra_short_hot_breakout
except ModuleNotFoundError:
    from .strategies.ultra_short_hot import build_ultra_short_hot_template, match_ultra_short_hot_breakout
import requests.sessions

try:
    import akshare as ak
except ImportError:
    ak = None


CST = timezone(timedelta(hours=8))

_A_SHARE_MAIN_CHINEXT_CODE_RE = re.compile(r"^(000|001|002|003|300|600|601|603|605)\d{3}$")
_CHINEXT_CODE_RE = re.compile(r"^300\d{3}$")
_SH_MAIN_CODE_RE = re.compile(r"^(600|601|603|605)\d{3}$")
_SZ_MAIN_CODE_RE = re.compile(r"^(000|001|002|003)\d{3}$")

MAIN_BOARD_ONLY = "main_board"
CHINEXT_ONLY = "chinext"
SH_MAIN_ONLY = "sh_main"
SZ_MAIN_ONLY = "sz_main"

BACKTEST_PERIOD_FOLDERS = {
    "长期": "长期",
    "中期": "中期",
    "短期": "短期",
    "超短期热门": "超短期热门",
}
ULTRA_SHORT_PATTERN_NAME = "超短期热门"
ULTRA_SHORT_FILE_STEM = "ultra_short_hot"


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
class PatternTemplate:
    name: str
    window_days: int
    b_window_days: int
    recent_low_window_days: int
    max_ab_gap_ratio: float
    low_ratio_threshold: float
    local_extrema_neighbor_days: int
    min_ac_amplitude_ratio: float = 0.0
    min_bd_amplitude_ratio: float = 0.0
    min_b_peak_prominence_ratio: float = 0.0
    post_d_peak_neighbor_days: int = 0
    min_breakout_over_d_ratio: float = 0.0
    amount_rank_min: int = 0
    amount_rank_max: int = 0
    # 窗口区间扫描：window_days_max > 0 时，在 [window_days, window_days_max] 遍历
    window_days_max: int = 0


def _default_pattern_templates() -> List[PatternTemplate]:
    return [
        PatternTemplate(
            name="长期",
            window_days=_env_int("LONG_WINDOW_DAYS", 140),
            b_window_days=_env_int("LONG_B_WINDOW_DAYS", 95),
            recent_low_window_days=_env_int("LONG_RECENT_LOW_WINDOW_DAYS", 70),
            max_ab_gap_ratio=_env_float("LONG_MAX_AB_GAP_RATIO", 0.12),
            low_ratio_threshold=_env_float("LONG_LOW_RATIO_THRESHOLD", 0.05),
            local_extrema_neighbor_days=_env_int("LONG_LOCAL_EXTREMA_NEIGHBOR_DAYS", 5),
        ),
        PatternTemplate(
            name="中期",
            window_days=_env_int("MID_WINDOW_DAYS", 100),
            b_window_days=_env_int("MID_B_WINDOW_DAYS", 68),
            recent_low_window_days=_env_int("MID_RECENT_LOW_WINDOW_DAYS", 50),
            max_ab_gap_ratio=_env_float("MID_MAX_AB_GAP_RATIO", 0.09),
            low_ratio_threshold=_env_float("MID_LOW_RATIO_THRESHOLD", 0.04),
            local_extrema_neighbor_days=_env_int("MID_LOCAL_EXTREMA_NEIGHBOR_DAYS", 3),
        ),
        PatternTemplate(
            name="短期",
            window_days=_env_int("SHORT_WINDOW_DAYS", 70),
            b_window_days=_env_int("SHORT_B_WINDOW_DAYS", 48),
            recent_low_window_days=_env_int("SHORT_RECENT_LOW_WINDOW_DAYS", 35),
            max_ab_gap_ratio=_env_float("SHORT_MAX_AB_GAP_RATIO", 0.06),
            low_ratio_threshold=_env_float("SHORT_LOW_RATIO_THRESHOLD", 0.035),
            local_extrema_neighbor_days=_env_int("SHORT_LOCAL_EXTREMA_NEIGHBOR_DAYS", 3),
        ),
        build_ultra_short_hot_template(
            pattern_template_cls=PatternTemplate,
            env_int=_env_int,
            env_float=_env_float,
        ),
    ]


def _parse_enabled_pattern_names(raw: str | None) -> List[str]:
    if raw is None:
        return []
    text = str(raw).strip()
    if not text:
        return []
    return [item.strip() for item in text.split(",") if item.strip()]


def _filter_templates_by_name(templates: List[PatternTemplate], enabled_names: List[str]) -> List[PatternTemplate]:
    if not enabled_names:
        return list(templates)
    enabled_set = {item.strip() for item in enabled_names if str(item).strip()}
    return [item for item in templates if item.name in enabled_set]


def _max_amount_rank_required(templates: List[PatternTemplate]) -> int:
    return max((item.amount_rank_max for item in templates if item.amount_rank_max > 0), default=0)


@dataclass
class PatternScanConfig:
    templates: List[PatternTemplate] = field(default_factory=_default_pattern_templates)
    prebreakout_gap_ratio: float = float(_ENV.get("PREBREAKOUT_GAP_RATIO", 0.02))
    watchlist_min_d_age_ratio: float = float(_ENV.get("WATCHLIST_MIN_D_AGE_RATIO", 0.08))
    watchlist_min_rebound_position_ratio: float = float(_ENV.get("WATCHLIST_MIN_REBOUND_POSITION_RATIO", 0.20))
    watchlist_max_rebound_position_ratio: float = float(_ENV.get("WATCHLIST_MAX_REBOUND_POSITION_RATIO", 0.80))
    pullback_confirm_lookback_days: int = int(_ENV.get("PULLBACK_CONFIRM_LOOKBACK_DAYS", 10))
    pullback_candidate_lookback_days: int = int(_ENV.get("PULLBACK_CANDIDATE_LOOKBACK_DAYS", 10))
    history_lookback_days: int = int(_ENV.get("HISTORY_LOOKBACK_DAYS", 365))
    cache_dir: str = _ENV.get("CACHE_DIR", os.path.join("data", "pattern_scan_cache"))
    max_workers: int = int(_ENV.get("MAX_WORKERS", 4))
    progress_every: int = 200

    @property
    def max_window_days(self) -> int:
        return max((max(item.window_days, item.window_days_max) for item in self.templates), default=0)

    @property
    def min_window_days(self) -> int:
        return min((item.window_days for item in self.templates), default=0)

    @property
    def template_summary_text(self) -> str:
        return self._template_summary_text(include_entry_filters=False)

    @property
    def backtest_template_summary_text(self) -> str:
        return self._template_summary_text(include_entry_filters=True)

    def _template_summary_text(self, include_entry_filters: bool = False) -> str:
        parts = []
        for item in self.templates:
            window_text = (
                f"{item.window_days}-{item.window_days_max}"
                if item.window_days_max > item.window_days
                else str(item.window_days)
            )
            cd_text = "D>C" if item.low_ratio_threshold <= 0 else f"C-D<={item.low_ratio_threshold:.1%}"
            parts.append(
                f"{item.name}(A={window_text}/B={item.b_window_days}/D={item.recent_low_window_days}/A-B<={item.max_ab_gap_ratio:.0%}/{cd_text}/局部峰谷={item.local_extrema_neighbor_days})"
            )
        return "；".join(parts)


@dataclass
class BacktestConfig:
    start_date: str
    end_date: str
    board_filter: str | None = None
    history_lookback_days: int = int(_ENV.get("HISTORY_LOOKBACK_DAYS", 365))
    max_workers: int = int(_ENV.get("MAX_WORKERS", 4))
    output_dir: str = _ENV.get("BACKTEST_OUTPUT_DIR", os.path.join("data", "backtest_reports"))
    enabled_patterns: List[str] = field(default_factory=list)
    
    # 交易参数
    entry_premium_threshold: float = float(_ENV.get("BACKTEST_ENTRY_PREMIUM_THRESHOLD", 0.05))
    stop_loss_ratio: float = float(_ENV.get("BACKTEST_STOP_LOSS_RATIO", 0.07))
    max_position_pct: float = 0.20
    
    # 资金管理 (支持从 .env 读取)
    initial_capital: float = float(_ENV.get("INITIAL_CAPITAL", 2_000_000.0))  # 初始总资金
    max_buy_pct: float = float(_ENV.get("MAX_BUY_PCT", 0.05))               # 单次买入最大仓位占比 (如 0.05 表示 5%)
    entry_gap_limit: float = 0.05


@dataclass
class Position:
    code: str
    name: str
    shares: int
    entry_date: str
    entry_price: float
    breakout_price: float
    take_profit: float
    stop_loss: float


# --- CSV 列名中外文映射 ---
CSV_COLUMN_MAPPING = {
    "code": "代码",
    "name": "名称",
    "pattern_name": "周期",
    "amount": "成交额",
    "amount_rank": "成交额名次",
    "signal_type": "信号类型",
    "point_a_label": "A点",
    "point_b_label": "B点",
    "point_c_label": "C点",
    "point_d_label": "D点",
    "signal_date": "信号日",
    "entry_date": "入场日",
    "signal_close": "信号收盘价",
    "entry_open": "入场开盘价",
    "entry_gap": "入场涨跌幅",
    "breakout_price": "突破价",
    "take_profit": "止盈价",
    "stop_loss": "止损价",
    "max_entry": "最大买入价",
    "entry_valid": "入场有效",
    "profit_target_valid": "价格目标有效",
    "buy_executed": "是否买入",
    "skip_reason": "跳过原因",
    "buy_amount": "买入金额",
    "shares": "买入股数",
    "cash_before_buy": "买入前现金",
    "cash_after_buy": "买入后现金",
    "total_asset_before_buy": "买入前总资产",
    "exit_type": "出场类型",
    "exit_date": "出场日期",
    "exit_price": "出场价格",
    "exit_return": "收益率",
    "holding_days": "持股天数",
    "realized_pnl": "已实现盈亏",
    "quality_exit_type": "独立信号出场类型",
    "quality_exit_date": "独立信号出场日期",
    "quality_exit_price": "独立信号出场价格",
    "quality_exit_return": "独立信号收益率",
    "quality_holding_days": "独立信号持股天数",
    "signal_close": "信号收盘价",
    "signal_high": "信号最高价",
    "close_gap_to_breakout": "距突破价差额",
    "close_gap_ratio": "距突破价差幅",
    "intraday_touch_breakout": "盘中触线",
    "pullback_confirmed": "回踩确认",
    "pullback_date": "回踩确认日",
    "pullback_low": "回踩最低价",
    "pullback_close": "回踩收盘价",
}

def _format_amount_brief(amount: Any) -> str:
    numeric = pd.to_numeric(amount, errors="coerce")
    if pd.isna(numeric):
        return ""
    value = float(numeric)
    if value >= 1e8:
        return f"{value / 1e8:.2f}亿"
    if value >= 1e4:
        return f"{value / 1e4:.2f}万"
    if value.is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")

@dataclass
class PatternDetail:
    """形态关键点价格及交易参数"""
    pattern_name: str
    point_a_date: str
    point_b_date: str
    point_c_date: str
    point_d_date: str
    point_a_price: float
    point_b_price: float
    point_c_price: float
    point_d_price: float
    breakout_price: float
    e_close: float
    e_high: float
    signal_type: str = "突破"
    pullback_confirmed: bool = False
    pullback_date: str = ""
    pullback_low: Optional[float] = None
    pullback_close: Optional[float] = None

    @property
    def take_profit_price(self) -> float:
        """止盈价 = 突破价 + B点价格 - (C点+D点)/2"""
        return self.breakout_price + self.point_b_price - (self.point_c_price + self.point_d_price) / 2.0

    def stop_loss_price(self, ratio: float = 0.06) -> float:
        """止损价 = 突破价 × (1 - ratio)"""
        return self.breakout_price * (1.0 - ratio)

    def max_entry_price(self, premium: float = 0.05) -> float:
        """最大买入价 = 突破价 × (1 + premium)"""
        return self.breakout_price * (1.0 + premium)

    @property
    def close_gap_to_breakout(self) -> float:
        return self.breakout_price - self.e_close

    @property
    def close_gap_ratio(self) -> float:
        if self.breakout_price <= 0:
            return np.inf
        return self.close_gap_to_breakout / self.breakout_price

    @property
    def intraday_touch_breakout(self) -> bool:
        return self.e_high >= self.breakout_price

    @property
    def ab_gap_ratio(self) -> float:
        if self.point_b_price <= 0:
            return np.inf
        return self.point_a_price / self.point_b_price - 1.0

    @staticmethod
    def _point_label(point_date: str, point_price: float) -> str:
        date_text = str(point_date or "").strip()
        if not date_text:
            return ""
        return f"{date_text}({point_price:.2f})"

    @property
    def point_a_label(self) -> str:
        return self._point_label(self.point_a_date, self.point_a_price)

    @property
    def point_b_label(self) -> str:
        return self._point_label(self.point_b_date, self.point_b_price)

    @property
    def point_c_label(self) -> str:
        return self._point_label(self.point_c_date, self.point_c_price)

    @property
    def point_d_label(self) -> str:
        return self._point_label(self.point_d_date, self.point_d_price)


@dataclass
class PatternScanOutcome:
    matched: Optional[PatternDetail]
    watch: Optional[PatternDetail]



def today_str(dt: Optional[datetime] = None) -> str:
    current = datetime.now(tz=CST) if dt is None else dt.astimezone(CST)
    return current.date().isoformat()


def bool_to_cn(value: Any) -> str:
    return "是" if bool(value) else "否"


def env_bool(name: str, default: bool = False) -> bool:
    raw = str(_ENV.get(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def safe_float(value: Any, default: float = 0.0) -> float:
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric):
        return default
    return float(numeric)


def normalize_symbol(raw_symbol: str) -> str:
    symbol = str(raw_symbol or "").strip().upper()
    if not symbol:
        return symbol
    if re.fullmatch(r"(SH|SSE)\d{6}", symbol):
        return f"{symbol[-6:]}.SS"
    if re.fullmatch(r"(SZ|SZE)\d{6}", symbol):
        return f"{symbol[-6:]}.SZ"
    matched = re.fullmatch(r"(\d{6})\.(SH|XSHG)", symbol)
    if matched:
        return f"{matched.group(1)}.SS"
    matched = re.fullmatch(r"(\d{6})\.(SZ|XSHE)", symbol)
    if matched:
        return f"{matched.group(1)}.SZ"
    return symbol


def _is_cn_trading_session(dt: Optional[datetime] = None) -> bool:
    current = datetime.now(tz=CST) if dt is None else dt.astimezone(CST)
    if current.weekday() >= 5:
        return False
    current_time = current.time()
    return (
        current_time >= datetime.strptime("09:25", "%H:%M").time()
        and current_time <= datetime.strptime("11:30", "%H:%M").time()
    ) or (
        current_time >= datetime.strptime("13:00", "%H:%M").time()
        and current_time <= datetime.strptime("15:05", "%H:%M").time()
    )


class MarketData:
    _eastmoney_good_ip_cache: Dict[str, str] = {}
    _eastmoney_notice_cache: set[str] = set()
    _kline_cache_dir: str = os.path.join("data", "kline_cache")
    _eastmoney_dns_lock = threading.Lock()

    @staticmethod
    def _kline_cache_path(symbol: str) -> str:
        code = normalize_symbol(symbol).split(".")[0]
        return os.path.join(MarketData._kline_cache_dir, f"{code}.csv")

    @staticmethod
    def _read_kline_cache(symbol: str) -> pd.DataFrame:
        path = MarketData._kline_cache_path(symbol)
        if not os.path.exists(path):
            return pd.DataFrame()
        try:
            frame = pd.read_csv(path, index_col=0, parse_dates=True)
            for col in ["Open", "High", "Low", "Close", "Volume", "Amount", "Turnover"]:
                if col in frame.columns:
                    frame[col] = pd.to_numeric(frame[col], errors="coerce")
            frame.index.name = "Date"
            return frame.sort_index()
        except Exception:
            return pd.DataFrame()

    @staticmethod
    def _save_kline_cache(symbol: str, frame: pd.DataFrame) -> None:
        if frame is None or frame.empty:
            return
        path = MarketData._kline_cache_path(symbol)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        frame.sort_index().to_csv(path)

    @staticmethod
    def _merge_kline_frames(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
        if existing.empty:
            return new
        if new.empty:
            return existing
        combined = pd.concat([existing, new])
        combined = combined[~combined.index.duplicated(keep="last")]
        return combined.sort_index()

    @staticmethod
    def append_realtime_daily_bars(
        market_data: Dict[str, pd.DataFrame],
        quotes: Dict[str, dict],
        *,
        trade_date: datetime | pd.Timestamp | str | None = None,
    ) -> Dict[str, pd.DataFrame]:
        if not market_data or not quotes:
            return market_data or {}

        bar_date = pd.Timestamp(trade_date or datetime.now(tz=CST).date()).tz_localize(None).normalize()
        out: Dict[str, pd.DataFrame] = {}
        for symbol, frame in market_data.items():
            quote = quotes.get(str(symbol)) or {}
            price = safe_float(quote.get("price"))
            if frame is None or frame.empty or price <= 0:
                out[symbol] = frame
                continue

            open_px = safe_float(quote.get("open"), price)
            high_px = max(safe_float(quote.get("high"), price), open_px, price)
            low_candidates = [safe_float(quote.get("low"), price), open_px, price]
            low_px = min(value for value in low_candidates if value > 0)
            volume = safe_float(quote.get("volume"), 0.0)
            amount = safe_float(quote.get("amount"), 0.0)
            turnover = safe_float(quote.get("turnover"), np.nan)
            today_bar = pd.DataFrame(
                {
                    "Open": [open_px],
                    "High": [high_px],
                    "Low": [low_px],
                    "Close": [price],
                    "Volume": [volume],
                    "Amount": [amount if amount > 0 else price * max(volume, 0.0)],
                    "Turnover": [turnover],
                },
                index=pd.DatetimeIndex([bar_date], name=getattr(frame.index, "name", None)),
            )
            merged = pd.concat([frame.copy(), today_bar])
            merged = merged[~merged.index.duplicated(keep="last")]
            out[symbol] = merged.sort_index()
        return out

    @staticmethod
    def realtime_quotes(symbols: List[str], source: str | None = None) -> Dict[str, dict]:
        source_name = (source or str(_ENV.get("QUOTE_DATA_SOURCE", "auto")) or "auto").strip().lower()
        symbols_by_code: Dict[str, str] = {}
        for item in symbols:
            normalized = normalize_symbol(item)
            matched = re.fullmatch(r"(\d{6})\.(SS|SZ)", normalized)
            if matched:
                symbols_by_code[matched.group(1)] = normalized
        if not symbols_by_code:
            return {}

        if source_name in {"akshare", "eastmoney"}:
            return MarketData._realtime_quotes_akshare(symbols_by_code)
        if source_name == "sina":
            return MarketData._realtime_quotes_sina(symbols_by_code)
        if source_name != "auto":
            print(f"[数据] 未知实时行情源 {source_name}，将回退 auto(akshare->sina)。")

        primary = MarketData._realtime_quotes_akshare(symbols_by_code)
        missing_codes = [code for code, symbol in symbols_by_code.items() if symbol not in primary]
        if not missing_codes:
            return primary
        secondary = MarketData._realtime_quotes_sina({code: symbols_by_code[code] for code in missing_codes})
        merged = dict(primary)
        merged.update(secondary)
        if secondary:
            print(f"[数据] 实时行情已回退新浪补齐 {len(secondary)} 只，总返回 {len(merged)}/{len(symbols_by_code)}")
        return merged

    @staticmethod
    def _realtime_quotes_akshare(symbols_by_code: Dict[str, str]) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        if ak is None or not hasattr(ak, "stock_zh_a_spot_em"):
            print("[数据] 实时行情不可用：akshare 未安装或版本不支持 stock_zh_a_spot_em。")
            return out
        try:
            with MarketData.without_proxy_env():
                spot = ak.stock_zh_a_spot_em()
        except Exception as exc:
            print(f"[数据] 获取实时行情失败(akshare): {exc}；将回退备用行情源。")
            return out
        if spot is None or spot.empty or "代码" not in spot.columns:
            return out

        matched_rows = spot[spot["代码"].astype(str).str.zfill(6).isin(symbols_by_code.keys())]
        timestamp = datetime.now(tz=timezone.utc).isoformat()
        for row in matched_rows.to_dict(orient="records"):
            code = str(row.get("代码", "")).strip().zfill(6)
            symbol = symbols_by_code.get(code)
            price = safe_float(row.get("最新价"))
            if not symbol or price <= 0:
                continue
            out[symbol] = {
                "price": price,
                "open": safe_float(row.get("今开"), price),
                "high": safe_float(row.get("最高"), price),
                "low": safe_float(row.get("最低"), price),
                "prev_close": safe_float(row.get("昨收"), price),
                "volume": safe_float(row.get("成交量"), 0.0),
                "amount": safe_float(row.get("成交额"), 0.0),
                "turnover": safe_float(row.get("换手率"), 0.0),
                "source": "akshare",
                "ts": timestamp,
            }
        return out

    @staticmethod
    def _realtime_quotes_sina(symbols_by_code: Dict[str, str]) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        if not symbols_by_code:
            return out

        def to_sina_symbol(normalized_symbol: str) -> str:
            matched = re.fullmatch(r"(\d{6})\.(SS|SZ)", normalized_symbol)
            if not matched:
                return ""
            code, suffix = matched.group(1), matched.group(2)
            return f"sh{code}" if suffix == "SS" else f"sz{code}"

        sina_symbols: List[str] = []
        code_by_sina_symbol: Dict[str, str] = {}
        for code, normalized_symbol in symbols_by_code.items():
            sina_symbol = to_sina_symbol(normalized_symbol)
            if sina_symbol:
                sina_symbols.append(sina_symbol)
                code_by_sina_symbol[sina_symbol] = code
        if not sina_symbols:
            return out

        timestamp = datetime.now(tz=timezone.utc).isoformat()
        pattern = re.compile(r'var hq_str_(?P<sid>[a-z]{2}\d{6})="(?P<body>[^"]*)";')
        chunk_size = max(1, _env_int("QUOTE_SINA_CHUNK_SIZE", 100))
        max_retries = max(1, _env_int("QUOTE_RETRY_TIMES", 3))
        failed_chunks = 0
        for start in range(0, len(sina_symbols), chunk_size):
            chunk = sina_symbols[start : start + chunk_size]
            text = ""
            last_error = ""
            for attempt in range(1, max_retries + 1):
                try:
                    with MarketData.without_proxy_env():
                        resp = requests.get(
                            "https://hq.sinajs.cn/list=" + ",".join(chunk),
                            timeout=8,
                            headers={"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"},
                        )
                    if resp.status_code >= 400:
                        last_error = f"status={resp.status_code}"
                    else:
                        text = resp.content.decode("gbk", errors="ignore")
                        if text.strip():
                            break
                        last_error = "empty response"
                except Exception as exc:
                    last_error = str(exc)
                if attempt < max_retries:
                    time.sleep(0.3 * attempt)
            if not text.strip():
                failed_chunks += 1
                if failed_chunks <= 3:
                    print(
                        f"[数据] 新浪实时行情分块请求失败 "
                        f"chunk={start // chunk_size + 1} size={len(chunk)} error={last_error}"
                    )
                continue
            before_count = len(out)
            for matched in pattern.finditer(text):
                sina_symbol = matched.group("sid")
                fields = matched.group("body").split(",")
                if len(fields) < 10:
                    continue
                code = code_by_sina_symbol.get(sina_symbol)
                symbol = symbols_by_code.get(code or "")
                price = safe_float(fields[3])
                if not symbol or price <= 0:
                    continue
                out[symbol] = {
                    "price": price,
                    "open": safe_float(fields[1], price),
                    "high": safe_float(fields[4], price),
                    "low": safe_float(fields[5], price),
                    "prev_close": safe_float(fields[2], price),
                    "volume": safe_float(fields[8], 0.0),
                    "amount": safe_float(fields[9], 0.0),
                    "turnover": 0.0,
                    "source": "sina",
                    "ts": timestamp,
                }
            if len(out) == before_count and failed_chunks <= 3:
                print(f"[数据] 新浪实时行情分块未解析到有效价格 chunk={start // chunk_size + 1} size={len(chunk)}")
        if failed_chunks > 0:
            total_chunks = (len(sina_symbols) + chunk_size - 1) // chunk_size
            print(f"[数据] 新浪实时行情失败分块 {failed_chunks}/{total_chunks}，已返回 {len(out)}/{len(symbols_by_code)} 只")
        return out

    @staticmethod
    @contextlib.contextmanager
    def without_proxy_env():
        proxy_keys = [
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ]
        saved = {k: os.environ.get(k) for k in proxy_keys}
        original_session_init = requests.sessions.Session.__init__
        original_getaddrinfo = socket.getaddrinfo

        def session_init_without_env_proxy(session, *args, **kwargs):
            original_session_init(session, *args, **kwargs)
            session.trust_env = False
            session.proxies.clear()

        def getaddrinfo_with_eastmoney_fallback(host, port, family=0, type=0, proto=0, flags=0):
            fallback_ip = None
            host_text = str(host or "").strip().lower()
            try:
                result = original_getaddrinfo(host, port, family, type, proto, flags)
                if not MarketData._should_override_eastmoney_dns(host_text, result):
                    return result
                fallback_ip = MarketData._resolve_host_via_public_dns(host_text)
            except socket.gaierror:
                if host_text.endswith("eastmoney.com"):
                    fallback_ip = MarketData._resolve_host_via_public_dns(host_text)
                else:
                    raise

            if fallback_ip:
                notice_key = f"dns:{host_text}:{fallback_ip}"
                if notice_key not in MarketData._eastmoney_notice_cache:
                    print(f"[网络] {host_text} 命中异常 DNS，已回退到公共解析 {fallback_ip}")
                    MarketData._eastmoney_notice_cache.add(notice_key)
                return original_getaddrinfo(fallback_ip, port, family, type, proto, flags)
            return original_getaddrinfo(host, port, family, type, proto, flags)

        try:
            for key in proxy_keys:
                os.environ.pop(key, None)
            requests.sessions.Session.__init__ = session_init_without_env_proxy
            socket.getaddrinfo = getaddrinfo_with_eastmoney_fallback
            yield
        finally:
            requests.sessions.Session.__init__ = original_session_init
            socket.getaddrinfo = original_getaddrinfo
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    @staticmethod
    def _should_override_eastmoney_dns(host: str, getaddrinfo_result: list) -> bool:
        if not host.endswith("eastmoney.com"):
            return False
        addresses = []
        for item in getaddrinfo_result:
            sockaddr = item[4] if len(item) > 4 else ()
            ip_text = sockaddr[0] if sockaddr else None
            if ip_text:
                addresses.append(ip_text)
        if not addresses:
            return True
        return all(MarketData._is_suspicious_eastmoney_ip(ip_text) for ip_text in addresses)

    @staticmethod
    def _is_suspicious_eastmoney_ip(ip_text: str) -> bool:
        try:
            ip_obj = ipaddress.ip_address(ip_text)
        except ValueError:
            return True
        reserved_test_net = ipaddress.ip_network("198.18.0.0/15")
        return (
            ip_obj in reserved_test_net
            or ip_obj.is_loopback
            or ip_obj.is_unspecified
            or ip_obj.is_multicast
            or ip_obj.is_reserved
            or ip_obj.is_link_local
        )

    @staticmethod
    def _resolve_host_via_public_dns(host: str) -> Optional[str]:
        cached = MarketData._eastmoney_good_ip_cache.get(host)
        if cached and not MarketData._is_suspicious_eastmoney_ip(cached):
            return cached

        preferred_ip = MarketData._preferred_eastmoney_ip(host)
        if preferred_ip and not MarketData._is_suspicious_eastmoney_ip(preferred_ip):
            MarketData._eastmoney_good_ip_cache[host] = preferred_ip
            return preferred_ip

        with MarketData._eastmoney_dns_lock:
            cached = MarketData._eastmoney_good_ip_cache.get(host)
            if cached and not MarketData._is_suspicious_eastmoney_ip(cached):
                return cached

            preferred_ip = MarketData._preferred_eastmoney_ip(host)
            if preferred_ip and not MarketData._is_suspicious_eastmoney_ip(preferred_ip):
                MarketData._eastmoney_good_ip_cache[host] = preferred_ip
                return preferred_ip

            candidates = MarketData._resolve_host_candidates_via_public_dns(host)
            if not candidates:
                return None

            for candidate in candidates:
                if MarketData._probe_eastmoney_ip(host, candidate):
                    MarketData._eastmoney_good_ip_cache[host] = candidate
                    return candidate

            fallback = candidates[0]
            MarketData._eastmoney_good_ip_cache[host] = fallback
            return fallback

    @staticmethod
    def _preferred_eastmoney_ip(host: str) -> Optional[str]:
        host = str(host or "").strip().lower()
        if re.fullmatch(r"\d+\.push2\.eastmoney\.com", host):
            return "14.103.191.91"
        if host == "push2.eastmoney.com":
            return "14.103.191.91"
        if host == "push2his.eastmoney.com":
            return "117.184.38.143"
        return None

    @staticmethod
    def _resolve_host_candidates_via_public_dns(host: str) -> List[str]:
        resolver_candidates = ["1.1.1.1", "8.8.8.8"]
        resolved: List[str] = []
        preferred_ip = MarketData._preferred_eastmoney_ip(host)
        for candidate in [preferred_ip] if preferred_ip else []:
            if candidate not in resolved and not MarketData._is_suspicious_eastmoney_ip(candidate):
                resolved.append(candidate)

        for resolver in resolver_candidates:
            try:
                completed = subprocess.run(
                    ["dig", "+short", f"@{resolver}", host],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=2,
                )
            except Exception:
                continue
            if completed.returncode != 0:
                continue
            for line in completed.stdout.splitlines():
                candidate = line.strip().rstrip(".")
                try:
                    ip_obj = ipaddress.ip_address(candidate)
                except ValueError:
                    continue
                if ip_obj.version != 4 or MarketData._is_suspicious_eastmoney_ip(candidate):
                    continue
                if candidate not in resolved:
                    resolved.append(candidate)
        return resolved

    @staticmethod
    def _probe_eastmoney_ip(host: str, ip_text: str) -> bool:
        if MarketData._is_suspicious_eastmoney_ip(ip_text):
            return False

        if ".push2.eastmoney.com" in host or host.endswith("push2.eastmoney.com"):
            url = (
                f"https://{host}/api/qt/clist/get"
                "?pn=1&pz=1&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281"
                "&fltt=2&invt=2&fid=f12&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
                "&fields=f2,f6,f8,f12,f14,f20"
            )
        else:
            url = f"https://{host}/"

        try:
            completed = subprocess.run(
                [
                    "curl",
                    "-sS",
                    "--noproxy",
                    "*",
                    "--max-time",
                    "4",
                    "--resolve",
                    f"{host}:443:{ip_text}",
                    url,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except Exception:
            return False

        if completed.returncode != 0:
            return False
        body = completed.stdout.strip()
        if not body:
            return False
        if ".push2.eastmoney.com" in host or host.endswith("push2.eastmoney.com"):
            return body.startswith("{") and '"data"' in body
        return True

    @staticmethod
    def from_akshare_with_reason(
        symbol: str,
        min_rows: int = 30,
        end_date: datetime | pd.Timestamp | str | None = None,
        lookback_days: int = 365,
        force_latest: bool = True,
    ) -> tuple[pd.DataFrame, str]:
        normalized = normalize_symbol(symbol)
        matched = re.fullmatch(r"(\d{6})(?:\.(SS|SZ))?", normalized)
        if not matched:
            return pd.DataFrame(), f"invalid A-share symbol: {symbol}"

        code = matched.group(1)
        end = pd.Timestamp(end_date).to_pydatetime() if end_date is not None else datetime.now()
        lookback_days = max(int(lookback_days), min_rows, 30)
        start = end - pd.Timedelta(days=lookback_days)

        # --- check local kline cache ---
        cached = MarketData._read_kline_cache(symbol)
        if not cached.empty:
            cols = [c for c in ["Open", "High", "Low", "Close", "Volume", "Amount", "Turnover"] if c in cached.columns]
            filtered = cached.loc[
                (cached.index >= pd.Timestamp(start)) & (cached.index <= pd.Timestamp(end)),
                cols,
            ]
            if len(filtered) >= min_rows:
                if not force_latest:
                    return filtered, ""
                if pd.Timestamp(end).date() <= pd.Timestamp(filtered.index.max()).date():
                    return filtered, ""

        if ak is None:
            if not cached.empty:
                return filtered if 'filtered' in locals() else cached, "akshare is not installed; using local cache only"
            return pd.DataFrame(), "akshare is not installed"

        # --- cache miss: fetch from network ---
        start_ymd = start.strftime("%Y%m%d")
        end_ymd = end.strftime("%Y%m%d")
        errors: List[str] = []
        result_frame = pd.DataFrame()

        with MarketData.without_proxy_env():
            try:
                code_with_prefix = f"sh{normalized[:6]}" if normalized.endswith(".SS") else f"sz{normalized[:6]}"
                raw = ak.stock_zh_a_hist_tx(symbol=code_with_prefix, start_date=start_ymd, end_date=end_ymd)
                frame = MarketData._normalize_tx_hist_frame(raw)
                if len(frame) >= min_rows:
                    result_frame = frame
                else:
                    errors.append("stock_zh_a_hist_tx returned empty or invalid columns")
            except Exception as exc:
                errors.append(f"stock_zh_a_hist_tx error: {exc}")

            if result_frame.empty:
                try:
                    raw = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start_ymd, end_date=end_ymd, adjust="qfq")
                    frame = MarketData._normalize_ohlcv_frame(raw)
                    if len(frame) >= min_rows:
                        result_frame = frame
                    else:
                        errors.append("stock_zh_a_hist returned empty or invalid columns")
                except Exception as exc:
                    errors.append(f"stock_zh_a_hist error: {exc}")

            if result_frame.empty:
                try:
                    raw = ak.index_zh_a_hist(symbol=code, period="daily", start_date=start_ymd, end_date=end_ymd)
                    frame = MarketData._normalize_ohlcv_frame(raw)
                    if len(frame) >= min_rows:
                        result_frame = frame
                    else:
                        errors.append("index_zh_a_hist returned empty or invalid columns")
                except Exception as exc:
                    errors.append(f"index_zh_a_hist error: {exc}")

        if result_frame.empty:
            return pd.DataFrame(), "; ".join(errors) if errors else "unknown akshare failure"

        # --- save to cache ---
        merged = MarketData._merge_kline_frames(cached, result_frame)
        MarketData._save_kline_cache(symbol, merged)
        return result_frame, ""

    @staticmethod
    def _normalize_ohlcv_frame(raw: Optional[pd.DataFrame]) -> pd.DataFrame:
        if raw is None or raw.empty:
            return pd.DataFrame()

        col_map = {str(col).lower(): col for col in raw.columns}

        def pick(*candidates: str) -> Optional[str]:
            for candidate in candidates:
                for lowered, original in col_map.items():
                    if lowered == candidate.lower():
                        return original
            return None

        date_col = pick("日期", "date")
        close_col = pick("收盘", "close")
        if not date_col or not close_col:
            return pd.DataFrame()

        open_col = pick("开盘", "open")
        high_col = pick("最高", "high")
        low_col = pick("最低", "low")
        volume_col = pick("成交量", "volume")
        amount_col = pick("成交额", "amount")
        turnover_col = pick("换手率", "turnover")

        frame = raw.copy()
        frame["Date"] = pd.to_datetime(frame[date_col], errors="coerce")
        frame["Close"] = pd.to_numeric(frame[close_col], errors="coerce")
        frame["Open"] = pd.to_numeric(frame[open_col], errors="coerce") if open_col else frame["Close"]
        frame["High"] = pd.to_numeric(frame[high_col], errors="coerce") if high_col else frame[["Open", "Close"]].max(axis=1)
        frame["Low"] = pd.to_numeric(frame[low_col], errors="coerce") if low_col else frame[["Open", "Close"]].min(axis=1)
        frame["Volume"] = pd.to_numeric(frame[volume_col], errors="coerce") if volume_col else 0.0
        frame["Amount"] = pd.to_numeric(frame[amount_col], errors="coerce") if amount_col else frame["Close"] * frame["Volume"]
        frame["Turnover"] = pd.to_numeric(frame[turnover_col], errors="coerce") if turnover_col else np.nan
        frame = frame.dropna(subset=["Date", "Close"]).set_index("Date").sort_index()
        return frame[["Open", "High", "Low", "Close", "Volume", "Amount", "Turnover"]]

    @staticmethod
    def _looks_like_tx_hist_frame(raw: Optional[pd.DataFrame]) -> bool:
        if raw is None or raw.empty:
            return False
        lowered = {str(col).lower() for col in raw.columns}
        return {"date", "open", "close", "high", "low", "amount"}.issubset(lowered) and "volume" not in lowered

    @staticmethod
    def _normalize_tx_hist_frame(raw: Optional[pd.DataFrame]) -> pd.DataFrame:
        frame = MarketData._normalize_ohlcv_frame(raw)
        if frame.empty:
            return frame
        # stock_zh_a_hist_tx 的 amount 字段实际是成交量（手），不是成交额。
        volume_lots = pd.to_numeric(frame["Amount"], errors="coerce")
        close = pd.to_numeric(frame["Close"], errors="coerce").replace(0, np.nan)
        frame["Volume"] = (volume_lots * 100.0).fillna(0.0)
        frame["Amount"] = (frame["Volume"] * close).fillna(0.0)
        return frame


def run_neckline_breakout_scan(
    board_filter: str | None = None,
    trade_date: str | None = None,
    cache_dir: str | None = None,
    max_workers: int | None = None,
    history_lookback_days: int | None = None,
    use_intraday: bool | None = None,
) -> List[Tuple[str, str]]:
    cfg = PatternScanConfig()
    if cache_dir:
        cfg.cache_dir = cache_dir
    if max_workers is not None:
        cfg.max_workers = max(1, int(max_workers))
    if history_lookback_days is not None:
        cfg.history_lookback_days = max(int(history_lookback_days), cfg.max_window_days, 30)
    target_date = _normalize_trade_date(trade_date)
    universe = _load_universe(
        board_filter=board_filter,
        apply_spot_prefilter=False,
        amount_rank_date=target_date,
        ensure_amount_cache=True,
        amount_rank_lookback_days=cfg.history_lookback_days,
        max_workers=cfg.max_workers,
        min_amount_limit=_max_amount_rank_required(cfg.templates),
    )
    if not universe:
        print("[形态筛选] 无可用股票池。")
        return []

    board_label = {
        None: "全A股",
        MAIN_BOARD_ONLY: "沪深主板",
        SH_MAIN_ONLY: "沪市主板",
        SZ_MAIN_ONLY: "深市主板",
        CHINEXT_ONLY: "创业板",
    }.get(board_filter, board_filter or "全A股")
    amount_limit = _market_top_amount_limit()
    if amount_limit > 0:
        board_label = f"{board_label}成交额前{amount_limit}"

    force_intraday = env_bool("FORCE_INTRADAY_SCAN", default=False)
    should_use_intraday = (
        (force_intraday or _is_cn_trading_session())
        if use_intraday is None
        else bool(use_intraday)
    ) and pd.Timestamp(target_date).date() == datetime.now(tz=CST).date()
    cache_state = (
        {}
        if should_use_intraday
        else _load_scan_cache(cfg, board_filter=board_filter, trade_date=target_date)
    )
    processed_symbols = {str(item).strip() for item in cache_state.get("processed_symbols", []) if str(item).strip()}
    matched_map: Dict[str, dict] = {}
    watchlist_map: Dict[str, dict] = {}
    for item in cache_state.get("matched", []):
        code = str(item.get("code", "")).strip()
        if code:
            matched_map[code] = {
                "name": str(item.get("name", "")).strip(),
                "pattern_name": str(item.get("pattern_name", "")).strip(),
                "amount": item.get("amount", ""),
                "amount_rank": item.get("amount_rank", ""),
                "signal_close": item.get("signal_close", ""),
                "signal_high": item.get("signal_high", ""),
                "point_a_label": str(item.get("point_a_label", "")).strip(),
                "point_b_label": str(item.get("point_b_label", "")).strip(),
                "point_c_label": str(item.get("point_c_label", "")).strip(),
                "point_d_label": str(item.get("point_d_label", "")).strip(),
                "breakout_price": item.get("breakout_price"),
                "take_profit": item.get("take_profit"),
                "stop_loss": item.get("stop_loss"),
            }
            if str(item.get("signal_type", "")).strip() == "突破回踩确认":
                matched_map[code].update(
                    {
                        "signal_type": "突破回踩确认",
                        "pullback_date": item.get("pullback_date", ""),
                        "pullback_low": item.get("pullback_low", ""),
                        "pullback_close": item.get("pullback_close", ""),
                    }
                )
    for item in cache_state.get("watchlist", []):
        code = str(item.get("code", "")).strip()
        if code:
            watchlist_map[code] = {
                "name": str(item.get("name", "")).strip(),
                "pattern_name": str(item.get("pattern_name", "")).strip(),
                "point_a_label": str(item.get("point_a_label", "")).strip(),
                "point_b_label": str(item.get("point_b_label", "")).strip(),
                "point_c_label": str(item.get("point_c_label", "")).strip(),
                "point_d_label": str(item.get("point_d_label", "")).strip(),
                "signal_close": item.get("signal_close"),
                "signal_high": item.get("signal_high"),
                "breakout_price": item.get("breakout_price"),
                "close_gap_to_breakout": item.get("close_gap_to_breakout"),
                "close_gap_ratio": item.get("close_gap_ratio"),
                "intraday_touch_breakout": bool_to_cn(item.get("intraday_touch_breakout"))
                if isinstance(item.get("intraday_touch_breakout"), bool)
                else str(item.get("intraday_touch_breakout", "")).strip(),
                "take_profit": item.get("take_profit"),
                "stop_loss": item.get("stop_loss"),
            }
    if processed_symbols:
        print(
            f"[形态筛选] 检测到目标日期缓存({target_date})：已分析 {len(processed_symbols)} 只，"
            f"已命中 {len(matched_map)} 只，观察名单 {len(watchlist_map)} 只，本次将从断点继续。"
        )
        if len(processed_symbols) >= len(universe):
            print("[形态筛选] 目标日期缓存已完成，无需重复扫描，将直接输出缓存结果。")

    print(
        f"[形态筛选] 启动：范围={board_label} 股票数={len(universe)} "
        f"模板={cfg.template_summary_text} "
        f"A/C为各模板窗口内高低点 "
        f"B/D为各模板右半段高低点 "
        f"E=目标日期当天收盘价站上A-B连线 "
        f"截止日期={target_date} "
        f"历史拉取天数={cfg.history_lookback_days} "
        f"并发={cfg.max_workers} "
        f"盘中实时={'on' if should_use_intraday else 'off'} "
        f"观察名单距突破阈值={cfg.prebreakout_gap_ratio:.2%} "
        f"D点最小账龄={cfg.watchlist_min_d_age_ratio:.1%}窗口 "
        f"D-B反弹位置={cfg.watchlist_min_rebound_position_ratio:.0%}~{cfg.watchlist_max_rebound_position_ratio:.0%}"
    )
    if should_use_intraday:
        print("[形态筛选] 盘中实时扫描：将读取本地/历史日线，并拼接今日实时快照生成临时日线；不会写入 K 线缓存。")

    skipped_short_history = int(cache_state.get("skipped_short_history", 0) or 0)
    skipped_no_data = int(cache_state.get("skipped_no_data", 0) or 0)
    pending = [(symbol, name) for symbol, name in universe if symbol not in processed_symbols]
    completed_count = len(processed_symbols)
    realtime_quotes: Dict[str, dict] = {}
    if should_use_intraday:
        realtime_quotes = MarketData.realtime_quotes([symbol for symbol, _ in universe])
        if realtime_quotes:
            print(f"[数据] 实时临时日线待合成 {len(realtime_quotes)}/{len(universe)} 只")
        else:
            print("[数据] 实时快照为空，本次仅使用日线缓存扫描")

    # 预计算成交额排名（用于超短期热门等区间筛选模板）
    _has_rank_template = any(
        t.amount_rank_min > 0 or t.amount_rank_max > 0 for t in cfg.templates
    )
    _ranked_codes: List[str] = []
    _code_amount_map: Dict[str, float] = {}
    if _has_rank_template:
        if should_use_intraday and realtime_quotes:
            _cached_amount_map = {}
            for symbol, _ in universe:
                quote = realtime_quotes.get(symbol)
                if not quote:
                    continue
                amount_value = safe_float(quote.get("amount"), 0.0)
                if amount_value > 0:
                    _cached_amount_map[symbol.split(".")[0]] = {"amount": amount_value}
        else:
            _ensure_amount_rank_cache(
                pairs=universe,
                amount_rank_date=target_date,
                lookback_days=cfg.history_lookback_days,
                max_workers=cfg.max_workers,
            )
            _cached_amount_map = _load_cached_amount_map(universe, amount_rank_date=target_date)
        _code_amount_map = {
            code: float(pd.to_numeric(info.get("amount"), errors="coerce"))
            for code, info in _cached_amount_map.items()
            if pd.notna(pd.to_numeric(info.get("amount"), errors="coerce"))
        }
        if should_use_intraday and realtime_quotes:
            _ranked_codes = [
                code
                for code, _amount in sorted(
                    _code_amount_map.items(),
                    key=lambda item: item[1],
                    reverse=True,
                )
            ]
        else:
            _ranked_codes = _get_amount_ranked_codes(universe, amount_rank_date=target_date)
    _code_rank_map: Dict[str, int] = {code: i + 1 for i, code in enumerate(_ranked_codes)}
    _enrich_cached_matched_quotes(
        matched_map=matched_map,
        amount_map=_code_amount_map,
        rank_map=_code_rank_map,
        target_date=target_date,
    )

    with ThreadPoolExecutor(max_workers=cfg.max_workers) as executor:
        future_map = {
            executor.submit(
                _analyze_symbol_for_pattern,
                symbol,
                cfg,
                target_date,
                _code_rank_map.get(symbol.split(".")[0], 0),
                realtime_quotes.get(symbol) if should_use_intraday else None,
            ): (symbol, name)
            for symbol, name in pending
        }
        for future in as_completed(future_map):
            symbol, name = future_map[future]
            status = "no_data"
            outcome = PatternScanOutcome(matched=None, watch=None)
            try:
                status, outcome = future.result()
            except Exception:
                status = "no_data"

            processed_symbols.add(symbol)
            completed_count += 1
            if status == "short_history":
                skipped_short_history += 1
            elif status == "no_data":
                skipped_no_data += 1
            elif outcome.matched is not None:
                detail = outcome.matched
                code = symbol.split(".")[0]
                amount_rank = _code_rank_map.get(code, 0)
                amount_value = _code_amount_map.get(code)
                entry_info = {
                    "name": name,
                    "pattern_name": detail.pattern_name,
                    "amount": round(amount_value, 2) if amount_value is not None else "",
                    "amount_rank": amount_rank if amount_rank > 0 else "",
                    "signal_close": round(detail.e_close, 2),
                    "signal_high": round(detail.e_high, 2),
                    "point_a_label": detail.point_a_label,
                    "point_b_label": detail.point_b_label,
                    "point_c_label": detail.point_c_label,
                    "point_d_label": detail.point_d_label,
                    "breakout_price": round(detail.breakout_price, 2),
                    "take_profit": round(detail.take_profit_price, 2),
                    "stop_loss": round(detail.stop_loss_price(), 2),
                }
                if detail.signal_type == "突破回踩确认":
                    entry_info.update(
                        {
                            "signal_type": detail.signal_type,
                            "pullback_date": detail.pullback_date,
                            "pullback_low": round(detail.pullback_low, 2) if detail.pullback_low is not None else "",
                            "pullback_close": round(detail.pullback_close, 2) if detail.pullback_close is not None else "",
                        }
                    )
                matched_map[code] = entry_info
                watchlist_map.pop(code, None)
                amount_text = _format_amount_brief(amount_value)
                amount_rank_text = f"第{amount_rank}名" if amount_rank > 0 else ""
                amount_segment = ""
                if detail.pattern_name == ULTRA_SHORT_PATTERN_NAME and (amount_text or amount_rank_text):
                    amount_parts = [part for part in [amount_text, amount_rank_text] if part]
                    amount_segment = f"  成交额={' / '.join(amount_parts)}  "
                print(
                    f"[形态命中] {code},{name}  "
                    f"周期={entry_info['pattern_name']}  "
                    f"突破价={entry_info['breakout_price']}  "
                    f"止盈={entry_info['take_profit']}  "
                    f"止损={entry_info['stop_loss']}  "
                    f"{amount_segment}"
                    f"A={entry_info.get('point_a_label', '')}  "
                    f"B={entry_info.get('point_b_label', '')}  "
                    f"C={entry_info.get('point_c_label', '')}  "
                    f"D={entry_info.get('point_d_label', '')}"
                )
            elif outcome.watch is not None:
                detail = outcome.watch
                code = symbol.split(".")[0]
                watch_info = {
                    "name": name,
                    "pattern_name": detail.pattern_name,
                    "point_a_label": detail.point_a_label,
                    "point_b_label": detail.point_b_label,
                    "point_c_label": detail.point_c_label,
                    "point_d_label": detail.point_d_label,
                    "signal_close": round(detail.e_close, 2),
                    "signal_high": round(detail.e_high, 2),
                    "breakout_price": round(detail.breakout_price, 2),
                    "close_gap_to_breakout": round(detail.close_gap_to_breakout, 2),
                    "close_gap_ratio": round(detail.close_gap_ratio, 4),
                    "intraday_touch_breakout": bool_to_cn(detail.intraday_touch_breakout),
                    "take_profit": round(detail.take_profit_price, 2),
                    "stop_loss": round(detail.stop_loss_price(), 2),
                }
                watchlist_map[code] = watch_info
                print(
                    f"[临近突破] {code},{name}  "
                    f"周期={watch_info['pattern_name']}  "
                    f"收盘={watch_info['signal_close']}  "
                    f"最高={watch_info['signal_high']}  "
                    f"突破价={watch_info['breakout_price']}  "
                    f"差额={watch_info['close_gap_to_breakout']}  "
                    f"差幅={watch_info['close_gap_ratio']:.2%}  "
                    f"盘中触线={watch_info['intraday_touch_breakout']}  "
                    f"A={watch_info.get('point_a_label', '')}  "
                    f"B={watch_info.get('point_b_label', '')}  "
                    f"C={watch_info.get('point_c_label', '')}  "
                    f"D={watch_info.get('point_d_label', '')}"
                )

            should_save_cache = (
                completed_count == len(universe)
                or completed_count % cfg.progress_every == 0
            )
            if should_save_cache:
                _save_scan_cache(
                    cfg,
                    board_filter=board_filter,
                    trade_date=target_date,
                    universe_size=len(universe),
                    processed_symbols=processed_symbols,
                    matched_map=matched_map,
                    watchlist_map=watchlist_map,
                    skipped_short_history=skipped_short_history,
                    skipped_no_data=skipped_no_data,
                )

            if completed_count % cfg.progress_every == 0:
                print(
                    f"[形态筛选] 进度 {completed_count}/{len(universe)}，"
                    f"当前命中 {len(matched_map)}，观察名单 {len(watchlist_map)}"
                )

    _save_scan_cache(
        cfg,
        board_filter=board_filter,
        trade_date=target_date,
        universe_size=len(universe),
        processed_symbols=processed_symbols,
        matched_map=matched_map,
        watchlist_map=watchlist_map,
        skipped_short_history=skipped_short_history,
        skipped_no_data=skipped_no_data,
    )

    matched_rows = sorted(matched_map.items(), key=lambda item: item[0])
    watch_rows = sorted(watchlist_map.items(), key=lambda item: item[0])
    print(f"[形态筛选] 完成：总命中 {len(matched_rows)} / {len(universe)}")
    print(f"[形态筛选] 临近突破观察名单 {len(watch_rows)} / {len(universe)}")
    print(f"[形态筛选] 跳过历史不足{cfg.min_window_days}日标的：{skipped_short_history}只")
    print(f"[形态筛选] 跳过无可用历史数据标的：{skipped_no_data}只")
    if matched_rows:
        print("[形态筛选] 命中列表(代码,名称,周期,成交额,成交额名次,突破价,止盈,止损,A点,B点,C点,D点)：")
        for code, info in matched_rows:
            name = info.get("name", code) if isinstance(info, dict) else info
            if isinstance(info, dict) and info.get("breakout_price"):
                print(
                    f"{code},{name},{info.get('pattern_name', '')},"
                    f"{_format_amount_brief(info.get('amount', ''))},{info.get('amount_rank', '')},"
                    f"{info['breakout_price']},{info['take_profit']},"
                    f"{info['stop_loss']},"
                    f"{info.get('point_a_label', '')},{info.get('point_b_label', '')},"
                    f"{info.get('point_c_label', '')},{info.get('point_d_label', '')}"
                )
            else:
                print(f"{code},{name}")
    else:
        print("[形态筛选] 无符合条件标的")
    if watch_rows:
        print("[形态筛选] 临近突破观察名单(代码,名称,周期,收盘价,最高价,突破价,距突破差额,距突破差幅,A点,B点,C点,D点,盘中触线)：")
        for code, info in watch_rows:
            print(
                f"{code},{info['name']},{info.get('pattern_name', '')},{info['signal_close']},{info['signal_high']},"
                f"{info['breakout_price']},{info['close_gap_to_breakout']},"
                f"{info['close_gap_ratio']},{info.get('point_a_label', '')},{info.get('point_b_label', '')},"
                f"{info.get('point_c_label', '')},{info.get('point_d_label', '')},{info['intraday_touch_breakout']}"
            )
    else:
        print("[形态筛选] 无临近突破观察名单")
    return [(code, info["name"] if isinstance(info, dict) else info) for code, info in matched_rows]


def _normalize_trade_date(trade_date: str | None) -> str:
    if not trade_date:
        return today_str()
    return pd.Timestamp(trade_date).strftime("%Y-%m-%d")


def _cache_file_prefix(board_filter: str | None) -> str:
    amount_limit = _market_top_amount_limit()
    if amount_limit > 0:
        return f"market_top{amount_limit}_amount"
    return f"{board_filter or 'all'}_full_universe"


def _cache_paths(cfg: PatternScanConfig, board_filter: str | None, trade_date: str) -> tuple[str, str, str]:
    folder = os.path.join(cfg.cache_dir, trade_date)
    prefix = _cache_file_prefix(board_filter)
    return (
        os.path.join(folder, f"{prefix}.json"),
        os.path.join(folder, f"{prefix}_matched.csv"),
        os.path.join(folder, f"{prefix}_watchlist.csv"),
    )


def _scan_cache_signature(cfg: PatternScanConfig) -> dict:
    return {
        "point_price_mode": "A/B=high,C/D=low",
        "templates": [
            {
                "name": item.name,
                "window_days": int(item.window_days),
                "window_days_max": int(item.window_days_max),
                "b_window_days": int(item.b_window_days),
                "recent_low_window_days": int(item.recent_low_window_days),
                "max_ab_gap_ratio": round(float(item.max_ab_gap_ratio), 6),
                "low_ratio_threshold": round(float(item.low_ratio_threshold), 6),
                "local_extrema_neighbor_days": int(item.local_extrema_neighbor_days),
                "min_ac_amplitude_ratio": round(float(item.min_ac_amplitude_ratio), 6),
                "min_bd_amplitude_ratio": round(float(item.min_bd_amplitude_ratio), 6),
                "min_b_peak_prominence_ratio": round(float(item.min_b_peak_prominence_ratio), 6),
                "post_d_peak_neighbor_days": int(item.post_d_peak_neighbor_days),
                "min_breakout_over_d_ratio": round(float(item.min_breakout_over_d_ratio), 6),
                "amount_rank_min": int(item.amount_rank_min),
                "amount_rank_max": int(item.amount_rank_max),
            }
            for item in cfg.templates
        ],
        "prebreakout_gap_ratio": round(float(cfg.prebreakout_gap_ratio), 6),
        "watchlist_min_d_age_ratio": round(float(cfg.watchlist_min_d_age_ratio), 6),
        "watchlist_min_rebound_position_ratio": round(float(cfg.watchlist_min_rebound_position_ratio), 6),
        "watchlist_max_rebound_position_ratio": round(float(cfg.watchlist_max_rebound_position_ratio), 6),
        "pullback_confirm_lookback_days": int(cfg.pullback_confirm_lookback_days),
        "pullback_candidate_lookback_days": int(cfg.pullback_candidate_lookback_days),
    }


def _load_scan_cache(cfg: PatternScanConfig, board_filter: str | None, trade_date: str) -> dict:
    json_path, _, _ = _cache_paths(cfg, board_filter, trade_date)
    legacy_path = os.path.join(cfg.cache_dir, f"{trade_date}_{_cache_file_prefix(board_filter)}.json")
    path = json_path if os.path.exists(json_path) else legacy_path
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return {}
    if payload.get("cache_signature") != _scan_cache_signature(cfg):
        return {}
    return payload


def _save_scan_cache(
    cfg: PatternScanConfig,
    board_filter: str | None,
    trade_date: str,
    universe_size: int,
    processed_symbols: set[str],
    matched_map: Dict[str, dict],
    watchlist_map: Dict[str, dict],
    skipped_short_history: int,
    skipped_no_data: int,
) -> None:
    json_path, csv_path, watchlist_csv_path = _cache_paths(cfg, board_filter, trade_date)
    os.makedirs(os.path.dirname(json_path), exist_ok=True)
    matched_rows = []
    for code, info in sorted(matched_map.items(), key=lambda item: item[0]):
        if isinstance(info, dict):
            row = {"code": code, **info}
        else:
            row = {"code": code, "name": info}
        matched_rows.append(row)
    watchlist_rows = []
    for code, info in sorted(watchlist_map.items(), key=lambda item: item[0]):
        if isinstance(info, dict):
            row = {"code": code, **info}
        else:
            row = {"code": code, "name": info}
        watchlist_rows.append(row)
    payload = {
        "trade_date": trade_date,
        "board_filter": board_filter or "all",
        "cache_signature": _scan_cache_signature(cfg),
        "universe_size": int(universe_size),
        "processed_count": len(processed_symbols),
        "processed_symbols": sorted(processed_symbols),
        "matched": matched_rows,
        "watchlist": watchlist_rows,
        "skipped_short_history": int(skipped_short_history),
        "skipped_no_data": int(skipped_no_data),
    }
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    csv_cols = [
        "code",
        "name",
        "pattern_name",
        "amount",
        "amount_rank",
        "signal_close",
        "signal_high",
        "breakout_price",
        "take_profit",
        "stop_loss",
        "point_a_label",
        "point_b_label",
        "point_c_label",
        "point_d_label",
    ]
    csv_frame = pd.DataFrame(matched_rows)
    has_pullback_candidate = (
        not csv_frame.empty
        and "signal_type" in csv_frame.columns
        and (csv_frame["signal_type"].astype(str).str.strip() == "突破回踩确认").any()
    )
    if has_pullback_candidate:
        insert_at = csv_cols.index("breakout_price")
        csv_cols[insert_at:insert_at] = [
            "signal_type",
            "pullback_date",
            "pullback_low",
            "pullback_close",
        ]
    for col in csv_cols:
        if col not in csv_frame.columns:
            csv_frame[col] = ""
    csv_frame_to_save = _build_scan_export_frame(
        _exclude_frame_by_pattern_name(csv_frame, ULTRA_SHORT_PATTERN_NAME),
        csv_cols,
    )
    csv_frame_to_save.to_csv(csv_path, index=False)

    watch_cols = [
        "code",
        "name",
        "pattern_name",
        "signal_close",
        "signal_high",
        "breakout_price",
        "close_gap_to_breakout",
        "close_gap_ratio",
        "point_a_label",
        "point_b_label",
        "point_c_label",
        "point_d_label",
        "intraday_touch_breakout",
        "take_profit",
        "stop_loss",
    ]
    watch_frame = pd.DataFrame(watchlist_rows)
    for col in watch_cols:
        if col not in watch_frame.columns:
            watch_frame[col] = ""
    watch_frame_to_save = _build_scan_export_frame(
        _exclude_frame_by_pattern_name(watch_frame, ULTRA_SHORT_PATTERN_NAME),
        watch_cols,
    )
    watch_frame_to_save.to_csv(watchlist_csv_path, index=False)
    _save_scan_pattern_outputs(
        matched_frame=csv_frame,
        matched_cols=csv_cols,
        matched_csv_path=csv_path,
        watch_frame=watch_frame,
        watch_cols=watch_cols,
        watchlist_csv_path=watchlist_csv_path,
        pattern_name=ULTRA_SHORT_PATTERN_NAME,
        file_stem=ULTRA_SHORT_FILE_STEM,
    )


def _build_scan_export_frame(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    return frame[columns].rename(columns=CSV_COLUMN_MAPPING)


def _filter_frame_by_pattern_name(frame: pd.DataFrame, pattern_name: str) -> pd.DataFrame:
    if frame is None or frame.empty or "pattern_name" not in frame.columns:
        return pd.DataFrame(columns=frame.columns if frame is not None else None)
    normalized = str(pattern_name).strip()
    filtered = frame[frame["pattern_name"].astype(str).str.strip() == normalized].copy()
    return filtered


def _exclude_frame_by_pattern_name(frame: pd.DataFrame, pattern_name: str) -> pd.DataFrame:
    if frame is None or frame.empty or "pattern_name" not in frame.columns:
        return frame.copy() if isinstance(frame, pd.DataFrame) else pd.DataFrame(columns=frame.columns if frame is not None else None)
    normalized = str(pattern_name).strip()
    filtered = frame[frame["pattern_name"].astype(str).str.strip() != normalized].copy()
    return filtered


def _pattern_specific_scan_csv_path(base_csv_path: str, file_stem: str) -> str:
    directory, filename = os.path.split(base_csv_path)
    stem, ext = os.path.splitext(filename)
    suffix = f"_{file_stem}"
    if stem.endswith("_matched"):
        stem = stem[: -len("_matched")] + suffix + "_matched"
    elif stem.endswith("_watchlist"):
        stem = stem[: -len("_watchlist")] + suffix + "_watchlist"
    else:
        stem = stem + suffix
    return os.path.join(directory, f"{stem}{ext}")


def _save_scan_pattern_outputs(
    matched_frame: pd.DataFrame,
    matched_cols: list[str],
    matched_csv_path: str,
    watch_frame: pd.DataFrame,
    watch_cols: list[str],
    watchlist_csv_path: str,
    pattern_name: str,
    file_stem: str,
) -> None:
    matched_filtered = _filter_frame_by_pattern_name(matched_frame, pattern_name)
    watch_filtered = _filter_frame_by_pattern_name(watch_frame, pattern_name)

    matched_output_path = _pattern_specific_scan_csv_path(matched_csv_path, file_stem)
    watch_output_path = _pattern_specific_scan_csv_path(watchlist_csv_path, file_stem)

    if matched_filtered.empty:
        pd.DataFrame(columns=_build_scan_export_frame(pd.DataFrame(columns=matched_cols), matched_cols).columns).to_csv(
            matched_output_path, index=False
        )
    else:
        _build_scan_export_frame(matched_filtered, matched_cols).to_csv(matched_output_path, index=False)

    if watch_filtered.empty:
        pd.DataFrame(columns=_build_scan_export_frame(pd.DataFrame(columns=watch_cols), watch_cols).columns).to_csv(
            watch_output_path, index=False
        )
    else:
        _build_scan_export_frame(watch_filtered, watch_cols).to_csv(watch_output_path, index=False)


def _load_universe(
    board_filter: str | None = None,
    apply_spot_prefilter: bool = True,
    amount_rank_date: str | None = None,
    ensure_amount_cache: bool = False,
    amount_rank_lookback_days: int = 365,
    max_workers: int | None = None,
    min_amount_limit: int = 0,
    disable_amount_limit: bool = False,
) -> List[Tuple[str, str]]:
    amount_limit = _market_top_amount_limit()
    if disable_amount_limit:
        amount_limit = 0
    if amount_limit > 0 and min_amount_limit > 0:
        amount_limit = max(amount_limit, int(min_amount_limit))
    spot_map = _load_spot_snapshot_map()
    code_name = _load_code_name_map()
    pairs: List[Tuple[str, str]] = []
    for code, name in code_name.items():
        current_name = str(spot_map.get(code, {}).get("name") or name or "").strip()
        upper_name = current_name.upper()
        if "ST" in upper_name:
            continue
        if not _A_SHARE_MAIN_CHINEXT_CODE_RE.fullmatch(code):
            continue
        if not _matches_board_filter(code, board_filter):
            continue
        if apply_spot_prefilter and amount_limit <= 0 and not _passes_spot_prefilter(code, current_name, spot_map):
            continue
        symbol = f"{code}.SS" if code.startswith(("600", "601", "603", "605")) else f"{code}.SZ"
        pairs.append((symbol, current_name or code))
    if amount_limit > 0:
        if amount_rank_date and ensure_amount_cache:
            _ensure_amount_rank_cache(
                pairs=pairs,
                amount_rank_date=amount_rank_date,
                lookback_days=amount_rank_lookback_days,
                max_workers=max_workers,
            )
        pairs = _limit_by_cached_amount(pairs, amount_limit, amount_rank_date=amount_rank_date)
    pairs.sort(key=lambda item: item[0])
    return pairs


def _market_top_amount_limit() -> int:
    raw_market_limit = str(_ENV.get("MARKET_TOP_AMOUNT_LIMIT", "")).strip()
    if raw_market_limit:
        return _env_int("MARKET_TOP_AMOUNT_LIMIT", 200)
    return _env_int("MAIN_BOARD_TOP_AMOUNT_LIMIT", 200)


def _limit_by_cached_amount(
    pairs: List[Tuple[str, str]],
    limit: int,
    amount_rank_date: str | None = None,
) -> List[Tuple[str, str]]:
    if limit <= 0 or len(pairs) <= limit:
        return pairs
    amount_map = _load_cached_amount_map(pairs, amount_rank_date=amount_rank_date)
    if not amount_map:
        print("[股票池] 无法从本地K线缓存获取成交额排行数据，暂时保留原股票池。")
        return pairs

    scored: List[Tuple[float, str, str]] = []
    for symbol, name in pairs:
        code = symbol.split(".")[0]
        amount = pd.to_numeric(amount_map.get(code, {}).get("amount"), errors="coerce")
        if pd.notna(amount):
            scored.append((float(amount), symbol, name))

    if not scored:
        print("[股票池] 无法从本地K线缓存匹配成交额排行数据，暂时保留原股票池。")
        return pairs
    scored.sort(key=lambda item: item[0], reverse=True)
    return [(symbol, name) for _, symbol, name in scored[:limit]]


def _get_amount_ranked_codes(
    pairs: List[Tuple[str, str]],
    amount_rank_date: str | None = None,
) -> List[str]:
    """返回按成交额从大到小排序的股票代码列表（不截断）。"""
    amount_map = _load_cached_amount_map(pairs, amount_rank_date=amount_rank_date)
    if not amount_map:
        return []
    scored: List[Tuple[float, str]] = []
    for symbol, _ in pairs:
        code = symbol.split(".")[0]
        amount = pd.to_numeric(amount_map.get(code, {}).get("amount"), errors="coerce")
        if pd.notna(amount):
            scored.append((float(amount), code))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [code for _, code in scored]


def _ensure_amount_rank_cache(
    pairs: List[Tuple[str, str]],
    amount_rank_date: str,
    lookback_days: int,
    max_workers: int | None,
) -> None:
    missing = [
        (symbol, name)
        for symbol, name in pairs
        if not _cached_kline_has_date(symbol, amount_rank_date)
    ]
    if not missing:
        return

    workers = max(1, int(max_workers or _ENV.get("MAX_WORKERS", 4)))
    print(
        f"[股票池] 成交额排行日期={amount_rank_date}，"
        f"本地K线缺少该日期 {len(missing)}/{len(pairs)} 只，先补齐缓存。"
    )

    completed = 0
    failed = 0

    def _download_one(symbol_name: tuple[str, str]) -> bool:
        symbol, _ = symbol_name
        try:
            frame, _ = MarketData.from_akshare_with_reason(
                symbol,
                min_rows=1,
                end_date=amount_rank_date,
                lookback_days=max(int(lookback_days), 30),
                force_latest=True,
            )
            return not frame.empty and _cached_kline_has_date(symbol, amount_rank_date)
        except Exception:
            return False

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(_download_one, item): item for item in missing}
        for future in as_completed(future_map):
            completed += 1
            if not future.result():
                failed += 1
            if completed % 100 == 0 or completed == len(missing):
                print(f"[股票池] K线缓存补齐进度 {completed}/{len(missing)}，失败 {failed}")


def _cached_kline_has_date(symbol: str, amount_rank_date: str) -> bool:
    frame = MarketData._read_kline_cache(symbol)
    if frame.empty:
        return False
    target_ts = pd.Timestamp(amount_rank_date).normalize()
    return bool((frame.index.normalize() == target_ts).any())


def _load_cached_amount_map(
    pairs: List[Tuple[str, str]],
    amount_rank_date: str | None = None,
) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    target_ts = pd.Timestamp(amount_rank_date).normalize() if amount_rank_date else None
    for symbol, _ in pairs:
        code = symbol.split(".")[0]
        path = MarketData._kline_cache_path(symbol)
        if not os.path.exists(path):
            continue
        try:
            frame = pd.read_csv(path, usecols=["Date", "Amount"])
        except Exception:
            continue
        if frame.empty or "Amount" not in frame.columns:
            continue
        if target_ts is not None:
            dates = pd.to_datetime(frame["Date"], errors="coerce").dt.normalize()
            frame = frame.loc[dates == target_ts]
            if frame.empty:
                continue
        amounts = pd.to_numeric(frame["Amount"], errors="coerce").dropna()
        if amounts.empty:
            continue
        out[code] = {"amount": float(amounts.iloc[-1])}
    return out


def _code_to_symbol(code: str) -> str:
    normalized = str(code or "").strip().zfill(6)
    return f"{normalized}.SS" if normalized.startswith(("600", "601", "603", "605")) else f"{normalized}.SZ"


def _cached_signal_prices(code: str, target_date: str) -> tuple[float | None, float | None]:
    frame = MarketData._read_kline_cache(_code_to_symbol(code))
    if frame.empty:
        return None, None
    target_ts = pd.Timestamp(target_date).normalize()
    matched = frame.loc[frame.index.normalize() == target_ts]
    if matched.empty:
        return None, None
    row = matched.iloc[-1]
    close = pd.to_numeric(row.get("Close"), errors="coerce")
    high = pd.to_numeric(row.get("High"), errors="coerce")
    close_value = round(float(close), 2) if pd.notna(close) else None
    high_value = round(float(high), 2) if pd.notna(high) else None
    return close_value, high_value


def _is_blank_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and pd.isna(value):
        return True
    return str(value).strip() == ""


def _enrich_cached_matched_quotes(
    matched_map: Dict[str, dict],
    amount_map: Dict[str, float],
    rank_map: Dict[str, int],
    target_date: str,
) -> None:
    if not matched_map:
        return
    for code, info in matched_map.items():
        if not isinstance(info, dict):
            continue
        if _is_blank_value(info.get("amount")):
            amount_value = amount_map.get(code)
            if amount_value is not None:
                info["amount"] = round(amount_value, 2)
        if _is_blank_value(info.get("amount_rank")):
            amount_rank = rank_map.get(code, 0)
            if amount_rank > 0:
                info["amount_rank"] = amount_rank
        if _is_blank_value(info.get("signal_close")) or _is_blank_value(info.get("signal_high")):
            signal_close, signal_high = _cached_signal_prices(code, target_date)
            if _is_blank_value(info.get("signal_close")) and signal_close is not None:
                info["signal_close"] = signal_close
            if _is_blank_value(info.get("signal_high")) and signal_high is not None:
                info["signal_high"] = signal_high


def _matches_board_filter(code: str, board_filter: str | None) -> bool:
    if board_filter == CHINEXT_ONLY:
        return bool(_CHINEXT_CODE_RE.fullmatch(code))
    if board_filter == SH_MAIN_ONLY:
        return bool(_SH_MAIN_CODE_RE.fullmatch(code))
    if board_filter == SZ_MAIN_ONLY:
        return bool(_SZ_MAIN_CODE_RE.fullmatch(code))
    if board_filter == MAIN_BOARD_ONLY:
        return not bool(_CHINEXT_CODE_RE.fullmatch(code))
    return True


def _load_code_name_map() -> Dict[str, str]:
    out: Dict[str, str] = {}
    cache_path = os.path.join("data", "universe_cache", "a_share_code_list.csv")
    cache_expired = not os.path.exists(cache_path) or (
        time.time() - os.path.getmtime(cache_path) > 86400
    )
    if os.path.exists(cache_path) and not cache_expired:
        try:
            cached = pd.read_csv(cache_path)
            if {"code", "name"}.issubset(cached.columns):
                for row in cached.to_dict(orient="records"):
                    code = str(row.get("code", "")).strip().zfill(6)
                    name = str(row.get("name", "")).strip()
                    if code:
                        out[code] = name or code
        except Exception:
            pass
    if out:
        return out
    if ak is None:
        return out

    with MarketData.without_proxy_env():
        try:
            spot = ak.stock_zh_a_spot_em()
            if spot is not None and not spot.empty and {"代码", "名称"}.issubset(spot.columns):
                for row in spot.to_dict(orient="records"):
                    code = str(row.get("代码", "")).strip().zfill(6)
                    name = str(row.get("名称", "")).strip()
                    if code:
                        out[code] = name or code
        except Exception:
            pass

        if not out:
            try:
                basic = ak.stock_info_a_code_name()
                if basic is not None and not basic.empty:
                    cols = set(basic.columns)
                    code_col = "code" if "code" in cols else "证券代码" if "证券代码" in cols else None
                    name_col = "name" if "name" in cols else "证券简称" if "证券简称" in cols else None
                    if code_col and name_col:
                        for row in basic.to_dict(orient="records"):
                            code = str(row.get(code_col, "")).strip().zfill(6)
                            name = str(row.get(name_col, "")).strip()
                            if code:
                                out[code] = name or code
            except Exception:
                pass

    if out:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        pd.DataFrame([{"code": c, "name": n} for c, n in out.items()]).to_csv(cache_path, index=False)

    return out


def _load_spot_snapshot_map() -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    if ak is None:
        return out
    try:
        with MarketData.without_proxy_env():
            spot = ak.stock_zh_a_spot_em()
        if spot is None or spot.empty or "代码" not in spot.columns:
            return out
        for row in spot.to_dict(orient="records"):
            code = str(row.get("代码", "")).strip().zfill(6)
            if not code:
                continue
            out[code] = {
                "name": str(row.get("名称", "")).strip(),
                "price": pd.to_numeric(row.get("最新价"), errors="coerce"),
                "amount": pd.to_numeric(row.get("成交额"), errors="coerce"),
                "volume": pd.to_numeric(row.get("成交量"), errors="coerce"),
            }
    except Exception:
        return {}
    return out


def _passes_spot_prefilter(code: str, name: str, spot_map: Dict[str, dict]) -> bool:
    snapshot = spot_map.get(code)
    if not snapshot:
        return True
    price = pd.to_numeric(snapshot.get("price"), errors="coerce")
    amount = pd.to_numeric(snapshot.get("amount"), errors="coerce")
    volume = pd.to_numeric(snapshot.get("volume"), errors="coerce")
    if pd.notna(price) and float(price) <= 0:
        return False
    if pd.notna(amount) and float(amount) <= 0:
        return False
    if pd.notna(volume) and float(volume) <= 0:
        return False
    return True


def _analyze_symbol_for_pattern(
    symbol: str,
    cfg: PatternScanConfig,
    trade_date: str,
    amount_rank: int = 0,
    realtime_quote: Optional[dict] = None,
) -> tuple[str, PatternScanOutcome]:
    target_ts = pd.Timestamp(trade_date)
    frame, _ = MarketData.from_akshare_with_reason(
        symbol,
        min_rows=cfg.max_window_days,
        end_date=trade_date,
        lookback_days=cfg.history_lookback_days,
        force_latest=realtime_quote is None,
    )
    if frame is None or frame.empty:
        return "no_data", PatternScanOutcome(matched=None, watch=None)
    if realtime_quote:
        frame = MarketData.append_realtime_daily_bars(
            {symbol: frame},
            {symbol: realtime_quote},
            trade_date=trade_date,
        ).get(symbol, frame)
    frame = frame.sort_index()
    frame = frame.loc[frame.index <= target_ts]
    if frame.empty or pd.Timestamp(frame.index.max()).date() < target_ts.date():
        return "no_data", PatternScanOutcome(matched=None, watch=None)
    if frame.empty or len(frame) < cfg.min_window_days:
        return "short_history", PatternScanOutcome(matched=None, watch=None)
    outcome, _ = _match_pattern_templates(frame=frame, cfg=cfg, amount_rank=amount_rank)
    if outcome.matched is not None:
        return "ok", outcome
    if outcome.watch is not None:
        return "ok", PatternScanOutcome(matched=None, watch=outcome.watch)
    return "ok", PatternScanOutcome(matched=None, watch=None)


def _match_pattern_templates(
    frame: pd.DataFrame,
    cfg: PatternScanConfig,
    amount_rank: int = 0,
) -> tuple[PatternScanOutcome, Optional[PatternTemplate]]:
    best_watch: Optional[PatternDetail] = None
    for template in cfg.templates:
        if len(frame) < template.window_days:
            continue
        if not _template_allows_amount_rank(template, amount_rank):
            continue
        win_max = template.window_days_max if template.window_days_max > template.window_days else template.window_days
        for win in range(template.window_days, win_max + 1):
            if len(frame) < win:
                break
            outcome = _matches_neckline_breakout(
                frame.tail(win),
                pattern_name=template.name,
                b_window_days=template.b_window_days,
                recent_low_window_days=template.recent_low_window_days,
                max_ab_gap_ratio=template.max_ab_gap_ratio,
                low_ratio_threshold=template.low_ratio_threshold,
                min_breakout_over_d_ratio=template.min_breakout_over_d_ratio,
                prebreakout_gap_ratio=cfg.prebreakout_gap_ratio,
                watchlist_min_d_age_ratio=cfg.watchlist_min_d_age_ratio,
                watchlist_min_rebound_position_ratio=cfg.watchlist_min_rebound_position_ratio,
                watchlist_max_rebound_position_ratio=cfg.watchlist_max_rebound_position_ratio,
                local_extrema_neighbor_days=template.local_extrema_neighbor_days,
                min_ac_amplitude_ratio=template.min_ac_amplitude_ratio,
                min_bd_amplitude_ratio=template.min_bd_amplitude_ratio,
                min_b_peak_prominence_ratio=template.min_b_peak_prominence_ratio,
                post_d_peak_neighbor_days=template.post_d_peak_neighbor_days,
                pullback_confirm_lookback_days=cfg.pullback_confirm_lookback_days,
            )
            if outcome.matched is not None:
                return outcome, template
            if best_watch is None and outcome.watch is not None:
                best_watch = outcome.watch
    if best_watch is not None:
        return PatternScanOutcome(matched=None, watch=best_watch), None
    return PatternScanOutcome(matched=None, watch=None), None


def _template_allows_amount_rank(template: PatternTemplate, amount_rank: int) -> bool:
    if template.amount_rank_min <= 0 and template.amount_rank_max <= 0:
        return True
    if amount_rank <= 0:
        return False
    if template.amount_rank_min > 0 and amount_rank < template.amount_rank_min:
        return False
    if template.amount_rank_max > 0 and amount_rank > template.amount_rank_max:
        return False
    return True


def _any_template_allows_amount_rank(templates: List[PatternTemplate], amount_rank: int) -> bool:
    return any(_template_allows_amount_rank(template, amount_rank) for template in templates)


def _match_template_pullback_candidate(
    frame: pd.DataFrame,
    cfg: PatternScanConfig,
    template: PatternTemplate,
) -> PatternScanOutcome:
    lookback_days = max(int(cfg.pullback_candidate_lookback_days), 0)
    if lookback_days <= 0 or frame is None or frame.empty or len(frame) < template.window_days + 1:
        return PatternScanOutcome(matched=None, watch=None)
    if "Low" not in frame.columns or "Close" not in frame.columns or "High" not in frame.columns:
        return PatternScanOutcome(matched=None, watch=None)

    latest_idx = len(frame) - 1
    latest_low = pd.to_numeric(frame.iloc[latest_idx].get("Low"), errors="coerce")
    latest_close = pd.to_numeric(frame.iloc[latest_idx].get("Close"), errors="coerce")
    latest_high = pd.to_numeric(frame.iloc[latest_idx].get("High"), errors="coerce")
    if pd.isna(latest_low) or pd.isna(latest_close) or pd.isna(latest_high):
        return PatternScanOutcome(matched=None, watch=None)

    max_offset = min(lookback_days, latest_idx)
    for offset in range(1, max_offset + 1):
        signal_end = latest_idx - offset
        signal_frame = frame.iloc[: signal_end + 1]
        win_max = template.window_days_max if template.window_days_max > template.window_days else template.window_days
        for win in range(template.window_days, win_max + 1):
            if len(signal_frame) < win:
                break
            outcome = _matches_neckline_breakout(
                signal_frame.tail(win),
                pattern_name=template.name,
                b_window_days=template.b_window_days,
                recent_low_window_days=template.recent_low_window_days,
                max_ab_gap_ratio=template.max_ab_gap_ratio,
                low_ratio_threshold=template.low_ratio_threshold,
                min_breakout_over_d_ratio=template.min_breakout_over_d_ratio,
                prebreakout_gap_ratio=cfg.prebreakout_gap_ratio,
                watchlist_min_d_age_ratio=cfg.watchlist_min_d_age_ratio,
                watchlist_min_rebound_position_ratio=cfg.watchlist_min_rebound_position_ratio,
                watchlist_max_rebound_position_ratio=cfg.watchlist_max_rebound_position_ratio,
                local_extrema_neighbor_days=template.local_extrema_neighbor_days,
                min_ac_amplitude_ratio=template.min_ac_amplitude_ratio,
                min_bd_amplitude_ratio=template.min_bd_amplitude_ratio,
                min_b_peak_prominence_ratio=template.min_b_peak_prominence_ratio,
                post_d_peak_neighbor_days=template.post_d_peak_neighbor_days,
                pullback_confirm_lookback_days=cfg.pullback_confirm_lookback_days,
            )
            detail = outcome.matched
            if detail is None:
                continue
            if float(latest_low) <= detail.breakout_price and float(latest_close) >= detail.breakout_price:
                detail.signal_type = "突破回踩确认"
                detail.pullback_confirmed = True
                detail.pullback_date = pd.Timestamp(frame.index[latest_idx]).strftime("%Y-%m-%d")
                detail.pullback_low = float(latest_low)
                detail.pullback_close = float(latest_close)
                detail.e_close = float(latest_close)
                detail.e_high = float(latest_high)
                return PatternScanOutcome(matched=detail, watch=None)
    return PatternScanOutcome(matched=None, watch=None)


def run_neckline_breakout_backtest(
    start_date: str,
    end_date: str,
    board_filter: str | None = None,
    history_lookback_days: int | None = None,
    max_workers: int | None = None,
    output_dir: str | None = None,
) -> pd.DataFrame:
    started_at = time.time()
    start_ts = pd.Timestamp(start_date).normalize()
    end_ts = pd.Timestamp(end_date).normalize()
    if start_ts > end_ts:
        raise ValueError("backtest start_date must be <= end_date")

    cfg = PatternScanConfig()
    enabled_pattern_names = _parse_enabled_pattern_names(_ENV.get("BACKTEST_ENABLED_PATTERNS", ""))
    if enabled_pattern_names:
        filtered_templates = _filter_templates_by_name(cfg.templates, enabled_pattern_names)
        if not filtered_templates:
            raise ValueError(
                "BACKTEST_ENABLED_PATTERNS 未匹配到任何有效模板，可选值："
                + "、".join(item.name for item in cfg.templates)
            )
        cfg.templates = filtered_templates
    if history_lookback_days is not None:
        cfg.history_lookback_days = max(int(history_lookback_days), cfg.max_window_days, 30)
    if max_workers is not None:
        cfg.max_workers = max(1, int(max_workers))

    bt_cfg = BacktestConfig(
        start_date=start_ts.strftime("%Y-%m-%d"),
        end_date=end_ts.strftime("%Y-%m-%d"),
        board_filter=board_filter,
        history_lookback_days=cfg.history_lookback_days,
        max_workers=cfg.max_workers,
        output_dir=output_dir or os.path.join("data", "backtest_reports"),
        enabled_patterns=[item.name for item in cfg.templates],
    )

    universe = _load_universe(
        board_filter=board_filter,
        apply_spot_prefilter=False,
        min_amount_limit=_max_amount_rank_required(cfg.templates),
        disable_amount_limit=_max_amount_rank_required(cfg.templates) > 0,
    )
    if not universe:
        print("[历史回测] 无可用股票池。")
        return pd.DataFrame()

    board_label = {
        None: "全A股",
        MAIN_BOARD_ONLY: "沪深主板",
        SH_MAIN_ONLY: "沪市主板",
        SZ_MAIN_ONLY: "深市主板",
        CHINEXT_ONLY: "创业板",
    }.get(board_filter, board_filter or "全A股")
    amount_limit = _market_top_amount_limit()
    if amount_limit > 0:
        board_label = f"{board_label}成交额前{amount_limit}"
    print(
        f"[历史回测] 启动：范围={board_label} 股票数={len(universe)} "
        f"区间={bt_cfg.start_date}~{bt_cfg.end_date} "
        f"模板={cfg.backtest_template_summary_text} "
        f"历史拉取天数={bt_cfg.history_lookback_days} "
        f"并发={bt_cfg.max_workers} "
    )

    signal_rows: List[dict] = []
    symbol_frames: Dict[str, pd.DataFrame] = {}
    symbol_names: Dict[str, str] = {}
    skipped_no_data = 0
    skipped_short_history = 0
    completed_count = 0

    with ThreadPoolExecutor(max_workers=bt_cfg.max_workers) as executor:
        future_map = {
            executor.submit(_fetch_backtest_symbol_frame, symbol, cfg, bt_cfg): (symbol, name)
            for symbol, name in universe
        }
        for future in as_completed(future_map):
            symbol, name = future_map[future]
            status = "no_data"
            frame: Optional[pd.DataFrame] = None
            try:
                status, frame = future.result()
            except Exception:
                status = "no_data"

            completed_count += 1
            if status == "short_history":
                skipped_short_history += 1
            elif status == "no_data":
                skipped_no_data += 1
            if frame is not None and not frame.empty:
                code = symbol.split(".")[0]
                symbol_frames[code] = frame
                symbol_names[code] = name

            if completed_count % cfg.progress_every == 0:
                print(
                    f"[历史回测] K线加载进度 {completed_count}/{len(universe)}"
                )

    print(
        f"[历史回测] K线加载完成：可用 {len(symbol_frames)} / {len(universe)}，"
        f"无数据 {skipped_no_data}，历史不足 {skipped_short_history}，"
        f"耗时 {time.time() - started_at:.1f}s"
    )
    amount_rank_started_at = time.time()
    print("[历史回测] 开始预计算区间内每日成交额排名...")
    amount_rank_by_date = _build_backtest_amount_rank_maps(
        symbol_frames=symbol_frames,
        start_ts=start_ts,
        end_ts=end_ts,
    )
    print(
        f"[历史回测] 成交额排名预计算完成：交易日 {len(amount_rank_by_date)}，"
        f"耗时 {time.time() - amount_rank_started_at:.1f}s"
    )
    eligible_rank_map_by_code = _build_backtest_eligible_rank_map_by_code(
        amount_rank_by_date=amount_rank_by_date,
        templates=cfg.templates,
    )
    eligible_symbol_days = sum(len(rank_map) for rank_map in eligible_rank_map_by_code.values())
    eligible_codes = [
        code for code in symbol_frames.keys()
        if code in eligible_rank_map_by_code
    ]
    print(
        f"[历史回测] 满足成交额范围的待扫标的 {len(eligible_codes)} / {len(symbol_frames)}，"
        f"有效股票-日期组合 {eligible_symbol_days}"
    )
    completed_count = 0
    scan_started_at = time.time()
    last_scan_log_at = scan_started_at
    print("[历史回测] 开始逐股扫描历史信号...")
    scan_workers = max(1, int(bt_cfg.max_workers))
    print(f"[历史回测] 历史信号扫描并发线程数 {scan_workers}")
    with ThreadPoolExecutor(max_workers=scan_workers) as executor:
        future_map = {
            executor.submit(
                _scan_backtest_symbol_frame,
                code,
                symbol_names.get(code, code),
                frame,
                cfg,
                bt_cfg,
                eligible_rank_map_by_code.get(code, {}),
            ): code
            for code, frame in symbol_frames.items()
            if code in eligible_rank_map_by_code
        }
        pending_futures = set(future_map.keys())
        scan_progress_every = min(cfg.progress_every, max(20, len(future_map) // 10 or 20))
        while pending_futures:
            done_futures, pending_futures = wait(
                pending_futures,
                timeout=30,
                return_when=FIRST_COMPLETED,
            )
            if not done_futures:
                elapsed = time.time() - scan_started_at
                print(
                    f"[历史回测] 信号扫描仍在进行：已完成 {completed_count}/{len(future_map)}，"
                    f"当前信号数 {len(signal_rows)}，耗时 {elapsed:.1f}s"
                )
                continue
            for future in done_futures:
                completed_count += 1
                try:
                    symbol_rows = future.result()
                except Exception:
                    symbol_rows = []
                if symbol_rows:
                    signal_rows.extend(symbol_rows)

            now = time.time()
            if (
                completed_count % scan_progress_every == 0
                or now - last_scan_log_at >= 30
                or completed_count == len(future_map)
            ):
                print(
                    f"[历史回测] 信号扫描进度 {completed_count}/{len(future_map)}，"
                    f"当前信号数 {len(signal_rows)}，"
                    f"阶段耗时 {now - scan_started_at:.1f}s"
                )
                last_scan_log_at = now

    print(
        f"[历史回测] 信号扫描完成：共 {len(signal_rows)} 条信号，"
        f"耗时 {time.time() - scan_started_at:.1f}s，累计 {time.time() - started_at:.1f}s"
    )

    result = _simulate_portfolio_backtest(
        signal_rows=signal_rows,
        symbol_frames=symbol_frames,
        bt_cfg=bt_cfg,
    )
    result = _attach_independent_signal_outcomes(
        result=result,
        symbol_frames=symbol_frames,
    )
    summary = _build_backtest_summary(
        result=result,
        universe_size=len(universe),
        skipped_no_data=skipped_no_data,
        skipped_short_history=skipped_short_history,
        cfg=bt_cfg,
    )
    _save_backtest_outputs(result=result, summary=summary, cfg=bt_cfg)
    _print_backtest_summary(summary, bt_cfg)
    return result


def _fetch_backtest_symbol_frame(
    symbol: str,
    scan_cfg: PatternScanConfig,
    bt_cfg: BacktestConfig,
) -> tuple[str, Optional[pd.DataFrame]]:
    start_ts = pd.Timestamp(bt_cfg.start_date)
    end_ts = pd.Timestamp(bt_cfg.end_date)
    fetch_end = end_ts
    fetch_lookback_days = max(
        scan_cfg.history_lookback_days,
        int((fetch_end - start_ts).days) + scan_cfg.max_window_days + 30,
    )
    frame, _ = MarketData.from_akshare_with_reason(
        symbol,
        min_rows=scan_cfg.max_window_days,
        end_date=fetch_end,
        lookback_days=fetch_lookback_days,
        force_latest=False,
    )
    if frame is None or frame.empty:
        return "no_data", None

    frame = frame.sort_index()
    frame = frame.loc[frame.index <= fetch_end]
    if len(frame) < scan_cfg.min_window_days:
        return "short_history", None

    frame = frame.copy()
    for col in ["Open", "High", "Low", "Close", "Amount"]:
        if col not in frame.columns:
            frame[col] = np.nan
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["Open", "High", "Low", "Close"])
    if len(frame) < scan_cfg.min_window_days:
        return "short_history", None

    return "ok", frame[["Open", "High", "Low", "Close", "Amount"]].copy()


def _build_backtest_amount_rank_maps(
    symbol_frames: Dict[str, pd.DataFrame],
    start_ts: pd.Timestamp,
    end_ts: pd.Timestamp,
) -> Dict[pd.Timestamp, Dict[str, int]]:
    amount_by_date: Dict[pd.Timestamp, List[Tuple[float, str]]] = {}
    for code, frame in symbol_frames.items():
        if frame is None or frame.empty or "Amount" not in frame.columns:
            continue
        signal_frame = frame.loc[(frame.index >= start_ts) & (frame.index <= end_ts)]
        for trade_dt, row in signal_frame.iterrows():
            amount = pd.to_numeric(row.get("Amount"), errors="coerce")
            if pd.notna(amount):
                amount_by_date.setdefault(pd.Timestamp(trade_dt).normalize(), []).append((float(amount), code))

    rank_by_date: Dict[pd.Timestamp, Dict[str, int]] = {}
    for trade_dt, scored in amount_by_date.items():
        scored.sort(key=lambda item: item[0], reverse=True)
        rank_by_date[trade_dt] = {code: i + 1 for i, (_, code) in enumerate(scored)}
    return rank_by_date


def _build_backtest_eligible_rank_map_by_code(
    amount_rank_by_date: Dict[pd.Timestamp, Dict[str, int]],
    templates: List[PatternTemplate],
) -> Dict[str, Dict[pd.Timestamp, int]]:
    eligible_by_code: Dict[str, Dict[pd.Timestamp, int]] = {}
    for trade_dt, rank_map in amount_rank_by_date.items():
        normalized_dt = pd.Timestamp(trade_dt).normalize()
        for code, rank in rank_map.items():
            if not _any_template_allows_amount_rank(templates, rank):
                continue
            eligible_by_code.setdefault(code, {})[normalized_dt] = int(rank)
    return eligible_by_code


def _scan_backtest_symbol_frame(
    code: str,
    name: str,
    frame: pd.DataFrame,
    scan_cfg: PatternScanConfig,
    bt_cfg: BacktestConfig,
    eligible_rank_by_date: Dict[pd.Timestamp, int],
) -> List[dict]:
    start_ts = pd.Timestamp(bt_cfg.start_date)
    end_ts = pd.Timestamp(bt_cfg.end_date)

    rows: List[dict] = []
    if not eligible_rank_by_date:
        return rows

    for signal_dt, amount_rank in sorted(eligible_rank_by_date.items()):
        normalized_signal_dt = pd.Timestamp(signal_dt).normalize()
        if normalized_signal_dt < start_ts or normalized_signal_dt > end_ts:
            continue
        signal_pos = frame.index.searchsorted(normalized_signal_dt, side="left")
        if signal_pos >= len(frame):
            continue
        if pd.Timestamp(frame.index[int(signal_pos)]).normalize() != normalized_signal_dt:
            continue
        signal_pos = int(signal_pos)
        if signal_pos + 1 < scan_cfg.min_window_days or amount_rank <= 0:
            continue
        upto_signal = frame.iloc[: signal_pos + 1]
        outcome, _ = _match_pattern_templates(
            frame=upto_signal,
            cfg=scan_cfg,
            amount_rank=amount_rank,
        )
        detail = outcome.matched
        if detail is None:
            continue

        entry_pos = signal_pos + 1
        if entry_pos >= len(frame):
            continue

        entry_dt = frame.index[entry_pos]
        signal_close = pd.to_numeric(frame.iloc[int(signal_pos)]["Close"], errors="coerce")
        entry_open = pd.to_numeric(frame.iloc[entry_pos]["Open"], errors="coerce")
        if pd.isna(signal_close) or float(signal_close) <= 0 or pd.isna(entry_open) or float(entry_open) <= 0:
            continue

        signal_close_f = float(signal_close)
        entry_open_f = float(entry_open)
        bp = detail.breakout_price
        tp = detail.take_profit_price
        sl = detail.stop_loss_price(bt_cfg.stop_loss_ratio)
        max_entry = detail.max_entry_price(bt_cfg.entry_premium_threshold)
        entry_gap = entry_open_f / bp - 1.0
        profit_target_valid = sl <= entry_open_f <= tp

        row: dict = {
            "code": code,
            "name": name,
            "pattern_name": detail.pattern_name,
            "signal_type": detail.signal_type,
            "signal_date": pd.Timestamp(signal_dt).strftime("%Y-%m-%d"),
            "entry_date": pd.Timestamp(entry_dt).strftime("%Y-%m-%d"),
            "signal_close": round(signal_close_f, 4),
            "entry_open": round(entry_open_f, 4),
            "entry_gap": round(entry_gap, 6),
            "breakout_price": round(bp, 4),
            "take_profit": round(tp, 4),
            "stop_loss": round(sl, 4),
            "max_entry": round(max_entry, 4),
            "entry_valid": True,
            "profit_target_valid": profit_target_valid,
        }
        if detail.signal_type == "突破回踩确认":
            row.update(
                {
                    "pullback_date": detail.pullback_date,
                    "pullback_low": round(float(detail.pullback_low), 4) if detail.pullback_low is not None else np.nan,
                    "pullback_close": round(float(detail.pullback_close), 4) if detail.pullback_close is not None else np.nan,
                }
            )

        rows.append(row)

    return rows


def _simulate_portfolio_backtest(
    signal_rows: List[dict],
    symbol_frames: Dict[str, pd.DataFrame],
    bt_cfg: BacktestConfig,
) -> pd.DataFrame:
    if not signal_rows:
        return pd.DataFrame()

    result = pd.DataFrame(signal_rows).copy()
    result["signal_date"] = pd.to_datetime(result["signal_date"])
    result["entry_date"] = pd.to_datetime(result["entry_date"])
    result = result.sort_values(["entry_date", "signal_date", "code"]).reset_index(drop=True)

    result["buy_executed"] = False
    result["skip_reason"] = ""
    result["buy_amount"] = np.nan
    result["shares"] = 0
    result["cash_before_buy"] = np.nan
    result["cash_after_buy"] = np.nan
    result["total_asset_before_buy"] = np.nan
    result["exit_type"] = ""
    result["exit_date"] = ""
    result["exit_price"] = np.nan
    result["exit_return"] = np.nan
    result["holding_days"] = 0
    result["realized_pnl"] = np.nan

    grouped_signals = {
        key: sub.index.tolist()
        for key, sub in result.groupby("entry_date", sort=True)
    }
    all_dates = sorted({dt for frame in symbol_frames.values() for dt in frame.index})
    if not all_dates:
        return result

    cash = float(bt_cfg.initial_capital)
    positions: Dict[str, Position] = {}

    for trade_dt in all_dates:
        day_signals = grouped_signals.get(pd.Timestamp(trade_dt), [])
        if day_signals:
            for idx in day_signals:
                row = result.loc[idx]
                code = str(row["code"])
                entry_open = float(row["entry_open"])
                result.at[idx, "cash_before_buy"] = round(cash, 2)

                if not bool(row.get("profit_target_valid", True)):
                    result.at[idx, "skip_reason"] = "价格超出止盈止损"
                    continue
                if code in positions:
                    result.at[idx, "skip_reason"] = "已持有"
                    continue

                total_asset = cash
                for held_code, pos in positions.items():
                    held_frame = symbol_frames.get(held_code)
                    if held_frame is None or trade_dt not in held_frame.index:
                        total_asset += pos.shares * pos.entry_price
                    else:
                        total_asset += pos.shares * float(held_frame.loc[trade_dt, "Open"])

                # --- 资金管理与买入逻辑 ---
                # 计算本次拟买入金额：基于初始总资金的一个比例 (例如 5%)
                buy_amount = bt_cfg.initial_capital * bt_cfg.max_buy_pct
                
                # 计算可买股数 (向下取整)
                shares = int(buy_amount // entry_open)
                result.at[idx, "total_asset_before_buy"] = round(total_asset, 2)

                # 检查现金是否足够执行该笔交易
                if shares <= 0 or cash + 1e-9 < shares * entry_open:
                    result.at[idx, "skip_reason"] = "资金不足"
                    continue

                # 执行交易，更新现金余额
                trade_cost = shares * entry_open
                cash -= trade_cost
                positions[code] = Position(
                    code=code,
                    name=str(row["name"]),
                    shares=shares,
                    entry_date=pd.Timestamp(row["entry_date"]).strftime("%Y-%m-%d"),
                    entry_price=entry_open,
                    breakout_price=float(row["breakout_price"]),
                    take_profit=float(row["take_profit"]),
                    stop_loss=float(row["stop_loss"]),
                )
                result.at[idx, "buy_executed"] = True
                result.at[idx, "buy_amount"] = round(trade_cost, 2)
                result.at[idx, "shares"] = shares
                result.at[idx, "cash_after_buy"] = round(cash, 2)

        closed_codes: List[str] = []
        for code, pos in positions.items():
            frame = symbol_frames.get(code)
            if frame is None or trade_dt not in frame.index:
                continue
            day_bar = frame.loc[trade_dt]
            holding_days = _count_holding_days(frame, pd.Timestamp(pos.entry_date), pd.Timestamp(trade_dt))

            day_low = float(day_bar["Low"])
            day_high = float(day_bar["High"])

            exit_type = ""
            exit_price = np.nan
            if day_low <= pos.stop_loss:
                exit_type = "止损"
                exit_price = pos.stop_loss
            elif day_high >= pos.take_profit:
                exit_type = "止盈" if pos.take_profit > pos.entry_price else "目标达成(未盈利)"
                exit_price = pos.take_profit
            if not exit_type:
                continue

            proceeds = pos.shares * float(exit_price)
            cash += proceeds
            closed_codes.append(code)

            mask = (
                (result["code"] == code)
                & result["buy_executed"].astype(bool)
                & (result["exit_type"] == "")
            )
            if not mask.any():
                continue
            open_idx = result[mask].index[0]
            result.at[open_idx, "exit_type"] = exit_type
            result.at[open_idx, "exit_date"] = pd.Timestamp(trade_dt).strftime("%Y-%m-%d")
            result.at[open_idx, "exit_price"] = round(float(exit_price), 4)
            result.at[open_idx, "exit_return"] = round(float(exit_price) / pos.entry_price - 1.0, 6)
            result.at[open_idx, "holding_days"] = holding_days
            result.at[open_idx, "realized_pnl"] = round(proceeds - pos.shares * pos.entry_price, 2)

        for code in closed_codes:
            positions.pop(code, None)

    last_date = max(all_dates)
    for code, pos in positions.items():
        frame = symbol_frames.get(code)
        if frame is None or frame.empty:
            continue
        mark_date = last_date if last_date in frame.index else frame.index.max()
        exit_price = float(frame.loc[mark_date, "Close"])
        proceeds = pos.shares * exit_price
        mask = (
            (result["code"] == code)
            & result["buy_executed"].astype(bool)
            & (result["exit_type"] == "")
        )
        if not mask.any():
            continue
        open_idx = result[mask].index[0]
        entry_dt = pd.Timestamp(result.at[open_idx, "entry_date"])
        result.at[open_idx, "exit_type"] = "待离场"
        result.at[open_idx, "exit_date"] = pd.Timestamp(mark_date).strftime("%Y-%m-%d")
        result.at[open_idx, "exit_price"] = round(exit_price, 4)
        result.at[open_idx, "exit_return"] = round(exit_price / pos.entry_price - 1.0, 6)
        result.at[open_idx, "holding_days"] = _count_holding_days(frame, entry_dt, pd.Timestamp(mark_date))
        result.at[open_idx, "realized_pnl"] = round(proceeds - pos.shares * pos.entry_price, 2)

    result["signal_date"] = result["signal_date"].dt.strftime("%Y-%m-%d")
    result["entry_date"] = result["entry_date"].dt.strftime("%Y-%m-%d")
    return result


def _attach_independent_signal_outcomes(
    result: pd.DataFrame,
    symbol_frames: Dict[str, pd.DataFrame],
) -> pd.DataFrame:
    if result is None or result.empty:
        return result

    enriched = result.copy()
    enriched["quality_exit_type"] = ""
    enriched["quality_exit_date"] = ""
    enriched["quality_exit_price"] = np.nan
    enriched["quality_exit_return"] = np.nan
    enriched["quality_holding_days"] = 0

    valid_mask = pd.Series(True, index=enriched.index)
    if "profit_target_valid" in enriched.columns:
        valid_mask &= enriched["profit_target_valid"].astype(bool)

    for idx in enriched[valid_mask].index:
        row = enriched.loc[idx]
        code = str(row["code"])
        frame = symbol_frames.get(code)
        if frame is None or frame.empty:
            continue

        entry_dt = pd.Timestamp(row["entry_date"])
        if entry_dt not in frame.index:
            continue

        entry_open = pd.to_numeric(pd.Series([row["entry_open"]]), errors="coerce").iloc[0]
        take_profit = pd.to_numeric(pd.Series([row["take_profit"]]), errors="coerce").iloc[0]
        stop_loss = pd.to_numeric(pd.Series([row["stop_loss"]]), errors="coerce").iloc[0]
        if pd.isna(entry_open) or pd.isna(take_profit) or pd.isna(stop_loss) or float(entry_open) <= 0:
            continue
        future_frame = frame.loc[frame.index >= entry_dt]
        if future_frame.empty:
            continue

        exit_type = "待离场"
        exit_dt = future_frame.index[-1]
        exit_price = float(future_frame.iloc[-1]["Close"])
        holding_days = _count_holding_days(frame, entry_dt, pd.Timestamp(exit_dt))

        for trade_dt, day_bar in future_frame.iterrows():
            day_low = float(day_bar["Low"])
            day_high = float(day_bar["High"])
            if day_low <= float(stop_loss):
                exit_type = "止损"
                exit_dt = trade_dt
                exit_price = float(stop_loss)
                holding_days = _count_holding_days(frame, entry_dt, pd.Timestamp(trade_dt))
                break
            if day_high >= float(take_profit):
                exit_type = "止盈" if float(take_profit) > float(entry_open) else "目标达成(未盈利)"
                exit_dt = trade_dt
                exit_price = float(take_profit)
                holding_days = _count_holding_days(frame, entry_dt, pd.Timestamp(trade_dt))
                break
        enriched.at[idx, "quality_exit_type"] = exit_type
        enriched.at[idx, "quality_exit_date"] = pd.Timestamp(exit_dt).strftime("%Y-%m-%d")
        enriched.at[idx, "quality_exit_price"] = round(float(exit_price), 4)
        enriched.at[idx, "quality_exit_return"] = round(float(exit_price) / float(entry_open) - 1.0, 6)
        enriched.at[idx, "quality_holding_days"] = int(holding_days)

    return enriched


def _count_holding_days(frame: pd.DataFrame, entry_dt: pd.Timestamp, exit_dt: pd.Timestamp) -> int:
    if frame is None or frame.empty:
        return 0
    try:
        return int(len(frame.loc[(frame.index >= entry_dt) & (frame.index <= exit_dt)]))
    except Exception:
        return max((exit_dt - entry_dt).days + 1, 1)


def _parse_positive_int(value: Any, default: int = 0) -> int:
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric):
        return int(default)
    return max(int(numeric), 0)


def _matches_dynamic_neckline_breakout(
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
) -> PatternScanOutcome:
    return match_ultra_short_hot_breakout(
        frame=frame,
        pattern_name=pattern_name,
        max_ab_gap_ratio=max_ab_gap_ratio,
        low_ratio_threshold=low_ratio_threshold,
        min_breakout_over_d_ratio=min_breakout_over_d_ratio,
        prebreakout_gap_ratio=prebreakout_gap_ratio,
        watchlist_min_d_age_ratio=watchlist_min_d_age_ratio,
        watchlist_min_rebound_position_ratio=watchlist_min_rebound_position_ratio,
        watchlist_max_rebound_position_ratio=watchlist_max_rebound_position_ratio,
        local_extrema_neighbor_days=local_extrema_neighbor_days,
        min_ac_amplitude_ratio=min_ac_amplitude_ratio,
        min_bd_amplitude_ratio=min_bd_amplitude_ratio,
        min_b_peak_prominence_ratio=min_b_peak_prominence_ratio,
        post_d_peak_neighbor_days=post_d_peak_neighbor_days,
        pullback_confirm_lookback_days=pullback_confirm_lookback_days,
        pattern_detail_cls=PatternDetail,
        build_scan_outcome=PatternScanOutcome,
        is_local_peak=_is_local_peak,
        is_local_trough=_is_local_trough,
        calc_neckline_price=_calc_neckline_price,
        passes_neckline_breakout=_passes_neckline_breakout,
        detect_neckline_pullback=_detect_neckline_pullback,
    )


def _matches_neckline_breakout(
    frame: pd.DataFrame,
    pattern_name: str,
    b_window_days: int,
    recent_low_window_days: int,
    max_ab_gap_ratio: float,
    low_ratio_threshold: float,
    prebreakout_gap_ratio: float,
    watchlist_min_d_age_ratio: float,
    watchlist_min_rebound_position_ratio: float,
    watchlist_max_rebound_position_ratio: float,
    local_extrema_neighbor_days: int = 1,
    min_ac_amplitude_ratio: float = 0.0,
    min_bd_amplitude_ratio: float = 0.0,
    min_b_peak_prominence_ratio: float = 0.0,
    post_d_peak_neighbor_days: int = 0,
    min_breakout_over_d_ratio: float = 0.0,
    pullback_confirm_lookback_days: int = 10,
) -> "PatternScanOutcome":
    if frame is None or frame.empty or len(frame) < max(b_window_days if b_window_days > 0 else 10, recent_low_window_days if recent_low_window_days > 0 else 10, 10):
        return PatternScanOutcome(matched=None, watch=None)
    if "Close" not in frame.columns or "High" not in frame.columns or "Low" not in frame.columns:
        return PatternScanOutcome(matched=None, watch=None)

    frame = frame.copy()
    frame["Close"] = pd.to_numeric(frame["Close"], errors="coerce")
    frame["High"] = pd.to_numeric(frame["High"], errors="coerce")
    frame["Low"] = pd.to_numeric(frame["Low"], errors="coerce")
    frame = frame.dropna(subset=["Close", "High", "Low"])
    if len(frame) < max(b_window_days if b_window_days > 0 else 10, recent_low_window_days if recent_low_window_days > 0 else 10, 10):
        return PatternScanOutcome(matched=None, watch=None)

    if b_window_days <= 0 and recent_low_window_days <= 0:
        return _matches_dynamic_neckline_breakout(
            frame=frame,
            pattern_name=pattern_name,
            max_ab_gap_ratio=max_ab_gap_ratio,
            low_ratio_threshold=low_ratio_threshold,
            min_breakout_over_d_ratio=min_breakout_over_d_ratio,
            prebreakout_gap_ratio=prebreakout_gap_ratio,
            watchlist_min_d_age_ratio=watchlist_min_d_age_ratio,
            watchlist_min_rebound_position_ratio=watchlist_min_rebound_position_ratio,
            watchlist_max_rebound_position_ratio=watchlist_max_rebound_position_ratio,
            local_extrema_neighbor_days=local_extrema_neighbor_days,
            min_ac_amplitude_ratio=min_ac_amplitude_ratio,
            min_bd_amplitude_ratio=min_bd_amplitude_ratio,
            min_b_peak_prominence_ratio=min_b_peak_prominence_ratio,
            post_d_peak_neighbor_days=post_d_peak_neighbor_days,
            pullback_confirm_lookback_days=pullback_confirm_lookback_days,
        )

    closes = frame["Close"]
    highs = frame["High"]
    lows = frame["Low"]
    latest_idx = len(frame) - 1
    latest_e_price = float(closes.iloc[latest_idx])
    latest_e_high = float(highs.iloc[latest_idx])
    if latest_e_price <= 0:
        return PatternScanOutcome(matched=None, watch=None)

    # A点排除当天，避免突破日最高价抢占A点
    a_idx = int(highs.iloc[:-1].values.argmax())
    point_a_price = float(highs.iloc[a_idx])

    # b_window_days=0: B点在A之后、当天之前的范围内找最高点
    if b_window_days > 0:
        b_start = len(closes) - b_window_days
        b_idx = b_start + int(highs.iloc[-b_window_days:].values.argmax())
    else:
        b_search = highs.iloc[a_idx + 1:latest_idx]
        if b_search.empty:
            return PatternScanOutcome(matched=None, watch=None)
        b_idx = a_idx + 1 + int(b_search.values.argmax())
    point_b_price = float(highs.iloc[b_idx])
    if b_idx <= a_idx:
        return PatternScanOutcome(matched=None, watch=None)
    if not point_a_price > point_b_price:
        return PatternScanOutcome(matched=None, watch=None)
    if point_b_price <= 0 or (point_a_price / point_b_price - 1.0) > max_ab_gap_ratio:
        return PatternScanOutcome(matched=None, watch=None)
    if not _is_local_peak(highs, a_idx, local_extrema_neighbor_days):
        return PatternScanOutcome(matched=None, watch=None)
    if not _is_local_peak(highs, b_idx, local_extrema_neighbor_days):
        return PatternScanOutcome(matched=None, watch=None)

    c_idx = int(lows.values.argmin())
    point_c_price = float(lows.iloc[c_idx])

    # recent_low_window_days=0: D点在B之后的全窗口内找最低点
    if recent_low_window_days > 0:
        d_start = len(closes) - recent_low_window_days
        d_idx = d_start + int(lows.iloc[-recent_low_window_days:].values.argmin())
    else:
        d_search = lows.iloc[b_idx + 1:]
        if d_search.empty:
            return PatternScanOutcome(matched=None, watch=None)
        d_idx = b_idx + 1 + int(d_search.values.argmin())
    point_d_price = float(lows.iloc[d_idx])
    if point_c_price <= 0 or point_d_price <= 0:
        return PatternScanOutcome(matched=None, watch=None)
    # low_ratio_threshold=0时只要求D高于C（右底不低于左底）；否则限制C-D偏差
    if low_ratio_threshold > 0:
        if abs(point_c_price / point_d_price - 1.0) >= low_ratio_threshold:
            return PatternScanOutcome(matched=None, watch=None)
    else:
        if point_d_price <= point_c_price:
            return PatternScanOutcome(matched=None, watch=None)
    if not _is_local_trough(lows, c_idx, local_extrema_neighbor_days):
        return PatternScanOutcome(matched=None, watch=None)
    if not _is_local_trough(lows, d_idx, local_extrema_neighbor_days):
        return PatternScanOutcome(matched=None, watch=None)
    if not (a_idx < c_idx < b_idx < d_idx < latest_idx):
        return PatternScanOutcome(matched=None, watch=None)

    # 振幅检查：AC段（A高到C低）和BD段（B高到D低）需满足最小振幅
    if min_ac_amplitude_ratio > 0:
        ac_amplitude = (point_a_price - point_c_price) / point_a_price
        if ac_amplitude < min_ac_amplitude_ratio:
            return PatternScanOutcome(matched=None, watch=None)
    if min_bd_amplitude_ratio > 0:
        bd_amplitude = (point_b_price - point_d_price) / point_b_price
        if bd_amplitude < min_bd_amplitude_ratio:
            return PatternScanOutcome(matched=None, watch=None)

    neckline_price = _calc_neckline_price(
        point_a_price=point_a_price,
        point_b_price=point_b_price,
        a_idx=a_idx,
        b_idx=b_idx,
        latest_idx=latest_idx,
    )
    detail = PatternDetail(
        pattern_name=pattern_name,
        point_a_date=pd.Timestamp(frame.index[a_idx]).strftime("%Y-%m-%d"),
        point_b_date=pd.Timestamp(frame.index[b_idx]).strftime("%Y-%m-%d"),
        point_c_date=pd.Timestamp(frame.index[c_idx]).strftime("%Y-%m-%d"),
        point_d_date=pd.Timestamp(frame.index[d_idx]).strftime("%Y-%m-%d"),
        point_a_price=point_a_price,
        point_b_price=point_b_price,
        point_c_price=point_c_price,
        point_d_price=point_d_price,
        breakout_price=round(neckline_price, 4),
        e_close=latest_e_price,
        e_high=latest_e_high,
    )

    confirmed_breakout = _passes_neckline_breakout(
        closes=closes,
        d_idx=d_idx,
        point_d_price=point_d_price,
        latest_idx=latest_idx,
        latest_e_price=latest_e_price,
        breakout_price=detail.breakout_price,
    )
    if confirmed_breakout:
        pullback = _detect_neckline_pullback(
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
        return PatternScanOutcome(matched=detail, watch=None)

    d_age_days = latest_idx - d_idx
    min_d_age_days = max(int(np.ceil(len(frame) * max(float(watchlist_min_d_age_ratio), 0.0))), 1)
    bd_span = point_b_price - point_d_price
    rebound_position_ratio = np.inf
    if bd_span > 0:
        rebound_position_ratio = (latest_e_price - point_d_price) / bd_span

    if (
        detail.close_gap_ratio >= 0
        and detail.close_gap_ratio <= prebreakout_gap_ratio
        and d_age_days >= min_d_age_days
        and latest_e_price > point_d_price
        and rebound_position_ratio >= float(watchlist_min_rebound_position_ratio)
        and rebound_position_ratio <= float(watchlist_max_rebound_position_ratio)
    ):
        return PatternScanOutcome(matched=None, watch=detail)
    return PatternScanOutcome(matched=None, watch=None)


def _is_local_peak(series: pd.Series, idx: int, neighbor_days: int) -> bool:
    neighbor_days = max(int(neighbor_days), 0)
    if neighbor_days == 0:
        return True
    if idx - neighbor_days < 0 or idx + neighbor_days >= len(series):
        return False
    value = float(series.iloc[idx])
    left = pd.to_numeric(series.iloc[idx - neighbor_days : idx], errors="coerce")
    right = pd.to_numeric(series.iloc[idx + 1 : idx + neighbor_days + 1], errors="coerce")
    neighbors = pd.concat([left, right]).dropna()
    return len(neighbors) == neighbor_days * 2 and bool((neighbors < value).all())


def _is_local_trough(series: pd.Series, idx: int, neighbor_days: int) -> bool:
    neighbor_days = max(int(neighbor_days), 0)
    if neighbor_days == 0:
        return True
    if idx - neighbor_days < 0 or idx + neighbor_days >= len(series):
        return False
    value = float(series.iloc[idx])
    left = pd.to_numeric(series.iloc[idx - neighbor_days : idx], errors="coerce")
    right = pd.to_numeric(series.iloc[idx + 1 : idx + neighbor_days + 1], errors="coerce")
    neighbors = pd.concat([left, right]).dropna()
    return len(neighbors) == neighbor_days * 2 and bool((neighbors > value).all())


def _calc_neckline_price(
    point_a_price: float,
    point_b_price: float,
    a_idx: int,
    b_idx: int,
    latest_idx: int,
) -> float:
    slope = (point_b_price - point_a_price) / (b_idx - a_idx)
    return point_a_price + slope * (latest_idx - a_idx)


def _passes_neckline_breakout(
    closes: pd.Series,
    d_idx: int,
    point_d_price: float,
    latest_idx: int,
    latest_e_price: float,
    breakout_price: float,
) -> bool:
    """Returns True when close-based breakout is confirmed."""
    if d_idx >= latest_idx:
        return False
    de_segment = closes.iloc[d_idx : latest_idx + 1]
    if len(de_segment) < 2:
        return False
    prev_close = float(closes.iloc[latest_idx - 1])
    if latest_e_price <= point_d_price:
        return False
    if latest_e_price <= prev_close:
        return False
    if latest_e_price < float(de_segment.max()):
        return False
    return latest_e_price > breakout_price


def _detect_neckline_pullback(
    frame: pd.DataFrame,
    breakout_price: float,
    d_idx: int,
    latest_idx: int,
    lookback_days: int = 10,
) -> Optional[dict]:
    if frame is None or frame.empty or breakout_price <= 0:
        return None
    if "Low" not in frame.columns or "Close" not in frame.columns:
        return None

    lows = pd.to_numeric(frame["Low"], errors="coerce")
    closes = pd.to_numeric(frame["Close"], errors="coerce")
    breakout_day_idx: Optional[int] = None
    for idx in range(d_idx + 1, latest_idx + 1):
        close = pd.to_numeric(closes.iloc[idx], errors="coerce")
        if pd.notna(close) and float(close) > breakout_price:
            breakout_day_idx = idx
            break
    if breakout_day_idx is None or breakout_day_idx >= latest_idx:
        return None

    search_start = breakout_day_idx + 1
    search_end = min(search_start + max(int(lookback_days), 1), latest_idx + 1)
    for idx in range(search_start, search_end):
        low = pd.to_numeric(lows.iloc[idx], errors="coerce")
        close = pd.to_numeric(closes.iloc[idx], errors="coerce")
        if pd.isna(low) or pd.isna(close):
            continue
        if float(low) <= breakout_price and float(close) >= breakout_price:
            return {
                "date": pd.Timestamp(frame.index[idx]).strftime("%Y-%m-%d"),
                "low": float(low),
                "close": float(close),
            }
    return None


def _build_backtest_summary(
    result: pd.DataFrame,
    universe_size: int,
    skipped_no_data: int,
    skipped_short_history: int,
    cfg: BacktestConfig,
) -> dict:
    summary: dict = {
        "start_date": cfg.start_date,
        "end_date": cfg.end_date,
        "board_filter": cfg.board_filter or "all",
        "initial_capital": round(cfg.initial_capital, 2),
        "max_buy_pct": cfg.max_buy_pct,
        "max_buy_amount_calculated": round(cfg.initial_capital * cfg.max_buy_pct, 2),
        "entry_gap_limit": cfg.entry_gap_limit,
        "universe_size": int(universe_size),
        "skipped_no_data": int(skipped_no_data),
        "skipped_short_history": int(skipped_short_history),
        "signal_count": 0,
        "unique_signal_codes": 0,
        "metrics": {},
    }
    if result is None or result.empty:
        return summary

    summary["signal_count"] = int(len(result))
    summary["unique_signal_codes"] = int(result["code"].nunique())

    valid_mask = result["entry_valid"].astype(bool) if "entry_valid" in result.columns else pd.Series([True] * len(result))
    target_valid_mask = result["profit_target_valid"].astype(bool) if "profit_target_valid" in result.columns else pd.Series([True] * len(result))
    executed = result[result.get("buy_executed", False).astype(bool)].copy() if "buy_executed" in result.columns else pd.DataFrame()
    summary["entry_valid_count"] = int(valid_mask.sum())
    summary["entry_skipped_count"] = int((~valid_mask).sum())
    summary["profit_target_valid_count"] = int(target_valid_mask.sum())
    summary["profit_target_skipped_count"] = int((~target_valid_mask).sum())
    summary["buy_executed_count"] = int(len(executed))
    quality_valid_mask = valid_mask & target_valid_mask
    summary["quality_signal_count"] = int(quality_valid_mask.sum())
    summary["skip_reason_counts"] = {}
    if "skip_reason" in result.columns:
        skip_counts = result.loc[result["skip_reason"].astype(str) != "", "skip_reason"].value_counts().to_dict()
        summary["skip_reason_counts"] = {str(k): int(v) for k, v in skip_counts.items()}

    quality_rows = pd.DataFrame()
    if "quality_exit_type" in result.columns and "quality_exit_return" in result.columns:
        quality_rows = result[quality_valid_mask].copy()
        quality_rows["quality_exit_return_num"] = pd.to_numeric(quality_rows["quality_exit_return"], errors="coerce")
        quality_rows = quality_rows.dropna(subset=["quality_exit_return_num"])

    summary["quality_metrics"] = {}
    if not quality_rows.empty:
        quality_realized = quality_rows[quality_rows["quality_exit_type"].astype(str).str.strip() != "待离场"].copy()
        quality_profit_count = int((quality_realized["quality_exit_return_num"] > 0).sum())
        quality_loss_count = int((quality_realized["quality_exit_return_num"] < 0).sum())
        quality_flat_count = int((quality_realized["quality_exit_return_num"] == 0).sum())
        quality_sample_size = quality_profit_count + quality_loss_count
        quality_win_rate = round(float(quality_profit_count / quality_sample_size), 4) if quality_sample_size else None
        quality_holding_days = pd.to_numeric(quality_rows["quality_holding_days"], errors="coerce").dropna()
        summary["quality_metrics"] = {
            "evaluated_signals": int(len(quality_rows)),
            "take_profit_count": int((quality_rows["quality_exit_type"] == "止盈").sum()),
            "stop_loss_count": int((quality_rows["quality_exit_type"] == "止损").sum()),
            "pending_count": int((quality_rows["quality_exit_type"] == "待离场").sum()),
            "target_hit_nonprofit_count": int((quality_rows["quality_exit_type"] == "目标达成(未盈利)").sum()),
            "sample_size": int(quality_sample_size),
            "profit_count": quality_profit_count,
            "loss_count": quality_loss_count,
            "flat_count": quality_flat_count,
            "win_rate": quality_win_rate,
            "avg_return": round(float(quality_rows["quality_exit_return_num"].mean()), 6),
            "median_return": round(float(quality_rows["quality_exit_return_num"].median()), 6),
            "avg_holding_days": round(float(quality_holding_days.mean()), 1) if len(quality_holding_days) else None,
        }

    if not executed.empty:
        ret_series = pd.to_numeric(executed["exit_return"], errors="coerce").dropna()
        tp_count = int((executed["exit_type"] == "止盈").sum())
        sl_count = int((executed["exit_type"] == "止损").sum())
        pending_count = int((executed["exit_type"] == "待离场").sum())
        target_hit_nonprofit_count = int((executed["exit_type"] == "目标达成(未盈利)").sum())
        realized = executed[executed["exit_type"].astype(str).str.strip() != "待离场"].copy()
        realized["exit_return_num"] = pd.to_numeric(realized["exit_return"], errors="coerce")
        realized = realized.dropna(subset=["exit_return_num"])
        realized_series = realized["exit_return_num"]
        profit_count = int((realized["exit_return_num"] > 0).sum())
        loss_count = int((realized["exit_return_num"] < 0).sum())
        flat_count = int((realized["exit_return_num"] == 0).sum())
        win_rate = round(float(profit_count / (profit_count + loss_count)), 4) if (profit_count + loss_count) else None
        pnl_series = pd.to_numeric(executed["realized_pnl"], errors="coerce").dropna()
        final_asset = float(cfg.initial_capital + pnl_series.sum()) if len(pnl_series) else float(cfg.initial_capital)
        summary["final_asset"] = round(final_asset, 2)
        summary["total_return"] = round(final_asset / cfg.initial_capital - 1.0, 6)
        summary["metrics"] = {
            "executed_trades": int(len(executed)),
            "take_profit_count": tp_count,
            "stop_loss_count": sl_count,
            "pending_count": pending_count,
            "target_hit_nonprofit_count": target_hit_nonprofit_count,
            "sample_size": int(len(realized_series)),
            "profit_count": profit_count,
            "loss_count": loss_count,
            "flat_count": flat_count,
            "win_rate": win_rate,
            "avg_return": round(float(ret_series.mean()), 6) if len(ret_series) else None,
            "median_return": round(float(ret_series.median()), 6) if len(ret_series) else None,
        }
        hd = pd.to_numeric(executed["holding_days"], errors="coerce").dropna()
        summary["metrics"]["avg_holding_days"] = round(float(hd.mean()), 1) if len(hd) else None
    else:
        summary["final_asset"] = round(cfg.initial_capital, 2)
        summary["total_return"] = 0.0

    summary["overview"] = {
        "signal_count": int(summary.get("signal_count", 0)),
        "unique_signal_codes": int(summary.get("unique_signal_codes", 0)),
        "universe_size": int(summary.get("universe_size", 0)),
        "entry_valid_count": int(summary.get("entry_valid_count", 0)),
        "entry_skipped_count": int(summary.get("entry_skipped_count", 0)),
        "profit_target_valid_count": int(summary.get("profit_target_valid_count", 0)),
        "profit_target_skipped_count": int(summary.get("profit_target_skipped_count", 0)),
        "skipped_no_data": int(summary.get("skipped_no_data", 0)),
        "skipped_short_history": int(summary.get("skipped_short_history", 0)),
    }
    summary["portfolio_backtest"] = {
        "initial_capital": round(float(summary.get("initial_capital", cfg.initial_capital)), 2),
        "max_buy_pct": cfg.max_buy_pct,
        "max_buy_amount": round(cfg.initial_capital * cfg.max_buy_pct, 2),
        "buy_executed_count": int(summary.get("buy_executed_count", 0)),
        "skip_reason_counts": summary.get("skip_reason_counts", {}),
        "final_asset": round(float(summary.get("final_asset", cfg.initial_capital)), 2),
        "total_return": round(float(summary.get("total_return", 0.0)), 6),
        "metrics": summary.get("metrics", {}),
    }
    summary["signal_quality"] = {
        "quality_signal_count": int(summary.get("quality_signal_count", 0)),
        "quality_metrics": summary.get("quality_metrics", {}),
    }
    return summary


def _save_backtest_outputs(result: pd.DataFrame, summary: dict, cfg: BacktestConfig) -> None:
    board_folder = os.path.join(
        cfg.output_dir,
        f"{cfg.start_date}_{cfg.end_date}",
        cfg.board_filter or "all",
    )
    _write_backtest_outputs_to_folder(result=result, summary=summary, folder=board_folder)
    _write_backtest_pattern_files(
        result=result,
        summary=summary,
        folder=board_folder,
        pattern_name=ULTRA_SHORT_PATTERN_NAME,
        file_stem=ULTRA_SHORT_FILE_STEM,
        cfg=cfg,
    )

    if not cfg.enabled_patterns:
        return

    for pattern_name in cfg.enabled_patterns:
        period_folder = os.path.join(board_folder, _backtest_period_folder_name(pattern_name))
        if result is None or result.empty or "pattern_name" not in result.columns:
            period_result = pd.DataFrame(columns=result.columns if result is not None else None)
        else:
            period_result = result[result["pattern_name"].astype(str) == str(pattern_name)].copy()
        period_summary = _build_backtest_summary(
            result=period_result,
            universe_size=summary.get("universe_size", 0),
            skipped_no_data=summary.get("skipped_no_data", 0),
            skipped_short_history=summary.get("skipped_short_history", 0),
            cfg=cfg,
        )
        period_summary["pattern_scope"] = pattern_name
        _write_backtest_outputs_to_folder(
            result=period_result,
            summary=period_summary,
            folder=period_folder,
        )


def _backtest_period_folder_name(pattern_name: str) -> str:
    normalized = str(pattern_name).strip()
    if normalized in BACKTEST_PERIOD_FOLDERS:
        return BACKTEST_PERIOD_FOLDERS[normalized]
    sanitized = re.sub(r"[\\\\/:*?\"<>|]+", "_", normalized)
    return sanitized or "未分类"


def _write_backtest_outputs_to_folder(result: pd.DataFrame, summary: dict, folder: str) -> None:
    os.makedirs(folder, exist_ok=True)
    result_path = os.path.join(folder, "signals.csv")
    summary_path = os.path.join(folder, "summary.json")
    if result is None or result.empty:
        pd.DataFrame().to_csv(result_path, index=False)
    else:
        # 创建拷贝并转换数值/布尔值为中文描述
        df_to_save = result.sort_values(["signal_date", "code"]).copy()
        
        if "entry_valid" in df_to_save.columns:
            df_to_save["entry_valid"] = df_to_save["entry_valid"].map({True: "入场有效", False: "入场无效"})
        if "profit_target_valid" in df_to_save.columns:
            df_to_save["profit_target_valid"] = df_to_save["profit_target_valid"].map({True: "价格目标有效", False: "价格目标无效"})
        if "buy_executed" in df_to_save.columns:
            df_to_save["buy_executed"] = df_to_save["buy_executed"].map({True: "买入", False: "未买入"})
        if "pullback_confirmed" in df_to_save.columns:
            df_to_save["pullback_confirmed"] = df_to_save["pullback_confirmed"].map({True: "是", False: "否"})

        # 转换为中文标题并保存
        df_to_save = df_to_save.rename(columns=CSV_COLUMN_MAPPING)
        df_to_save.to_csv(result_path, index=False)
    with open(summary_path, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)


def _write_backtest_pattern_files(
    result: pd.DataFrame,
    summary: dict,
    folder: str,
    pattern_name: str,
    file_stem: str,
    cfg: BacktestConfig,
) -> None:
    os.makedirs(folder, exist_ok=True)
    result_path = os.path.join(folder, f"signals_{file_stem}.csv")
    summary_path = os.path.join(folder, f"summary_{file_stem}.json")
    if result is None or result.empty or "pattern_name" not in result.columns:
        period_result = pd.DataFrame(columns=result.columns if result is not None else None)
    else:
        period_result = result[result["pattern_name"].astype(str).str.strip() == str(pattern_name).strip()].copy()

    if period_result.empty:
        pd.DataFrame().to_csv(result_path, index=False)
    else:
        df_to_save = period_result.sort_values(["signal_date", "code"]).copy()
        if "entry_valid" in df_to_save.columns:
            df_to_save["entry_valid"] = df_to_save["entry_valid"].map({True: "入场有效", False: "入场无效"})
        if "profit_target_valid" in df_to_save.columns:
            df_to_save["profit_target_valid"] = df_to_save["profit_target_valid"].map({True: "价格目标有效", False: "价格目标无效"})
        if "buy_executed" in df_to_save.columns:
            df_to_save["buy_executed"] = df_to_save["buy_executed"].map({True: "买入", False: "未买入"})
        if "pullback_confirmed" in df_to_save.columns:
            df_to_save["pullback_confirmed"] = df_to_save["pullback_confirmed"].map({True: "是", False: "否"})
        df_to_save = df_to_save.rename(columns=CSV_COLUMN_MAPPING)
        df_to_save.to_csv(result_path, index=False)

    period_summary = _build_backtest_summary(
        result=period_result,
        universe_size=summary.get("universe_size", 0),
        skipped_no_data=summary.get("skipped_no_data", 0),
        skipped_short_history=summary.get("skipped_short_history", 0),
        cfg=cfg,
    )
    period_summary["pattern_scope"] = pattern_name
    with open(summary_path, "w", encoding="utf-8") as handle:
        json.dump(period_summary, handle, ensure_ascii=False, indent=2)


def _print_backtest_summary(summary: dict, cfg: BacktestConfig) -> None:
    overview = summary.get("overview", {})
    portfolio = summary.get("portfolio_backtest", {})
    signal_quality = summary.get("signal_quality", {})
    qm = signal_quality.get("quality_metrics", summary.get("quality_metrics", {}))
    m = portfolio.get("metrics", summary.get("metrics", {}))

    print("[回测总览]")
    print(
        f"信号总数={overview.get('signal_count', summary.get('signal_count', 0))} "
        f"覆盖股票数={overview.get('unique_signal_codes', summary.get('unique_signal_codes', 0))} "
        f"股票池={overview.get('universe_size', summary.get('universe_size', 0))}"
    )
    print(
        f"跳过无数据={overview.get('skipped_no_data', summary.get('skipped_no_data', 0))} "
        f"跳过历史不足={overview.get('skipped_short_history', summary.get('skipped_short_history', 0))}"
    )
    print(
        f"入场记录有效={overview.get('entry_valid_count', summary.get('entry_valid_count', 0))} "
        f"入场记录无效={overview.get('entry_skipped_count', summary.get('entry_skipped_count', 0))}"
    )
    print(
        f"止盈止损价格有效={overview.get('profit_target_valid_count', summary.get('profit_target_valid_count', 0))} "
        f"止盈止损价格无效={overview.get('profit_target_skipped_count', summary.get('profit_target_skipped_count', 0))}"
    )

    print("[组合回测结果]")
    if portfolio.get("buy_executed_count", summary.get("buy_executed_count", 0)) > 0 or portfolio.get("skip_reason_counts", summary.get("skip_reason_counts", {})):
        print(
            f"实际买入={portfolio.get('buy_executed_count', summary.get('buy_executed_count', 0))} "
            f"跳过明细={portfolio.get('skip_reason_counts', summary.get('skip_reason_counts', {}))}"
        )
    if not m or not m.get("executed_trades"):
        print("无可统计的组合成交结果。")
    else:
        print(
            f"成交笔数={m.get('executed_trades', '?')} "
            f"止盈={m.get('take_profit_count', 0)} "
            f"止损={m.get('stop_loss_count', 0)} "
            f"目标达成但未盈利={m.get('target_hit_nonprofit_count', 0)} "
            f"待离场={m.get('pending_count', 0)}"
        )
        wr = m.get('win_rate')
        ar = m.get('avg_return')
        mr = m.get('median_return')
        hd = m.get('avg_holding_days')
        print(
            f"胜率={f'{wr:.2%}' if wr is not None else 'N/A'} "
            f"平均收益={f'{ar:.2%}' if ar is not None else 'N/A'} "
            f"中位收益={f'{mr:.2%}' if mr is not None else 'N/A'} "
            f"平均持仓天数={hd if hd is not None else 'N/A'}"
        )
    print(
        f"初始资金={portfolio.get('initial_capital', summary.get('initial_capital', cfg.initial_capital)):,.2f} "
        f"期末资产={portfolio.get('final_asset', summary.get('final_asset', cfg.initial_capital)):,.2f} "
        f"总收益={portfolio.get('total_return', summary.get('total_return', 0.0)):.2%}"
    )

    print("[信号质量结果]")
    if qm and qm.get("evaluated_signals"):
        qwr = qm.get("win_rate")
        qar = qm.get("avg_return")
        qmr = qm.get("median_return")
        qhd = qm.get("avg_holding_days")
        print(
            f"有效信号={signal_quality.get('quality_signal_count', summary.get('quality_signal_count', 0))} "
            f"独立评估={qm.get('evaluated_signals', 0)} "
            f"止盈={qm.get('take_profit_count', 0)} "
            f"止损={qm.get('stop_loss_count', 0)} "
            f"待离场={qm.get('pending_count', 0)}"
        )
        print(
            f"胜率={f'{qwr:.2%}' if qwr is not None else 'N/A'} "
            f"平均收益={f'{qar:.2%}' if qar is not None else 'N/A'} "
            f"中位收益={f'{qmr:.2%}' if qmr is not None else 'N/A'} "
            f"平均持仓天数={qhd if qhd is not None else 'N/A'}"
        )
    else:
        print("无可统计的独立信号质量结果。")

    print("[资金与风控]")
    print(
        f"初始资金={cfg.initial_capital:,.2f} "
        f"单次买入上限={cfg.max_buy_pct:.1%} ({cfg.initial_capital * cfg.max_buy_pct:,.0f}元) "
        f"同一股票不重复持有"
    )
    report_dir = os.path.join(
        cfg.output_dir,
        f"{cfg.start_date}_{cfg.end_date}",
        cfg.board_filter or "all",
    )
    print("[输出文件]")
    print(f"目录={report_dir}")


def download_kline_cache(
    board_filter: str | None = None,
    years: int = 2,
    max_workers: int | None = None,
) -> None:
    if max_workers is None:
        max_workers = int(_ENV.get("MAX_WORKERS", 4))
    """Bulk download kline data for all stocks and save to local cache."""
    universe = _load_universe(board_filter=board_filter, apply_spot_prefilter=False)
    if not universe:
        print("[K线缓存] 无可用股票池。")
        return

    lookback_days = int(years * 365)
    end_date = datetime.now()
    print(
        f"[K线缓存] 开始批量下载：股票数={len(universe)} "
        f"历史={years}年({lookback_days}天) "
        f"并发={max_workers}"
    )

    completed = 0
    failed = 0

    def _download_one(symbol_name: tuple[str, str]) -> bool:
        symbol, _ = symbol_name
        try:
            frame, reason = MarketData.from_akshare_with_reason(
                symbol, min_rows=1, end_date=end_date, lookback_days=lookback_days,
            )
            return not frame.empty
        except Exception:
            return False

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(_download_one, item): item for item in universe
        }
        for future in as_completed(future_map):
            completed += 1
            if not future.result():
                failed += 1
            if completed % 100 == 0:
                print(f"[K线缓存] 进度 {completed}/{len(universe)}，失败 {failed}")

    print(
        f"[K线缓存] 完成：总计={len(universe)} 成功={completed - failed} 失败={failed}\n"
        f"[K线缓存] 缓存目录：{MarketData._kline_cache_dir}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="A-share daily pattern scan tool")
    board_choices = ["all", MAIN_BOARD_ONLY, SH_MAIN_ONLY, SZ_MAIN_ONLY, CHINEXT_ONLY]
    parser.add_argument(
        "board",
        nargs="?",
        choices=board_choices,
        default="all",
        help="board scope to scan",
    )
    parser.add_argument("--date", help="target date in YYYY-MM-DD, default today")
    parser.add_argument(
        "--board",
        dest="legacy_board",
        choices=board_choices,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--cache-dir", default=os.path.join("data", "pattern_scan_cache"), help="cache directory")
    parser.add_argument("--max-workers", type=int, default=None, help="parallel workers")
    parser.add_argument(
        "--history-lookback-days",
        type=int,
        default=None,
        help="calendar days of history to fetch before target date",
    )
    parser.add_argument("--backtest-start", help="backtest start date in YYYY-MM-DD")
    parser.add_argument("--backtest-end", help="backtest end date in YYYY-MM-DD")
    parser.add_argument(
        "--backtest-output-dir",
        default=os.path.join("data", "backtest_reports"),
        help="output directory for backtest reports",
    )
    parser.add_argument(
        "--download-cache",
        action="store_true",
        help="bulk download kline data to local cache",
    )
    parser.add_argument(
        "--cache-years",
        type=int,
        default=2,
        help="years of history to cache when using --download-cache (default 2)",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    
    # 优先级: 命令行显式 Flag > .env 中的 EXEC_MODE
    mode = _ENV.get("EXEC_MODE", "scan").lower()
    if args.download_cache:
        mode = "download"
    elif args.backtest_start or args.backtest_end:
        mode = "backtest"
    
    board_value = args.legacy_board or args.board
    # 如果命令行没有指定板块(即为默认值'all')，且 .env 中有配置，则使用 .env 的配置
    if board_value == "all" and _ENV.get("BOARD"):
        board_value = _ENV.get("BOARD")
    
    board_filter = None if board_value == "all" else board_value
    
    if mode == "download":
        download_kline_cache(
            board_filter=board_filter,
            years=args.cache_years,
            max_workers=args.max_workers,
        )
    elif mode == "backtest":
        bt_start = args.backtest_start or _ENV.get("BACKTEST_START")
        bt_end = args.backtest_end or _ENV.get("BACKTEST_END")
        
        if not bt_start or not bt_end:
            parser.error("Backtest mode requires both START and END dates (via CLI or .env)")
            
        run_neckline_breakout_backtest(
            start_date=bt_start,
            end_date=bt_end,
            board_filter=board_filter,
            history_lookback_days=args.history_lookback_days,
            max_workers=args.max_workers,
            output_dir=args.backtest_output_dir,
        )
    else:
        # Default: scan mode
        trade_date = args.date or _ENV.get("DATE")
        run_neckline_breakout_scan(
            board_filter=board_filter,
            trade_date=trade_date,
            cache_dir=args.cache_dir,
            max_workers=args.max_workers,
            history_lookback_days=args.history_lookback_days,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
