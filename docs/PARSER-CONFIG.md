# Parser configuration reference

How to configure a flow **source** so its files are ingested correctly. No code
needed — everything here is set per source (in the **/flows** UI or the seed) via
the source's fields and its `parser_config` JSON.

---

## 1. Where this lives

Each flow has one or more **sources** (`reco.flow_source`). A file source is
described by these fields:

| Field | Meaning |
|---|---|
| `source_type` | `file` (parsed from the inbox) or `finacle_db` (datamart, see §8). |
| `parser_type` | Which parser runs: `csv`, `cobol_mosel`, `mt940`, `excel`, `xml`. |
| `match_key_strategy` | How entries are grouped for auto-matching: `reco_id_amount` (group by reco_id+currency, match when the sum is 0), `file_ref_amount`, `ref_amount`. Use `reco_id_amount` for finacle-vs-file reconciliation. |
| `inbox_subfolder` | Subfolder of the inbox where files are dropped (e.g. `webripost`). |
| `file_pattern` | `fnmatch` glob; only matching files are picked up (e.g. `WEBRIPOSTE*.csv`, `*` for any). The **extension is not interpreted** — content is what matters. Empty = all files. |
| `is_active` | If false, the source is skipped. |
| `parser_config` | The JSON described below — parser-specific. |
| `accounts` | Reference accounts (finacle sources only). |

**How it runs**: `get_parser(parser_type, parser_config)`
(`backend/app/services/parsers/__init__.py`) builds the parser;
`ingestion_service.ingest_inbox_for_flow` lists files in `inbox_subfolder`
matching `file_pattern`, parses each, and bulk-inserts the rows (deduplicated on
`source_hash`). A row that can't be parsed becomes a **per-row error** on the
`IngestionRun` (run status `PARTIAL`) — the rest still ingest.

---

## 2. What you map TO (canonical entry fields)

Every parser produces normalized entries with these fields. Your `column_map` /
`field_xpaths` point source columns at them:

| Field | Notes |
|---|---|
| `reco_id` | **The reconciliation key.** Two sides match when entries sharing a `reco_id` (+ currency) sum to 0. |
| `amount` | Signed `Decimal`. **Convention: credit = positive, debit = negative**, so a balanced pair nets to 0. |
| `direction` | `credit` / `debit`. |
| `currency` | Defaults to `default_currency` when absent. |
| `value_date` | Datetime (required). |
| `operation_date` | Defaults to `value_date`. |
| `account` | Account number (optional). |
| `event_type` | Transaction type/code (optional). |
| `external_ref` | Technical unique id; contributes to `source_hash` (dedup). |
| `ref_no`, `remarks_1`, `transaction_particulars` | Extra context (csv supports mapping these). |
| `payload_raw` | The full original row, always stored for audit. |

---

## 3. `csv`

Generic `;`/`,`-delimited CSV. All keys optional unless noted.

| Key | Default | Meaning |
|---|---|---|
| `delimiter` | `,` | Field separator (`;` for Riposte). |
| `encoding` | `utf-8` | File encoding. |
| `has_header` | `true` | First row is the header; `column_map` uses header names (else 0-based indices as strings). |
| `decimal_separator` | `.` | For amount strings (`,` for European decimals). |
| `default_currency` | `EUR` | Used when no `currency` column. |
| `column_map` | — | Maps canonical fields → CSV columns. Supported keys: `reco_id`, `ref_no`, `remarks_1`, `transaction_particulars`, `account`, `currency`, `amount` (**required**), `value_date`, `operation_date`, `event_type`, `external_ref`. |
| `date_format` | `%Y-%m-%d` | Used with a single `column_map.value_date`. |
| `datetime_fields` | — | List of columns joined with a space to build `value_date` (e.g. `["Date","Time"]`); overrides the single `value_date` path. |
| `datetime_format` | `%Y-%m-%d %H:%M:%S` | strptime format for `datetime_fields`. `%b` (e.g. `Jun`) is parsed **independently of the server locale**. |
| `amount_in_cents` | `false` | When true, `amount = value / 100` (2 decimals). Use when the file stores integer cents. |
| `direction` | — | Derive credit/debit from a text field (when the file has no sign). See below. |
| `skip_status` | — | `{ "field": "Status", "values": ["InDoubt"] }` → drop those rows (not errors). |

