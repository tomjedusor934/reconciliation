"""Query-parameter filter schemas used with FastAPI Depends().

Each class groups the business filter parameters for an endpoint so that
the endpoint signature only exposes skip, limit, db and the auth dependency.
"""
from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from fastapi import Query

from app.models.ingestion_run import IngestionStatus
from app.models.reconciliation_entry import EntryStatus

# ── Archive ─────────────────────────────────────────────────────────

class SnapshotFilter:
    def __init__(
        self,
        snapshot_date: date = Query(..., alias="date", description="Snapshot date (YYYY-MM-DD)"),
        flow_id: Optional[int] = Query(default=None, description="Filter by flow"),
        status: Optional[str] = Query(default=None, description="Filter by status"),
    ):
        self.snapshot_date = snapshot_date
        self.flow_id = flow_id
        self.status = status


class ActivityFilter:
    def __init__(
        self,
        activity_date: date = Query(..., alias="date", description="Activity date (YYYY-MM-DD)"),
        flow_id: Optional[int] = Query(default=None, description="Filter by flow"),
        user_id: Optional[int] = Query(default=None, description="Filter by user"),
    ):
        self.activity_date = activity_date
        self.flow_id = flow_id
        self.user_id = user_id


class ExportFilter:
    def __init__(
        self,
        snapshot_date: date = Query(..., alias="date", description="Snapshot date (YYYY-MM-DD)"),
        flow_id: Optional[int] = Query(default=None, description="Filter by flow"),
    ):
        self.snapshot_date = snapshot_date
        self.flow_id = flow_id


# ── Reconciliation entries ──────────────────────────────────────────

class EntryListFilter:
    def __init__(
        self,
        flow_id: Optional[int] = Query(default=None, description="Filter by flow"),
        status: Optional[EntryStatus] = Query(default=None, alias="status", description="Filter by status"),
        reco_id: Optional[str] = Query(default=None, description="Filter by reco ID"),
        amount_min: Optional[Decimal] = Query(default=None, description="Min |amount| (absolute value)"),
        amount_max: Optional[Decimal] = Query(default=None, description="Max |amount| (absolute value)"),
        payment_statuses: Optional[List[str]] = Query(
            default=None,
            description="Keep rows carrying at least one of these payment statuses ('?' = unknown)",
        ),
        # Naive datetimes (YYYY-MM-DDTHH:MM) — the column is TIMESTAMP WITHOUT
        # TIME ZONE and the UI sends wall clock, so the hour filtered on is the
        # hour displayed. Both bounds inclusive.
        payment_timestamp_from: Optional[datetime] = Query(
            default=None,
            description="Keep rows with at least one payment accepted at or after this datetime",
        ),
        payment_timestamp_to: Optional[datetime] = Query(
            default=None,
            description="Keep rows with at least one payment accepted at or before this datetime",
        ),
        account: Optional[str] = Query(default=None, description="Filter by account"),
        date_from: Optional[date] = Query(default=None, description="Start date (YYYY-MM-DD)"),
        date_to: Optional[date] = Query(default=None, description="End date (YYYY-MM-DD)"),
        search: Optional[str] = Query(default=None, description="Free-text search"),
    ):
        self.flow_id = flow_id
        self.status = status
        self.reco_id = reco_id
        self.amount_min = amount_min
        self.amount_max = amount_max
        self.payment_statuses = payment_statuses
        self.payment_timestamp_from = payment_timestamp_from
        self.payment_timestamp_to = payment_timestamp_to
        self.account = account
        self.date_from = date_from
        self.date_to = date_to
        self.search = search


# ── Ingestion runs ──────────────────────────────────────────────────

class IngestionRunFilter:
    def __init__(
        self,
        flow_id: Optional[int] = Query(default=None, description="Filter by flow"),
        status: Optional[IngestionStatus] = Query(default=None, description="Filter by status"),
    ):
        self.flow_id = flow_id
        self.status = status


# ── Audit ───────────────────────────────────────────────────────────

class DataAuditFilter:
    def __init__(
        self,
        table_name: Optional[str] = Query(default=None, description="Filter by table name"),
    ):
        self.table_name = table_name


class UIAuditFilter:
    def __init__(
        self,
        action: Optional[str] = Query(default=None, description="Filter by action"),
        user_id: Optional[int] = Query(default=None, description="Filter by user"),
    ):
        self.action = action
        self.user_id = user_id


# ── Dashboards ──────────────────────────────────────────────────────

class IngestionCalendarFilter:
    def __init__(
        self,
        flow_id: int = Query(..., description="Flow ID"),
        year: int = Query(..., description="Year (e.g. 2026)"),
        month: int = Query(..., ge=1, le=12, description="Month (1-12)"),
    ):
        self.flow_id = flow_id
        self.year = year
        self.month = month


class DeepSearchFilter:
    def __init__(
        self,
        q: str = Query(
            ..., min_length=1,
            description="Any identifier: payment number, lot uuid, reco ID, "
                        "TransactionID, ref_no, remarks_1, lot key",
        ),
        broad: bool = Query(
            default=False,
            description="Fall back to a partial (ILIKE) match when the exact "
                        "lookup finds nothing — sequential scan, opt-in only",
        ),
    ):
        self.q = q
        self.broad = broad


# ── Movement lots (Finacle Batch Booking True) ──────────────────────

class LotListFilter:
    def __init__(
        self,
        flow_id: Optional[int] = Query(default=None, description="Filter by flow"),
        status: Optional[str] = Query(
            default=None,
            pattern="^(pending|matched|merged)$",
            description="Derived lot status",
        ),
        balanced: Optional[bool] = Query(default=None, description="Every currency nets to 0"),
        date_from: Optional[date] = Query(default=None, description="Member activity from (YYYY-MM-DD)"),
        date_to: Optional[date] = Query(default=None, description="Member activity to (YYYY-MM-DD)"),
        search: Optional[str] = Query(default=None, description="Lot uuid or key value (substring)"),
        bucket_kind: Optional[str] = Query(
            default=None,
            pattern="^(PAIR|PACS_ONLY|MSGID_ONLY|PO|RESIDUAL|LEGACY)$",
            description="What the bucket stands for (RESIDUAL = a split's leftover)",
        ),
        synthetic_only: Optional[bool] = Query(
            default=None,
            description="Buckets whose every member is a ghost (balanced by construction)",
        ),
        payment_gap: Optional[bool] = Query(
            default=None,
            description="Buckets holding a split whose booked amount disagrees with std.Payment",
        ),
    ):
        self.flow_id = flow_id
        self.status = status
        self.balanced = balanced
        self.date_from = date_from
        self.date_to = date_to
        self.search = search
        self.bucket_kind = bucket_kind
        self.synthetic_only = synthetic_only
        self.payment_gap = payment_gap
