"""MT940 parser — BCEE SWIFT MT940 (Customer Statement).

Parses files with SWIFT tags :20:, :25:, :28C:, :60F:/:60M:, :61:, :86:, :62F:/:62M:.

Each :61: tag is a transaction line; the immediately following :86: tag contains
structured details (BCEE format with +XX sub-field separators).

Configurable via flow.parser_config:
  - encoding: file encoding (default 'utf-8')
  - default_currency: fallback currency (default 'EUR')
  - account_override: force a specific account for all entries (optional)
  - reco_id_field: which :86: sub-field code to use as reco_id (default '21',
    meaning the parser extracts the SCRT reference from +21)
  - reco_id_prefix: only keep reco_id values starting with this prefix
    (default 'SCRT' — strips it before storing)
  - customer_ref_as_reco_id: if True and +21 is empty, use the customer
    reference from :61: continuation line as reco_id (default False)
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from app.services.parsers.base_parser import BaseParser, ParsedEntry, ParseResult

# Regex for :61: transaction line (SWIFT standard).
# Format: 6!n[4!n]2a[1!a]15d1!a3!c16x[//16x]
_RE_TAG_61 = re.compile(
    r"^(?P<val_date>\d{6})"            # 6!n  Value date YYMMDD
    r"(?P<entry_date>\d{4})?"           # [4!n] Entry date MMDD (optional)
    r"(?P<dc>[DCRdcr]{1,2})"           # 2a   Debit/Credit (D,C,RD,RC)
    r"(?P<funds>[A-Z])?"               # [1!a] Funds code (optional)
    r"(?P<amount>[\d,]+)"              # 15d  Amount (digits + comma)
    r"(?P<tx_type>[A-Z]\w{3})"         # 1!a3!c Transaction type
    r"(?P<ref>[^\s/]{0,16})"           # 16x  Account owner reference
    r"(?://(?P<bank_ref>[^\s]{0,16}))?" # [//16x] Bank reference
    r"(?:\s+(?P<extra>.+))?"           # Supplementary details (same line)
    r"$"
)

# Regex to split :86: content into structured sub-fields.
# BCEE uses +XX (2-digit numeric code) as separator.
_RE_SUBFIELD = re.compile(r"\+(\d{2})")


class MT940Parser(BaseParser):
    def parse_file(self, file_path: str) -> ParseResult:
        result = ParseResult()
        encoding = self.config.get("encoding", "utf-8")
        default_currency = self.config.get("default_currency", "EUR")
        account_override = self.config.get("account_override")
        reco_id_field = str(self.config.get("reco_id_field", "21"))
        reco_id_prefix = self.config.get("reco_id_prefix", "SCRT")
        customer_ref_as_reco = self.config.get("customer_ref_as_reco_id", False)
        file_name = self.basename(file_path)

        with open(file_path, "rb") as fh:
            raw = fh.read()
        try:
            text = raw.decode(encoding, errors="replace")
        except Exception as exc:
            raise ValueError(f"Cannot decode MT940 with encoding '{encoding}': {exc}") from exc

        messages = self._split_messages(text)

        for msg_idx, msg in enumerate(messages, start=1):
            try:
                tags = self._extract_tags(msg)
            except Exception as exc:
                result.errors.append(f"message {msg_idx}: failed to extract tags: {exc}")
                continue

            account = account_override or tags.get("25", "").strip()
            statement_ref = tags.get("20", "").strip()
            currency = self._extract_currency_from_balance(tags) or default_currency

            # :61: and :86: come in pairs
            transactions = tags.get("_transactions", [])

            for tx_idx, (tag61, tag86) in enumerate(transactions, start=1):
                try:
                    entry = self._parse_transaction(
                        tag61=tag61,
                        tag86=tag86,
                        account=account,
                        currency=currency,
                        statement_ref=statement_ref,
                        reco_id_field=reco_id_field,
                        reco_id_prefix=reco_id_prefix,
                        customer_ref_as_reco=customer_ref_as_reco,
                        file_name=file_name,
                        msg_idx=msg_idx,
                        tx_idx=tx_idx,
                    )
                    result.entries.append(entry)
                except Exception as exc:
                    result.errors.append(f"msg {msg_idx}, tx {tx_idx}: {exc}")

        return result

    # ------------------------------------------------------------------
    # Message splitting
    # ------------------------------------------------------------------
    @staticmethod
    def _split_messages(text: str) -> List[str]:
        """Split MT940 file into individual SWIFT messages.

        Each message starts with {1:... and ends with -}.
        """
        messages: List[str] = []
        # Split on the SWIFT block 1 marker
        parts = re.split(r"(?=\{1:F01)", text)
        for part in parts:
            part = part.strip()
            if part and ":61:" in part:
                messages.append(part)
        return messages

    # ------------------------------------------------------------------
    # Tag extraction
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_tags(message: str) -> Dict[str, Any]:
        """Extract SWIFT tags from a single MT940 message.

        Returns a dict of tag_code → value. :61:/:86: pairs are stored
        under the special key '_transactions' as a list of (tag61, tag86) tuples.
        """
        tags: Dict[str, Any] = {}
        transactions: List[Tuple[str, str]] = []

        # Remove SWIFT envelope (blocks {1:...}{2:...}{3:...}{4:\n ... -})
        # Extract block 4 content
        block4_match = re.search(r"\{4:\s*\n?(.*?)(?:-\}|$)", message, re.DOTALL)
        if block4_match:
            content = block4_match.group(1)
        else:
            content = message

        # Split into tag lines: :XX: marks a new tag
        tag_pattern = re.compile(r"^:(\d{2}[A-Z]?):(.*)$", re.MULTILINE)
        tag_positions = [(m.start(), m.group(1), m.group(2)) for m in tag_pattern.finditer(content)]

        current_61: Optional[str] = None

        for i, (pos, code, first_line) in enumerate(tag_positions):
            # Determine full tag value (including continuation lines)
            if i + 1 < len(tag_positions):
                next_pos = tag_positions[i + 1][0]
                full_value = content[pos + len(f":{code}:"):next_pos].strip()
            else:
                full_value = content[pos + len(f":{code}:"):].strip()

            if code == "61":
                current_61 = full_value
            elif code == "86":
                # Pair with preceding :61:
                if current_61 is not None:
                    transactions.append((current_61, full_value))
                    current_61 = None
                # Standalone :86: without :61: → skip
            else:
                tags[code] = full_value
                current_61 = None  # reset if a non-61/86 tag appears

        # Handle unpaired :61: (no :86: following)
        if current_61 is not None:
            transactions.append((current_61, ""))

        tags["_transactions"] = transactions
        return tags

    # ------------------------------------------------------------------
    # Balance parsing (for currency extraction)
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_currency_from_balance(tags: Dict[str, Any]) -> Optional[str]:
        """Try to extract currency from :60F:/:60M:/:62F:/:62M: balance tags."""
        for tag_code in ("60F", "60M", "62F", "62M"):
            val = tags.get(tag_code, "")
            # Balance format: D/C + YYMMDD + CCY + amount
            m = re.match(r"[DC]\d{6}([A-Z]{3})", val)
            if m:
                return m.group(1)
        return None

    # ------------------------------------------------------------------
    # Transaction parsing
    # ------------------------------------------------------------------
    def _parse_transaction(
        self,
        *,
        tag61: str,
        tag86: str,
        account: str,
        currency: str,
        statement_ref: str,
        reco_id_field: str,
        reco_id_prefix: str,
        customer_ref_as_reco: bool,
        file_name: str,
        msg_idx: int,
        tx_idx: int,
    ) -> ParsedEntry:
        # :61: can span two lines: transaction line + optional customer reference
        lines_61 = tag61.split("\n", 1)
        main_line = lines_61[0].strip()
        customer_ref = lines_61[1].strip() if len(lines_61) > 1 else ""

        m = _RE_TAG_61.match(main_line)
        if not m:
            raise ValueError(f"cannot parse :61: line: '{main_line[:80]}'")

        val_date_str = m.group("val_date")
        entry_date_str = m.group("entry_date") or ""
        dc_mark = m.group("dc").upper()
        amount_str = m.group("amount")
        tx_type = m.group("tx_type") or ""
        owner_ref = m.group("ref") or ""
        bank_ref = m.group("bank_ref") or ""
        extra = m.group("extra") or ""

        # Parse value date (YYMMDD → datetime)
        value_date = self._parse_date(val_date_str)

        # Parse entry date (MMDD → use year from value_date)
        operation_date = None
        if entry_date_str:
            try:
                operation_date = datetime(
                    value_date.year,
                    int(entry_date_str[:2]),
                    int(entry_date_str[2:4]),
                    tzinfo=timezone.utc,
                )
            except (ValueError, IndexError):
                pass

        # Amount: comma → dot, apply sign from D/C
        amount = self._parse_amount(amount_str)
        is_debit = dc_mark in ("D", "RD")
        if is_debit:
            amount = -abs(amount)
        else:
            amount = abs(amount)
        direction = "debit" if is_debit else "credit"

        # Parse :86: sub-fields (BCEE structured format)
        subfields = self._parse_tag86(tag86) if tag86 else {}

        # Extract reco_id from configured sub-field
        reco_id = self._extract_reco_id(
            subfields, reco_id_field, reco_id_prefix, customer_ref, customer_ref_as_reco
        )

        # Build external_ref from owner_ref, bank_ref, or customer_ref
        external_ref = customer_ref or owner_ref or bank_ref or None

        # Extract beneficiary info from :86:
        beneficiary = subfields.get("32", "")
        beneficiary_addr = subfields.get("33", "")

        # Extract OCMT (original amount) from +26 if present
        ocmt = ""
        field_26 = subfields.get("26", "")
        ocmt_match = re.search(r"/OCMT/([A-Z]{3}[\d,]+)", field_26)
        if ocmt_match:
            ocmt = ocmt_match.group(1)

        return ParsedEntry(
            reco_id=reco_id,
            account=account or None,
            currency=currency,
            amount=amount,
            value_date=value_date,
            operation_date=operation_date or value_date,
            direction=direction,
            event_type=tx_type or None,
            external_ref=external_ref,
            file_name=file_name,
            payload_raw={
                "statement_ref": statement_ref,
                "dc_mark": dc_mark,
                "owner_ref": owner_ref,
                "bank_ref": bank_ref,
                "customer_ref": customer_ref,
                "tx_type": tx_type,
                "tag86_raw": tag86[:500] if tag86 else "",
                "tag86_subfields": subfields,
                "beneficiary": beneficiary,
                "beneficiary_address": beneficiary_addr,
                "ocmt": ocmt,
                "_msg_idx": msg_idx,
                "_tx_idx": tx_idx,
            },
        )

    # ------------------------------------------------------------------
    # :86: sub-field parser (BCEE structured format)
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_tag86(tag86: str) -> Dict[str, str]:
        """Parse BCEE structured :86: into {code: value} dict.

        Format: GVC_CODE+00VALUE+20VALUE+21VALUE+22...
        Sub-fields use +XX where XX is a 2-digit numeric code.
        Non-numeric sequences after + are part of the preceding field value.
        """
        if not tag86:
            return {}

        # Normalize: join continuation lines, collapse whitespace
        text = re.sub(r"\n\s*", "", tag86)

        subfields: Dict[str, str] = {}

        # Find all sub-field positions
        matches = list(_RE_SUBFIELD.finditer(text))
        if not matches:
            # No structured sub-fields; store raw text under key "_raw"
            subfields["_raw"] = text
            return subfields

        # Content before first sub-field is the GVC code
        gvc_text = text[:matches[0].start()].strip()
        if gvc_text:
            subfields["_gvc"] = gvc_text

        # Extract each sub-field
        for i, m in enumerate(matches):
            code = m.group(1)
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            value = text[start:end].strip()
            subfields[code] = value

        return subfields

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_reco_id(
        subfields: Dict[str, str],
        reco_id_field: str,
        reco_id_prefix: str,
        customer_ref: str,
        customer_ref_as_reco: bool,
    ) -> Optional[str]:
        """Extract the reconciliation ID from :86: sub-fields."""
        raw = subfields.get(reco_id_field, "").strip()

        if raw and reco_id_prefix:
            # Look for prefix (e.g. SCRT1001944193 → strip SCRT → 1001944193)
            if raw.startswith(reco_id_prefix):
                return raw[len(reco_id_prefix):]
            # If prefix not found, still use the raw value
            return raw
        if raw:
            return raw

        # Fallback: use customer reference from :61: continuation
        if customer_ref_as_reco and customer_ref:
            return customer_ref

        return None

    @staticmethod
    def _parse_date(yymmdd: str) -> datetime:
        """Parse YYMMDD date string into a datetime."""
        if len(yymmdd) != 6:
            raise ValueError(f"invalid date '{yymmdd}' (expected YYMMDD)")
        year = 2000 + int(yymmdd[:2])
        month = int(yymmdd[2:4])
        day = int(yymmdd[4:6])
        return datetime(year, month, day, tzinfo=timezone.utc)

    @staticmethod
    def _parse_amount(raw: str) -> Decimal:
        """Parse MT940 amount (European format: comma as decimal separator)."""
        s = raw.strip().replace(",", ".")
        if not s:
            raise ValueError("empty amount")
        try:
            return Decimal(s)
        except InvalidOperation as exc:
            raise ValueError(f"cannot parse amount '{raw}'") from exc
