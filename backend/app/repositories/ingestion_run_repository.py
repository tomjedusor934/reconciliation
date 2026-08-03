from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.ingestion_run import IngestionRun, IngestionStatus


class IngestionRunRepository:
    def create(self, db: Session, *, run: IngestionRun) -> IngestionRun:
        db.add(run)
        db.commit()
        db.refresh(run)
        return run

    def update(self, db: Session, *, run: IngestionRun, **fields) -> IngestionRun:
        for k, v in fields.items():
            setattr(run, k, v)
        db.add(run)
        db.commit()
        db.refresh(run)
        return run

    def get(self, db: Session, *, run_id: int) -> Optional[IngestionRun]:
        return db.query(IngestionRun).filter(IngestionRun.id == run_id).first()

    def last_success_for_source(self, db: Session, *, flow_source_id: int):
        """Watermark for incremental Finacle extraction: started_at of the last
        run that actually ingested data (SUCCESS or PARTIAL) for this source."""
        row = (
            db.query(IngestionRun.started_at)
            .filter(
                IngestionRun.flow_source_id == flow_source_id,
                IngestionRun.status.in_(
                    [IngestionStatus.SUCCESS, IngestionStatus.PARTIAL]
                ),
            )
            .order_by(IngestionRun.started_at.desc())
            .first()
        )
        return row[0] if row else None

    def list_filtered(
        self,
        db: Session,
        *,
        flow_id: Optional[int] = None,
        status: Optional[IngestionStatus] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> List[IngestionRun]:
        q = db.query(IngestionRun)
        if flow_id is not None:
            q = q.filter(IngestionRun.flow_id == flow_id)
        if status is not None:
            q = q.filter(IngestionRun.status == status)
        return (
            q.order_by(IngestionRun.started_at.desc()).offset(skip).limit(limit).all()
        )
    def recent_by_flow(self, db: Session, *, limit: int = 10) -> List:
        """Return the last N successful ingestion runs per flow (for dashboard), with source code."""
        from sqlalchemy import text as sa_text
        rows = db.execute(
            sa_text(
                """
                SELECT ir.id, ir.flow_id, ir.flow_source_id, ir.rows_ok, ir.started_at,
                       fs.code AS source_code
                FROM reco.ingestion_run ir
                LEFT JOIN reco.flow_source fs ON ir.flow_source_id = fs.id
                WHERE ir.status IN ('SUCCESS', 'PARTIAL')
                ORDER BY ir.started_at DESC
                LIMIT :lim
                """
            ),
            {"lim": limit * 10},
        ).all()
        return rows

    def last_rows_ok_per_flow(self, db: Session) -> dict[int, int]:
        """Return rows_ok of the most recent successful ingestion run for each flow."""
        from sqlalchemy import text as sa_text
        rows = db.execute(
            sa_text(
                """
                SELECT DISTINCT ON (ir.flow_id)
                    ir.flow_id,
                    COALESCE(ir.rows_ok, 0) AS rows_ok
                FROM reco.ingestion_run ir
                WHERE ir.status IN ('SUCCESS', 'PARTIAL')
                ORDER BY ir.flow_id, ir.started_at DESC
                """
            )
        ).all()
        return {r.flow_id: r.rows_ok for r in rows}

    def daily_counts(
        self, db: Session, *, flow_id: int, year: int, month: int
    ) -> List:
        """Aggregate rows_ok by day for a given flow and month."""
        from sqlalchemy import text as sa_text
        rows = db.execute(
            sa_text(
                """
                SELECT EXTRACT(DAY FROM ir.started_at)::int AS day,
                       COALESCE(SUM(ir.rows_ok), 0)::int AS count
                FROM reco.ingestion_run ir
                WHERE ir.flow_id = :fid
                  AND ir.status IN ('SUCCESS', 'PARTIAL')
                  AND EXTRACT(YEAR FROM ir.started_at) = :yr
                  AND EXTRACT(MONTH FROM ir.started_at) = :mo
                GROUP BY EXTRACT(DAY FROM ir.started_at)
                ORDER BY day
                """
            ),
            {"fid": flow_id, "yr": year, "mo": month},
        ).all()
        return [(r.day, r.count) for r in rows]


ingestion_run_repository = IngestionRunRepository()
