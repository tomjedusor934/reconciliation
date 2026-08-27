"""
DAG: WERO_RECONCILIATION

Objectif
--------
Réconciliation WERO ↔ FINACLE.
- Extraction de toutes les transactions FINACLE (INITGMODULE IN ('WERO','WEROEMC'))
- Jointure LEFT FINACLE ← WERO (F_E2E = W_ORG_REF)
- MARK_OFF_STATUS Y si émargé avec WERO.RECONCILIATION, N sinon
- Distinction P2P (NCP/WERO) vs EMC (NCC/WEROEMC)
- Cible : <target_db>.wero.WERO_RECON_RESULT

Variables d'environnement
-------------------------
Les noms de bases (odspfindba/odspfindbt, regdma/regdmp) et les conn_id Airflow
sont résolus dynamiquement via le module parallel_run et les Params du DAG.
Ne pas hardcoder de noms de bases ici — utiliser resolve_* à chaque task.
"""

from __future__ import annotations

import csv
import logging
import os
import shutil
import sys
import subprocess
import tempfile
from datetime import datetime, timedelta

import pandas as pd
from airflow import DAG
from airflow.models import Param
from airflow.operators.python import PythonOperator
from airflow.providers.microsoft.mssql.hooks.mssql import MsSqlHook

# Module utilitaire partagé : résolution des conn_id et noms de bases
# selon l'environnement (ACC→regdma/ODS, PRD→regdmp/ODSP, TNG→regdmt/ODST)
sys.path.insert(0, '/POST_HOME/programs/datamart/utils')
try:
    from parallel_run import (
        resolve_target_db       as _resolve_target_db,
        resolve_datamart_conn_id as _resolve_dm_conn,
        resolve_ods_conn_id      as _resolve_ods_conn,
        resolve_ods_db           as _resolve_ods_db,
        build_target_db_param    as _build_target_db_param,
    )
    HAS_PARALLEL_RUN = True
except ImportError:
    HAS_PARALLEL_RUN = False
    # Fallback pour les environnements sans parallel_run (dev local)
    def _resolve_target_db(kwargs):      return "regdma"
    def _resolve_dm_conn(db):            return "datamart"
    def _resolve_ods_conn(db):           return "ODS"
    def _resolve_ods_db(db):             return "odspfindba"
    def _build_target_db_param():        return {"target_db": Param("regdma", type="string")}

try:
    import pyodbc
    HAS_PYODBC = True
except Exception:
    pyodbc = None
    HAS_PYODBC = False

# ---------------------------------------------------------------------
# CONFIGURATION — pas de noms de bases hardcodés ici
# Tous les noms de bases/conn_id sont résolus au runtime dans chaque task
# via _resolve_target_db(kwargs), _resolve_dm_conn(db), _resolve_ods_db(db)
# ---------------------------------------------------------------------
TARGET_TABLE  = "WERO_RECON_RESULT"
TARGET_SCHEMA_SUFFIX = "wero"         # <target_db>.wero.WERO_RECON_RESULT
WERO_SCHEMA   = "WERO"
WERO_TABLE    = "RECONCILIATION"
FINACLE_SCHEMA = "FINACLE_H"

FINACLE_PAYMORD_TABLES: list[str] = []  # Rempli dynamiquement au runtime
FINACLE_RETURN_TABLES:  list[str] = []
FINACLE_WHERE_EXTRA = ""
INCLUDE_FINACLE_ORPHANS = False


def _resolve_context(kwargs: dict) -> tuple[str, str, str, str, str]:
    """Résout au runtime : target_db, conn_id_tgt, conn_id_src, ods_db, target_qual."""
    target_db  = _resolve_target_db(kwargs)
    conn_tgt   = _resolve_dm_conn(target_db)
    conn_src   = _resolve_ods_conn(target_db)
    ods_db     = _resolve_ods_db(target_db)
    target_qual = f"{target_db}.{TARGET_SCHEMA_SUFFIX}.{TARGET_TABLE}"
    return target_db, conn_tgt, conn_src, ods_db, target_qual


def discover_finacle_paymord_tables(src_hook: MsSqlHook, ods_db: str) -> list[str]:
    """Découvre dynamiquement toutes les tables *PAYMORD* et *RETURN* dans FINACLE_H
    sur toutes les bases accessibles (ods_db + regdma/regdmdra/regdmp si disponibles)."""
    # Chercher dans toutes les DBs accessibles contenant FINACLE_H
    dbs_to_search = [ods_db]
    for extra_db in ['regdma', 'regdmdra', 'regdmp']:
        if extra_db != ods_db:
            dbs_to_search.append(extra_db)

    tables = []
    for db in dbs_to_search:
        try:
            sql = f"""
                SELECT TABLE_SCHEMA + '.' + TABLE_NAME AS full_name
                FROM {db}.INFORMATION_SCHEMA.TABLES
                WHERE TABLE_SCHEMA = '{FINACLE_SCHEMA}'
                  AND (TABLE_NAME LIKE '%PAYMORD%' OR TABLE_NAME LIKE '%RETURN%')
                  AND TABLE_TYPE = 'BASE TABLE'
                ORDER BY TABLE_NAME
            """
            df = src_hook.get_pandas_df(sql)
            found = [f"{db}.{row['full_name']}" for _, row in df.iterrows()]
            if found:
                logger.info("Tables découvertes dans [%s]: %s", db, found)
                tables.extend(found)
        except Exception as e:
            logger.debug("DB [%s] inaccessible ou sans FINACLE_H : %s", db, e)

    logger.info("Tables PAYMORD/RETURN découvertes au total (%s): %s", len(tables), tables)
    return tables

