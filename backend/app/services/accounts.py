from __future__ import annotations

import csv
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import ROOT_DIR
from app.models import AccountPosition, AccountSnapshot, Signal
from app.schemas.accounts import PositionChangeOut, PositionQuoteOut, SnapshotIn


def normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def parse_float(value: object) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


@lru_cache(maxsize=1)
def load_code_name_map() -> dict[str, str]:
    paths = [
        ROOT_DIR / "data" / "universe_cache" / "a_share_code_list.csv",
    ]
    names: dict[str, str] = {}
    for path in paths:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                code = str(row.get("code") or row.get("代码") or "").strip().zfill(6)
                name = str(row.get("name") or row.get("名称") or "").strip()
                if code and name:
                    names[code] = name
        if names:
            return names
    return names


def latest_kline_close(symbol: str, snapshot_date: date) -> Optional[float]:
    paths = [
        ROOT_DIR / "data" / "kline_cache" / f"{symbol}.csv",
    ]
    latest_close: Optional[float] = None
    latest_date: Optional[date] = None
    for path in paths:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                raw_date = row.get("Date") or row.get("date") or row.get("日期")
                if not raw_date:
                    continue
                try:
                    row_date = date.fromisoformat(raw_date.strip())
                except ValueError:
                    continue
                if row_date > snapshot_date or (latest_date and row_date <= latest_date):
                    continue
                close_price = parse_float(row.get("Close") or row.get("close") or row.get("收盘"))
                if close_price is not None:
                    latest_date = row_date
                    latest_close = close_price
        if latest_close is not None:
            return latest_close
    return latest_close


def get_position_quotes(db: Session, symbols: list[str], snapshot_date) -> dict[str, PositionQuoteOut]:
    quotes: dict[str, PositionQuoteOut] = {}
    code_names = load_code_name_map()
    for raw_symbol in symbols:
        symbol = normalize_symbol(raw_symbol)
        if not symbol or symbol in quotes:
            continue
        signal = db.scalar(
            select(Signal)
            .where(Signal.symbol == symbol, Signal.signal_date <= snapshot_date)
            .order_by(Signal.signal_date.desc())
            .limit(1)
        )
        quotes[symbol] = PositionQuoteOut(
            symbol=symbol,
            name=(signal.name if signal else None) or code_names.get(symbol),
            latest_price=(signal.close_price if signal else None) or latest_kline_close(symbol, snapshot_date),
        )
    return quotes


def upsert_snapshot(db: Session, user_id: int, payload: SnapshotIn) -> AccountSnapshot:
    snapshot = db.scalar(
        select(AccountSnapshot)
        .where(AccountSnapshot.user_id == user_id, AccountSnapshot.snapshot_date == payload.snapshot_date)
        .options(selectinload(AccountSnapshot.positions))
    )
    if snapshot is None:
        snapshot = AccountSnapshot(
            user_id=user_id,
            snapshot_date=payload.snapshot_date,
            total_assets=payload.total_assets,
            cash=payload.cash,
            note=payload.note,
        )
        db.add(snapshot)
        db.flush()
    else:
        snapshot.total_assets = payload.total_assets
        snapshot.cash = payload.cash
        snapshot.note = payload.note
        snapshot.positions.clear()
        db.flush()

    quotes = get_position_quotes(db, [item.symbol for item in payload.positions], payload.snapshot_date)

    for item in payload.positions:
        symbol = normalize_symbol(item.symbol)
        quote = quotes.get(symbol)
        latest_price = item.latest_price if item.latest_price is not None else quote.latest_price if quote else None
        latest_price = latest_price if latest_price is not None else item.cost_price
        market_value = latest_price * item.quantity
        cost_value = item.cost_price * item.quantity
        profit_loss = market_value - cost_value
        db.add(
            AccountPosition(
                user_id=user_id,
                snapshot_id=snapshot.id,
                symbol=symbol,
                name=item.name or (quote.name if quote else None),
                quantity=item.quantity,
                cost_price=item.cost_price,
                latest_price=latest_price,
                market_value=market_value,
                profit_loss=profit_loss,
                profit_loss_pct=(profit_loss / cost_value if cost_value else 0),
                position_pct=(market_value / payload.total_assets if payload.total_assets else 0),
            )
        )

    db.commit()
    db.refresh(snapshot)
    return snapshot


def attach_recent_signal_counts(db: Session, snapshot: AccountSnapshot, days: int = 10) -> AccountSnapshot:
    start_date = snapshot.snapshot_date - timedelta(days=days)
    for position in snapshot.positions:
        count = db.scalar(
            select(Signal.id)
            .where(
                Signal.symbol == position.symbol,
                Signal.signal_date >= start_date,
                Signal.signal_date <= snapshot.snapshot_date,
            )
            .limit(1)
        )
        position.recent_signal_count = 1 if count else 0
    return snapshot


def infer_changes(current: AccountSnapshot, previous: Optional[AccountSnapshot]) -> List[PositionChangeOut]:
    previous_map = {p.symbol: p for p in previous.positions} if previous else {}
    current_map = {p.symbol: p for p in current.positions}
    symbols = sorted(set(previous_map) | set(current_map))
    changes: List[PositionChangeOut] = []

    for symbol in symbols:
        prev_qty = previous_map[symbol].quantity if symbol in previous_map else 0
        curr_qty = current_map[symbol].quantity if symbol in current_map else 0
        delta = curr_qty - prev_qty
        if prev_qty == 0 and curr_qty > 0:
            change_type = "新开"
        elif prev_qty > 0 and curr_qty == 0:
            change_type = "清仓"
        elif delta > 0:
            change_type = "加仓"
        elif delta < 0:
            change_type = "减仓"
        else:
            change_type = "持仓不变"
        position = current_map.get(symbol) or previous_map.get(symbol)
        changes.append(
            PositionChangeOut(
                symbol=symbol,
                name=position.name,
                change_type=change_type,
                previous_quantity=prev_qty,
                current_quantity=curr_qty,
                quantity_delta=delta,
            )
        )
    return changes
