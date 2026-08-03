"""Parser package — pluggable parsers for ingestion sources."""
from app.services.parsers.base_parser import BaseParser, ParsedEntry, ParserError
from app.services.parsers.csv_parser import CSVParser
from app.services.parsers.excel_parser import ExcelParser
from app.services.parsers.cobol_mosel_parser import CobolMoselParser
from app.services.parsers.mt940_parser import MT940Parser
from app.services.parsers.xml_parser import XMLParser

__all__ = [
    "BaseParser",
    "ParsedEntry",
    "ParserError",
    "CSVParser",
    "ExcelParser",
    "XMLParser",
    "CobolMoselParser",
    "MT940Parser",
    "get_parser",
]


def get_parser(parser_type: str, config: dict | None = None) -> BaseParser:
    """Factory: return the parser implementation matching `parser_type`."""
    config = config or {}
    mapping = {
        "csv": CSVParser,
        "excel": ExcelParser,
        "xml": XMLParser,
        "cobol_mosel": CobolMoselParser,
        "mt940": MT940Parser,
    }
    cls = mapping.get(parser_type)
    if cls is None:
        raise ValueError(f"Unknown parser_type: {parser_type}")
    return cls(config=config)
