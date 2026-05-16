from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel


class ScanRunOut(BaseModel):
    id: int
    scan_date: date
    board: Optional[str]
    source: str
    status: str
    error_message: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


class SignalOut(BaseModel):
    id: int
    signal_date: date
    strategy_name: str
    signal_type: str
    symbol: str
    name: Optional[str]
    close_price: Optional[float]
    high_price: Optional[float]
    breakout_price: Optional[float]
    stop_loss_price: Optional[float]
    take_profit_price: Optional[float]
    amount_rank: Optional[int]
    payload: Optional[Dict[str, Any]] = None

    model_config = {"from_attributes": True}


class KlineOut(BaseModel):
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = None
    amount: Optional[float] = None
    change_pct: Optional[float] = None


class PatternPointOut(BaseModel):
    label: str
    date: date
    price: float


class SignalChartOut(BaseModel):
    signal: SignalOut
    klines: list[KlineOut]
    points: list[PatternPointOut]
