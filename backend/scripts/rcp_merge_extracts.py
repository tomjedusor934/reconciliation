#!/usr/bin/env python3
"""Merge the daily RCP extracts into one deduplicated set of dumps.

WHY. Finacle ships the return/reject tables as a FULL export every day, not as
a delta: the six folders of 2026-08 hold 207 202 rows for 40 519 distinct ones,
83 Mo across 66 files — over nginx's ``client_max_body_size`` (64 Mo), so they
cannot even be uploaded together. Merging them is not an optimisation, it is
what makes a multi-day extract usable at all, and it is what fills the gaps:
the 2026-08-13 export left ``msgid`` empty on rows the 2026-08-14 one names
(the column was renamed ``pmsgid`` in between), so the union answers where a
single day does not — 1 903 movements controlled exactly against 1 810.

WHAT IT REFUSES TO DO. It never resolves a disagreement. Two days giving one
``entitySrlNum`` a different key, original payment or amount is a fact about the
extract, printed and exited on (code 2), not something to pick a winner for.
(Measured on the current extract: zero.)

The link workbook is NOT merged: each day's is a strict superset of the day
before, and its amounts get FILLED IN over time (an outward reject batch is
listed at 0.00 and priced a day or two later). Only the newest one is right, and
the script says which it is.

Usage:
    python scripts/rcp_merge_extracts.py ../extract -o ../extract/_merged
    python scripts/rcp_merge_extracts.py ../extract -o out --drop-unusable
    python scripts/rcp_merge_extracts.py day1/*.csv day2/*.csv -o out
"""
import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.rcp_link_parser import (  # noqa: E402
    DUMP_AMOUNT,
    DUMP_MSGID,
    DUMP_ORIG,
    DUMP_SRL,
    sniff_delimiter,
    unwrap_quoted_export,
)

csv.field_size_limit(1 << 30)  # a reject row carries a whole JSON blob

# The key column Finacle settled on. Older exports say ``msgid`` for the same
# thing; the merged file is written with one name so the app never has to guess.
CANONICAL_KEY = "pmsgid"


def family_of(path: Path) -> str:
    """The table one file is an export of, whatever the day spelled it.

    ``rejectevent_sctxb_0.csv`` (2026-08-13, a digit zero) and
    ``rejectevent_sctxb_O.csv`` are the same outward reject table; the direction
    suffix is folded, everything before it identifies the family.
    """
    stem = path.stem.lower()
    match = re.match(r"^(?P<base>.+?)_(?P<dir>[oi0])$", stem)
    if not match:
        return stem
    direction = "o" if match.group("dir") in ("o", "0") else "i"
    return f"{match.group('base')}_{direction}"


def read_rows(path: Path) -> Tuple[List[str], List[List[str]], str]:
    """(headers folded to lowercase, rows, delimiter) of one export."""
    text, _wrapped = unwrap_quoted_export(
        path.read_bytes().decode("utf-8-sig", errors="replace")
    )
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return [], [], ";"
    delimiter = sniff_delimiter(lines[0])
    reader = csv.reader(lines, delimiter=delimiter)
    headers = [(h or "").strip().lower() for h in next(reader)]
    return headers, [row for row in reader], delimiter


def _pick(row: Dict[str, str], names: Sequence[str]) -> str:
    for name in names:
        if row.get(name):
            return row[name].strip()
    return ""


