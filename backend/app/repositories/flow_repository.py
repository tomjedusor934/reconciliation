from typing import List, Optional

from sqlalchemy.orm import Session, selectinload

from app.models.flow import Flow, FlowSource, FlowSourceAccount
from app.models.movement_lot import MovementLot
from app.repositories.base import BaseRepository
from app.schemas.flow import FlowCreate, FlowUpdate


class SourceHasMovementLots(ValueError):
    """A source removed from the update payload still has movement lots
    attached (hard FK ``movement_lot.flow_source_id``): deleting it would
    orphan them. Deactivate the source instead. Mapped to 409 by the API."""


class FlowRepository(BaseRepository[Flow, FlowCreate, FlowUpdate]):
    def get_by_code(self, db: Session, *, code: str) -> Optional[Flow]:
        return db.query(Flow).filter(Flow.code == code).first()

    def list_active(self, db: Session) -> List[Flow]:
        return (
            db.query(Flow)
            .options(selectinload(Flow.sources).selectinload(FlowSource.accounts))
            .filter(Flow.is_active.is_(True))
            .order_by(Flow.code)
            .all()
        )

    def list_all(self, db: Session) -> List[Flow]:
        return (
            db.query(Flow)
            .options(selectinload(Flow.sources).selectinload(FlowSource.accounts))
            .order_by(Flow.code)
            .all()
        )

    def get_with_accounts(self, db: Session, *, flow_id: int) -> Optional[Flow]:
        return (
            db.query(Flow)
            .options(selectinload(Flow.sources).selectinload(FlowSource.accounts))
            .filter(Flow.id == flow_id)
            .first()
        )


class FlowSourceRepository(BaseRepository[FlowSource, dict, dict]):
    def replace_for_flow(self, db: Session, *, flow_id: int, sources: list) -> None:
        """Reconcile the flow's sources BY CODE, updating rows in place.

        Source ids must survive a flow update: ``reco.movement_lot`` holds a
        hard FK to them (the old delete+recreate 500'd on BB flows), and the
        ingestion watermark (``last_success_for_source``) is keyed on them
        (delete+recreate silently reset it — full re-backfill after every
        flow edit). Removing a source that still has lots is refused."""
        existing = {
            s.code: s
            for s in db.query(FlowSource).filter(FlowSource.flow_id == flow_id).all()
        }
        seen = set()
        for s in sources:
            s = dict(s)
            accounts = s.pop("accounts", []) or []
            code = s.get("code")
            seen.add(code)
            current = existing.get(code)
            if current is None:
                db.add(
                    FlowSource(
                        flow_id=flow_id,
                        **s,
                        accounts=[FlowSourceAccount(**a) for a in accounts],
                    )
                )
                continue
            for key, value in s.items():
                setattr(current, key, value)
            # Replace the account rows; flush the orphan DELETEs before the
            # re-INSERTs (same-flush ordering would otherwise collide on the
            # (flow_source_id, account_number) unique constraint).
            current.accounts.clear()
            db.flush()
            current.accounts.extend(FlowSourceAccount(**a) for a in accounts)
        for code, source in existing.items():
            if code in seen:
                continue
            has_lots = (
                db.query(MovementLot.id)
                .filter(MovementLot.flow_source_id == source.id)
                .first()
            )
            if has_lots is not None:
                raise SourceHasMovementLots(
                    f"source '{code}' still has movement lots attached — "
                    "deactivate it instead of removing it"
                )
            db.delete(source)

    def get_active_for_flow(self, db: Session, *, flow_id: int) -> List[FlowSource]:
        return (
            db.query(FlowSource)
            .filter(FlowSource.flow_id == flow_id, FlowSource.is_active.is_(True))
            .order_by(FlowSource.id)
            .all()
        )


flow_repository = FlowRepository(Flow)
flow_source_repository = FlowSourceRepository(FlowSource)
