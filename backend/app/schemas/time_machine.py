"""Schemas for the Archive (time-machine) feature."""
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict


class SnapshotEntryResponse(BaseModel):
    id: int
    flow_id: int
    reco_id: Optional[str] = None
    account: Optional[str] = None
    currency: str
    amount: Decimal
    direction: Optional[str] = None
    value_date: datetime
    operation_date: Optional[datetime] = None
    event_type: Optional[str] = None
    external_ref: Optional[str] = None
    file_name: Optional[str] = None
    status_at_snapshot: str
    match_group_id: Optional[int] = None
    matched_at: Optional[datetime] = None
    exclusion_reason: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)


class SnapshotSummary(BaseModel):
    snapshot_date: date
    total_entries: int
    pending_count: int
    matched_count: int
    forced_count: int
    excluded_count: int


class SnapshotResponse(BaseModel):
    summary: SnapshotSummary
    items: List[SnapshotEntryResponse]
    total_count: int


class DailyActivityEntry(BaseModel):
    id: int
    user_id: Optional[int] = None
    user_name: Optional[str] = None
    action: str
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    ts: datetime


class DailyActivityResponse(BaseModel):
    activity_date: date
    actions: List[DailyActivityEntry]
    total_count: int
