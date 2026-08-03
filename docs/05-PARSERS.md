# File Parsers & Data Ingestion

## Overview

Reconciliation app supports multiple input formats via **pluggable parsers**. Each flow can use a different parser, configured via `Flow.parser_config`.

## Parser Architecture

### Base Class — `base_parser.py`

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from datetime import date
import hashlib

@dataclass
class ParsedEntry:
    """Normalized transaction entry (flow-agnostic)."""
    
    # Key fields (used in matching)
    reco_id: str | None = None
    account: str | None = None
    amount: Decimal
    currency: str
    value_date: date
    
    # Additional context
    external_ref: str | None = None
    event_type: str | None = None
    operation_date: date | None = None
    
    # Raw payload (for audit/debugging)
    payload_raw: dict = field(default_factory=dict)
    
    # Computed (set by parser)
    file_name: str | None = None
    source_hash: str | None = None
    
    def compute_source_hash(self) -> str:
        """
        Unique hash for deduplication.
        Based on: flow_id, external_ref, reco_id, amount, value_date, account, event_type
        """
        content = f"{self.external_ref}|{self.reco_id}|{self.amount}|{self.value_date}|{self.account}|{self.event_type}"
        return hashlib.sha256(content.encode()).hexdigest()


class BaseParser(ABC):
    """Abstract parser interface."""
    
    def __init__(self, config: dict):
        """
        config: Flow.parser_config (parsed from DB)
        Examples:
          - CSV: { "delimiter": ",", "has_header": True, "column_map": {...} }
          - Cobol: { "encoding": "utf-8", "allowed_event_types": [...] }
          - XML: { "row_xpath": "//transaction" }
        """
        self.config = config
    
    @abstractmethod
    def parse(self, file_path: str) -> list[ParsedEntry]:
        """
        Read file, validate, transform to ParsedEntry list.
        Raises: ValueError if parsing fails.
        """
        pass
```

### Factory Function

```python
# services/parsers/__init__.py

def get_parser(parser_type: ParserType, parser_config: dict) -> BaseParser:
    """
    Factory function to get the right parser.
    Dispatches based on Flow.parser_type.
    """
    
    from app.services.parsers.cobol_mosel_parser import CobolMoselParser
    from app.services.parsers.csv_parser import CSVParser
    from app.services.parsers.xml_parser import XMLParser
    from app.services.parsers.mt940_parser import MT940Parser
    from app.services.parsers.finacle_db_extractor import FinacleDBExtractor
    
    return {
        ParserType.COBOL_MOSEL: CobolMoselParser(parser_config),
        ParserType.CSV: CSVParser(parser_config),
        ParserType.XML: XMLParser(parser_config),
        ParserType.MT940: MT940Parser(parser_config),
        ParserType.FINACLE_DB: FinacleDBExtractor(parser_config),
    }[parser_type]
```

---

## Parser Implementations

### 1. Cobol/MOSEL Parser — `cobol_mosel_parser.py`

**Purpose**: Parse ATM files (fixed-width, Cobol MOSEL format)

**File Format Spec** (from `schema_fichier_reco.txt`):

```
Record Type: HEDTRA (header)
  Field 0    | X(1)   | Record type
  Field 1    | X(30)  | Unused
  ...

Record Type: Detail
  F_HEDTRA   | X(1)   | '0'
  F_REFCTR   | X(30)  | Centre reference (reco_id)
  F_REFEXN   | X(30)  | External reference
  F_DATOPN   | X(8)   | Operation date (YYYYMMDD)
  F_DEVISE   | X(3)   | Currency (EUR, USD, etc.)
  F_AMOUNT   | X(27)  | Amount (signed, decimal)
  F_PAYEEE   | X(3200)| Payee data
