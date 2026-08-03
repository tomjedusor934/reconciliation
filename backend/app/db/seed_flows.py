"""Seed the reconciliation flows from `example/account_type.txt`.

Each flow maps a Finacle GL/mirror account (and its transaction_particulars
pattern: SCTXB / SDDXB / SDXBB / SCRT1 / SWIFT / BKRTP) to a reconciliation
channel. Every flow has one ``finacle_db`` source carrying its reference
account(s) (used by the ingest_finacle DAG to filter ``std.Movement`` with
AccountNumber IN (...)); a few flows also have a file source providing the
counterpart side of the reconciliation (ATM cobol, Webripost CSV, MT940).

All flows are seeded inactive - they are activated/refined once the datamart
connection and sample files are in place.
"""
import logging

from sqlalchemy.orm import Session

from app.models.flow import (
    Flow,
    FlowSource,
    FlowSourceAccount,
    FlowSourceType,
    MatchKeyStrategy,
    ParserType,
)
from app.repositories.flow_repository import flow_repository

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Parser configs for the file-side sources (reused verbatim from the originals)
# ---------------------------------------------------------------------------

# ATM event types → GL account mapping (auto-assigned at parse time).
# New MOSEL codes:
#   MSLRCLTAC: WITHDRAW (withdrawal) / CANCWITH (cancel withdrawal)
#   MSLRCLDEP: DEPOSITS (deposit)    / CANCDEPO (cancel deposit)
#   MSLRCLDEV: TOPUPVPP (card top-up)/ CANCTOPU (cancel top-up)
# Withdrawals → 0010110040001, deposits → 0010110035001. Top-up events are
# intentionally unmapped (account stays None) - atm_mirror has no top-up GL
# account yet; they are still parsed/extracted.
ATM_EVENT_ACCOUNT_MAP = {
    "WITHDRAW": "0010110040001",
    "CANCWITH": "0010110040001",
    "DEPOSITS": "0010110035001",
    "CANCDEPO": "0010110035001",
}

# Whitelist of accepted TYPEVT codes (includes top-up so its rows are extracted
# even though they are not mapped to an account above).
ATM_ALLOWED_EVENTS = [
    "DEPOSITS", "CANCDEPO",
    "WITHDRAW", "CANCWITH",
    "TOPUPVPP", "CANCTOPU",
]

ATM_PARSER_CONFIG = {
    "encoding": "utf-8",
    "record_separator": "newline",
    "date_format": "%Y%m%d",
    "default_currency": "EUR",
    "allowed_event_types": ATM_ALLOWED_EVENTS,
    "event_type_account_map": ATM_EVENT_ACCOUNT_MAP,
}

# MT940 BCEE parser configs.
# The reconciliation reference is the :61: continuation line (customer ref,
# e.g. "CCI226187S000054"), identical to the +00 sub-field of :86: (fallback).
# It corresponds to std.Payment.TransactionRef and is stored raw in remarks_1;
# ref_no is left EMPTY on purpose (it is the classic-IP key slot, and
# ref_no-keyed retry/sweep logic must ignore MT940 entries).
# extract_via_dag=True routes extraction to the Airflow orchestrator (the
# 3_1_0 NOSTRO IP DAG logic), which parses the :61:/:86: lines and resolves
# reco_id = the payment's po_id via the datamart TransactionRef join (see
# mt940_extract.resolve_mt940_reco_ids), then pushes the entries back via
# /tasks/mt940/* - these sources are NOT parsed locally by the backend.
# Keys the DAG could not resolve (payment not yet in std.Payment — or rejected,
# dedicated handling being a next step) are retried by the DAG itself each run
# (/tasks/mt940/unresolved + /tasks/mt940/resolve).
# resolve_reco_id_via_finacle=False: the ref_no-based pre-match sweep is inert
# by construction now that ref_no is empty (deployed flows still carrying True
# are harmless for the same reason).
MT940_IP_PARSER_CONFIG = {
    "encoding": "utf-8",
    "default_currency": "EUR",
    "reco_id_prefix": "SCRT",
    "resolve_reco_id_via_finacle": False,
    "extract_via_dag": True,
    "customer_ref_as_reco_id": False,
}