BCP_BATCH_SIZE = 100000
FAST_EXECUTEMANY_BATCH_SIZE = 10000

logger = logging.getLogger(__name__)


def qident(identifier: str) -> str:
    return "[" + identifier.replace("]", "]]") + "]"


def split_schema_table(qualified: str) -> tuple[str, str]:
    parts = qualified.replace("[", "").replace("]", "").split(".")
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 3:
        # database.schema.table -> schema.table for object naming in current db
        return parts[1], parts[2]
    raise ValueError(f"Nom de table invalide: {qualified}")


def get_pyodbc_conn(hook: MsSqlHook):
    """Connexion pyodbc directe inspiree de commons_functions_load._get_pyodbc_conn."""
    if not HAS_PYODBC:
        return None

    conn_obj = hook.get_connection(hook.mssql_conn_id)
    host = conn_obj.host or "localhost"
    port = conn_obj.port or 1433
    user = conn_obj.login or ""
    password = conn_obj.password or ""
    database = hook.schema or conn_obj.schema

    if not database:
        extra = conn_obj.extra_dejson if hasattr(conn_obj, "extra_dejson") else {}
        database = extra.get("database", extra.get("schema", ""))

    drivers = pyodbc.drivers()
    driver = None
    for candidate in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"):
        if candidate in drivers:
            driver = candidate
            break
    if not driver:
        return None

    conn_str = (
        f"DRIVER={{{driver}}};"
        f"SERVER={host},{port};"
        f"DATABASE={database};"
        f"UID={user};PWD={password};"
        "TrustServerCertificate=yes;"
    )
    conn = pyodbc.connect(conn_str, autocommit=False, timeout=900)
    cur = conn.cursor()
    cur.execute("SET NOCOUNT ON")
    cur.execute("SET XACT_ABORT ON")
    cur.execute("SET LOCK_TIMEOUT 600000")
    cur.close()
    conn.commit()
    return conn


def get_bcp_path() -> str | None:
    for path in (
        shutil.which("bcp"),
        "/opt/mssql-tools18/bin/bcp",
        "/opt/mssql-tools/bin/bcp",
        "/usr/local/bin/bcp",
    ):
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def get_bcp_conn_args(hook: MsSqlHook) -> list[str] | None:
    conn_obj = hook.get_connection(hook.mssql_conn_id)
    host = conn_obj.host or "localhost"
    port = conn_obj.port or 1433
    user = conn_obj.login or ""
    password = conn_obj.password or ""
    database = hook.schema or conn_obj.schema

    if not database:
        extra = conn_obj.extra_dejson if hasattr(conn_obj, "extra_dejson") else {}
        database = extra.get("database", extra.get("schema", ""))

    if not database or not user:
        return None

    # Alignement sur la connection string pyodbc (TrustServerCertificate=yes, sans Encrypt=strict).
    # Le serveur regdmartdbt01 rejette -Ys (strict → WSAECONNRESET 0x2746 = TCP reset pendant TLS).
    #   -Yo  → encrypt=optional : chiffrement si dispo, pas forcé (= comportement pyodbc par défaut)
    #   -u   → TrustServerCertificate (= pyodbc TrustServerCertificate=yes)
    # PAS de -d : BCP interdit -d avec un nom de table en 3 parties (database.schema.table).
    return ["-S", f"{host},{port}", "-U", user, "-P", password, "-Yo", "-u"]


TARGET_COLUMNS = [
    "CREATION_DATE", "CREATION_TIME", "RECORD_TYPE",
    "MESSAGE_ID", "FINACLE_ID", "ORIGINATOR_REF", "ACCOUNT_NUMBER",
    "OPERATION_TYPE", "WERO_STATUS", "FINACLE_STATUS", "RECON_DIAGNOSIS",
    "AMOUNT_WERO", "AMOUNT_FINACLE", "DELTA_AMOUNT", "CURRENCY",
    "MARK_OFF_STATUS", "MARK_OFF_DATE", "MARK_OFF_TIME", "FILE_NAME",
    "SETTLEMENT_TS",
]