**`direction` block** (signs the amount — `abs` for credit, `-abs` for debit):
```json
"direction": {
  "field": "TxnType",
  "keywords": { "withdraw": "debit", "cheque": "credit", "depot": "credit" },
  "reversal_keyword": "reverse",
  "default": null
}
```
- `keywords`: case-insensitive **substring** match on `field`; first match wins.
- `reversal_keyword`: if also present in the value, **flips** the matched direction (e.g. `CCPWithdrawWiiReverse` → credit).
- `default`: used when no keyword matches; `null` → the row is a **row error** (surfaces unknown types so you add them to the map).
- When the `direction` block is omitted, direction is inferred from the amount's sign.

**Live example (WebriPost / Riposte CSV)** — amounts in cents, always positive:
```json
{
  "delimiter": ";", "encoding": "utf-8", "has_header": true,
  "amount_in_cents": true, "default_currency": "EUR",
  "datetime_fields": ["Date", "Time"], "datetime_format": "%d-%b-%Y %H:%M:%S",
  "column_map": {
    "amount": "Amount", "reco_id": "Reference", "ref_no": "Reference",
    "remarks_1": "CCPAccount", "external_ref": "TxnId", "event_type": "TxnType"
  },
  "direction": {
    "field": "TxnType",
    "keywords": { "withdraw": "debit", "cheque": "credit", "depot": "credit" },
    "reversal_keyword": "reverse", "default": null
  },
  "skip_status": { "field": "Status", "values": ["InDoubt"] }
}
```

---

## 4. `cobol_mosel`

Fixed-width ATM/MOSEL flat file. Field **positions are fixed in code** (not
configurable); these keys tune parsing:

| Key | Default | Meaning |
|---|---|---|
| `encoding` | `utf-8` | Set `cp037` for EBCDIC. |
| `record_separator` | `newline` | `newline` (one record per line) or `fixed` (3269-byte records). |
| `date_format` | `%Y%m%d` | Format of the DATOPN date field. |
| `default_currency` | `EUR` | Fallback when DEVISE is blank. |
| `devise_length` | `3` | Currency field width (3 in practice, 8 per the legacy schema). |
| `allowed_event_types` | — | Optional whitelist of TYPEVT codes; others are skipped. |
| `event_type_account_map` | — | `{ "ARACCMVT": "0010110040001", ... }` → auto-assigns `account` from the event type. |

Amount sign comes from the file (`-90,00` / `890,00`; comma or dot decimals).

---

## 5. `mt940`

SWIFT MT940 customer statement (BCEE structured `:86:`).

| Key | Default | Meaning |
|---|---|---|
| `encoding` | `utf-8` | File encoding. |
| `default_currency` | `EUR` | Fallback when no balance currency. |
| `account_override` | — | Force one account for all entries (else taken from `:25:`). |
| `reference_owner_field` | `21` | `:86:` `+XX` sub-field holding the reconciliation key (`REFERENCE_OWNER`, e.g. `SCRT1001944193`). Stored **raw** in `ref_no`. |
| `reco_id_prefix` | `SCRT` | Prefix tried both ways when resolving (the key is matched with **and** without it). |
| `resolve_reco_id_via_finacle` | `false` | When **true**, `reco_id` is left empty at parse and resolved by matching `ref_no` (the key) against already-ingested **finacle** entries' `reco_id`/`ref_no`/`remarks_1` in the same flow; no match → NULL (retried at the next reconcile). When false, `reco_id` = the prefix-stripped key directly (legacy). |
| `reco_id_field` | `21` | (Legacy, used only when `resolve_reco_id_via_finacle=false`.) |
| `customer_ref_as_reco_id` | `false` | (Legacy.) If the sub-field is empty, fall back to the `:61:` customer reference. |

Direction/amount come from the `:61:` D/C marker (debit → negative). The finacle-lookup
mode requires finacle to be ingested **before** the MT940 file (the orchestrator already
does finacle → files → reconcile).

**How the reco_id is resolved (lookup mode).** The `+21` key never matches an Oracle DB
(unreachable from the app); instead it is matched against our **own** finacle entries in
two passes:
1. **Flow sweep at the start of file ingestion** — before parsing any file, the flow's
   entries that still have a key (`ref_no`) but no `reco_id` (e.g. MT940 lines from earlier
   cycles) are re-matched against the finacle entries now present. This catches the cross-run
   case where the MT940 file arrived before its finacle counterpart.
2. **Inline at parse time** — each newly parsed MT940 line is resolved immediately against
   the already-ingested finacle entries.

