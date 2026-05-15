from __future__ import annotations

import json
import csv
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.core.config import ROOT_DIR, settings
from app.core.database import get_db
from app.models import Signal
from app.schemas.signals import KlineOut, PatternPointOut, SignalChartOut, SignalOut
from app.services.auth import get_current_user


router = APIRouter(prefix="/signals", tags=["signals"], dependencies=[Depends(get_current_user)])


def to_signal_out(signal: Signal) -> SignalOut:
    data = SignalOut.model_validate(signal)
    if signal.payload_json:
        try:
            data.payload = json.loads(signal.payload_json)
        except json.JSONDecodeError:
            data.payload = {"raw": signal.payload_json}
    return data


def parse_float(value: object) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def parse_pattern_point(label: str, value: object) -> Optional[PatternPointOut]:
    text = str(value or "").strip()
    if not text or "(" not in text or ")" not in text:
        return None
    date_text, price_text = text.split("(", 1)
    price_text = price_text.split(")", 1)[0]
    price = parse_float(price_text)
    if price is None:
        return None
    try:
        return PatternPointOut(label=label, date=date.fromisoformat(date_text.strip()), price=price)
    except ValueError:
        return None


def signal_points(signal: Signal) -> list[PatternPointOut]:
    payload = {}
    if signal.payload_json:
        try:
            payload = json.loads(signal.payload_json)
        except json.JSONDecodeError:
            payload = {}
    keys = {
        "A": ("A点", "point_a_label"),
        "B": ("B点", "point_b_label"),
        "C": ("C点", "point_c_label"),
        "D": ("D点", "point_d_label"),
    }
    points: list[PatternPointOut] = []
    for label, candidates in keys.items():
        raw = next((payload.get(key) for key in candidates if payload.get(key)), None)
        point = parse_pattern_point(label, raw)
        if point:
            points.append(point)
    return points


def kline_file_path(symbol: str) -> Path:
    candidates = [
        Path(settings.scanner_project_path) / "data" / "kline_cache" / f"{symbol}.csv",
        ROOT_DIR / "data" / "kline_cache" / f"{symbol}.csv",
    ]
    for path in candidates:
        if path.exists():
            return path
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"没有找到 {symbol} 的 K 线缓存")


def read_klines(symbol: str, end_date: date, limit: int = 160) -> list[KlineOut]:
    path = kline_file_path(symbol)
    rows: list[KlineOut] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            raw_date = row.get("Date") or row.get("date") or row.get("日期")
            if not raw_date:
                continue
            try:
                row_date = date.fromisoformat(raw_date.strip())
            except ValueError:
                continue
            if row_date > end_date:
                continue
            open_price = parse_float(row.get("Open") or row.get("open") or row.get("开盘"))
            high_price = parse_float(row.get("High") or row.get("high") or row.get("最高"))
            low_price = parse_float(row.get("Low") or row.get("low") or row.get("最低"))
            close_price = parse_float(row.get("Close") or row.get("close") or row.get("收盘"))
            if None in (open_price, high_price, low_price, close_price):
                continue
            rows.append(
                KlineOut(
                    date=row_date,
                    open=open_price or 0,
                    high=high_price or 0,
                    low=low_price or 0,
                    close=close_price or 0,
                    volume=parse_float(row.get("Volume") or row.get("volume") or row.get("成交量")),
                    amount=parse_float(row.get("Amount") or row.get("amount") or row.get("成交额")),
                )
            )
    return rows[-limit:]


@router.get("", response_model=list[SignalOut])
def list_signals(
    signal_date: Optional[date] = None,
    symbol: Optional[str] = None,
    strategy_name: Optional[str] = None,
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    stmt = select(Signal).order_by(desc(Signal.signal_date), Signal.strategy_name, Signal.symbol).limit(limit)
    if signal_date:
        stmt = stmt.where(Signal.signal_date == signal_date)
    if symbol:
        stmt = stmt.where(Signal.symbol == symbol)
    if strategy_name:
        stmt = stmt.where(Signal.strategy_name == strategy_name)
    return [to_signal_out(signal) for signal in db.scalars(stmt).all()]


@router.get("/today", response_model=list[SignalOut])
def today_signals(db: Session = Depends(get_db)):
    latest_date = db.scalar(select(Signal.signal_date).order_by(desc(Signal.signal_date)).limit(1))
    if not latest_date:
        return []
    signals = db.scalars(
        select(Signal)
        .where(Signal.signal_date == latest_date)
        .order_by(Signal.strategy_name, Signal.signal_type, Signal.symbol)
    ).all()
    return [to_signal_out(signal) for signal in signals]


@router.get("/{signal_id}/chart", response_model=SignalChartOut)
def signal_chart(signal_id: int, db: Session = Depends(get_db)):
    signal = db.get(Signal, signal_id)
    if not signal:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Signal not found")
    return SignalChartOut(
        signal=to_signal_out(signal),
        klines=read_klines(signal.symbol, signal.signal_date),
        points=signal_points(signal),
    )


@router.get("/by-symbol/{symbol}", response_model=list[SignalOut])
def by_symbol(symbol: str, db: Session = Depends(get_db)):
    signals = db.scalars(
        select(Signal).where(Signal.symbol == symbol).order_by(desc(Signal.signal_date)).limit(200)
    ).all()
    return [to_signal_out(signal) for signal in signals]