```

**Implementation**:

```python
class CobolMoselParser(BaseParser):
    # Field offsets (0-indexed)
    F_HEDTRA = (0, 1)
    F_REFCTR = (1, 31)
    F_REFEXN = (31, 61)
    F_DATOPN = (61, 69)
    F_DEVISE = (69, 72)
    F_AMOUNT = (72, 99)
    F_PAYEEE = (99, 3299)
    
    def parse(self, file_path: str) -> list[ParsedEntry]:
        config_encoding = self.config.get("encoding", "utf-8")
        allowed_events = self.config.get("allowed_event_types", [])
        
        entries = []
        
        try:
            with open(file_path, 'r', encoding=config_encoding) as f:
                for line_num, line in enumerate(f, 1):
                    # Skip empty/header lines
                    if not line.strip() or line[0] not in ['0', 'D']:
                        continue
                    
                    try:
                        # Extract fields by offset
                        record_type = line[self.F_HEDTRA[0]:self.F_HEDTRA[1]].strip()
                        reco_id = line[self.F_REFCTR[0]:self.F_REFCTR[1]].strip()
                        external_ref = line[self.F_REFEXN[0]:self.F_REFEXN[1]].strip()
                        date_str = line[self.F_DATOPN[0]:self.F_DATOPN[1]].strip()
                        currency = line[self.F_DEVISE[0]:self.F_DEVISE[1]].strip()
                        amount_str = line[self.F_AMOUNT[0]:self.F_AMOUNT[1]].strip()
                        event_type = extract_event_type_from_payee(line[self.F_PAYEEE[0]:self.F_PAYEEE[1]])
                        
                        # Validate event type
                        if allowed_events and event_type not in allowed_events:
                            continue
                        
                        # Parse amount (handle leading/overpunch signs)
                        amount = Decimal(amount_str)
                        
                        # Parse date
                        value_date = datetime.strptime(date_str, "%Y%m%d").date()
                        
                        entries.append(ParsedEntry(
                            reco_id=reco_id,
                            amount=amount,
                            currency=currency,
                            value_date=value_date,
                            external_ref=external_ref,
                            event_type=event_type,
                            file_name=Path(file_path).name,
                        ))
                    
                    except ValueError as e:
                        raise ValueError(f"Line {line_num}: {e}")
        
        except Exception as e:
            raise ValueError(f"Failed to parse Cobol file: {e}")
        
        return entries
```

**Notes**:
- Encoding: configurable (UTF-8 or EBCDIC `cp037`)
- Amount sign: assumes leading sign; overpunch not supported yet
- Event type: extracted from payee field (configurable whitelist)

### 2. CSV Parser — `csv_parser.py`

**Purpose**: Generic CSV with configurable column mapping

**Example config**:
```python
{
    "delimiter": ";",
    "encoding": "utf-8",
    "has_header": True,
    "date_format": "%d/%m/%Y",
    "column_map": {
        "reco_id": "RECO_ID",
        "amount": "AMOUNT",
        "currency": "CURRENCY",
        "value_date": "VALUE_DATE",
        "external_ref": "REFERENCE",
        "account": "ACCOUNT_NUMBER",
    }
}
```

**Implementation**:

```python
import csv

class CSVParser(BaseParser):
    def parse(self, file_path: str) -> list[ParsedEntry]:
        delimiter = self.config.get("delimiter", ",")
        encoding = self.config.get("encoding", "utf-8")
        has_header = self.config.get("has_header", True)
        column_map = self.config.get("column_map", {})
        date_format = self.config.get("date_format", "%Y-%m-%d")
        
        entries = []
        
        try:
            with open(file_path, 'r', encoding=encoding) as f:
                reader = csv.DictReader(f, delimiter=delimiter) if has_header else csv.reader(f)
                
                for row_num, row in enumerate(reader, 1 if has_header else 0):
                    try:
                        # Map columns
                        reco_id = row.get(column_map.get("reco_id"))
                        amount_str = row.get(column_map.get("amount"))
                        currency = row.get(column_map.get("currency"))
                        date_str = row.get(column_map.get("value_date"))
                        
                        # Convert types
                        amount = Decimal(amount_str.replace(",", "."))
                        value_date = datetime.strptime(date_str, date_format).date()
                        
                        entries.append(ParsedEntry(
                            reco_id=reco_id,
                            amount=amount,
                            currency=currency,
                            value_date=value_date,
                            external_ref=row.get(column_map.get("external_ref")),
                            account=row.get(column_map.get("account")),
                            file_name=Path(file_path).name,
                        ))
                    
                    except ValueError as e:
                        raise ValueError(f"Row {row_num}: {e}")
        
        except Exception as e:
            raise ValueError(f"Failed to parse CSV: {e}")
        
        return entries
