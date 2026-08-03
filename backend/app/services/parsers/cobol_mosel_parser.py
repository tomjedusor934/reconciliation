"""Parser for the ATM MOSEL flat file (Cobol-style fixed-width records).

Format (from format_cobol_mosel_parser.txt + real file analysis):

  Header record  : HEDTRA='0' + filler
  Trailer record : HEDTRA='9' + filler
  Detail record  (140 bytes, fixed-width):
    HEDTRA     X(01)  Type d'enregistrement ('1' for data)
    REFCTR     X(30)  Référence du contrat (blank in practice)
    TMSEXT     X(30)  Timestamp (AAAAMMJJHHmmsscc, padded)
    TYPEVT     X(08)  Type d'événement (DEPOSITS / CANCDEPO / WITHDRAW /
                      CANCWITH / TOPUPVPP / CANCTOPU)
    PERIODE    X(12)  Période de réconciliation       → external_ref
    DATOPN     X(08)  Date de traitement (YYYYMMDD)   → value_date
    DEVISE     X(03)  Devise (ISO 4217, e.g. EUR)     → currency
    MONTANT    X(27)  Montant du mouvement (2 dec.)   → amount
    EXTTXNREF  X(20)  ExternalTransactionRef          → reco_id
                      (Mosel Transaction Id, per-transaction key)
    ONOFF      X(01)  Online/Offline indicator (O/F)

Field → ParsedEntry mapping (confirmed with business):
  - reco_id      = EXTTXNREF (Mosel Transaction Id; 1:1 match vs Finacle)
  - external_ref = PERIODE   (réconciliation period)
  - value_date / operation_date = DATOPN
  - currency = DEVISE, amount = MONTANT, event_type = TYPEVT
  - account  = event_type_account_map[TYPEVT] (None when unmapped, e.g. top-up)
  - TMSEXT also feeds source_discriminator → part of the source_hash identity, so
    two records sharing EXTTXNREF/amount/date/period/type but with different
    timestamps remain two distinct entries (rather than colliding on one hash).

Note: real files embed COBOL LOW-VALUES (NUL, 0x00) filler bytes inside the
timestamp and amount fields; they are stripped before fixed-width slicing so the
offsets line up (see parse_file).

Configurable via flow.parser_config:
  - encoding: default 'utf-8' (set 'cp037' for EBCDIC)
  - record_separator: 'newline' (default) or 'fixed'
  - date_format: format string for DATOPN — default '%Y%m%d'
  - allowed_event_types: optional whitelist of TYPEVT codes
  - default_currency: fallback if DEVISE is blank (default: 'EUR')
  - event_type_account_map: dict mapping TYPEVT → account_number
      e.g. {"DEPOSITS": "0010110035001", "WITHDRAW": "0010110040001"}
      If a TYPEVT is absent, account is left as None.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional

from app.services.parsers.base_parser import BaseParser, ParsedEntry, ParseResult


# Field offsets within a detail record (0-based, end-exclusive), applied AFTER
# stripping NUL filler bytes.
F_HEDTRA = (0, 1)
F_REFCTR = (1, 31)
F_TMSEXT = (31, 61)
F_TYPEVT = (61, 69)
F_PERIODE = (69, 81)    # Période de réconciliation  → external_ref
F_DATOPN = (81, 89)     # Date de traitement YYYYMMDD → value_date
F_DEVISE = (89, 92)     # Devise (3 chars)            → currency
F_MONTANT = (92, 119)   # Montant (27 chars)          → amount
F_EXTREF = (119, 139)   # ExternalTransactionRef      → reco_id
F_ONOFF = (139, 140)    # Online/Offline indicator (O/F)

RECORD_LEN = 141


class CobolMoselParser(BaseParser):
    def parse_file(self, file_path: str) -> ParseResult:
        result = ParseResult()
        encoding = self.config.get("encoding", "utf-8")
        sep = self.config.get("record_separator", "newline")
        date_fmt = self.config.get("date_format", "%Y%m%d")
        allowed_events = set(self.config.get("allowed_event_types") or [])
        default_currency = self.config.get("default_currency", "EUR")
        # Map TYPEVT → account_number for automatic account assignment.
        event_type_account_map: Dict[str, str] = self.config.get("event_type_account_map") or {}
        file_name = self.basename(file_path)

        try:
            with open(file_path, "rb") as fh:
                raw = fh.read()
            try:
                text = raw.decode(encoding, errors="strict")
            except UnicodeDecodeError as exc:
                raise ValueError(f"Cannot decode file with encoding '{encoding}': {exc}") from exc

            records = self._split_records(text, sep)
            # Defensive: drop any residual control chars per record (C0 < 0x20 and
            # DEL 0x7f) so a stray non-0x00 filler byte cannot shift the fixed-width
            # offsets. Chars >= 0x80 are kept (REFCTR may hold accented letters).
            records = [
                "".join(c for c in r if c >= " " and c != "\x7f") for r in records
            ]
        except Exception as exc:
            print(f"[CobolMoselParser] error reading/parsing {file_name}: {exc}")
            result.errors.append(f"Error reading/parsing {file_path}: {exc}")
            return result

        print(f"[CobolMoselParser] {file_name}: {len(records)} records found (after splitting and cleaning)")
        try:
            for idx, rec in enumerate(records, start=1):
                try:
                    entry = self._parse_record(
                        rec,
                        line_no=idx,
                        date_fmt=date_fmt,
                        default_currency=default_currency,
                        event_type_account_map=event_type_account_map,
                        file_name=file_name,
                    )
                    if entry is None:
                        continue  # header/footer record
                    result.entries.append(entry)
                except Exception as exc:  # noqa: BLE001 — collect line-level errors
                    print(f"[CobolMoselParser] line {idx} error: {exc}")
                    result.errors.append(f"line {idx}: {exc}")
        except Exception as exc:
            print(f"[CobolMoselParser] unexpected error while parsing {file_name}: {exc}")
            result.errors.append(f"Unexpected error while parsing {file_path}: {exc}")
        return result

    # ------------------------------------------------------------------
    @staticmethod
    def _split_records(text: str, sep: str) -> List[str]:
        if sep == "fixed":
            # Legacy fixed-length mode (RECORD_LEN = 3269 bytes)
            record_len = 3269
            return [text[i: i + record_len] for i in range(0, len(text), record_len)]
        # newline mode (default) — strip trailing CR/LF, ignore blank lines
        return [line for line in text.splitlines() if line.strip()]

    @classmethod
    def _slice(cls, rec: str, start: int, end: Optional[int] = None) -> str:
        return rec[start:end]

    def _parse_record(
        self,
        rec: str,
        *,
        line_no: int,
        date_fmt: str,
        default_currency: str,
        event_type_account_map: Dict[str, str],
        file_name: str,
    ) -> Optional[ParsedEntry]:
        # Check HEDTRA first (only needs 1 char) so header ('0') / footer ('9')
        # lines are silently skipped before the length guard fires.
        hedtra = self._slice(rec, *F_HEDTRA).strip()
        if hedtra in ("0", "9"):
            return None

        # reco_id (EXTTXNREF) must be fully present; the trailing 1-char
        # indicator may be missing if upstream trimmed it, so pad to RECORD_LEN.
        if len(rec) < F_EXTREF[1]:
            raise ValueError(f"record too short ({len(rec)} chars, need {F_EXTREF[1]})")
        rec = rec.ljust(RECORD_LEN)

        refctr = self._slice(rec, *F_REFCTR).strip()
        tmsext = self._slice(rec, *F_TMSEXT).strip()
        typevt = self._slice(rec, *F_TYPEVT).strip()
        periode = self._slice(rec, *F_PERIODE).strip()
        datopn = self._slice(rec, *F_DATOPN).strip()
        devise = self._slice(rec, *F_DEVISE).strip() or default_currency
        montant = self._slice(rec, *F_MONTANT).strip()
        extref = self._slice(rec, *F_EXTREF).strip()
        onoff = self._slice(rec, *F_ONOFF).strip()
        print(f"[CobolMoselParser] line {line_no}: parsed record: HEDTRA={hedtra}, REFCTR={refctr}, TMSEXT={tmsext}, TYPEVT={typevt}, PERIODE={periode}, DATOPN={datopn}, DEVISE={devise}, MONTANT={montant}, EXTTXNREF={extref}, ONOFF={onoff}")
        # Date parsing — DATOPN is YYYYMMDD
        try:
            value_date = datetime.strptime(datopn, date_fmt).replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise ValueError(f"invalid DATOPN '{datopn}' (expected {date_fmt}): {exc}") from exc

        if typevt == "WITHDRAW" and Decimal(montant) > 0:
            montant = "-" + montant  # negative for withdrawals
        amount = self._parse_amount(montant)

        # Account: auto-assign from event_type map if available (None otherwise).
        account = event_type_account_map.get(typevt) if event_type_account_map else None

        return ParsedEntry(
            reco_id=extref or None,
            account=account,
            currency=devise,
            amount=amount,
            value_date=value_date,
            operation_date=value_date,
            direction="credit" if amount >= 0 else "debit",
            event_type=typevt or None,
            external_ref=periode or None,
            file_name=file_name,
            # TMSEXT (transaction timestamp) joins the source_hash identity so two
            # ATM lines sharing EXTTXNREF/amount/date/period/type but with distinct
            # timestamps stay two entries (business investigates the shared ref)
            # instead of colliding on one source_hash.
            source_discriminator=tmsext or None,
            payload_raw={
                "HEDTRA": hedtra,
                "REFCTR": refctr,
                "TMSEXT": tmsext,
                "TYPEVT": typevt,
                "PERIODE": periode,
                "DATOPN": datopn,
                "DEVISE": devise,
                "MONTANT": montant,
                "EXTREF": extref,
                "ONOFF": onoff,
                "_line_no": line_no,
            },
        )

    @staticmethod
    def _parse_amount(raw: str) -> Decimal:
        """Parse signed amount from ATM file.

        Accepted formats:
          - '80.00'    → 80.00   (positive, dot decimal)
          - '-90,00'   → -90.00  (negative, comma decimal)
          - '890,00'   → 890.00  (positive, comma decimal)
          - '+150.00'  → 150.00  (explicit plus sign)

        Cobol overpunch encoding (e.g. '12345{') is not supported.
        """
        s = raw.replace(" ", "").replace(",", ".")
        if not s:
            raise ValueError("empty amount field")
        try:
            return Decimal(s)
        except InvalidOperation as exc:
            raise ValueError(f"cannot parse amount '{raw}'") from exc