A global sweep also runs at the start of every reconciliation as a safety net. In every
pass the match is scoped to **finacle sources of the same flow**, trying the key both with
and without the `reco_id_prefix`; no match → `reco_id` stays NULL and is retried next cycle.

---

## 6. `excel` (legacy)

XLSX/XLS via openpyxl — same column-mapping idea as CSV. WebriPost moved to
`csv`; kept for other spreadsheet sources.

| Key | Default | Meaning |
|---|---|---|
| `sheet` | `0` | Sheet index or name. |
| `has_header` | `true` | First row is the header. |
| `skip_rows` | `0` | Data rows to skip after the header. |
| `column_map` | — | `amount` & `value_date` **required**; also `reco_id`, `external_ref`, `event_type`, `account`, `currency`, `operation_date`. |
| `date_format` | `int_yyyymmdd` | `int_yyyymmdd` (e.g. `20260428`), `excel_date` (native cell), or any strftime string. |
| `decimal_separator` | `.` | For string amount cells. |
| `default_currency` | `EUR` | — |
| `direction_column` | — | Column whose value sets debit/credit. |
| `direction_map` | — | `{ "1": "debit", "2": "credit" }`. |
| `amount_sign_as_fallback` | `true` | Use the amount sign when a code isn't in `direction_map`. |
| `apply_sign_from_direction` | `false` | Force `+abs`/`-abs` from direction (use when file amounts are always positive). |
| `operation_type_pending_values` | — | Codes whose direction is unknown → stored with `direction=null` and flagged in `payload_raw`. |

---

## 7. `xml` (skeleton)

Minimal XPath-driven parser (install `lxml` for full XPath).

| Key | Default | Meaning |
|---|---|---|
| `record_xpath` | — | **Required**; selects record nodes (e.g. `//transaction`). |
| `field_xpaths` | — | `{ "reco_id": "ref/text()", "amount": "amount/text()", "value_date": "date/text()", ... }`. `amount` & `value_date` required. |
| `date_format` | `%Y-%m-%d` | — |
| `decimal_separator` | `.` | — |
| `default_currency` | `EUR` | — |

---

## 8. `finacle_db` (no parser_config)

These sources are **not** file-parsed. The `ingest_finacle` Airflow DAG pulls
`std.Movement` from the datamart for the source's **`accounts`** and computes the
`reco_id` itself. The `parser_config` is unused (`{}`). See
[`04-AIRFLOW.md`](04-AIRFLOW.md).

Same for `finacle_batch_booking_true`: same source type, no `parser_config`, but
the `ingest_finacle_bb` DAG buckets the movements into lots and `reco_id` is the
lot uuid.

---

## 9. `wero` (datamart, `parser_config` **used**)

Also a `finacle_db` **source type**, but the only datamart parser whose
`parser_config` matters — and the only flow that is a **payment**
reconciliation rather than an accounting one. `std.Movement` is never read.

The `ingest_wero` DAG (`shared/dags/reco_wero.py`) streams three legs and pushes
one entry each, all legs of a payment sharing the same `reco_id` (the
**end-to-end reference**, normalised):

| Leg | Source | `event_type` | Sign |
|---|---|---|---|
| WERO payment | the datamart WERO table | `WERO` | **credit `+X`** |
| WERO reversal | same, rows in `wero_reversal_statuses` | `WERO_RVSL` | credit `+X`, `reco_id` suffixed `#RET` |
| Finacle payment | `std.Payment` filtered on `InitModule` | `PAYMENT` | **debit `−X`** |
| Finacle return | `std.[Return]` → its original payment | `RETURN` | debit `−X`, `reco_id` suffixed `#RET` |

The standard sum-to-zero engine then reconciles them with no change. What you
read in the operational view:

- pending **credit** → a WERO row with no Finacle counterpart
- pending **debit** → a WERO payment in Finacle with no WERO row
- two pending rows summing ≠ 0 → an amount discrepancy
- `reco_id` NULL → a leg with no usable end-to-end reference

The sign is a matching device, not an accounting statement: the business
direction stays in `remarks_1` (`WERO/IN`, `P2P`, `EMC/RETURN`) and in
`payload_raw`. A return and its WERO reversal form their own group under
`#RET`, so an unmirrored return does not drag the reconciled original pair back
to pending. The source carries **no reference account** — its perimeter is
`InitModule`, not a GL account.

**No reference account, and no `BaseParser` subclass**: like the other datamart
types it is never routed through `get_parser()`.

