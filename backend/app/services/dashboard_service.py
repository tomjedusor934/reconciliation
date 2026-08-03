"""Dashboard service — aggregates KPIs for the 3 UI views."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

from sqlalchemy.orm import Session

from app.repositories.flow_repository import flow_repository
from app.repositories.ingestion_run_repository import ingestion_run_repository
from app.repositories.reconciliation_entry_repository import (
    reconciliation_entry_repository,
)
from app.repositories.reconciliation_run_repository import reconciliation_run_repository
from app.schemas.reconciliation import (
    DashboardResponse,
    FlowKPI,
    IngestionCalendarDay,
    IngestionCalendarResponse,
    IngestionRunSummary,
    ReconciliationRunResponse,
    TransversalGroup,
    ReconciliationEntryResponse,
)


class DashboardService:
    def get_dashboard(self, db: Session) -> DashboardResponse:
        flows = flow_repository.list_all(db)

        rows = reconciliation_entry_repository.kpis_by_flow(db)

        # Index per (flow_id, status) → (count, net_sum, credit_sum, debit_sum)
        idx: dict[int, dict[str, tuple[int, Decimal, Decimal, Decimal]]] = defaultdict(dict)
        for r in rows:
            idx[r["flow_id"]][r["status"]] = (
                int(r["cnt"]),
                Decimal(str(r["amount_total"])),
                Decimal(str(r["amount_credit"])),
                Decimal(str(r["amount_debit"])),
            )

        # Oldest pending per flow (always current)
        oldest_pending = reconciliation_entry_repository.oldest_pending_per_flow(db)

        # Recent ingestion runs
        recent_runs = ingestion_run_repository.recent_by_flow(db, limit=10)
        runs_by_flow: dict[int, list] = defaultdict(list)
        for r in recent_runs:
            source_code = r.source_code if hasattr(r, 'source_code') else None
            runs_by_flow[r.flow_id].append(
                IngestionRunSummary(
                    run_id=r.id,
                    row_count=r.rows_ok or 0,
                    run_date=r.started_at,
                    source_code=source_code,
                )
            )

        # new_entries_delta = rows_ok of the most recent ingestion run per flow
        new_entries_by_flow = ingestion_run_repository.last_rows_ok_per_flow(db)

        totals: dict[int, int] = {}
        for fid, frd in runs_by_flow.items():
            totals[fid] = sum(r.row_count for r in frd)

        kpis: List[FlowKPI] = []
        for f in flows:
            stats = idx.get(f.id, {})
            zero = (0, Decimal("0"), Decimal("0"), Decimal("0"))
            pending = stats.get("PENDING", zero)
            matched = stats.get("MATCHED", zero)
            forced = stats.get("FORCED", zero)
            excluded = stats.get("EXCLUDED", zero)
            kpis.append(
                FlowKPI(
                    flow_id=f.id,
                    flow_code=f.code,
                    flow_name=f.name,
                    pending_count=pending[0],
                    matched_count=matched[0] + forced[0],
                    excluded_count=excluded[0],
                    pending_amount_total=pending[1],
                    pending_amount_credit=pending[2],
                    pending_amount_debit=pending[3],
                    new_entries_delta=new_entries_by_flow.get(f.id, 0),  # rows_ok of the last ingestion run
                    total_entries_current=totals.get(f.id, 0),
                    oldest_unmatched_date=oldest_pending.get(f.id),
                    ingestion_runs=runs_by_flow.get(f.id, [])[:10],
                    is_active=f.is_active,
                )
            )

        # Get the most recent reconciliation run (for response)
        last_reco_run = reconciliation_run_repository.latest(db)

        total_matched = sum(k.matched_count for k in kpis)

        return DashboardResponse(
            flows=kpis,
            total_matched_count=total_matched,
            last_reconciliation_run=ReconciliationRunResponse.model_validate(last_reco_run)
            if last_reco_run
            else None,
        )

    def ingestion_calendar(
        self, db: Session, *, flow_id: int, year: int, month: int
    ) -> IngestionCalendarResponse:
        """Aggregate ingestion rows_ok by day for a given flow and month."""
        daily_counts = ingestion_run_repository.daily_counts(
            db, flow_id=flow_id, year=year, month=month
        )
        days = [
            IngestionCalendarDay(day=day, count=count)
            for day, count in daily_counts
        ]
        return IngestionCalendarResponse(
            flow_id=flow_id, year=year, month=month, days=days
        )

    def transversal(self, db: Session, *, reco_id: str) -> List[TransversalGroup]:
        entries = reconciliation_entry_repository.list_by_reco_id(db, reco_id=reco_id)
        # Group by currency (a single reco_id can in theory span flows but rarely currencies)
        groups: dict[str, list] = defaultdict(list)
        for e in entries:
            groups[e.currency].append(e)
        out: List[TransversalGroup] = []
        for cur, items in groups.items():
            total = sum((e.amount for e in items), Decimal("0"))
            out.append(
                TransversalGroup(
                    reco_id=reco_id,
                    currency=cur,
                    entries=[ReconciliationEntryResponse.model_validate(e) for e in items],
                    sum=total,
                    is_balanced=(total == Decimal("0")),
                )
            )
        return out


dashboard_service = DashboardService()