def create_result_table_task(**kwargs):
    """Crée le schéma wero et la table WERO_RECON_RESULT si elle n'existe pas encore.

    Ne droppe PAS la table existante — les données historiques sont préservées.
    La suppression du jour courant est faite par clean_current_run_task (DELETE WHERE CREATION_DATE=today).
    """
    target_db, conn_tgt, _, _, target_qual = _resolve_context(kwargs)
    hook = MsSqlHook(mssql_conn_id=conn_tgt)
    logger.info("Init table cible [%s] (env: %s)", target_qual, target_db)

    hook.run(f"IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = '{TARGET_SCHEMA_SUFFIX}') EXEC('CREATE SCHEMA {TARGET_SCHEMA_SUFFIX}')")

    hook.run(f"""
    IF OBJECT_ID(N'{target_qual}', N'U') IS NULL
    CREATE TABLE {target_qual}(
        ID              bigint IDENTITY(1,1) NOT NULL PRIMARY KEY,
        CREATION_DATE   varchar(8)      NULL,
        CREATION_TIME   varchar(8)      NULL,
        RECORD_TYPE     varchar(20)     NULL,
        MESSAGE_ID      nvarchar(100)   NULL,
        FINACLE_ID      nvarchar(100)   NULL,
        ORIGINATOR_REF  nvarchar(100)   NULL,
        ACCOUNT_NUMBER  nvarchar(50)    NULL,
        OPERATION_TYPE  nvarchar(20)    NULL,
        WERO_STATUS     nvarchar(50)    NULL,
        FINACLE_STATUS  nvarchar(50)    NULL,
        RECON_DIAGNOSIS nvarchar(30)    NULL,
        AMOUNT_WERO     decimal(19,2)   NULL,
        AMOUNT_FINACLE  decimal(19,2)   NULL,
        DELTA_AMOUNT    decimal(19,2)   NULL,
        CURRENCY        nvarchar(3)     NULL,
        MARK_OFF_STATUS char(1)         NULL,
        MARK_OFF_DATE   varchar(8)      NULL,
        MARK_OFF_TIME   varchar(8)      NULL,
        FILE_NAME       nvarchar(255)   NULL,
        SETTLEMENT_TS   nvarchar(50)    NULL
    )
    """)
    logger.info("Table %s vérifiée/créée.", target_qual)

    # Ajout automatique des colonnes manquantes (idempotent)
    expected_cols = {
        "MESSAGE_ID":      "nvarchar(100)",
        "FINACLE_ID":      "nvarchar(100)",
        "ORIGINATOR_REF":  "nvarchar(100)",
        "ACCOUNT_NUMBER":  "nvarchar(50)",
        "OPERATION_TYPE":  "nvarchar(20)",
        "WERO_STATUS":     "nvarchar(50)",
        "FINACLE_STATUS":  "nvarchar(50)",
        "RECON_DIAGNOSIS": "nvarchar(30)",
        "AMOUNT_WERO":     "decimal(19,2)",
        "AMOUNT_FINACLE":  "decimal(19,2)",
        "DELTA_AMOUNT":    "decimal(19,2)",
        "CURRENCY":        "nvarchar(3)",
        "MARK_OFF_STATUS": "char(1)",
        "MARK_OFF_DATE":   "varchar(8)",
        "MARK_OFF_TIME":   "varchar(8)",
        "FILE_NAME":       "nvarchar(255)",
        "SETTLEMENT_TS":   "nvarchar(50)",
    }
    schema, table = TARGET_SCHEMA_SUFFIX, TARGET_TABLE
    for col_name, col_type in expected_cols.items():
        hook.run(f"""
            IF NOT EXISTS (
                SELECT 1 FROM {target_db}.INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = '{schema}' AND TABLE_NAME = '{table}' AND COLUMN_NAME = '{col_name}'
            )
            BEGIN
                ALTER TABLE {target_qual} ADD {col_name} {col_type} NULL;
            END
        """)
    logger.info("Colonnes vérifiées/ajoutées sur %s.", target_qual)


def clean_current_run_task(**kwargs):
    _, conn_tgt, _, _, target_qual = _resolve_context(kwargs)
    hook = MsSqlHook(mssql_conn_id=conn_tgt)
    today = datetime.now().strftime("%Y%m%d")
    hook.run(f"DELETE FROM {target_qual} WHERE CREATION_DATE = %s", parameters=(today,))
    logger.info("Données du run %s nettoyées dans %s.", today, target_qual)


# Taille des batches pour le filtre IN-clause envoyé à ODS.
# SQL Server supporte jusqu'à ~2100 paramètres par requête ; 500 est une valeur sûre.
WERO_KEYS_BATCH_SIZE = 500


def _keys_to_in_clause(keys: list[str]) -> str:
    """Construit un littéral IN(…) sécurisé pour un batch de clés WERO.

    Les clés passent par repr() Python → apostrophes doublées → injection impossible.
    """
    escaped = ["'" + k.replace("'", "''") + "'" for k in keys]
    return "(" + ", ".join(escaped) + ")"


def _expand_uuid_variants(keys: list[str]) -> list[str]:
    """Pour chaque clé, ajoute la variante avec/sans tirets UUID.

    WERO stocke CaptureIDMoneyTransferID avec tirets : '019edb2f-1c12-72d3-9345-91a6317de646'
    FINACLE peut stocker transactionref sans tirets   : '019edb2f1c1272d3934591a6317de646'
    → on envoie les deux formes dans la IN-clause pour garantir le match.
    """
    expanded = []
    seen = set()
    for k in keys:
        variants = {k}
        # Variante sans tirets (si la clé ressemble à un UUID avec tirets)
        no_dash = k.replace("-", "")
        if no_dash != k:
            variants.add(no_dash)
        # Variante avec tirets (si la clé ressemble à un UUID sans tirets, 32 hex chars)
        if len(k) == 32 and all(c in "0123456789abcdefABCDEF" for c in k):
            with_dash = f"{k[0:8]}-{k[8:12]}-{k[12:16]}-{k[16:20]}-{k[20:]}"
            variants.add(with_dash)
        for v in variants:
            if v not in seen:
                seen.add(v)
                expanded.append(v)
    return expanded