| Key | Default | Notes |
|---|---|---|
| `wero_table` | `std.Wero` | **confirmed on production 2026-08-26**; schema-qualified name accepted |
| `wero_ref_column` | `OriginatorReference` | the end-to-end reference |
| `wero_id_column` | `CaptureIDMoneyTransferID` | feeds `external_ref` / `ref_no` |
| `wero_date_column` | `SettlementRelatedTimestamp` | business date (a varchar in the datamart) |
| `wero_watermark_column` | `StartDate` | incremental filter — a real DATE, deliberately not the varchar above |
| `wero_amount_column` | `TransactionAmount` | varchar; `125,00` and `1 250.55` are accepted, `0` and unparsable are row errors |
| `wero_currency_column` | `Currency` | |
| `wero_direction_column` | `TransactionDirection` | goes to `remarks_1`, never to the sign |
| `wero_status_column` | `SettlementStatus` | goes to `transaction_particulars` |
| `wero_reversal_statuses` | `[]` | **confirmed 2026-08-26: no reversal status exists** (only Accepted / Failed / Rejected / Settled). Stays empty ⇒ **no reversal leg emitted** |
| `wero_settlement_statuses` | `[]` | whitelist of statuses worth reconciling; empty = all (no filtering). Applied in SQL, not when mapping the row |
| `payment_e2e_column` | `EndToEndId` | **confirmed on production 2026-08-26** — the datamart's `endtoendidentification` |
| `payment_init_modules` | `["WERO","WEROEMC"]` | the perimeter; also gives P2P vs EMC |
| `payment_date_column` | `CreatedOn` | |
| `payment_currency_column` | `null` | `null` → `default_currency`. Held there: std.Payment carries two currency families (`SettlementCurrency` / `SettlementCcy`) and currency is part of the match key |
| `return_date_column` | `null` | `null` → the original payment's date **and** watermark. `CreatedOn` / `SettlementDate` exist but an empty date makes every return a row error |
| `return_amount_column` | `null` | `null` → the original payment's `SettlementAmount`. `ReturnSettlementAmount` exists but an empty amount raises in `parse_amount` |
| `return_status_column` | `Status` | **confirmed 2026-08-26**, and risk-free: only feeds `transaction_particulars` |
| `returns_enabled` | `true` | |
| `default_currency` | `EUR` | |

Every identifier is validated as a bare (optionally schema-qualified) SQL
identifier when the config loads: these values reach SQL by interpolation
because a column name cannot be a bind parameter. List values are bound.

Both guessed identifiers are **confirmed on production (2026-08-26)**:
`std.Wero` and `std.Payment.EndToEndId`. Three keys stay `null` on purpose —
`payment_currency_column`, `return_amount_column`, `return_date_column`: their
columns **exist** but are not known to be **populated**, and each one feeds the
currency (part of the match group key) or the amount, where an empty value
breaks matching silently or turns every return into a row error. The queries in
[`wero-datamart-diagnostics.sql`](wero-datamart-diagnostics.sql) unblock them —
and setting them stays a UI edit, never a DAG redeploy.

Still open: `std.Wero` carries no reversal status, so a Finacle `std.[Return]`
has no WERO counterpart today and sits in pending debit under `#RET` — visible
and unmatched, which is the truthful signal. Diagnostics query C settles it.

The WERO flow is seeded **inactive**: production `std.Wero` was still empty on
2026-08-26 (the datamart table is loaded downstream of the ODS
`WERO.RECONCILIATION`), while `std.Payment` already carried 21 `WERO` + 3
`WEROEMC` payments. Diagnostics query E tells the two situations apart.

**Reference normalisation** — one function, used on all three sides: `upper()`
(MSSQL's collation is case-insensitive, Python's comparison is not), then
dashes dropped **only** when the result is a 32-hex-digit UUID (WERO stores the
transfer id with dashes, Finacle sometimes without).

---

## 10. Tips

- **Matching**: the file side and its finacle/GL counterpart must produce the
  **same `reco_id`** and **opposite-signed amounts** for a group to net to 0.
  If a reconciliation doesn't match, check those two first.
- **Unknown rows**: `csv.direction.default = null` (and any parse failure) makes a
  row a per-run error rather than ingesting it wrongly — inspect the
  `IngestionRun` to see what to add to the config.
- **Re-running**: re-dropping the same file is safe — rows dedup on `source_hash`.
- **Dates**: all dates are stored as UTC; locale-dependent month names (`%b`) are
  handled by the csv parser, but prefer numeric formats elsewhere.