MT940_OTHER_PARSER_CONFIG = {
    "encoding": "utf-8",
    "default_currency": "EUR",
    "reco_id_prefix": "SCRT",
    "resolve_reco_id_via_finacle": False,
    "extract_via_dag": True,
    "customer_ref_as_reco_id": False,
}

# WebriPost XLSX parser config.
# OperationType mapping: configurable direction + sign application.
# Types: 1=debit (dépôt), 2=credit, 30=debit (rollback), 31=credit (rollback).
# apply_sign_from_direction=true ensures amounts are negated for debits.
# WebriPost CSV (Riposte/Thaler) - ';'-delimited, amounts in CENTS, always positive
# (sign derived from TxnType). One config handles Cheque / Deposit / Withdrawal files.
# Keyword→direction map is modular: add new TxnType keywords as more types appear.
WEBRIPOST_PARSER_CONFIG = {
    "delimiter": ";",
    "encoding": "utf-8",
    "has_header": True,
    "amount_in_cents": True,
    "default_currency": "EUR",
    "datetime_fields": ["Date", "Time"],
    "datetime_format": "%d-%b-%Y %H:%M:%S",
    "column_map": {
        "amount":       "Amount",
        "reco_id":      "Reference",
        "ref_no":       "Reference",
        "remarks_1":    "CCPAccount",
        "external_ref": "TxnId",
        "event_type":   "TxnType",
    },
    "direction": {
        "field": "TxnType",
        # substring match (case-insensitive); first hit wins. Update as needed.
        "keywords": {
            "CCPDepotWiiTiersDedouanementPOST": 	"credit",
            "CCPDepotWiiPropre": 					"credit",
            "CCPWithdrawWiiDebitThaler": 			"debit",
            "CCPChequeWiiProBarcode": 				"debit",
            "CCPDepotWiiDedouanementOffline": 		"credit",
            "CCPDepotWiiReverse": 					"credit",
            "CCPDepotWiiTiers": 					"credit",
            "CCPWithdrawWiiReverseThaler": 			"debit",
            "CCPChequeWiiProManual": 				"debit",
            "withdraw": "debit", 
            "cheque": "credit", 
            "depot": "credit"},
        "reversal_keyword": "reverse",   # CCP*Reverse* flips the matched direction
        "default": None,                 # unknown TxnType → row error (surfaced)
    },
    # Uncertain operations are not reconciled.
    "skip_status": {"field": "Status", "values": ["InDoubt"]},
    # Reversals are kept for traceability but with zero amount (not reconciled on value).
    "zero_amount_when": {"field": "TxnType", "contains": "Reverse"},
}


# ---------------------------------------------------------------------------
# Source builders
# ---------------------------------------------------------------------------

def _finacle_source(accounts: list) -> dict:
    """A finacle_db source carrying the flow's reference account(s)."""
    return {
        "code": "finacle_db",
        "name": "Finacle Database",
        "description": "Finacle movements pushed by the Airflow ingest_finacle DAG.",
        "is_active": True,
        "source_type": FlowSourceType.FINACLE_DB,
        "parser_type": ParserType.FINACLE_DB,
        "match_key_strategy": MatchKeyStrategy.RECO_ID_AMOUNT,
        "inbox_subfolder": None,
        "file_pattern": None,
        "parser_config": {},
        "accounts": accounts,
    }


def _finacle_bb_source(accounts: list) -> dict:
    """A finacle_db source extracted in BATCH BOOKING TRUE mode: movements are
    clustered into lots (shared PACS008/MSGID/PO keys against std.Payment) by
    the ingest_finacle_bb DAG, and reco_id is the lot uuid."""
    source = _finacle_source(accounts)
    source["name"] = "Finacle Database (Batch Booking True)"
    source["description"] = (
        "Finacle movements clustered into lots by the Airflow ingest_finacle_bb DAG."
    )
    source["parser_type"] = ParserType.FINACLE_BATCH_BOOKING_TRUE
    return source


