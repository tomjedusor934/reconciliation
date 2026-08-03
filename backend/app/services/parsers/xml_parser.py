"""Generic XML parser — skeleton driven by `parser_config` (XPath-style selectors).

Expected config keys:
  - record_xpath: '//transaction'
  - field_xpaths: {
        'reco_id':       'reco_id/text()',
        'account':       'account/text()',
        'currency':      'currency/text()',
        'amount':        'amount/text()',
        'value_date':    'value_date/text()',
        ...
    }
  - date_format: '%Y-%m-%d'
  - decimal_separator: '.'

Implementation is intentionally minimal — to be enriched once we receive
real XML samples from Post Finance.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

from app.services.parsers.base_parser import BaseParser, ParsedEntry, ParseResult


class XMLParser(BaseParser):
    def parse_file(self, file_path: str) -> ParseResult:
        # lxml is preferred for XPath; fallback to ElementTree for now to stay dep-light.
        try:
            from lxml import etree  # type: ignore

            use_lxml = True
        except ImportError:  # pragma: no cover
            import xml.etree.ElementTree as etree  # type: ignore

            use_lxml = False

        result = ParseResult()
        cfg = self.config
        record_xpath = cfg.get("record_xpath")
        if not record_xpath:
            raise ValueError("XMLParser requires 'record_xpath' in parser_config")

        field_xpaths: Dict[str, str] = cfg.get("field_xpaths") or {}
        date_fmt = cfg.get("date_format", "%Y-%m-%d")
        dec_sep = cfg.get("decimal_separator", ".")
        default_currency = cfg.get("default_currency", "EUR")
        file_name = self.basename(file_path)

        tree = etree.parse(file_path)
        root = tree.getroot()
        if use_lxml:
            records = root.xpath(record_xpath)
        else:
            # ElementTree only supports a limited XPath subset; users with complex
            # selectors should install lxml in requirements.
            records = root.findall(record_xpath.lstrip("/"))

        for idx, rec in enumerate(records, start=1):
            try:
                values = self._extract_fields(rec, field_xpaths, use_lxml=use_lxml)
                entry = self._build_entry(
                    values,
                    date_fmt=date_fmt,
                    dec_sep=dec_sep,
                    default_currency=default_currency,
                    file_name=file_name,
                )
                result.entries.append(entry)
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"record #{idx}: {exc}")
        return result

    @staticmethod
    def _extract_fields(node, field_xpaths: Dict[str, str], *, use_lxml: bool) -> Dict[str, Optional[str]]:
        out: Dict[str, Optional[str]] = {}
        for key, xp in field_xpaths.items():
            if use_lxml:
                vals = node.xpath(xp)
                v = vals[0] if vals else None
            else:
                el = node.find(xp.lstrip("/"))
                v = el.text if el is not None else None
            out[key] = None if v is None else str(v).strip() or None
        return out

    @staticmethod
    def _build_entry(
        values: Dict[str, Optional[str]],
        *,
        date_fmt: str,
        dec_sep: str,
        default_currency: str,
        file_name: str,
    ) -> ParsedEntry:
        amount_raw = values.get("amount")
        if not amount_raw:
            raise ValueError("missing amount")
        try:
            amount = Decimal(amount_raw.replace(dec_sep, "."))
        except InvalidOperation as exc:
            raise ValueError(f"invalid amount '{amount_raw}'") from exc

        date_raw = values.get("value_date")
        if not date_raw:
            raise ValueError("missing value_date")
        try:
            value_date = datetime.strptime(date_raw, date_fmt).replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise ValueError(f"invalid value_date '{date_raw}': {exc}") from exc

        return ParsedEntry(
            reco_id=values.get("reco_id"),
            account=values.get("account"),
            currency=values.get("currency") or default_currency,
            amount=amount,
            value_date=value_date,
            operation_date=value_date,
            direction="credit" if amount >= 0 else "debit",
            event_type=values.get("event_type"),
            external_ref=values.get("external_ref"),
            file_name=file_name,
            payload_raw={k: v for k, v in values.items() if v is not None},
        )