```

**Flexibility**: Works with any CSV structure as long as columns are mapped.

### 3. XML Parser — `xml_parser.py`

**Purpose**: Generic XML with XPath-based extraction

**Example config**:
```python
{
    "row_xpath": "//transaction",
    "fields": {
        "reco_id": "@id",
        "amount": "amount/text()",
        "currency": "currency/@code",
        "value_date": "date/text()",
    },
    "date_format": "%Y-%m-%d",
}
```

**Implementation** (simplified):

```python
from lxml import etree

class XMLParser(BaseParser):
    def parse(self, file_path: str) -> list[ParsedEntry]:
        row_xpath = self.config.get("row_xpath", "//row")
        fields = self.config.get("fields", {})
        date_format = self.config.get("date_format", "%Y-%m-%d")
        
        entries = []
        
        try:
            tree = etree.parse(file_path)
            root = tree.getroot()
            rows = root.xpath(row_xpath)
            
            for row_num, row in enumerate(rows, 1):
                try:
                    # Extract fields via XPath
                    data = {}
                    for key, xpath in fields.items():
                        result = row.xpath(xpath)
                        data[key] = result[0] if result else None
                    
                    entries.append(ParsedEntry(
                        reco_id=data.get("reco_id"),
                        amount=Decimal(data.get("amount", 0)),
                        currency=data.get("currency"),
                        value_date=datetime.strptime(data.get("value_date"), date_format).date(),
                        external_ref=data.get("external_ref"),
                        file_name=Path(file_path).name,
                    ))
                
                except ValueError as e:
                    raise ValueError(f"Row {row_num}: {e}")
        
        except Exception as e:
            raise ValueError(f"Failed to parse XML: {e}")
        
        return entries
```

### 4. MT940 Parser — `mt940_parser.py` (STUB)

**Purpose**: Parse SWIFT MT940 bank statement format (BCEE variant for IP flows)

**Status**: NOT YET IMPLEMENTED — awaiting sample file from Post Finance

**Stub**:
```python
class MT940Parser(BaseParser):
    def parse(self, file_path: str) -> list[ParsedEntry]:
        raise NotImplementedError("MT940 parser awaits BCEE sample file from Post Finance")
```

**When implemented, will**:
- Parse SWIFT MT940 structure (tags 20, 25, 28D, etc.)
- Handle multi-message files
- Extract transaction details (debit/credit, amount, date, ref)
- Normalize to ParsedEntry format

### 5. Finacle DB Extractor — `finacle_db_extractor.py`

**Purpose**: Direct extraction from Finacle ODS (Oracle/DB2)

**Example config**:
```python
{
    "connection_id": 1,  # FK to source_connection table
    "query": """
        SELECT 
            reference AS reco_id,
            amount,
            currency,
            value_date,
            account_number AS account,
            event_code AS event_type
        FROM finacle_movements
        WHERE extraction_date = CURRENT_DATE
    """
}
```

**Implementation**:

```python
from sqlalchemy import create_engine, text

class FinacleDBExtractor(BaseParser):
    def parse(self, file_path: str = None) -> list[ParsedEntry]:
        """
        Note: file_path is ignored; we extract from DB via connection config.
        """
        
        connection_id = self.config.get("connection_id")
        query_str = self.config.get("query")
        
        if not connection_id or not query_str:
            raise ValueError("Finacle extractor requires connection_id and query")
        
        # Get connection details from DB (source_connection table)
        from app.repositories.source_connection_repository import source_connection_repository
        
        conn_config = source_connection_repository.get(db, connection_id)
        if not conn_config:
            raise ValueError(f"Connection {connection_id} not found")
        
        # Build DSN
        dsn = conn_config.build_dsn()  # e.g., oracle://user:pass@host:1521/db
        engine = create_engine(dsn)
        
        entries = []
        try:
            with engine.connect() as conn:
                result = conn.execute(text(query_str))
                
                for row_num, row in enumerate(result, 1):
                    try:
                        entries.append(ParsedEntry(
                            reco_id=row["reco_id"],
                            amount=Decimal(row["amount"]),
                            currency=row["currency"],
                            value_date=row["value_date"],
                            account=row.get("account"),
                            event_type=row.get("event_type"),
                            external_ref=row.get("external_ref"),
                        ))
                    except ValueError as e:
                        raise ValueError(f"Row {row_num}: {e}")
        
        finally:
            engine.dispose()
        
        return entries
