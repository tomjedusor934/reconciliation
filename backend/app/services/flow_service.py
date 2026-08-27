from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.flow import Flow, FlowSource, FlowSourceAccount, ParserType
from app.repositories.flow_repository import flow_repository, flow_source_repository
from app.schemas.flow import FlowCreate, FlowUpdate


# finacle_db parsers whose perimeter is NOT a set of reference accounts, so the
# "at least one account" rule must not apply to them. WERO filters std.Payment
# on InitModule and reads the WERO table wholesale — it has no GL account, and
# requiring one would make the flow uneditable from the UI.
_ACCOUNTLESS_PARSERS = {ParserType.WERO.value}


class FlowService:
    def _validate_sources(self, sources) -> None:
        """Validate source constraints before persisting."""
        for src in sources:
            src_data = src if isinstance(src, dict) else src.model_dump()
            stype = src_data.get("source_type", "file")
            ptype = src_data.get("parser_type")
            ptype = getattr(ptype, "value", ptype)
            if stype == "finacle_db" and ptype not in _ACCOUNTLESS_PARSERS:
                if not src_data.get("accounts"):
                    raise ValueError(
                        f"Source '{src_data.get('code', '?')}': at least one reference account is required for finacle_db sources"
                    )

    def create_flow(self, db: Session, *, payload: FlowCreate) -> Flow:
        if flow_repository.get_by_code(db, code=payload.code):
            raise ValueError(f"Flow code '{payload.code}' already exists")
        self._validate_sources(payload.sources)
        data = payload.model_dump(exclude={"sources"})
        flow = Flow(**data)
        db.add(flow)
        db.commit()
        db.refresh(flow)
        for src in payload.sources:
            src_data = src.model_dump()
            accounts = src_data.pop("accounts", []) or []
            db.add(
                FlowSource(
                    flow_id=flow.id,
                    **src_data,
                    accounts=[FlowSourceAccount(**a) for a in accounts],
                )
            )
        db.commit()
        db.refresh(flow)
        return flow

    def update_flow(self, db: Session, *, flow_id: int, payload: FlowUpdate) -> Flow:
        flow = flow_repository.get_with_accounts(db, flow_id=flow_id)
        if not flow:
            raise ValueError("Flow not found")
        data = payload.model_dump(exclude_unset=True)
        sources = data.pop("sources", None)
        for k, v in data.items():
            setattr(flow, k, v)
        db.add(flow)
        if sources is not None:
            self._validate_sources(
                [s if isinstance(s, dict) else s for s in sources]
            )
            flow_source_repository.replace_for_flow(
                db, flow_id=flow.id, sources=sources
            )
        db.commit()
        db.refresh(flow)
        return flow

    def delete_flow(self, db: Session, *, flow_id: int) -> None:
        flow = flow_repository.get(db, id=flow_id)
        if not flow:
            raise ValueError("Flow not found")
        flow_repository.remove(db, id=flow_id)

    def get_flow(self, db: Session, *, flow_id: int) -> Optional[Flow]:
        return flow_repository.get_with_accounts(db, flow_id=flow_id)

    def get_flow_by_code(self, db: Session, *, code: str) -> Optional[Flow]:
        return flow_repository.get_by_code(db, code=code)

    def list_flows(self, db: Session, *, only_active: bool = False) -> List[Flow]:
        return (
            flow_repository.list_active(db)
            if only_active
            else flow_repository.list_all(db)
        )

    def toggle_source(self, db: Session, *, source_id: int, is_active: bool) -> FlowSource:
        source = db.query(FlowSource).filter(FlowSource.id == source_id).first()
        if not source:
            raise ValueError("Source not found")
        source.is_active = is_active
        db.commit()
        db.refresh(source)
        return source


flow_service = FlowService()