# One-shot parser upgrades for EXISTING installs. seed_flows() never overrides
# user customizations on existing sources, so a deliberate parser change must
# ship here: (flow_code, source_code) -> (old parser, new parser). Applied only
# while the source still carries the OLD value (idempotent); remove the entry
# once every environment has flipped.
_SOURCE_PARSER_UPGRADES = {
    ("float_account_outward", "finacle_db"): (
        ParserType.FINACLE_DB,
        ParserType.FINACLE_BATCH_BOOKING_TRUE,
    ),
}


COBOL_SOURCE = {
    "code": "cobol_file",
    "name": "Cobol/MOSEL File",
    "description": "ATM transaction files in Cobol fixed-width format.",
    "is_active": True,
    "source_type": FlowSourceType.FILE,
    "parser_type": ParserType.COBOL_MOSEL,
    "match_key_strategy": MatchKeyStrategy.RECO_ID_AMOUNT,
    "inbox_subfolder": "atm",
    "file_pattern": None,
    "parser_config": ATM_PARSER_CONFIG,
}

WEBRIPOST_SOURCE = {
    "code": "webripost_csv",
    "name": "Webripost CSV Export",
    "description": "Riposte/Thaler CSV files (Cheque/Deposit/Withdrawal) from Webripost.",
    "is_active": True,
    "source_type": FlowSourceType.FILE,
    "parser_type": ParserType.CSV,
    "match_key_strategy": MatchKeyStrategy.RECO_ID_AMOUNT,
    "inbox_subfolder": "webripost",
    "file_pattern": "WEBRIPOSTE*.csv",
    "parser_config": WEBRIPOST_PARSER_CONFIG,
}

MT940_IP_SOURCE = {
    "code": "mt940_ip",
    "name": "MT940 BCEE File (IP)",
    "description": "SWIFT MT940 bank statement files from BCEE (IP).",
    "is_active": True,
    "source_type": FlowSourceType.FILE,
    "parser_type": ParserType.MT940,
    "match_key_strategy": MatchKeyStrategy.RECO_ID_AMOUNT,
    "inbox_subfolder": "mt940_ip",
    "file_pattern": None,
    "parser_config": MT940_IP_PARSER_CONFIG,
}

MT940_OTHER_SOURCE = {
    "code": "mt940_other",
    "name": "MT940 BCEE File (Other)",
    "description": "SWIFT MT940 bank statement files from BCEE (other payments).",
    "is_active": True,
    "source_type": FlowSourceType.FILE,
    "parser_type": ParserType.MT940,
    "match_key_strategy": MatchKeyStrategy.RECO_ID_AMOUNT,
    "inbox_subfolder": "mt940_other",
    "file_pattern": None,
    "parser_config": MT940_OTHER_PARSER_CONFIG,
}


# ---------------------------------------------------------------------------
# Seed definitions - derived from example/account_type.txt (accounts 00-prefixed)
# ---------------------------------------------------------------------------

