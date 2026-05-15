from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import desc, select
from sqlalchemy.orm import Session, selectinload

from app.core.database import get_db
from app.models import AccountSnapshot, User
from app.schemas.accounts import PositionChangeOut, PositionQuoteOut, SnapshotIn, SnapshotOut
from app.services.accounts import attach_recent_signal_counts, get_position_quotes, infer_changes, upsert_snapshot
from app.services.auth import get_current_user


router = APIRouter(prefix="/account", tags=["account"])


@router.get("/position-quotes", response_model=list[PositionQuoteOut])
def position_quotes(
    symbols: str,
    snapshot_date: date,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del user
    symbol_list = [symbol for symbol in symbols.split(",") if symbol.strip()]
    return list(get_position_quotes(db, symbol_list, snapshot_date).values())


@router.get("/snapshots", response_model=list[SnapshotOut])
def list_snapshots(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    snapshots = db.scalars(
        select(AccountSnapshot)
        .where(AccountSnapshot.user_id == user.id)
        .options(selectinload(AccountSnapshot.positions))
        .order_by(desc(AccountSnapshot.snapshot_date))
        .limit(100)
    ).all()
    return snapshots


@router.post("/snapshots", response_model=SnapshotOut)
def save_snapshot(payload: SnapshotIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    snapshot = upsert_snapshot(db, user.id, payload)
    snapshot = db.scalar(
        select(AccountSnapshot)
        .where(AccountSnapshot.id == snapshot.id, AccountSnapshot.user_id == user.id)
        .options(selectinload(AccountSnapshot.positions))
    )
    return attach_recent_signal_counts(db, snapshot)


@router.get("/snapshots/{snapshot_id}", response_model=SnapshotOut)
def get_snapshot(snapshot_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    snapshot = db.scalar(
        select(AccountSnapshot)
        .where(AccountSnapshot.id == snapshot_id, AccountSnapshot.user_id == user.id)
        .options(selectinload(AccountSnapshot.positions))
    )
    if not snapshot:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Snapshot not found")
    return attach_recent_signal_counts(db, snapshot)


@router.delete("/snapshots/{snapshot_id}")
def delete_snapshot(snapshot_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    snapshot = db.scalar(
        select(AccountSnapshot).where(AccountSnapshot.id == snapshot_id, AccountSnapshot.user_id == user.id)
    )
    if not snapshot:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Snapshot not found")
    db.delete(snapshot)
    db.commit()
    return {"ok": True}


@router.get("/current", response_model=Optional[SnapshotOut])
def current_snapshot(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    snapshot = db.scalar(
        select(AccountSnapshot)
        .where(AccountSnapshot.user_id == user.id)
        .options(selectinload(AccountSnapshot.positions))
        .order_by(desc(AccountSnapshot.snapshot_date))
        .limit(1)
    )
    return attach_recent_signal_counts(db, snapshot) if snapshot else None


@router.get("/changes", response_model=list[PositionChangeOut])
def current_changes(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    snapshots = db.scalars(
        select(AccountSnapshot)
        .where(AccountSnapshot.user_id == user.id)
        .options(selectinload(AccountSnapshot.positions))
        .order_by(desc(AccountSnapshot.snapshot_date))
        .limit(2)
    ).all()
    if not snapshots:
        return []
    current = snapshots[0]
    previous = snapshots[1] if len(snapshots) > 1 else None
    return infer_changes(current, previous)
