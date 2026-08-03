"""Generic CSV parser, parameterized by `parser_config` JSON.

Core config keys:
  - delimiter: ',' (default)              - encoding: 'utf-8' (default)
  - has_header: true (default)            - decimal_separator: '.' (default)
  - default_currency: 'EUR' (default)
  - column_map: maps canonical fields → CSV columns. Supported keys:
        reco_id, ref_no, remarks_1, transaction_particulars, account, currency,
        amount, value_date, operation_date, event_type, external_ref
  - date_format: '%Y-%m-%d' (default, used with column_map['value_date'])

Optional modular extensions (all opt-in):
  - datetime_fields: ['Date', 'Time'] + datetime_format: '%d-%b-%Y %H:%M:%S'
        → value_date built by joining those columns with a space (overrides the
          single value_date/date_format path). Month names parsed locale-independently.
  - amount_in_cents: true → amount = Decimal(raw) / 100
  - direction: {                          # derive credit/debit from a text field
        "field": "TxnType",
        "keywords": {"withdraw": "debit", "cheque": "credit", "depot": "credit"},
        "reversal_keyword": "reverse",    # flips the matched direction (optional)
        "default": null                   # used when no keyword matches; null → row error
    }
        → amount is signed: abs(amount), negated for debit. When absent, the
          direction is inferred from the amount sign (legacy behavior).
  - skip_status: {"field": "Status", "values": ["InDoubt"]} → drop those rows.
"""
from __future__ import annotations

import csv
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

from app.services.parsers.base_parser import BaseParser, ParsedEntry, ParseResult

_EN_MONTHS = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
    "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}


def _strptime_safe(value: str, fmt: str) -> datetime:
    """strptime that doesn't depend on the process locale for ``%b`` (month abbrev)."""
    try:
        return datetime.strptime(value, fmt)
    except ValueError:
        if "%b" not in fmt:
            raise
        normalized = re.sub(
            r"[A-Za-z]{3,}",
            lambda m: _EN_MONTHS.get(m.group(0)[:3].lower(), m.group(0)),
            value,
        )
        return datetime.strptime(normalized, fmt.replace("%b", "%m"))