SEED_FLOWS = [
    {
        "code": "float_account_outward", "name": "Float account for OUTWARD",
        "description": "Float account outward transactions.",
        "is_active": True, "default_currency": "EUR",
        "sources": [_finacle_bb_source([("0010130015001", "Float account for OUTWARD")])],
    },
    {
        "code": "float_account_inward", "name": "Float account for INWARD",
        "description": "Float account inward transactions.",
        "is_active": True, "default_currency": "EUR",
        "sources": [_finacle_source([("0010130015002", "Float account for INWARD")])],
    },
    {
        "code": "float_ip_inward", "name": "Float IP INWARD",
        "description": "Float IP inward transactions.",
        "is_active": True, "default_currency": "EUR",
        "sources": [_finacle_source([("0010130025001", "Float IP INWARD")])],
    },
    {
        "code": "float_ip_outward", "name": "Float IP OUTWARD",
        "description": "Float IP outward transactions.",
        "is_active": True, "default_currency": "EUR",
        "sources": [_finacle_source([("0010130030035", "Float IP OUTWARD")])],
    },
    {
        "code": "nostro_mirror", "name": "NOSTRO MIRROR",
        "description": "NOSTRO mirror accounts (EBA-STEP + IP), reconciled against MT940 BCEE statements.",
        "is_active": False, "default_currency": "EUR",
        "sources": [
            _finacle_source([
                ("0010112001002", "NOSTRO MIRROR - IP"),
            ]),
            MT940_IP_SOURCE,
            MT940_OTHER_SOURCE,
        ],
    },
    {
        "code": "atm_mirror", "name": "ATM MIRROR",
        "description": "ATM deposit/withdrawal mirror, reconciled against the Cobol/MOSEL ATM files.",
        "is_active": False, "default_currency": "EUR",
        "sources": [
            _finacle_source([
                ("0010110035001", "ATM DEPOSIT"),
                ("0010110040001", "ATM WITHDRAWAL"),
            ]),
            COBOL_SOURCE,
        ],
    },
    {
        "code": "cash_in_shop", "name": "CASH IN SHOP",
        "description": "Cash-in-shop mirror, reconciled against the Webripost XLSX export.",
        "is_active": False, "default_currency": "EUR",
        "sources": [
            _finacle_source([("0010110001080", "CASH IN SHOP")]),
            WEBRIPOST_SOURCE,
        ],
    }

    # ---- Legacy flows (commented out) ----
    # {
    #     "code": "float_sct_out", "name": "Float SCT OUT",
    #     "description": "SEPA SCT outward float mirror (SCTXB).",
    #     "is_active": True, "default_currency": "EUR",
    #     "sources": [_finacle_source([("0010130015001", "Float SCT OUT")])],
    # },
    # {
    #     "code": "float_sct_in", "name": "Float SCT IN",
    #     "description": "SEPA SCT inward float mirror (SCTXB).",
    #     "is_active": True, "default_currency": "EUR",
    #     "sources": [_finacle_source([("0010130015002", "Float SCT IN")])],
    # },
    # {
    #     "code": "float_sdd_out", "name": "Float SDD OUT",
    #     "description": "SEPA SDD outward float mirror (SDDXB / SDXBB).",
    #     "is_active": True, "default_currency": "EUR",
    #     "sources": [_finacle_source([("0010130020001", "Float SDD OUT")])],
    # },
    # {
    #     "code": "float_sdd_in", "name": "Float SDD IN",
    #     "description": "SEPA SDD inward float mirror (SDDXB / SDXBB).",
    #     "is_active": True, "default_currency": "EUR",
    #     "sources": [_finacle_source([("0010130020002", "Float SDD IN")])],
    # },
    # {
    #     "code": "float_ip_in", "name": "Float IP IN",
    #     "description": "Instant Payments inward float mirror (SCRT1).",
    #     "is_active": True, "default_currency": "EUR",
    #     "sources": [_finacle_source([("0010130025001", "Float IP IN")])],
    # },
    # {
    #     "code": "float_swift", "name": "Float SWIFT",
    #     "description": "SWIFT float mirror.",
    #     "is_active": True, "default_currency": "EUR",
    #     "sources": [_finacle_source([("0010130030001", "Float SWIFT")])],
    # },
    # {
    #     "code": "float_ip_out_bkrtp", "name": "Float IP OUT / BKRTP",
    #     "description": "Instant Payments outward / BKRTP float mirror (SCRT1 / BKRTP).",
    #     "is_active": True, "default_currency": "EUR",
    #     "sources": [_finacle_source([("0010130030035", "Float IP OUT / BKRTP")])],
    # },
    # {
    #     "code": "float_sct_bulk", "name": "FLOAT accounts  SCT Bulk (BB true)",
    #     "description": "waiting for description",
    #     "is_active": True, "default_currency": "EUR",
    #     "sources": [_finacle_source([("0010130040002", "FLOAT accounts  SCT Bulk (BB true)")])],
    # },
    # {
    #     "code": "float_sdd_bulk", "name": "FLOAT accounts  SDD Bulk (BB true)",
    #     "description": "waiting for description",
    #     "is_active": True, "default_currency": "EUR",
    #     "sources": [_finacle_source([("0010130045002", "FLOAT accounts  SDD Bulk (BB true)")])],
    # },
    # {
    #     "code": "nostro_mirror", "name": "NOSTRO MIRROR",
    #     "description": "NOSTRO mirror accounts (EBA-STEP + IP), reconciled against MT940 BCEE statements.",
    #     "is_active": False, "default_currency": "EUR",
    #     "sources": [
    #         _finacle_source([
    #             ("0010112001002", "NOSTRO MIRROR - FLUX FINANCIERS EBA-STEP"),
    #             ("0010112001001", "NOSTRO MIRROR - IP"),
    #         ]),
    #         MT940_IP_SOURCE,
    #         MT940_OTHER_SOURCE,
    #     ],
    # },
    # {
    #     "code": "atm_mirror", "name": "ATM MIRROR",
    #     "description": "ATM deposit/withdrawal mirror, reconciled against the Cobol/MOSEL ATM files.",
    #     "is_active": False, "default_currency": "EUR",
    #     "sources": [
    #         _finacle_source([
    #             ("0010110035001", "ATM DEPOSIT"),
    #             ("0010110040001", "ATM WITHDRAWAL"),
    #         ]),
    #         COBOL_SOURCE,
    #     ],
    # },
    # {
    #     "code": "cash_in_shop", "name": "CASH IN SHOP",
    #     "description": "Cash-in-shop mirror, reconciled against the Webripost XLSX export.",
    #     "is_active": False, "default_currency": "EUR",
    #     "sources": [
    #         _finacle_source([("0010110001080", "CASH IN SHOP")]),
    #         WEBRIPOST_SOURCE,
    #     ],
    # },
]

