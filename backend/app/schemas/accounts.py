from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


class PositionIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    name: Optional[str] = None
    quantity: float = Field(ge=0)
    cost_price: float = Field(ge=0)
    latest_price: Optional[float] = Field(default=None, ge=0)


class SnapshotIn(BaseModel):
    snapshot_date: date
    total_assets: float = Field(ge=0)
    cash: float = Field(ge=0)
    note: Optional[str] = None
    positions: List[PositionIn] = Field(default_factory=list)

    @field_validator("snapshot_date")
    @classmethod
    def snapshot_date_cannot_be_future(cls, value: date) -> date:
        if value > date.today():
            raise ValueError("快照日期不能晚于今天")
        return value


class PositionOut(BaseModel):
    id: int
    symbol: str
    name: Optional[str]
    quantity: float
    cost_price: float
    latest_price: Optional[float]
    market_value: Optional[float]
    profit_loss: Optional[float]
    profit_loss_pct: Optional[float]
    position_pct: Optional[float]
    recent_signal_count: int = 0

    model_config = {"from_attributes": True}


class SnapshotOut(BaseModel):
    id: int
    snapshot_date: date
    total_assets: float
    cash: float
    note: Optional[str]
    created_at: datetime
    updated_at: datetime
    positions: List[PositionOut]

    model_config = {"from_attributes": True}


class PositionChangeOut(BaseModel):
    symbol: str
    name: Optional[str]
    change_type: str
    previous_quantity: float
    current_quantity: float
    quantity_delta: float


class PositionQuoteOut(BaseModel):
    symbol: str
    name: Optional[str]
    latest_price: Optional[float]