class CSVParser(BaseParser):
    def parse_file(self, file_path: str) -> ParseResult:
        result = ParseResult()
        cfg = self.config
        encoding = cfg.get("encoding", "utf-8")
        delimiter = cfg.get("delimiter", ",")
        has_header = cfg.get("has_header", True)
        col_map: Dict[str, str] = cfg.get("column_map") or {}
        skip_status = cfg.get("skip_status") or {}
        skip_field = skip_status.get("field")
        skip_values = {str(v).strip().lower() for v in (skip_status.get("values") or [])}
        file_name = self.basename(file_path)

        print(f"-------- parsing {file_name} path={file_path}")
        try:
            with open(file_path, "r", encoding=encoding, newline="") as fh:
                reader = csv.DictReader(fh, delimiter=delimiter) if has_header else csv.reader(
                    fh, delimiter=delimiter
                )
                for idx, row in enumerate(reader, start=2 if has_header else 1):
                    row_dict = row if isinstance(row, dict) else {str(i): v for i, v in enumerate(row)}
                    # Configurable status filter (e.g. drop 'InDoubt') — not an error.
                    if skip_field and str(row_dict.get(skip_field) or "").strip().lower() in skip_values:
                        continue
                    try:
                        entry = self._row_to_entry(row_dict, col_map, file_name)
                        result.entries.append(entry)
                    except Exception as exc:  # noqa: BLE001
                        print(f"-------- line {idx} error: {exc}")
                        result.errors.append(f"line {idx}: {exc}")
        except Exception as exc:
            print(f"-------- file error: {exc}")
            result.errors.append(f"file error: {exc}")
        
        return result

    @staticmethod
    def _get(row: Dict[str, Any], col_map: Dict[str, str], key: str) -> Optional[str]:
        col = col_map.get(key)
        if not col:
            return None
        v = row.get(col)
        return None if v is None else (str(v).strip() or None)

    def _resolve_direction(self, row: Dict[str, Any]) -> Optional[str]:
        """Return 'credit'/'debit' from a text field per config, or raise/None."""
        d = self.config.get("direction")
        if not d:
            return None  # caller falls back to amount-sign
        value = str(row.get(d.get("field")) or "").lower()
        direction = None
        for keyword, sense in (d.get("keywords") or {}).items():
            if str(keyword).lower() in value:
                direction = sense
                break
        if direction is not None:
            reversal = d.get("reversal_keyword")
            if reversal and str(reversal).lower() in value:
                direction = "debit" if direction == "credit" else "credit"
            return direction
        default = d.get("default")
        if default:
            return default
        raise ValueError(f"unmapped direction for {d.get('field')}={row.get(d.get('field'))!r}")

    def _resolve_value_date(self, row: Dict[str, Any], col_map: Dict[str, str]) -> datetime:
        cfg = self.config
        dt_fields = cfg.get("datetime_fields")
        if dt_fields:
            parts = [str(row.get(f) or "").strip() for f in dt_fields]
            raw = " ".join(p for p in parts if p)
            if not raw:
                raise ValueError("missing datetime fields " + "/".join(dt_fields))
            fmt = cfg.get("datetime_format", "%Y-%m-%d %H:%M:%S")
            try:
                return _strptime_safe(raw, fmt).replace(tzinfo=timezone.utc)
            except ValueError as exc:
                raise ValueError(f"invalid datetime '{raw}': {exc}") from exc
        date_raw = self._get(row, col_map, "value_date")
        if not date_raw:
            raise ValueError("missing value_date")
        try:
            return _strptime_safe(date_raw, cfg.get("date_format", "%Y-%m-%d")).replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise ValueError(f"invalid value_date '{date_raw}': {exc}") from exc

    def _row_to_entry(self, row: Dict[str, Any], col_map: Dict[str, str], file_name: str) -> ParsedEntry:
        cfg = self.config
        dec_sep = cfg.get("decimal_separator", ".")
        default_currency = cfg.get("default_currency", "EUR")

        amount_raw = self._get(row, col_map, "amount")
        if not amount_raw:
            raise ValueError("missing amount")
        try:
            amount = Decimal(amount_raw.replace(dec_sep, "."))
        except InvalidOperation as exc:
            raise ValueError(f"invalid amount '{amount_raw}'") from exc
        if cfg.get("amount_in_cents"):
            amount = (amount / Decimal(100)).quantize(Decimal("0.01"))

        direction = self._resolve_direction(row)
        if direction is not None:
            amount = -abs(amount) if direction == "debit" else abs(amount)
        else:
            direction = "credit" if amount >= 0 else "debit"

        # zero_amount_when: set amount to 0 when a field contains a substring
        zaw = cfg.get("zero_amount_when")
        if zaw:
            field_val = str(row.get(zaw["field"]) or "").lower()
            if zaw["contains"].lower() in field_val:
                amount = Decimal(0)

        value_date = self._resolve_value_date(row, col_map)
        op_raw = self._get(row, col_map, "operation_date")
        operation_date = value_date
        if op_raw:
            try:
                operation_date = _strptime_safe(
                    op_raw, cfg.get("operation_date_format", cfg.get("date_format", "%Y-%m-%d"))
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                operation_date = value_date

        return ParsedEntry(
            reco_id=self._get(row, col_map, "reco_id"),
            account=self._get(row, col_map, "account"),
            currency=self._get(row, col_map, "currency") or default_currency,
            amount=amount,
            value_date=value_date,
            operation_date=operation_date,
            direction=direction,
            event_type=self._get(row, col_map, "event_type"),
            external_ref=self._get(row, col_map, "external_ref"),
            file_name=file_name,
            transaction_particulars=self._get(row, col_map, "transaction_particulars"),
            ref_no=self._get(row, col_map, "ref_no"),
            remarks_1=self._get(row, col_map, "remarks_1"),
            payload_raw={k: v for k, v in row.items()},
        )
