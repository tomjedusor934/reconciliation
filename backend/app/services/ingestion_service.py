"""Ingestion service — orchestrates parser → normalization → bulk insert."""
from __future__ import annotations

import fnmatch
import os
import shutil
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.flow import Flow, FlowSource, FlowSourceType
from app.models.ingestion_run import IngestionRun, IngestionStatus
from app.repositories.ingestion_run_repository import ingestion_run_repository
from app.repositories.reconciliation_entry_repository import reconciliation_entry_repository
from app.services.parsers import get_parser
from app.services.parsers.base_parser import ParsedEntry, ParseResult


class IngestionService:
    # --------------------------------------------------------------
    # File-based ingestion (per source)
    # --------------------------------------------------------------
    def ingest_inbox_for_flow(self, db: Session, *, flow: Flow) -> List[IngestionRun]:
        """Process every file currently sitting in each active source's inbox.

        Iterates over all active file-type sources within the flow.
        If the flow itself is inactive, returns empty.
        """
        if not flow.is_active:
            return []

        # Step 1 — resolve reco_id for this flow's reference entries (e.g. MT940 +21
        # keys) left unresolved by earlier cycles, now that finacle is ingested (DAG
        # order is finacle → files → reconcile). Independent of reading any file;
        # skipped when no source opts into finacle lookup.
        if any(
            s.is_active and (s.parser_config or {}).get("resolve_reco_id_via_finacle")
            for s in flow.sources
        ):
            reconciliation_entry_repository.resolve_flow_references(db, flow_id=flow.id)

        # Step 2 — parse and ingest each active file source (new entries are resolved
        # inline in ingest_file via _resolve_finacle_lookup).
        runs: List[IngestionRun] = []
        for source in flow.sources:
            if not source.is_active:
                continue
            if source.source_type != FlowSourceType.FILE:
                continue
            # extract_via_dag sources are parsed by the Airflow DAG (it fetches the
            # file content and pushes entries back via /tasks/mt940/*), not locally.
            if (source.parser_config or {}).get("extract_via_dag"):
                continue
            runs.extend(self._ingest_source_inbox(db, flow=flow, source=source))
        return runs

    def _ingest_source_inbox(
        self, db: Session, *, flow: Flow, source: FlowSource
    ) -> List[IngestionRun]:
        runs: List[IngestionRun] = []
        inbox_dir = self._inbox_dir(source, flow, settings.INBOX_BASE_PATH)
        print(f"-------- ingesting inbox {inbox_dir} for flow {flow.code} / source {source.code}")
        if not os.path.isdir(inbox_dir):
            os.makedirs(inbox_dir, exist_ok=True)
            return runs

        files = sorted(
            os.path.join(inbox_dir, f)
            for f in os.listdir(inbox_dir)
            if os.path.isfile(os.path.join(inbox_dir, f))
            and not f.startswith(".")
            and (not source.file_pattern or fnmatch.fnmatch(f, source.file_pattern))
        )
        print(f"-------- found {len(files)} files in inbox for flow {flow.code} / source {source.code}")
        for path in files:
            runs.append(self.ingest_file(db, flow=flow, source=source, file_path=path))
        return runs

    def ingest_file(
        self, db: Session, *, flow: Flow, source: FlowSource, file_path: str
    ) -> IngestionRun:
        # Path safety: only accept files under INBOX_BASE_PATH
        safe_root = os.path.realpath(settings.INBOX_BASE_PATH)
        real_path = os.path.realpath(file_path)
        if not real_path.startswith(safe_root + os.sep):
            raise ValueError(f"refusing to ingest path outside inbox: {file_path}")

        run = ingestion_run_repository.create(
            db,
            run=IngestionRun(
                flow_id=flow.id,
                flow_source_id=source.id,
                source_file=os.path.basename(file_path),
                started_at=datetime.now(timezone.utc),
                status=IngestionStatus.RUNNING,
            ),
        )

        try:
            parser = get_parser(source.parser_type.value, source.parser_config or {})
            result = parser.parse_file(real_path)
            lookup = bool((source.parser_config or {}).get("resolve_reco_id_via_finacle"))
            if lookup:
                # Resolve each entry's reco_id from already-ingested finacle entries
                # (finacle is ingested before files). Unmatched stay None and are
                # retried at reconcile time. source_hash is reco_id-independent here.
                self._resolve_finacle_lookup(db, flow=flow, source=source, entries=result.entries)
            inserted, dup = self._persist(
                db, flow=flow, result=result, run=run, stable_hash=lookup
            )
            # Run status: errors but nothing ingested → FAILED (the file could
            # not be parsed at all); some rows ok + some errors → PARTIAL; all
            # rows ok (or only duplicates) → SUCCESS.
            if result.errors and inserted == 0:
                status = IngestionStatus.FAILED
            elif result.errors:
                status = IngestionStatus.PARTIAL
            else:
                status = IngestionStatus.SUCCESS
            ingestion_run_repository.update(
                db,
                run=run,
                finished_at=datetime.now(timezone.utc),
                rows_in=len(result.entries) + len(result.errors),
                rows_ok=inserted,
                rows_ko=len(result.errors),
                rows_duplicate=dup,
                status=status,
                error="\n".join(result.errors[:50]) if result.errors else None,
            )
            # A fully failed parse goes to the error inbox (not processed) so the
            # file can be re-fed after the parser/format is fixed.
            dest = (
                settings.INBOX_ERROR_PATH
                if status == IngestionStatus.FAILED
                else settings.INBOX_PROCESSED_PATH
            )
            self._move_to(real_path, dest, source, flow)
        except Exception as exc:  # noqa: BLE001
            print(f"[ingest_file] error ingesting {real_path}: {exc}")
            try:
                db.rollback()
            except Exception as exc:
                print(f"[ingest_file] error rolling back after {real_path}: {exc}")
                pass
            try:
                ingestion_run_repository.update(
                    db,
                    run=run,
                    finished_at=datetime.now(timezone.utc),
                    status=IngestionStatus.FAILED,
                    error=str(exc)[:4096],
                )
            except Exception as exc:
                print(f"[ingest_file] error updating run {run.id} to FAILED: {exc}")
                pass
            self._move_to(real_path, settings.INBOX_ERROR_PATH, source, flow)
        return run

    # --------------------------------------------------------------
    # Finacle ingestion — data is extracted by the Airflow DAG and
    # pushed here in batches; the backend only persists and logs runs.
    # --------------------------------------------------------------
    def start_finacle_run(
        self, db: Session, *, flow: Flow, source: FlowSource, dag_run_id: Optional[str] = None
    ) -> IngestionRun:
        """Open a RUNNING ingestion run for a Finacle source (data pushed by the DAG)."""
        if source.source_type != FlowSourceType.FINACLE_DB:
            raise ValueError(f"source {source.code} is not Finacle-DB type")
        if not flow.is_active or not source.is_active:
            raise ValueError(f"flow {flow.code} / source {source.code} is not active")

        source_file = f"finacle:{source.code}"
        if dag_run_id:
            source_file = f"{source_file} ({dag_run_id})"
        return ingestion_run_repository.create(
            db,
            run=IngestionRun(
                flow_id=flow.id,
                flow_source_id=source.id,
                source_file=source_file[:512],
                started_at=datetime.now(timezone.utc),
                status=IngestionStatus.RUNNING,
            ),
        )

    def ingest_finacle_batch(
        self,
        db: Session,
        *,
        run: IngestionRun,
        entries: List[ParsedEntry],
        errors: List[str],
    ) -> tuple:
        """Persist one batch of DAG-pushed entries and accumulate run counters."""
        result = ParseResult(entries=entries, errors=errors)
        inserted, dup = self._persist(db, flow=run.flow, result=result, run=run, finacle=True)

        error_log = run.error
        if errors:
            combined = (error_log + "\n" if error_log else "") + "\n".join(errors[:50])
            error_log = combined[:4096]
        ingestion_run_repository.update(
            db,
            run=run,
            rows_in=(run.rows_in or 0) + len(entries) + len(errors),
            rows_ok=(run.rows_ok or 0) + inserted,
            rows_ko=(run.rows_ko or 0) + len(errors),
            rows_duplicate=(run.rows_duplicate or 0) + dup,
            error=error_log,
        )
        return inserted, dup

    def complete_finacle_run(
        self,
        db: Session,
        *,
        run: IngestionRun,
        failed: bool = False,
        error: Optional[str] = None,
    ) -> IngestionRun:
        """Close a Finacle run: SUCCESS, PARTIAL (row errors) or FAILED."""
        if failed:
            status = IngestionStatus.FAILED
        elif (run.rows_ko or 0) > 0:
            status = IngestionStatus.PARTIAL
        else:
            status = IngestionStatus.SUCCESS

        error_log = run.error
        if error:
            combined = (error_log + "\n" if error_log else "") + error
            error_log = combined[:4096]
        return ingestion_run_repository.update(
            db,
            run=run,
            finished_at=datetime.now(timezone.utc),
            status=status,
            error=error_log,
        )

    # --------------------------------------------------------------
    # MT940 ingestion — like Finacle, the heavy extraction happens in
    # the Airflow DAG: the backend hands the raw file content to the DAG,
    # which parses the :61:/+21 lines and resolves reco_id against the
    # datamart, then pushes the entries back here in batches.
    # --------------------------------------------------------------
    def list_mt940_inbox(self, flow: Flow, source: FlowSource) -> List[str]:
        """File names sitting in a DAG-extracted (MT940) source's inbox."""
        inbox_dir = self._inbox_dir(source, flow, settings.INBOX_BASE_PATH)
        if not os.path.isdir(inbox_dir):
            os.makedirs(inbox_dir, exist_ok=True)
            return []
        return sorted(
            f
            for f in os.listdir(inbox_dir)
            if os.path.isfile(os.path.join(inbox_dir, f))
            and not f.startswith(".")
            and (not source.file_pattern or fnmatch.fnmatch(f, source.file_pattern))
        )

    def open_mt940_run(
        self, db: Session, *, flow: Flow, source: FlowSource, file_name: str
    ) -> tuple:
        """Open a RUNNING run for one MT940 file and return its decoded content.

        Returns (run, content). The file stays in the inbox until the run is
        completed (then it is moved to processed/error)."""
        if not flow.is_active or not source.is_active:
            raise ValueError(f"flow {flow.code} / source {source.code} is not active")
        if not (source.parser_config or {}).get("extract_via_dag"):
            raise ValueError(f"source {source.code} is not a DAG-extracted source")

        inbox_dir = self._inbox_dir(source, flow, settings.INBOX_BASE_PATH)
        real_path = os.path.realpath(os.path.join(inbox_dir, os.path.basename(file_name)))
        if not real_path.startswith(os.path.realpath(inbox_dir) + os.sep):
            raise ValueError(f"refusing to read path outside inbox: {file_name}")
        if not os.path.isfile(real_path):
            raise FileNotFoundError(f"file not found in inbox: {file_name}")

        encoding = (source.parser_config or {}).get("encoding", "utf-8")
        with open(real_path, "rb") as fh:
            content = fh.read().decode(encoding, errors="replace")

        run = ingestion_run_repository.create(
            db,
            run=IngestionRun(
                flow_id=flow.id,
                flow_source_id=source.id,
                source_file=os.path.basename(file_name)[:512],
                started_at=datetime.now(timezone.utc),
                status=IngestionStatus.RUNNING,
            ),
        )
        return run, content

    def ingest_mt940_batch(
        self,
        db: Session,
        *,
        run: IngestionRun,
        entries: List[ParsedEntry],
        errors: List[str],
    ) -> tuple:
        """Persist one batch of DAG-extracted MT940 entries (reco_id resolved by the
        DAG). stable_hash=True keeps the identity reco_id-independent so a still
        unresolved reco_id can be filled later (pre-match sweep) without changing
        the hash, and re-pushing the same file stays a no-op."""
        result = ParseResult(entries=entries, errors=errors)
        inserted, dup = self._persist(db, flow=run.flow, result=result, run=run, stable_hash=True)

        error_log = run.error
        if errors:
            combined = (error_log + "\n" if error_log else "") + "\n".join(errors[:50])
            error_log = combined[:4096]
        ingestion_run_repository.update(
            db,
            run=run,
            rows_in=(run.rows_in or 0) + len(entries) + len(errors),
            rows_ok=(run.rows_ok or 0) + inserted,
            rows_ko=(run.rows_ko or 0) + len(errors),
            rows_duplicate=(run.rows_duplicate or 0) + dup,
            error=error_log,
        )
        return inserted, dup

    def complete_mt940_run(
        self,
        db: Session,
        *,
        run: IngestionRun,
        file_name: str,
        failed: bool = False,
        error: Optional[str] = None,
    ) -> IngestionRun:
        """Close an MT940 run (SUCCESS/PARTIAL/FAILED) and move the file out of the
        inbox to processed (success) or error (failure)."""
        if failed:
            status = IngestionStatus.FAILED
        elif (run.rows_ko or 0) > 0:
            status = IngestionStatus.PARTIAL
        else:
            status = IngestionStatus.SUCCESS

        flow, source = run.flow, run.flow_source
        inbox_dir = self._inbox_dir(source, flow, settings.INBOX_BASE_PATH)
        real_path = os.path.realpath(os.path.join(inbox_dir, os.path.basename(file_name)))
        if os.path.isfile(real_path) and real_path.startswith(os.path.realpath(inbox_dir) + os.sep):
            dest_base = settings.INBOX_ERROR_PATH if failed else settings.INBOX_PROCESSED_PATH
            self._move_to(real_path, dest_base, source, flow)

        error_log = run.error
        if error:
            combined = (error_log + "\n" if error_log else "") + error
            error_log = combined[:4096]
        return ingestion_run_repository.update(
            db,
            run=run,
            finished_at=datetime.now(timezone.utc),
            status=status,
            error=error_log,
        )

    # --------------------------------------------------------------
    # Internals
    # --------------------------------------------------------------
    def _resolve_finacle_lookup(
        self, db: Session, *, flow: Flow, source: FlowSource, entries: List[ParsedEntry]
    ) -> None:
        """Fill reco_id for parsed entries by matching their reference key (ref_no)
        against already-ingested finacle entries of the same flow."""
        prefix = (source.parser_config or {}).get("reco_id_prefix")
        for e in entries:
            if e.reco_id is not None or not e.ref_no:
                continue
            keys = reconciliation_entry_repository.reference_keys(e.ref_no, prefix)
            e.reco_id = reconciliation_entry_repository.find_finacle_reco_id(
                db, flow_id=flow.id, keys=keys
            )

    def _persist(
        self,
        db: Session,
        *,
        flow: Flow,
        result: ParseResult,
        run: IngestionRun,
        finacle: bool = False,
        stable_hash: bool = False,
    ) -> tuple:
        rows = [
            self._to_db_row(flow, e, run.id, finacle=finacle, stable_hash=stable_hash)
            for e in result.entries
        ]
        if finacle:
            # Finacle reco_id is an enrichment recomputed each run → upsert so that
            # an unresolved movement's reco_id can be updated in place on retry.
            inserted, updated, skipped = reconciliation_entry_repository.upsert_finacle(db, rows)
            return inserted + updated, skipped
        inserted, skipped = reconciliation_entry_repository.bulk_insert(db, rows)
        return inserted, skipped

    @staticmethod
    def _to_db_row(
        flow: Flow, e: ParsedEntry, run_id: int, *, finacle: bool = False, stable_hash: bool = False
    ) -> dict:
        return {
            "flow_id": flow.id,
            "ingestion_run_id": run_id,
            "reco_id": e.reco_id,
            "account": e.account,
            "currency": e.currency,
            "amount": e.amount,
            "direction": e.direction,
            "value_date": e.value_date,
            "operation_date": e.operation_date,
            "event_type": e.event_type,
            "external_ref": e.external_ref,
            "file_name": e.file_name,
            "transaction_particulars": e.transaction_particulars,
            "ref_no": e.ref_no,
            "remarks_1": e.remarks_1,
            "transaction_id": e.transaction_id,
            "payload_raw": e.payload_raw,
            # Identity must exclude reco_id when it is resolved AFTER ingestion
            # (finacle, or file sources looking reco_id up against finacle) so it
            # stays stable while reco_id is filled in / re-ingested.
            "source_hash": (
                e.compute_finacle_hash(flow.id)
                if finacle
                else e.compute_source_hash(flow.id, include_reco_id=not stable_hash)
            ),
            "status": "pending",
        }

    def _inbox_dir(self, source: FlowSource, flow: Flow, base: str) -> str:
        sub = source.inbox_subfolder or flow.code
        return os.path.join(base, sub)

    def _move_to(self, path: str, base: str, source: FlowSource, flow: Flow) -> None:
        try:
            sub = source.inbox_subfolder or flow.code
            dest_dir = os.path.join(base, sub)
            os.makedirs(dest_dir, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            dest = os.path.join(dest_dir, f"{stamp}__{os.path.basename(path)}")
            shutil.move(path, dest)
        except Exception:
            pass


ingestion_service = IngestionService()