def fetch_finacle_candidates(src_hook: MsSqlHook, wero_keys: list[str]) -> pd.DataFrame:
    """Récupère les lignes FINACLE correspondant aux clés WERO par batches IN-clause.

    Stratégie :
    - Deux serveurs distincts → pas de linked server → pas de JOIN cross-serveur.
    - On ne peut pas créer de table dans ODS (pas de droits DDL).
    - On envoie les clés WERO (+ variantes sans/avec tirets UUID) dans le WHERE
      via des IN(…) par batches de WERO_KEYS_BATCH_SIZE.
    """
    if not wero_keys:
        return pd.DataFrame()

    # Expansion UUID : WERO stocke CaptureIDMoneyTransferID avec tirets UUID,
    # FINACLE peut stocker transactionref/endtoendidentification sans tirets (ou l'inverse).
    expanded_keys = _expand_uuid_variants(wero_keys)
    if len(expanded_keys) != len(wero_keys):
        logger.info("Expansion UUID: %s clés WERO → %s variantes (avec/sans tirets).",
                    len(wero_keys), len(expanded_keys))
        logger.info("Exemple variantes: %s", expanded_keys[:6])

    all_chunks: list[pd.DataFrame] = []

    for batch_start in range(0, len(expanded_keys), WERO_KEYS_BATCH_SIZE):
        batch = expanded_keys[batch_start: batch_start + WERO_KEYS_BATCH_SIZE]
        in_clause = _keys_to_in_clause(batch)

        queries = []

        for tbl in FINACLE_PAYMORD_TABLES:
            queries.append(f"""
            SELECT
                CAST(transactionref AS nvarchar(100))          AS F_REF,
                CAST(endtoendidentification AS nvarchar(100))  AS F_E2E,
                TRY_CAST(instructedamt AS decimal(19,2))       AS F_AMT,
                CAST(instructedamtcrncy AS nvarchar(3))        AS F_CCY,
                CAST(derivedstatus AS nvarchar(50))            AS F_STATUS,
                CAST('{tbl}' AS nvarchar(255))                 AS F_SRC,
                CAST(dracct AS nvarchar(50))                   AS F_ACCT,
                -- Distinction P2P vs EMC (mail Bhaswanth M.) :
                -- servicetype/paymenttypecode diffèrent entre les deux flows
                CAST(ISNULL(servicetype, '') AS nvarchar(20))        AS F_SERVICE_TYPE,
                CAST(ISNULL(paymenttypecode, '') AS nvarchar(20))    AS F_PAYMENT_TYPE,
                CAST(ISNULL(vopresult, '') AS nvarchar(20))          AS F_VOP_RESULT,
                CAST(ISNULL(serviceid, '') AS nvarchar(20))          AS F_SERVICE_ID
            FROM {tbl} f
            WHERE f.ods_active = 1
              {FINACLE_WHERE_EXTRA}
              AND f.endtoendidentification IN {in_clause}
            """)

        for tbl in FINACLE_RETURN_TABLES:
            queries.append(f"""
            SELECT
                CAST(returnid AS nvarchar(100)) AS F_REF,
                CAST(orgnlinstrid AS nvarchar(100)) AS F_E2E,
                TRY_CAST(returnsttlmamt AS decimal(19,2)) AS F_AMT,
                CAST(returnsttlmamtccy AS nvarchar(3)) AS F_CCY,
                CAST(status AS nvarchar(50)) AS F_STATUS,
                CAST('{tbl}' AS nvarchar(255)) AS F_SRC,
                CAST(dracct AS nvarchar(50)) AS F_ACCT
            FROM {tbl} f
            WHERE f.ods_active = 1
              {FINACLE_WHERE_EXTRA}
              AND (
                    f.returnid     IN {in_clause}
                 OR f.orgnlinstrid IN {in_clause}
              )
            """)

        sql = "\nUNION ALL\n".join(queries)
        logger.info(
            "Extraction FINACLE batch %s-%s / %s variantes clés...",
            batch_start + 1, min(batch_start + WERO_KEYS_BATCH_SIZE, len(expanded_keys)), len(expanded_keys),
        )
        chunk = src_hook.get_pandas_df(sql)
        all_chunks.append(chunk)

    if not all_chunks:
        return pd.DataFrame()

    result = pd.concat(all_chunks, ignore_index=True)
    # Plusieurs lignes FINACLE par endtoendidentification (étapes de traitement distinctes).
    # On garde la dernière ligne par F_E2E + F_SRC (ordre d'apparition = ordre de traitement).
    result = result.drop_duplicates(subset=["F_E2E", "F_SRC"], keep="last")
    logger.info("FINACLE après dédoublonnage par e2e: %s lignes.", len(result))
    return result


def load_dataframe_bcp_or_fast_executemany(tgt_hook: MsSqlHook, df: pd.DataFrame):
    if df.empty:
        logger.info("Aucune ligne à charger.")
        return

    bcp_path = get_bcp_path()
    bcp_args = get_bcp_conn_args(tgt_hook) if bcp_path else None

    if bcp_path and bcp_args:
        field_term = "\x1c"
        row_term = "\n"
        tmp = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", suffix=".csv", delete=False)
        err_file = tmp.name + ".err"
        try:
            def _clean(v) -> str:
                if v is None or (isinstance(v, float) and pd.isna(v)):
                    return ""
                s = str(v)
                for c in ("\x1c", "\n", "\r", "\x0b", "\x0c", "\x00"):
                    s = s.replace(c, " " if c != "\x00" else "")
                return s

            for row in df[TARGET_COLUMNS].itertuples(index=False, name=None):
                tmp.write(field_term.join([_clean(v) for v in row]) + "\n")
            tmp.close()

            # Diagnostic : afficher les premières lignes du CSV pour détecter les truncations
            with open(tmp.name, encoding="utf-8") as f_diag:
                csv_lines = f_diag.readlines()
            logger.info("CSV généré — headers: %s", TARGET_COLUMNS)
            for i, line in enumerate(csv_lines[:3], 1):
                fields = line.rstrip("\n").split("\x1c")
                for col, val in zip(TARGET_COLUMNS, fields):
                    if len(val) > 0:
                        logger.info("  CSV ligne %s | %-20s = [%s] (len=%s)", i, col, val[:80], len(val))

            cmd = [
                bcp_path, target_qual, "in", tmp.name,
                "-c", "-C", "UTF8",
                "-t", "0x1c", "-r", "0x0a",
                "-b", str(BCP_BATCH_SIZE),
                "-h", "TABLOCK",
                *bcp_args,
                "-e", err_file,
            ]
            safe_cmd = []
            mask = False
            for token in cmd:
                safe_cmd.append("***" if mask else token)
                mask = token == "-P"
            logger.info("BCP command: %s", " ".join(safe_cmd))

            result = subprocess.run(cmd, capture_output=True, text=True)
            # Logger stdout/stderr systématiquement pour faciliter le diagnostic
            if result.stdout.strip():
                logger.info("BCP stdout: %s", result.stdout.strip())
            if result.stderr.strip():
                logger.warning("BCP stderr: %s", result.stderr.strip())
            if result.returncode != 0:
                err_content = ""
                if os.path.exists(err_file):
                    with open(err_file, encoding="utf-8", errors="replace") as ef:
                        err_content = ef.read(4000)
                raise RuntimeError(
                    f"BCP failed rc={result.returncode}\n"
                    f"stdout: {result.stdout[:1000]}\n"
                    f"stderr: {result.stderr[:1000]}\n"
                    f"err_file: {err_content}"
                )
            logger.info("BCP terminé avec succès.")
            return
        finally:
            for path in (tmp.name, err_file):
                try:
                    if path and os.path.exists(path):
                        os.unlink(path)
                except Exception:
                    pass

    logger.warning("BCP indisponible, fallback pyodbc fast_executemany.")
    conn = get_pyodbc_conn(tgt_hook)
    if conn is None:
        conn = tgt_hook.get_conn()
    cur = conn.cursor()
    if HAS_PYODBC and hasattr(cur, "fast_executemany"):
        cur.fast_executemany = True

    placeholders = ", ".join("?" for _ in TARGET_COLUMNS)
    cols = ", ".join(qident(c) for c in TARGET_COLUMNS)
    sql = f"INSERT INTO {target_qual} ({cols}) VALUES ({placeholders})"

    rows = [tuple(None if pd.isna(v) else v for v in row) for row in df[TARGET_COLUMNS].itertuples(index=False, name=None)]
    for i in range(0, len(rows), FAST_EXECUTEMANY_BATCH_SIZE):
        cur.executemany(sql, rows[i:i + FAST_EXECUTEMANY_BATCH_SIZE])
        conn.commit()
        logger.info("fast_executemany: %s/%s lignes insérées", min(i + FAST_EXECUTEMANY_BATCH_SIZE, len(rows)), len(rows))
    cur.close()
    conn.close()