```

---

## Ingestion Workflow

**Endpoint**: `POST /tasks/ingest/{flow_code}`

```python
async def task_ingest(flow_code: str, db: Session):
    # 1. Get flow config
    flow = flow_repository.get_by_code(db, flow_code)
    if not flow or not flow.is_active:
        return {"status": "skipped", "reason": "flow_not_active"}
    
    # 2. List files in inbox
    inbox_path = Path(settings.INBOX_BASE_PATH) / flow.inbox_subfolder
    files = list(inbox_path.glob("*"))
    
    if not files:
        return {"status": "success", "rows_in": 0, "rows_ok": 0}
    
    # 3. Get parser
    parser = get_parser(flow.parser_type, flow.parser_config)
    
    # 4. Process each file
    stats = {"in": 0, "ok": 0, "ko": 0, "dup": 0}
    ingestion_run = IngestionRun(flow_id=flow.id)
    
    for file_path in files:
        try:
            # Parse
            parsed_entries = parser.parse(str(file_path))
            
            # Compute hashes
            for entry in parsed_entries:
                entry.source_hash = entry.compute_source_hash()
            
            # Bulk insert
            in_, ok, ko, dup = reconciliation_entry_repository.bulk_insert(
                db, parsed_entries
            )
            stats["in"] += in_
            stats["ok"] += ok
            stats["ko"] += ko
            stats["dup"] += dup
            
            # Move to processed
            file_path.rename(
                Path(settings.INBOX_BASE_PATH).parent / "inbox_processed" / 
                flow.inbox_subfolder / file_path.name
            )
        
        except Exception as e:
            # Move to error
            file_path.rename(
                Path(settings.INBOX_BASE_PATH).parent / "inbox_error" / 
                flow.inbox_subfolder / f"{file_path.name}.error"
            )
            # Log error
            ingestion_run.error = str(e)
    
    # 5. Create ingestion_run record
    ingestion_run.rows_in = stats["in"]
    ingestion_run.rows_ok = stats["ok"]
    ingestion_run.rows_ko = stats["ko"]
    ingestion_run.rows_duplicate = stats["dup"]
    ingestion_run.status = "success" if stats["ko"] == 0 else "partial"
    db.add(ingestion_run)
    db.commit()
    
    return ingestion_run.to_dict()
