#!/usr/bin/env python3
"""Extract the *structural* patterns of ``transaction_particulars``.

Goal: not literal diffing, but grouping "in the gross". Each value is split on
``/`` and every segment is either kept **literal** (short uppercase codes:
prefix, direction I/O, sub-type NCC/NCP, ...) or **generalised** to a class
placeholder (``<NUM>``, ``<ALNUM>``, ``<TEXT>``, ``<OTHER>``). Values sharing the
same rendered signature are grouped, e.g.::

    NDGB/I/aaa   ->  NDGB / I / <TEXT>
    NDGB/I/bbb   ->  NDGB / I / <TEXT>   (same group)
    BKRTP/NCP/O/000008870881/ROLLINGER GUY E  ->  BKRTP / NCP / O / <NUM> / <TEXT>

For every pattern the script also surfaces, per variable position, the distinct
value count and a sample of concrete values — so all variances are exposed. The
JSON/text output is meant to be handed to an LLM for a cleaner human summary.

Data sources (pick one):
    --dsn / $DATABASE_URL   query Postgres reco.reconciliation_entry (+ emargement)
    --file PATH             .csv (col 'transaction_particulars') or plain text (1/line)
    --stdin                 read values from stdin (1 per line)

Examples:
    python scripts/extract_tp_patterns.py                    # uses $DATABASE_URL
    python scripts/extract_tp_patterns.py --file dump.csv --json patterns.json
    grep -h ... dumps.txt | python scripts/extract_tp_patterns.py --stdin
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from typing import Iterable, Iterator, List, Tuple

# ── Segment classification ────────────────────────────────────────────────
# Whether a segment position is a *code* (kept literal) or a *variable* (kept
# as a class placeholder) is decided by cardinality, not by looks: a position
# with few distinct values across the dataset is an enumerable code (prefix,
# direction I/O, sub-type NCC/NCP, ...); a position with many distinct values is
# a free identifier / name. The prefix (position 0 of a structured value) is
# always kept literal so patterns stay anchored on it.
EMPTY_TOKEN = "∅"


def split_segments(value: str) -> List[str]:
    return [s.strip() for s in value.split("/")]


def shape_class(seg: str) -> str:
    """Coarse class of a variable segment (used for its placeholder token)."""
    if seg == "":
        return "EMPTY"
    if seg.isdigit():
        return "NUM"
    if re.fullmatch(r"[A-Za-z0-9]+", seg) and any(c.isdigit() for c in seg) and any(c.isalpha() for c in seg):
        return "ALNUM"  # letters + digits → reference / code (e.g. BKRTP000305424)
    return "TEXT"       # names, punctuation, spaces, pure words → free text


def placeholder(cls: str) -> str:
    return EMPTY_TOKEN if cls == "EMPTY" else f"<{cls}>"


# ── Input sources ─────────────────────────────────────────────────────────
def from_stdin() -> Iterator[Tuple[str, int]]:
    for line in sys.stdin:
        v = line.rstrip("\n")
        if v.strip():
            yield v, 1


def from_file(path: str) -> Iterator[Tuple[str, int]]:
    if path.lower().endswith(".csv"):
        with open(path, newline="", encoding="utf-8", errors="replace") as fh:
            reader = csv.DictReader(fh)
            col = None
            for cand in ("transaction_particulars", "TransactionParticulars"):
                if reader.fieldnames and cand in reader.fieldnames:
                    col = cand
                    break
            if col is None:
                sys.exit(
                    f"CSV {path} has no 'transaction_particulars' column "
                    f"(found: {reader.fieldnames})"
                )
            for row in reader:
                v = (row.get(col) or "").strip()
                if v:
                    yield v, 1
    else:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                v = line.rstrip("\n")
                if v.strip():
                    yield v, 1


def from_db(dsn: str, include_emargement: bool) -> Iterator[Tuple[str, int]]:
    try:
        from sqlalchemy import create_engine, text
    except ImportError:
        sys.exit("SQLAlchemy is required for DB mode (pip install sqlalchemy psycopg2-binary).")

    tables = ["reco.reconciliation_entry"]
    if include_emargement:
        tables.append("reco.reconciliation_entry_emargement")
    union = " UNION ALL ".join(
        f"SELECT transaction_particulars AS tp FROM {t} "
        f"WHERE transaction_particulars IS NOT NULL AND transaction_particulars <> ''"
        for t in tables
    )
    sql = f"SELECT tp, COUNT(*) AS n FROM ({union}) s GROUP BY tp"

    engine = create_engine(dsn, pool_pre_ping=True)
    with engine.connect() as conn:
        for tp, n in conn.execute(text(sql)):
            yield tp, int(n)


# ── Aggregation ───────────────────────────────────────────────────────────
class Pattern:
    __slots__ = ("pattern", "meta", "count", "raw", "examples", "pos_values")

    def __init__(self, pattern: str, meta: List[Tuple[bool, str]]):
        self.pattern = pattern
        self.meta = meta                    # per pos: (is_variable, class)
        self.count = 0                      # total occurrences (weighted)
        self.raw: set[str] = set()          # distinct raw values
        self.examples: List[str] = []       # a few raw examples
        # per variable position: distinct value counter
        self.pos_values: dict[int, Counter] = defaultdict(Counter)


def decide_enum_positions(
    rows: List[Tuple[List[str], int]], threshold: int
) -> dict[int, dict[int, bool]]:
    """Per segment-count bucket N, decide which positions are enumerable codes.

    A position is a code (literal) when its distinct-value count ≤ threshold.
    For structured values (N > 1) position 0 (the prefix) is always literal.
    """
    distinct: dict[int, dict[int, set]] = defaultdict(lambda: defaultdict(set))
    for segs, _w in rows:
        n = len(segs)
        for i, seg in enumerate(segs):
            distinct[n][i].add(seg)
    enum: dict[int, dict[int, bool]] = {}
    for n, positions in distinct.items():
        enum[n] = {
            i: (len(vals) <= threshold) or (n > 1 and i == 0)
            for i, vals in positions.items()
        }
    return enum


def aggregate(
    rows: List[Tuple[List[str], int]], threshold: int, max_examples: int
) -> dict[str, Pattern]:
    enum = decide_enum_positions(rows, threshold)
    patterns: dict[str, Pattern] = {}
    for segs, weight in rows:
        n = len(segs)
        tokens: List[str] = []
        meta: List[Tuple[bool, str]] = []
        for i, seg in enumerate(segs):
            if enum[n].get(i):
                tokens.append(seg if seg else EMPTY_TOKEN)
                meta.append((False, "CODE" if seg else "EMPTY"))
            else:
                cls = shape_class(seg)
                tokens.append(placeholder(cls))
                meta.append((True, cls))
        pat = " / ".join(tokens)
        p = patterns.get(pat)
        if p is None:
            p = patterns[pat] = Pattern(pat, meta)
        raw = "/".join(segs)
        p.count += weight
        if raw not in p.raw:
            p.raw.add(raw)
            if len(p.examples) < max_examples:
                p.examples.append(raw)
        for i, (is_var, _cls) in enumerate(meta):
            if is_var:
                p.pos_values[i][segs[i]] += weight
    return patterns


def build_report(patterns: dict[str, Pattern], samples: int) -> dict:
    total = sum(p.count for p in patterns.values())
    distinct = sum(len(p.raw) for p in patterns.values())
    out_patterns = []
    for p in sorted(patterns.values(), key=lambda x: x.count, reverse=True):
        segments = []
        for i, (is_var, cls) in enumerate(p.meta):
            if is_var:
                vc = p.pos_values.get(i, Counter())
                segments.append(
                    {
                        "pos": i,
                        "kind": "variable",
                        "class": cls,
                        "distinct": len(vc),
                        "samples": [v for v, _ in vc.most_common(samples)],
                    }
                )
            else:
                # literal code (or empty marker) — value read from the pattern token
                segments.append(
                    {
                        "pos": i,
                        "kind": "literal",
                        "class": cls,
                        "value": p.pattern.split(" / ")[i],
                    }
                )
        out_patterns.append(
            {
                "pattern": p.pattern,
                "count": p.count,
                "distinct_raw": len(p.raw),
                "examples": p.examples,
                "segments": segments,
            }
        )
    return {
        "summary": {
            "total_values": total,
            "distinct_values": distinct,
            "patterns": len(patterns),
        },
        "patterns": out_patterns,
    }


def print_text(report: dict, top: int) -> None:
    s = report["summary"]
    print(
        f"# {s['patterns']} patterns · {s['distinct_values']} distinct values "
        f"· {s['total_values']} total occurrences\n"
    )
    for p in report["patterns"][:top]:
        print(f"{p['count']:>9,}  {p['pattern']}   (distinct raw: {p['distinct_raw']})")
        for seg in p["segments"]:
            if seg["kind"] == "variable":
                ex = ", ".join(seg["samples"][:5])
                print(
                    f"            └ pos{seg['pos']} <{seg['class']}> "
                    f"{seg['distinct']} distinct — e.g. {ex}"
                )
        if p["examples"]:
            print(f"            ex: {p['examples'][0]}")
        print()


# ── CLI ───────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--file", help="Read from a .csv (col transaction_particulars) or plain text file")
    src.add_argument("--stdin", action="store_true", help="Read values from stdin, one per line")
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL"), help="Postgres DSN (default $DATABASE_URL)")
    ap.add_argument("--no-emargement", action="store_true", help="Skip the emargement table in DB mode")
    ap.add_argument("--threshold", type=int, default=25,
                    help="Max distinct values for a position to be a literal code; above it, generalise (default 25)")
    ap.add_argument("--samples", type=int, default=10, help="Sample values kept per variable position (default 10)")
    ap.add_argument("--examples", type=int, default=5, help="Raw examples kept per pattern (default 5)")
    ap.add_argument("--top", type=int, default=100, help="Patterns printed in the text report (default 100)")
    ap.add_argument("--json", help="Write the full report as JSON to this path")
    args = ap.parse_args()

    if args.stdin:
        values: Iterable[Tuple[str, int]] = from_stdin()
    elif args.file:
        values = from_file(args.file)
    else:
        if not args.dsn:
            ap.error("no source: pass --file, --stdin, --dsn or set $DATABASE_URL")
        values = from_db(args.dsn, include_emargement=not args.no_emargement)

    rows = [(split_segments(v), w) for v, w in values]
    if not rows:
        sys.exit("No transaction_particulars values found.")
    patterns = aggregate(rows, args.threshold, args.examples)

    report = build_report(patterns, args.samples)
    print_text(report, args.top)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print(f"\n→ full report written to {args.json}", file=sys.stderr)


if __name__ == "__main__":
    main()