def extract_task(**context):
    """EXTRACT — Lit WERO et FINACLE, retourne les DataFrames bruts via XCom (fichier JSON)."""
    import tempfile, json

    _, _, conn_src, ods_db, _ = _resolve_context(context)
    src_hook = MsSqlHook(mssql_conn_id=conn_src)
    global FINACLE_PAYMORD_TABLES
    FINACLE_PAYMORD_TABLES = discover_finacle_paymord_tables(src_hook, ods_db)

    # ── EXTRACT FINACLE ──────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("EXTRACT FINACLE")
    logger.info("  Tables cherchées : %s", FINACLE_PAYMORD_TABLES)
    logger.info("  Filtre           : ods_active=1 AND initgmodule IN ('WERO','WEROEMC') %s", FINACLE_WHERE_EXTRA or "")

    fin_chunks = []
    for tbl in FINACLE_PAYMORD_TABLES:
        try:
            sql_fin = f"""
                SELECT
                    CAST(transactionref          AS nvarchar(100)) AS F_REF,
                    CAST(endtoendidentification  AS nvarchar(100)) AS F_E2E,
                    TRY_CAST(instructedamt       AS decimal(19,2)) AS F_AMT,
                    CAST(instructedamtcrncy      AS nvarchar(3))   AS F_CCY,
                    CAST(derivedstatus           AS nvarchar(50))  AS F_STATUS,
                    CAST('{tbl}'                 AS nvarchar(255)) AS F_SRC,
                    CAST(dracct                  AS nvarchar(50))  AS F_ACCT,
                    CAST(ISNULL(servicetype,     '') AS nvarchar(20)) AS F_SERVICE_TYPE,
                    CAST(ISNULL(paymenttypecode, '') AS nvarchar(20)) AS F_PAYMENT_TYPE,
                    CAST(ISNULL(vopresult,       '') AS nvarchar(20)) AS F_VOP_RESULT,
                    CAST(ISNULL(initgmodule,     '') AS nvarchar(20)) AS F_INITGMODULE
                FROM {tbl}
                WHERE ods_active = 1
                  AND initgmodule IN ('WERO', 'WEROEMC')
                {FINACLE_WHERE_EXTRA}
            """
            chunk = src_hook.get_pandas_df(sql_fin)
            logger.info("  [%s] : %s lignes", tbl, len(chunk))
            fin_chunks.append(chunk)
        except Exception as e:
            logger.warning("  [%s] extraction échouée: %s", tbl, e)

    df_fin = pd.concat(fin_chunks, ignore_index=True) if fin_chunks else pd.DataFrame()

    # Dédoublonnage : garder la dernière ligne par (F_E2E, F_SRC) = étape la plus avancée
    if not df_fin.empty:
        df_fin = df_fin.drop_duplicates(subset=["F_E2E", "F_SRC"], keep="last")
    logger.info("  Total FINACLE après dédoublonnage : %s lignes", len(df_fin))

    if df_fin.empty:
        logger.warning("Aucune donnée FINACLE — arrêt du traitement.")
        context["ti"].xcom_push(key="extract_path", value=None)
        return

    # ── EXTRACT WERO ─────────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("EXTRACT WERO")
    src_table_wero = f"{ods_db}.{WERO_SCHEMA}.{WERO_TABLE}"
    logger.info("  Table : %s", src_table_wero)
    sql_wero = f"""
        SELECT
            CAST(CaptureIDMoneyTransferID    AS nvarchar(100)) AS W_ID,
            CAST(OriginatorReference         AS nvarchar(100)) AS W_ORG_REF,
            TRY_CAST(TransactionAmount       AS decimal(19,2)) AS W_AMT,
            CAST(Currency                    AS nvarchar(3))   AS W_CCY,
            CAST(SettlementStatus            AS nvarchar(50))  AS W_STATUS,
            CAST(TransactionDirection        AS nvarchar(50))  AS W_DIR,
            CAST(SettlementRelatedTimestamp  AS nvarchar(50))  AS W_SETTLEMENT_TS
        FROM {src_table_wero}
        WHERE CaptureIDMoneyTransferID IS NOT NULL
    """
    df_wero = src_hook.get_pandas_df(sql_wero)
    logger.info("  Total WERO : %s lignes", len(df_wero))

    # Sérialisation
    tmp = tempfile.NamedTemporaryFile(delete=False, mode="w", encoding="utf-8",
                                      suffix=".json", prefix="wero_extract_")
    json.dump({
        "fin":  df_fin.to_json(orient="split"),
        "wero": df_wero.to_json(orient="split"),
        "paymord_tables": FINACLE_PAYMORD_TABLES,
    }, tmp)
    tmp.close()
    logger.info("Données extraites sérialisées dans %s", tmp.name)
    context["ti"].xcom_push(key="extract_path", value=tmp.name)


