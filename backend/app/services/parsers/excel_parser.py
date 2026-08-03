"""Excel (XLSX/XLS) parser for file-based reconciliation sources such as WebriPost.

Reads an Excel workbook and converts rows to normalized ParsedEntry objects.
Uses the same column-mapping approach as CSVParser.

Configuration (flow.parser_config):
  - sheet: sheet name or index (default: 0, i.e. first sheet)
  - has_header: true (default) — first row is header; column_map uses header names
  - skip_rows: int (default 0) — rows to skip before data (0-indexed, after header)
  - column_map: dict mapping canonical field → column header name (or 0-based index if no header)
      Required keys:
        'amount'       → column containing the transaction amount (positive number)
        'value_date'   → column containing the operation/value date
      Recommended keys:
        'reco_id'      → reconciliation reference (used for matching)
        'external_ref' → unique transaction ID
        'event_type'   → transaction type/code
        'account'      → account number (if present in file)
        'currency'     → currency code (if present; else default_currency)
        'operation_date' → operation date (defaults to value_date if absent)
  - date_format: strftime format for date columns IF they are strings (default: '%Y%m%d')
                 Set to 'excel_date' if the cell contains an Excel date serial
                 Set to 'int_yyyymmdd' if cell value is an integer like 20260428
  - decimal_separator: '.' (default) — for string amount cells
  - default_currency: 'EUR' (default) — used when currency column is absent or blank
  - direction_column: column header whose value determines debit/credit (optional)
  - direction_map: dict mapping direction_column values → 'debit' or 'credit'
      e.g. {"1": "debit", "2": "credit", "30": "debit", "31": "credit"}
      If a value is not in the map AND direction_column is set:
        - if amount_sign_as_fallback is true (default), use amount sign
        - otherwise raise a warning and default to 'credit'
  - amount_sign_as_fallback: true (default) — use amount sign when direction_map
      doesn't cover a given operation type code
  - apply_sign_from_direction: false (default) — when true, forces the amount sign
      based on direction: +abs(amount) for credit, -abs(amount) for debit.
      Essential when source file amounts are always positive.
  - operation_type_pending_values: list of operation type values whose direction is not
      yet known. These entries are stored with direction=null and flagged via payload_raw.
      e.g. ["02"]  — useful when you know a code exists but don't know its direction yet.

Example parser_config for WebriPost XLSX:
  {
    "sheet": 0,
    "has_header": true,
    "column_map": {
      "reco_id":       "ReferenceRiposte",
      "external_ref":  "IdThaler",
      "value_date":    "OperationDate",
      "operation_date":"OperationDate",
      "amount":        "OperationAmount",
      "event_type":    "TxnTypeRiposte"
    },
    "date_format": "int_yyyymmdd",
    "decimal_separator": ".",
    "default_currency": "EUR",
    "direction_column": "OperationType",
    "direction_map": {"1": "debit", "2": "credit", "30": "debit", "31": "credit"},
    "amount_sign_as_fallback": true,
    "apply_sign_from_direction": true
  }
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Union

from app.services.parsers.base_parser import BaseParser, ParsedEntry, ParseResult


class ExcelParser(BaseParser):
    def parse_file(self, file_path: str) -> ParseResult:
        try:
            import openpyxl  # noqa: PLC0415 — optional dependency
        except ImportError as exc:
            raise ImportError(
                "openpyxl is required for ExcelParser. "
                "Add it to requirements.txt: openpyxl>=3.1"
            ) from exc

        result = ParseResult()
        cfg = self.config

        sheet_ref = cfg.get("sheet", 0)
        has_header: bool = cfg.get("has_header", True)
        skip_rows: int = int(cfg.get("skip_rows", 0))
        col_map: Dict[str, str] = cfg.get("column_map") or {}
        date_fmt: str = cfg.get("date_format", "int_yyyymmdd")
        dec_sep: str = cfg.get("decimal_separator", ".")
        default_currency: str = cfg.get("default_currency", "EUR")
        direction_column: Optional[str] = cfg.get("direction_column")
        direction_map: Dict[str, str] = cfg.get("direction_map") or {}
        amount_sign_fallback: bool = cfg.get("amount_sign_as_fallback", True)
        pending_direction_values: set = set(cfg.get("operation_type_pending_values") or [])
        apply_sign: bool = cfg.get("apply_sign_from_direction", False)
        file_name = self.basename(file_path)

        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)

        # Resolve sheet
        if isinstance(sheet_ref, int):
            ws = wb.worksheets[sheet_ref]
        else:
            ws = wb[sheet_ref]

        rows = list(ws.iter_rows(values_only=True))
        wb.close()

        if not rows:
            return result

        # Build header index
        header_idx: Dict[str, int] = {}
        data_start = 0
        if has_header:
            header_row = rows[0]
            header_idx = {
                str(v).strip(): i
                for i, v in enumerate(header_row)
                if v is not None
            }
            data_start = 1 + skip_rows
        else:
            # Numeric keys for column_map (as str)
            header_idx = {str(i): i for i in range(len(rows[0]) if rows else 0)}
            data_start = skip_rows

        for row_idx, row in enumerate(rows[data_start:], start=data_start + 1):
            if all(v is None for v in row):
                continue  # skip fully empty rows
            try:
                entry = self._row_to_entry(
                    row=row,
                    header_idx=header_idx,
                    col_map=col_map,
                    date_fmt=date_fmt,
                    dec_sep=dec_sep,
                    default_currency=default_currency,
                    direction_column=direction_column,
                    direction_map=direction_map,
                    amount_sign_fallback=amount_sign_fallback,
                    pending_direction_values=pending_direction_values,
                    apply_sign=apply_sign,
                    file_name=file_name,
                )
                result.entries.append(entry)
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"row {row_idx}: {exc}")

        return result

    # ------------------------------------------------------------------
    def _get_cell(
        self,
        row: tuple,
        header_idx: Dict[str, int],
        col_map: Dict[str, str],
        key: str,
    ) -> Optional[Any]:
        col_name = col_map.get(key)
        if not col_name:
            return None
        idx = header_idx.get(col_name)
        if idx is None:
            return None
        if idx >= len(row):
            return None
        v = row[idx]
        return v

    def _get_str(
        self,
        row: tuple,
        header_idx: Dict[str, int],
        col_map: Dict[str, str],
        key: str,
    ) -> Optional[str]:
        v = self._get_cell(row, header_idx, col_map, key)
        if v is None:
            return None
        return str(v).strip() or None

    def _row_to_entry(
        self,
        row: tuple,
        header_idx: Dict[str, int],
        col_map: Dict[str, str],
        date_fmt: str,
        dec_sep: str,
        default_currency: str,
        direction_column: Optional[str],
        direction_map: Dict[str, str],
        amount_sign_fallback: bool,
        pending_direction_values: set,
        apply_sign: bool,
        file_name: str,
    ) -> ParsedEntry:
        get = lambda key: self._get_str(row, header_idx, col_map, key)  # noqa: E731
        get_raw = lambda key: self._get_cell(row, header_idx, col_map, key)  # noqa: E731

        reco_id = get("reco_id")
        account = get("account")
        currency = get("currency") or default_currency
        event_type = get("event_type")
        external_ref = get("external_ref")

        # Amount
        amount_raw = get_raw("amount")
        if amount_raw is None:
            raise ValueError("missing amount")
        amount = self._parse_amount(amount_raw, dec_sep)

        # Dates
        date_raw = get_raw("value_date")
        if date_raw is None:
            raise ValueError("missing value_date")
        value_date = self._parse_date(date_raw, date_fmt)

        op_date_raw = get_raw("operation_date")
        operation_date = self._parse_date(op_date_raw, date_fmt) if op_date_raw else value_date

        # Direction: from column mapping, then amount sign
        direction: Optional[str] = None
        direction_code: Optional[str] = None
        direction_unknown = False

        if direction_column:
            dir_idx = header_idx.get(direction_column)
            if dir_idx is not None and dir_idx < len(row):
                direction_code = str(row[dir_idx]).strip() if row[dir_idx] is not None else None

            if direction_code:
                if direction_code in pending_direction_values:
                    # Direction is not yet known for this code — flag it
                    direction_unknown = True
                    direction = "credit" if amount >= 0 else "debit"  # best-effort
                elif direction_code in direction_map:
                    direction = direction_map[direction_code]
                elif amount_sign_fallback:
                    direction = "credit" if amount >= 0 else "debit"
                else:
                    direction = "credit" if amount >= 0 else "debit"

        if direction is None:
            direction = "credit" if amount >= 0 else "debit"

        # Apply sign from direction: force positive for credit, negative for debit
        if apply_sign and direction:
            if direction == "debit":
                amount = -abs(amount)
            else:
                amount = abs(amount)

        # Build full payload (all columns as raw dict for audit)
        payload: Dict[str, Any] = {}
        if header_idx:
            payload = {
                str(k): (str(row[v]) if row[v] is not None else None)
                for k, v in header_idx.items()
                if v < len(row)
            }
        if direction_unknown:
            payload["_direction_unknown"] = True
            payload["_direction_code"] = direction_code

        return ParsedEntry(
            reco_id=reco_id,
            account=account,
            currency=currency,
            amount=amount,
            value_date=value_date,
            operation_date=operation_date,
            direction=direction,
            event_type=event_type,
            external_ref=external_ref,
            file_name=file_name,
            payload_raw=payload,
        )

    @staticmethod
    def _parse_amount(raw: Any, dec_sep: str) -> Decimal:
        """Parse amount from Excel cell. Cell may be int, float, or str."""
        if isinstance(raw, (int, float)):
            return Decimal(str(raw))
        s = str(raw).strip().replace(" ", "")
        if dec_sep != ".":
            s = s.replace(dec_sep, ".")
        if not s:
            raise ValueError("empty amount cell")
        try:
            return Decimal(s)
        except InvalidOperation as exc:
            raise ValueError(f"cannot parse amount '{raw}'") from exc

    @staticmethod
    def _parse_date(raw: Any, date_fmt: str) -> dt.datetime:
        """Parse date from Excel cell.

        Modes (controlled by date_format config):
          - 'int_yyyymmdd': cell value is int/str like 20260428
          - 'excel_date': cell is already a datetime.datetime (openpyxl native)
          - any strftime format string: parse from string
        """
        if raw is None:
            raise ValueError("date value is None")

        # Native datetime from openpyxl
        if isinstance(raw, (dt.datetime, dt.date)):
            if isinstance(raw, dt.datetime):
                return raw.replace(tzinfo=dt.timezone.utc)
            return dt.datetime(raw.year, raw.month, raw.day, tzinfo=dt.timezone.utc)

        if date_fmt == "excel_date":
            # openpyxl should have resolved it already; if not, treat as YYYYMMDD int
            try:
                return dt.datetime.strptime(str(int(raw)), "%Y%m%d").replace(
                    tzinfo=dt.timezone.utc
                )
            except (ValueError, TypeError):
                pass

        if date_fmt == "int_yyyymmdd":
            try:
                return dt.datetime.strptime(str(int(raw)), "%Y%m%d").replace(
                    tzinfo=dt.timezone.utc
                )
            except (ValueError, TypeError) as exc:
                raise ValueError(f"cannot parse int date '{raw}': {exc}") from exc

        # Strftime format
        try:
            return dt.datetime.strptime(str(raw).strip(), date_fmt).replace(
                tzinfo=dt.timezone.utc
            )
        except ValueError as exc:
            raise ValueError(f"cannot parse date '{raw}' with format '{date_fmt}': {exc}") from exc
