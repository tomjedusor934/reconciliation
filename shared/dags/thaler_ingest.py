"""DAG : thaler_ingest
---------------------
Extraction incrémentale des mouvements comptables Thaler (TDREXP.CGD30)
vers notre PostgreSQL applicatif (reco.reconciliation_entry) via le backend.

Connexions Oracle :
  - TDR  (ADENZA_USR_RO) — lecture seule sur CGD30, ZCD01, ZZ164  <- UNIQUE connexion Oracle

Le DAG ne touche JAMAIS a la base Oracle en ecriture.

Flux :
  extract_group
    get_watermark          : MAX(value_date) du flow Thaler dans notre PostgreSQL
    extract_cgd30_data     : SELECT CGD30 + ZCD01 (IBAN) + ZZ164 (NOSTRO) Oracle read-only
  transform_group
    transform_and_push     : formatage ParsedEntry POST /tasks/ingest/thaler/push PostgreSQL
  audit_group
    audit_load             : log des compteurs inseres / dupliques
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from airflow.decorators import dag, task, task_group

logger = logging.getLogger(__name__)

ORACLE_CONN_ID = "TDR"
FLOW_CODE = "thaler"
SOURCE_CODE = "thaler_oracle"
NOSTRO_FILTER_VAR = "THALER_NOSTRO_FILTER"
NOSTRO_FILTER_DEFAULT = "EUR-BCEE-IP"
PUSH_BATCH_SIZE = 2000


def _get_nostro_filter() -> str:
    try:
        from airflow.sdk import Variable
        try:
            return Variable.get(NOSTRO_FILTER_VAR)
        except KeyError:
            pass
    except Exception:
        pass
    return os.environ.get(NOSTRO_FILTER_VAR, NOSTRO_FILTER_DEFAULT)


def _clean_str(val) -> str:
    if val is None:
        return ""
    s = str(val).strip()
    if s.lower() in ("nan", "none", "null", "<na>", "nat"):
        return ""
    return s


def _safe_decimal(raw: str) -> Decimal:
    try:
        return Decimal(raw) if raw else Decimal("0")
    except InvalidOperation:
        return Decimal("0")


def _parse_yyyymmdd(s: str):
    s = (s or "").strip()
    if not s or s == "0" or len(s) != 8:
        return None
    try:
        return datetime(int(s[0:4]), int(s[4:6]), int(s[6:8]), tzinfo=timezone.utc)
    except (ValueError, IndexError):
        return None


def _parse_operation_dt(date_str: str, time_str: str):
    d = _parse_yyyymmdd(date_str)
    if d is None:
        return None
    ts = (time_str or "").strip().zfill(6)
    try:
        return d.replace(hour=int(ts[0:2]), minute=int(ts[2:4]), second=int(ts[4:6]))
    except (ValueError, IndexError):
        return d


default_args = {
    "owner": "reconciliation",
    "depends_on_past": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}


@dag(
    dag_id="thaler_ingest",
    description="Ingestion incrementale Thaler CGD30 vers PostgreSQL reco.reconciliation_entry",
    schedule=None,
    start_date=datetime(2025, 1, 1),
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    tags=["reconciliation", "ingest", "thaler", "oracle"],
    default_args=default_args,
)
def thaler_ingest():

    @task_group(group_id="extract_group")
    def extract_group():

        @task(task_id="get_watermark")
        def get_watermark() -> str:
            """Recupere MAX(value_date) du flow Thaler depuis notre PostgreSQL.
            Retourne YYYYMMDD pour le filtre Oracle DATCTA, ou '0' si vide."""
            try:
                from reco_common import task_ingest_watermark
                watermark_iso = task_ingest_watermark(FLOW_CODE)
                if watermark_iso:
                    dt = datetime.fromisoformat(watermark_iso.replace("Z", "+00:00"))
                    yyyymmdd = dt.strftime("%Y%m%d")
                    logger.info("Watermark PostgreSQL : %s -> filtre Oracle DATCTA > '%s'", watermark_iso, yyyymmdd)
                    return yyyymmdd
                else:
                    logger.info("Aucune entree Thaler en base -- chargement complet (watermark='0').")
                    return "0"
            except Exception as exc:
                logger.warning("Impossible de recuperer le watermark (%s) -- chargement complet.", exc)
                return "0"

        @task(task_id="extract_cgd30_data")
        def extract_cgd30_data(watermark: str) -> str:
            """Extrait les lignes CGD30 posterieures au watermark.
            Connexion Oracle TDR (read-only uniquement). Ecrit JSON temporaire."""
            from airflow.providers.oracle.hooks.oracle import OracleHook

            nostro_filter = _get_nostro_filter()
            logger.info("Extraction CGD30 | NOSTRO=%s | DATCTA > '%s'", nostro_filter, watermark)

            hook = OracleHook(oracle_conn_id=ORACLE_CONN_ID)
            conn = hook.get_conn()
            cursor = conn.cursor()
            tmp_path = None
            try:
                sql = """
                    SELECT
                        CGD30.DATMAJ,
                        CGD30.HEUMAJ,
                        CGD30.DATCTA,
                        CGD30.REFCTA,
                        CGD30.NUMCPT,
                        CGD30.NUMSEQ,
                        CGD30.REFEXN,
                        CGD30.SGNMVT,
                        CGD30.MONMVT,
                        CGD30.DEVMVT,
                        NVL(ZCD01.NUMCPT_IBA, '') AS NUMCPT_IBA
                    FROM TDREXP.CGD30 CGD30
                    LEFT JOIN TDREXP.ZCD01 ZCD01
                        ON  ZCD01.NUMCPT     = CGD30.NUMCPT
                        AND ZCD01.swiact_tdr = 'Y'
                    WHERE CGD30.swiact_tdr = 'Y'
                      AND CGD30.NUMCPT IN (
                            SELECT ZONTBL
                            FROM   TDREXP.ZZ164
                            WHERE  ARGTBL     = :nostro_filter
                              AND  swiact_tdr = 'Y'
                      )
                      AND CGD30.DATCTA > :watermark
                """
                cursor.execute(sql, {"nostro_filter": nostro_filter, "watermark": watermark})
                columns = [col[0] for col in cursor.description]
                rows = cursor.fetchall()
                logger.info("%d lignes extraites de CGD30.", len(rows))

                records = [
                    {col: (str(val) if val is not None else "") for col, val in zip(columns, row)}
                    for row in rows
                ]

                fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="thaler_extract_")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(records, fh, ensure_ascii=False)

                logger.info("Fichier temporaire ecrit : %s (%d lignes)", tmp_path, len(records))
                return tmp_path

            except Exception:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                raise
            finally:
                cursor.close()
                conn.close()

        wm = get_watermark()
        return extract_cgd30_data(wm)

    @task_group(group_id="transform_group")
    def transform_group(extract_path: str):

        @task(task_id="transform_and_push")
        def transform_and_push(extract_path: str) -> dict:
            """Transforme les lignes CGD30 en ParsedEntry et les pousse vers
            notre PostgreSQL via POST /tasks/ingest/thaler/push.
            Aucune ecriture Oracle."""
            from reco_common import task_ingest_push

            # Si le fichier n'existe plus (retry apres suppression par tentative precedente),
            # on retourne gracieusement sans erreur — les donnees du batch manquant seront
            # recuperees lors du prochain run incremental (watermark-based).
            if not extract_path or not os.path.exists(extract_path):
                logger.warning(
                    "Fichier d'extraction '%s' introuvable (retry apres suppression ?)."
                    " Aucune donnee a pousser pour ce run.",
                    extract_path,
                )
                return {"inserted": 0, "skipped": 0, "run_id": None}

            try:
                with open(extract_path, "r", encoding="utf-8") as fh:
                    records = json.load(fh)
            finally:
                if extract_path and os.path.exists(extract_path):
                    try:
                        os.remove(extract_path)
                    except OSError:
                        pass

            if not records:
                logger.info("Aucune ligne a pousser (extraction vide).")
                return {"inserted": 0, "skipped": 0, "run_id": None}

            entries = []
            nb_bad_date = 0

            for rec in records:
                datcta = _clean_str(rec.get("DATCTA", ""))
                value_date = _parse_yyyymmdd(datcta)
                if value_date is None:
                    nb_bad_date += 1
                    logger.warning("DATCTA invalide ('%s') -- ligne ignoree.", datcta)
                    continue

                datmaj = _clean_str(rec.get("DATMAJ", ""))
                heumaj = _clean_str(rec.get("HEUMAJ", ""))
                operation_date = _parse_operation_dt(datmaj, heumaj) or value_date

                refcta = _clean_str(rec.get("REFCTA", ""))
                numcpt = _clean_str(rec.get("NUMCPT", ""))
                numseq = _clean_str(rec.get("NUMSEQ", ""))
                numseq_fmt = numseq.zfill(6) if numseq else ""

                refexn = _clean_str(rec.get("REFEXN", ""))
                sgnmvt = _clean_str(rec.get("SGNMVT", "")).upper()
                monmvt = _safe_decimal(_clean_str(rec.get("MONMVT", "0")))
                devmvt = _clean_str(rec.get("DEVMVT", "EUR")) or "EUR"
                iban = _clean_str(rec.get("NUMCPT_IBA", ""))

                if sgnmvt == "D":
                    amount = float(-abs(monmvt))
                    direction = "debit"
                else:
                    amount = float(abs(monmvt))
                    direction = "credit"

                reco_id = (
                    f"{datcta}-{refcta}-{numseq_fmt}"
                    if refcta
                    else f"{datcta}-{numcpt}-{numseq_fmt}"
                )

                entries.append({
                    "reco_id": reco_id,
                    "account": iban if iban else numcpt,
                    "currency": devmvt,
                    "amount": str(amount),
                    "direction": direction,
                    "value_date": value_date.isoformat(),
                    "operation_date": operation_date.isoformat(),
                    "event_type": refcta or None,
                    "external_ref": refexn or None,
                    "file_name": "extract from CGD30",
                    "payload_raw": dict(rec),
                })

            if nb_bad_date:
                logger.warning("%d lignes ignorees (DATCTA invalide).", nb_bad_date)

            logger.info("%d entrees pretes pour PostgreSQL.", len(entries))

            if not entries:
                return {"inserted": 0, "skipped": 0, "run_id": None}

            total_inserted = 0
            total_skipped = 0
            last_run_id = None
            nb_batches = -(-len(entries) // PUSH_BATCH_SIZE)

            for i in range(0, len(entries), PUSH_BATCH_SIZE):
                batch = entries[i: i + PUSH_BATCH_SIZE]
                result = task_ingest_push(FLOW_CODE, batch, SOURCE_CODE)
                data = result.get("data", {})
                b_ins = data.get("inserted", 0)
                b_dup = data.get("skipped", 0)
                total_inserted += b_ins
                total_skipped += b_dup
                last_run_id = data.get("run_id")
                logger.info(
                    "Batch %d/%d -> %d inserees, %d dupliquees.",
                    i // PUSH_BATCH_SIZE + 1, nb_batches, b_ins, b_dup,
                )

            return {"inserted": total_inserted, "skipped": total_skipped, "run_id": last_run_id}

        return transform_and_push(extract_path)

    @task_group(group_id="audit_group")
    def audit_group(push_result: dict):

        @task(task_id="audit_load")
        def audit_load(push_result: dict) -> None:
            logger.info(
                "[AUDIT Thaler] Inserees : %d | Doublons ignores : %d | IngestionRun ID : %s",
                push_result.get("inserted", 0),
                push_result.get("skipped", 0),
                push_result.get("run_id"),
            )

        audit_load(push_result)

    extract = extract_group()
    transform = transform_group(extract)
    audit_group(transform)


thaler_ingest()