```

---

## Adding a New Parser

When a new file format arrives:

1. **Create parser class**:
   ```python
   # backend/app/services/parsers/new_format_parser.py
   from .base_parser import BaseParser, ParsedEntry
   
   class NewFormatParser(BaseParser):
       def parse(self, file_path: str) -> list[ParsedEntry]:
           # Your implementation
           pass
   ```

2. **Add to factory**:
   ```python
   # backend/app/services/parsers/__init__.py
   
   def get_parser(parser_type: ParserType, config: dict) -> BaseParser:
       return {
           # ... existing
           ParserType.NEW_FORMAT: NewFormatParser(config),
       }[parser_type]
   ```

3. **Add enum**:
   ```python
   # backend/app/models/flow.py
   
   class ParserType(str, Enum):
       COBOL_MOSEL = "cobol_mosel"
       CSV = "csv"
       XML = "xml"
       MT940 = "mt940"
       FINACLE_DB = "finacle_db"
       NEW_FORMAT = "new_format"  # ADD THIS
   ```

4. **Test locally**:
   - Create test file in `shared/inbox/flow_code/`
   - Trigger ingest DAG
   - Check logs + DB for success/errors

---

## Parser Config Examples

### ATM (Cobol/MOSEL)
```json
{
  "encoding": "utf-8",
  "record_separator": "newline",
  "date_format": "%Y%m%d",
  "default_currency": "EUR",
  "allowed_event_types": ["ARACCMVT", "ARCLHSAN", "ARCLHSBK", "AREXCMVT", "SLFRECDP", "SLFRECRT"]
}
```

### IP (Webripost CSV)
```json
{
  "delimiter": ";",
  "encoding": "utf-8",
  "has_header": true,
  "date_format": "%Y-%m-%d",
  "column_map": {
    "reco_id": "RECO_ID",
    "account": "ACCOUNT",
    "currency": "CURRENCY",
    "amount": "AMOUNT",
    "value_date": "VALUE_DATE",
    "external_ref": "REF"
  }
}
```

### Webripost (Excel/XLSX)

**Purpose**: Parse Webripost XLSX exports with configurable OperationType → direction mapping.

```json
{
  "sheet": 0,
  "has_header": true,
  "column_map": {
    "reco_id":        "ReferenceRiposte",
    "external_ref":   "IdThaler",
    "value_date":     "OperationDate",
    "operation_date": "OperationDate",
    "amount":         "OperationAmount",
    "event_type":     "TxnTypeRiposte"
  },
  "date_format": "int_yyyymmdd",
  "decimal_separator": ".",
  "default_currency": "EUR",
  "direction_column": "OperationType",
  "direction_map": {
    "1": "debit",
    "2": "credit",
    "30": "debit",
    "31": "credit"
  },
  "operation_type_pending_values": [],
  "amount_sign_as_fallback": true,
  "apply_sign_from_direction": true
}
```

**Key config options for OperationType handling**:

| Config key | Type | Default | Description |
|------------|------|---------|-------------|
| `direction_column` | string | null | Column header containing the operation type code |
| `direction_map` | dict | `{}` | Maps operation type code → `"credit"` or `"debit"` |
| `apply_sign_from_direction` | bool | `false` | When `true`, forces `+abs(amount)` for credit, `-abs(amount)` for debit. **Essential when source amounts are always positive.** |
| `operation_type_pending_values` | list | `[]` | Codes whose direction is not yet confirmed — entries are flagged in `payload_raw._direction_unknown` |
| `amount_sign_as_fallback` | bool | `true` | When a code is not in `direction_map`, use amount sign to determine direction |

**Current OperationType mapping** (to update when definitive values are confirmed):
- `1` → debit (dépôt)
- `2` → credit (crédit)
- `30` → debit (rollback, treated as withdrawal)
- `31` → credit (rollback, treated as deposit)

To change the mapping, update `parser_config.direction_map` in the `flow_source` table (no code change needed).

### Finacle (DB extraction)
```json
{
  "connection_id": 1,
  "query": "SELECT ... FROM finacle_movements WHERE extraction_date = CURRENT_DATE"
}
```

---

## Multi-Source File Selection — `file_pattern`

Each `FlowSource` can specify a `file_pattern` (glob syntax) to select which files
in the inbox directory should be processed by that source's parser.

### Configuration

| Field | Type | Example | Description |
|-------|------|---------|-------------|
| `file_pattern` | `string` (nullable) | `"*.dat"` | Glob pattern to filter files |

### Behavior

- **`file_pattern` is set**: Only files matching `fnmatch.fnmatch(filename, pattern)` are processed
- **`file_pattern` is null/empty**: All files in the inbox are processed (backward compatible)

### Use Cases

1. **Two parsers, same inbox**: Source A has `*.dat` (Cobol), Source B has `*.csv` (CSV)
2. **Selective prefixes**: `"ATM_*"` to only process files starting with "ATM_"
3. **Single source**: Leave null — all files go through the same parser

### Examples

```python
# Source 1: Cobol parser for .dat files
file_pattern = "*.dat"

# Source 2: CSV parser for .csv files  
file_pattern = "*.csv"

# Source 3: Excel files with specific prefix
file_pattern = "REPORT_*.xlsx"

# Source 4: All files (default behavior)
file_pattern = None
```