def transform_task(**context):
    """TRANSFORM — Join FINACLE (source) <- WERO (mark-off), calcul MARK_OFF_STATUS."""
    import json, tempfile, os
    from io import StringIO

    extract_path = context["ti"].xcom_pull(task_ids="extract", key="extract_path")
    if not extract_path:
        logger.warning("Pas de donnees a transformer.")
        context["ti"].xcom_push(key="transform_path", value=None)
        return

    with open(extract_path, encoding="utf-8") as f:
        payload = json.load(f)

    df_fin  = pd.read_json(StringIO(payload["fin"]),  orient="split")
    df_wero = pd.read_json(StringIO(payload["wero"]), orient="split")

    now = datetime.now()
    creation_date = now.strftime("%Y%m%d")
    creation_time = now.strftime("%H%M%S")

    logger.info("=" * 60)
    logger.info("TRANSFORM — %s lignes FINACLE + %s lignes WERO (mark-off)", len(df_fin), len(df_wero))

    # Normalisation des clés de join : suppression des tirets UUID
    # WERO stocke OriginatorReference avec tirets, FINACLE sans tirets (ou vice versa)
    if not df_wero.empty and "W_ORG_REF" in df_wero.columns:
        df_wero["W_ORG_REF_NORM"] = df_wero["W_ORG_REF"].astype(str).str.replace("-", "", regex=False).str.strip()
    if not df_fin.empty and "F_E2E" in df_fin.columns:
        df_fin["F_E2E_NORM"] = df_fin["F_E2E"].astype(str).str.replace("-", "", regex=False).str.strip()

    # ── OUTER JOIN FINACLE <-> WERO ──────────────────────────────────────────
    # FINACLE est la source principale. WERO est le référentiel de mark-off.
    # OUTER JOIN pour inclure aussi les lignes WERO sans contrepartie FINACLE.
    if not df_wero.empty:
        merged = pd.merge(df_fin, df_wero, left_on="F_E2E_NORM", right_on="W_ORG_REF_NORM", how="outer")
    else:
        merged = df_fin.copy()
        for col in ["W_ID", "W_ORG_REF", "W_AMT", "W_CCY", "W_STATUS", "W_DIR", "W_SETTLEMENT_TS"]:
            merged[col] = None

    has_wero    = merged["W_ORG_REF"].notna() if "W_ORG_REF" in merged.columns else pd.Series(False, index=merged.index)
    has_finacle = merged["F_E2E"].notna()      if "F_E2E"    in merged.columns else pd.Series(True,  index=merged.index)

    n_matched   = int((has_wero & has_finacle).sum())
    n_fin_only  = int((~has_wero & has_finacle).sum())
    n_wero_only = int((has_wero & ~has_finacle).sum())

    logger.info("=" * 60)
    logger.info("BILAN RECONCILIATION")
    logger.info("  Lignes FINACLE (après dédoublonnage) : %s", len(df_fin))
    logger.info("  Lignes WERO                          : %s", len(df_wero))
    logger.info("  Records émargés Y (FINACLE+WERO)     : %s", n_matched)
    logger.info("  Records non émargés N (FINACLE seul) : %s", n_fin_only)
    logger.info("  Records WERO sans FINACLE             : %s", n_wero_only)
    logger.info("=" * 60)

    # MARK_OFF : Y = matché (FINACLE+WERO), N = non matché (FINACLE seul ou WERO seul)
    merged["MARK_OFF_STATUS"] = (has_wero & has_finacle).map({True: "Y", False: "N"}).astype(str)
    merged["MARK_OFF_DATE"] = merged["MARK_OFF_STATUS"].eq("Y").map({True: creation_date, False: None})
    merged["MARK_OFF_TIME"] = merged["MARK_OFF_STATUS"].eq("Y").map({True: creation_time, False: None})

    def derive_operation_type(row) -> str:
        module = str(row.get("F_INITGMODULE", "") or "").upper()
        src    = str(row.get("F_SRC",          "") or "").upper()
        ptype  = str(row.get("F_PAYMENT_TYPE", "") or "").upper()
        vop    = str(row.get("F_VOP_RESULT",   "") or "").upper()
        if module == "WERO":    return "P2P"
        if module == "WEROEMC": return "EMC"
        if "NCP" in src: return "P2P"
        if "NCC" in src: return "EMC"
        if ptype == "EMC" or vop: return "EMC"
        return "UNKNOWN"

    merged["OPERATION_TYPE"] = merged.apply(derive_operation_type, axis=1)

    def _s(series: pd.Series, n: int) -> pd.Series:
        return series.astype(object).apply(
            lambda v: str(v)[:n] if v is not None and not (isinstance(v, float) and pd.isna(v)) else None
        )

    out = pd.DataFrame({
        "CREATION_DATE":  creation_date,
        "CREATION_TIME":  creation_time,
        "RECORD_TYPE":    "WERO_RECON",
        "MESSAGE_ID":     _s(merged.get("W_ID",     pd.Series(dtype=object, index=merged.index)), 100),
        "FINACLE_ID":     _s(merged.get("F_REF",    pd.Series(dtype=object, index=merged.index)), 100),
        "ORIGINATOR_REF": _s(merged.get("F_E2E",   pd.Series(dtype=object, index=merged.index)), 100),
        "ACCOUNT_NUMBER": _s(merged.get("F_ACCT",  pd.Series(dtype=object, index=merged.index)), 50),
        "OPERATION_TYPE": _s(merged["OPERATION_TYPE"], 20),
        "WERO_STATUS":    _s(merged.get("W_STATUS", pd.Series(dtype=object, index=merged.index)), 50),
        "FINACLE_STATUS": _s(merged.get("F_STATUS", pd.Series(dtype=object, index=merged.index)), 50),
        "RECON_DIAGNOSIS":_s(merged["MARK_OFF_STATUS"].map({"Y": "MATCHED", "N": "UNMATCHED"}), 30),
        "AMOUNT_WERO":    pd.to_numeric(merged.get("W_AMT", pd.Series(dtype=float, index=merged.index)), errors="coerce").fillna(0),
        "AMOUNT_FINACLE": pd.to_numeric(merged.get("F_AMT", pd.Series(dtype=float, index=merged.index)), errors="coerce").fillna(0),
        "DELTA_AMOUNT":   pd.to_numeric(merged.get("W_AMT", pd.Series(dtype=float, index=merged.index)), errors="coerce").fillna(0)
                        - pd.to_numeric(merged.get("F_AMT", pd.Series(dtype=float, index=merged.index)), errors="coerce").fillna(0),
        "CURRENCY":       _s(merged.get("W_CCY", pd.Series(dtype=object, index=merged.index))
                             .combine_first(merged.get("F_CCY", pd.Series(dtype=object, index=merged.index))), 3),
        "MARK_OFF_STATUS":_s(merged["MARK_OFF_STATUS"], 1),
        "MARK_OFF_DATE":  merged["MARK_OFF_DATE"].astype(object).where(merged["MARK_OFF_DATE"].notna(), None).apply(
                              lambda v: str(v)[:8] if v is not None else None),
        "MARK_OFF_TIME":  merged["MARK_OFF_TIME"].astype(object).where(merged["MARK_OFF_TIME"].notna(), None).apply(
                              lambda v: str(v)[:8] if v is not None else None),
        "FILE_NAME":      _s(merged.get("F_SRC", pd.Series(dtype=object, index=merged.index)).fillna(""), 255),
        "SETTLEMENT_TS":  _s(merged.get("W_SETTLEMENT_TS", pd.Series(dtype=object, index=merged.index)), 50),
    })
    out = out.where(pd.notnull(out), None)
    logger.info("DataFrame de sortie: %s lignes", len(out))

    # Sérialisation directe en CSV BCP-ready sans passer par csv.writer
    # (csv.writer avec QUOTE_NONE+escapechar peut introduire des \ qui décalent les colonnes)
    field_term = "\x1c"
    tmp = tempfile.NamedTemporaryFile(delete=False, mode="w", encoding="utf-8",
                                      newline="\n", suffix=".csv", prefix="wero_transform_")

    def _clean_csv(v) -> str:
        """Nettoie une valeur pour BCP : supprime tout caractère qui pourrait
        être interprété comme délimiteur (\x1c), fin de ligne (\n, \r, \x0a, \x0d)
        ou null (\x00). Pas d'escapechar — on nettoie à la source."""
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return ""
        s = str(v)
        # Supprimer tous les caractères de contrôle dangereux
        s = s.replace("\x1c", " ")   # field terminator BCP
        s = s.replace("\n",  " ")    # \x0a = row terminator BCP
        s = s.replace("\r",  " ")    # \x0d
        s = s.replace("\x0b", " ")   # vertical tab
        s = s.replace("\x0c", " ")   # form feed
        s = s.replace("\x00", "")    # null byte
        return s

    for row in out[TARGET_COLUMNS].itertuples(index=False, name=None):
        tmp.write(field_term.join([_clean_csv(v) for v in row]) + "\n")
    tmp.close()

    logger.info("CSV de sortie écrit dans %s", tmp.name)
    try:
        os.remove(extract_path)
    except Exception:
        pass
    context["ti"].xcom_push(key="transform_path", value=tmp.name)