# Pre-`account_type.txt` placeholder flows, replaced by the list above.
_LEGACY_FLOW_CODES = ["atm", "ip", "other_payments", "webripost", "float_ip"]


def _build_source(flow_id: int, src_spec: dict) -> FlowSource:
    spec = dict(src_spec)
    accounts = spec.pop("accounts", []) or []
    return FlowSource(
        flow_id=flow_id,
        **spec,
        accounts=[
            FlowSourceAccount(account_number=number, label=label)
            for number, label in accounts
        ],
    )


def _purge_legacy_flows(db: Session) -> None:
    """Remove the old placeholder flows. Skipped (with a warning) when a flow
    still has ingestion runs / entries, so the seed never destroys live data."""
    for code in _LEGACY_FLOW_CODES:
        flow = flow_repository.get_by_code(db, code=code)
        if not flow:
            continue
        try:
            db.delete(flow)  # cascades to its sources/accounts
            db.commit()
            logger.info("[seed_flows] removed legacy flow '%s'", code)
        except Exception as exc:  # noqa: BLE001 - FK from ingestion_run/entries
            db.rollback()
            logger.warning(
                "[seed_flows] legacy flow '%s' kept (has dependent data): %s", code, exc
            )


def seed_flows(db: Session) -> None:
    """Idempotent: purges legacy flows, then creates missing flows / sources / accounts."""
    _purge_legacy_flows(db)

    for spec in SEED_FLOWS:
        sources = spec.get("sources", [])
        flow_data = {k: v for k, v in spec.items() if k != "sources"}
        existing = flow_repository.get_by_code(db, code=spec["code"])
        if existing:
            # Don't override user customizations; just add missing sources/accounts.
            existing_by_code = {s.code: s for s in existing.sources}
            for src_spec in sources:
                existing_src = existing_by_code.get(src_spec["code"])
                if existing_src is None:
                    db.add(_build_source(existing.id, src_spec))
                    continue
                upgrade = _SOURCE_PARSER_UPGRADES.get((spec["code"], src_spec["code"]))
                if upgrade and existing_src.parser_type == upgrade[0]:
                    existing_src.parser_type = upgrade[1]
                    logger.info(
                        "[seed_flows] %s/%s: parser upgraded %s -> %s",
                        spec["code"], src_spec["code"],
                        upgrade[0].value, upgrade[1].value,
                    )
                existing_acct = {a.account_number for a in existing_src.accounts}
                for number, label in src_spec.get("accounts", []) or []:
                    if number not in existing_acct:
                        db.add(
                            FlowSourceAccount(
                                flow_source_id=existing_src.id,
                                account_number=number,
                                label=label,
                            )
                        )
            db.commit()
            continue

        flow = Flow(**flow_data)
        db.add(flow)
        db.commit()
        db.refresh(flow)
        for src_spec in sources:
            db.add(_build_source(flow.id, src_spec))
        db.commit()