class Family:
    """Every export of one table, collapsed on ``entitySrlNum``."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.files: List[str] = []
        self.rows_read = 0
        self.duplicates = 0
        self.key_columns: set = set()
        self.headers: List[str] = []
        self.delimiter = ";"
        # identity -> {"values": {column: value}, "key": str, "file": str}
        self.kept: Dict[str, dict] = {}
        self.conflicts: List[str] = []

    # -- ingestion --------------------------------------------------
    def add(self, path: Path) -> None:
        headers, rows, delimiter = read_rows(path)
        if not headers:
            return
        self.files.append(path.name)
        self.key_columns.update(h for h in headers if h in DUMP_MSGID)
        # The newest file wins the header: a column appears (pmsgid), never
        # disappears, and files are fed in chronological order.
        self.headers, self.delimiter = headers, delimiter
        for raw in rows:
            self.rows_read += 1
            values = {headers[i]: raw[i].strip() for i in range(min(len(headers), len(raw)))}
            self._merge(values, path.name)

    def _merge(self, values: Dict[str, str], file_name: str) -> None:
        key = _pick(values, DUMP_MSGID)
        srl = _pick(values, DUMP_SRL)
        identity = srl or "|".join(values.get(h, "") for h in sorted(values))
        previous = self.kept.get(identity)
        if previous is None:
            self.kept[identity] = {"values": values, "key": key, "file": file_name}
            return
        self.duplicates += 1
        self._check_conflict(previous, values, key, srl, file_name)
        # A later export only ever fills gaps in — never overwrite a value that
        # was already there with an empty one.
        merged = dict(previous["values"])
        for column, value in values.items():
            if value or not merged.get(column):
                merged[column] = value
        previous["values"] = merged
        previous["key"] = key or previous["key"]

    def _check_conflict(
        self, previous: dict, values: Dict[str, str], key: str, srl: str, file_name: str
    ) -> None:
        """Two days describing one row differently — reported, never arbitrated."""
        for label, names in (
            ("clé", DUMP_MSGID), ("origEntityId", DUMP_ORIG), ("montant", DUMP_AMOUNT),
        ):
            before, after = _pick(previous["values"], names), _pick(values, names)
            if before and after and before != after:
                self.conflicts.append(
                    f"{self.name} {srl or '?'}: {label} {before!r} ({previous['file']}) "
                    f"vs {after!r} ({file_name})"
                )

    # -- output -----------------------------------------------------
    @property
    def with_key(self) -> int:
        return sum(1 for row in self.kept.values() if row["key"])

    @property
    def usable(self) -> bool:
        return bool(self.with_key)

    def out_headers(self) -> List[str]:
        """The newest header, with the key column named ``pmsgid`` once."""
        out = [h for h in self.headers if h not in DUMP_MSGID]
        return out + [CANONICAL_KEY]

    def write(self, path: Path) -> int:
        headers = self.out_headers()
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, delimiter=self.delimiter)
            writer.writerow(headers)
            for row in self.kept.values():
                values = row["values"]
                writer.writerow(
                    [row["key"] if h == CANONICAL_KEY else values.get(h, "") for h in headers]
                )
        return len(self.kept)


def merge(paths: Sequence[Path]) -> Dict[str, Family]:
    """Group the exports by family and collapse each one. Order matters: the
    caller feeds them oldest first, so the newest description wins."""
    families: Dict[str, Family] = {}
    for path in paths:
        family = families.setdefault(family_of(path), Family(family_of(path)))
        family.add(path)
    return families


def newest_workbook(root: Path) -> Optional[Path]:
    books = sorted(root.glob("*/SP_LINK_REPORT*.xls*")) or sorted(
        root.glob("SP_LINK_REPORT*.xls*")
    )
    return books[-1] if books else None


def collect(inputs: Sequence[str]) -> Tuple[List[Path], Optional[Path]]:
    """CSV paths oldest-first, plus the workbook to use when given a folder."""
    if len(inputs) == 1 and Path(inputs[0]).is_dir():
        root = Path(inputs[0])
        days = sorted(d for d in root.iterdir() if d.is_dir() and d.name[:8].isdigit())
        paths = [p for day in days for p in sorted(day.glob("*.csv"))]
        return (paths or sorted(root.glob("*.csv"))), newest_workbook(root)
    return [Path(i) for i in inputs], None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "inputs", nargs="+",
        help="dossier des extraits datés (extract/) ou liste de fichiers CSV",
    )
    parser.add_argument("-o", "--out", required=True, help="dossier de sortie")
    parser.add_argument(
        "--drop-unusable", action="store_true",
        help="ne pas écrire les familles dont aucune ligne ne porte de clé",
    )
    args = parser.parse_args()

    paths, workbook = collect(args.inputs)
    if not paths:
        print("aucun CSV trouvé", file=sys.stderr)
        return 2
    print(f"{len(paths)} fichier(s) lus")

    families = merge(paths)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print(f"\n{'famille':32s} {'fichiers':>8s} {'lues':>8s} {'gardées':>8s} "
          f"{'doublons':>9s} {'avec clé':>9s}  colonnes clé")
    written, total_bytes = [], 0
    for name in sorted(families):
        family = families[name]
        note = ""
        if family.usable or not args.drop_unusable:
            target = out / f"{name}.csv"
            family.write(target)
            total_bytes += target.stat().st_size
            written.append(target)
        else:
            note = "  → ignorée (aucune clé)"
        print(
            f"{name:32s} {len(family.files):8d} {family.rows_read:8d} "
            f"{len(family.kept):8d} {family.duplicates:9d} {family.with_key:9d}  "
            f"{','.join(sorted(family.key_columns)) or '-'}{note}"
        )
        if not family.usable and not args.drop_unusable:
            print(f"    ATTENTION {name} : aucune ligne exploitable, "
                  "fichier inutilisable en l'état (à faire corriger côté Finacle)")

    conflicts = [c for family in families.values() for c in family.conflicts]
    print(f"\n{len(written)} fichier(s) écrits dans {out} "
          f"({total_bytes / 1024 / 1024:.1f} Mo)")
    if workbook:
        print(f"classeur à utiliser (le plus récent, sur-ensemble des autres) : {workbook}")
        print(f"contrôle : python scripts/rcp_link_report.py {workbook} {out}/*.csv")
    if conflicts:
        print(f"\n{len(conflicts)} CONFLIT(S) entre jours — rien n'a été arbitré :")
        for line in conflicts[:20]:
            print(f"  {line}")
        if len(conflicts) > 20:
            print(f"  … et {len(conflicts) - 20} autre(s)")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