def load_task(**context):
    """LOAD — Envoie le CSV BCP-ready généré par transform dans <target_db>.wero.WERO_RECON_RESULT."""
    import os, subprocess, tempfile

    transform_path = context["ti"].xcom_pull(task_ids="transform", key="transform_path")
    if not transform_path:
        logger.warning("Pas de données à charger.")
        return

    _, conn_tgt, _, _, target_qual = _resolve_context(context)
    tgt_hook = MsSqlHook(mssql_conn_id=conn_tgt)
    bcp_path = get_bcp_path()
    bcp_args = get_bcp_conn_args(tgt_hook) if bcp_path else None

    # Compter les lignes pour le log
    with open(transform_path, encoding="utf-8") as f:
        n_lines = sum(1 for _ in f)
    logger.info("=" * 60)
    logger.info("LOAD — %s lignes à charger dans [%s]...", n_lines, target_qual)

    # Diagnostic : afficher les 3 premières lignes du CSV
    with open(transform_path, encoding="utf-8") as f:
        csv_lines = [next(f) for _ in range(min(3, n_lines))]
    logger.info("CSV headers: %s", TARGET_COLUMNS)
    for i, line in enumerate(csv_lines, 1):
        fields = line.rstrip("\n").split("\x1c")
        for col, val in zip(TARGET_COLUMNS, fields):
            if val:
                logger.info("  CSV ligne %s | %-20s = [%s] (len=%s)", i, col, val[:80], len(val))

    if not bcp_path or not bcp_args:
        logger.warning("BCP non disponible, fallback pyodbc.")
        out = pd.read_csv(transform_path, sep="\x1c", header=None, names=TARGET_COLUMNS,
                          dtype=str, keep_default_na=False)
        out = out.replace("", None)
        conn = get_pyodbc_conn(tgt_hook) or tgt_hook.get_conn()
        cur = conn.cursor()
        if hasattr(cur, "fast_executemany"):
            cur.fast_executemany = True
        placeholders = ", ".join("?" for _ in TARGET_COLUMNS)
        cols = ", ".join(qident(c) for c in TARGET_COLUMNS)
        sql = f"INSERT INTO {target_qual} ({cols}) VALUES ({placeholders})"
        rows = [tuple(row) for row in out.itertuples(index=False, name=None)]
        for i in range(0, len(rows), FAST_EXECUTEMANY_BATCH_SIZE):
            cur.executemany(sql, rows[i:i + FAST_EXECUTEMANY_BATCH_SIZE])
            conn.commit()
        cur.close()
        conn.close()
        logger.info("LOAD fallback — %s lignes insérées.", len(rows))
        return

    err_file = transform_path + ".err"
    fmt_file = transform_path + ".fmt"

    # Générer un format file BCP pour mapper les 19 colonnes CSV
    # vers les 19 colonnes non-IDENTITY de la table (en sautant ID IDENTITY).
    # Format file version 13.0 (SQL Server 2016+)
    # Colonnes cible dans l'ordre DDL (sans ID) :
    #   CREATION_DATE, CREATION_TIME, RECORD_TYPE, MESSAGE_ID, FINACLE_ID,
    #   ORIGINATOR_REF, ACCOUNT_NUMBER, OPERATION_TYPE, WERO_STATUS, FINACLE_STATUS,
    #   RECON_DIAGNOSIS, AMOUNT_WERO, AMOUNT_FINACLE, DELTA_AMOUNT, CURRENCY,
    #   MARK_OFF_STATUS, MARK_OFF_DATE, MARK_OFF_TIME, FILE_NAME
    n_cols = len(TARGET_COLUMNS)
    fmt_lines = ["13.0", str(n_cols)]
    for i, col in enumerate(TARGET_COLUMNS, 1):
        term = "\\x1c" if i < n_cols else "\\n"
        # Format : col_num  SQLCHAR  0  max_len  terminator  server_col_num  col_name  collation
        fmt_lines.append(f'{i}\tSQLCHAR\t0\t8000\t"{term}"\t{i + 1}\t{col}\t""')
    with open(fmt_file, "w", encoding="utf-8") as ff:
        ff.write("\n".join(fmt_lines) + "\n")
    logger.info("Format file BCP généré : %s", fmt_file)

    cmd = [
        bcp_path, target_qual, "in", transform_path,
        "-f", fmt_file,
        "-b", str(BCP_BATCH_SIZE),
        "-h", "TABLOCK",
        *bcp_args,
        "-e", err_file,
    ]
    safe_cmd = []
    mask = False
    for token in cmd:
        safe_cmd.append("***" if mask else token)
        mask = token == "-P"
    logger.info("BCP command: %s", " ".join(safe_cmd))

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout.strip():
        logger.info("BCP stdout: %s", result.stdout.strip())
    if result.stderr.strip():
        logger.warning("BCP stderr: %s", result.stderr.strip())
    if result.returncode != 0:
        err_content = ""
        if os.path.exists(err_file):
            with open(err_file, encoding="utf-8", errors="replace") as ef:
                err_content = ef.read(4000)
        raise RuntimeError(
            f"BCP failed rc={result.returncode}\n"
            f"stdout: {result.stdout[:1000]}\n"
            f"stderr: {result.stderr[:1000]}\n"
            f"err_file: {err_content}"
        )
    logger.info("BCP terminé — %s lignes chargées dans %s.", n_lines, target_qual)
    logger.info("=" * 60)

    for path in (transform_path, err_file, fmt_file):
        try:
            if path and os.path.exists(path):
                os.unlink(path)
        except Exception:
            pass


DEFAULT_ARGS = {
    "owner": "airflow",
    "start_date": datetime(2024, 1, 1),
    "retries": 0,
    "retry_delay": timedelta(minutes=5),
}

with DAG(
    dag_id="WERO_RECONCILIATION",
    default_args=DEFAULT_ARGS,
    schedule=None,
    catchup=False,
    tags=["WERO", "FINACLE", "RECONCILIATION"],
    params={**_build_target_db_param()},
) as dag:

    t_init = PythonOperator(
        task_id="init",
        python_callable=create_result_table_task,
    )

    t_clean = PythonOperator(
        task_id="clean",
        python_callable=clean_current_run_task,
    )

    t_extract = PythonOperator(
        task_id="extract",
        python_callable=extract_task,
    )

    t_transform = PythonOperator(
        task_id="transform",
        python_callable=transform_task,
    )

    t_load = PythonOperator(
        task_id="load",
        python_callable=load_task,
    )

    t_init >> t_clean >> t_extract >> t_transform >> t_load