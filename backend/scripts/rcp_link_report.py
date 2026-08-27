#!/usr/bin/env python3
"""Offline control of the RCP link extracts (part 1) — no database, no datamart.

Replays exactly what the app's ``/rcp-reattribution/analyze`` computes as its
control section: for every RCP movement of the link workbook, its return/reject
rows must be ``num_0f_records`` of them and add up to ``settlementamt``. Use it
to qualify an extract before uploading it — a dump whose ``msgid`` column is
empty (2026-08-12 ``reject_o``) shows up immediately in the per-file report.

Usage:
    python scripts/rcp_link_report.py SP_LINK_REPORT.xlsx dump1.csv dump2.csv ...
    python scripts/rcp_link_report.py ... --csv control.csv   # full result as CSV
    python scripts/rcp_link_report.py ... --all               # list OK rows too

Exit code is 1 when at least one movement fails the control, so it can gate a
pipeline.
"""
import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.rcp_link_parser import (  # noqa: E402
    CTRL_NOT_SETTLED,
    REATTRIBUTABLE_SERVICES,
    build_dump_index,
    control_all,
    parse_dump_file,
    parse_link_workbook,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("link_file", help="SP_LINK_REPORT .xlsx")
    parser.add_argument("dump_files", nargs="+", help="return/reject dumps (.csv/.txt)")
    parser.add_argument("--csv", dest="csv_out", help="write the full control table here")
    parser.add_argument("--all", action="store_true", help="print the OK movements too")
    parser.add_argument(
        "--services",
        help="serviceid à traiter, séparés par des virgules "
             f"(défaut: {','.join(sorted(REATTRIBUTABLE_SERVICES))})",
    )
    args = parser.parse_args()

    services = args.services.split(",") if args.services else None
    link_path = Path(args.link_file)
    movements, link_report = parse_link_workbook(
        link_path.read_bytes(), link_path.name, services
    )
    breakdown = ", ".join(
        f"{service}={count}" for service, count in sorted(link_report.services.items())
    )
    print(f"{link_report.file_name}: {link_report.rows} lignes, {len(movements)} à traiter"
          + (f" [{breakdown}]" if breakdown else "")
          + (f" — ERREUR: {link_report.error}" if link_report.error else ""))

    rows = []
    for name in args.dump_files:
        path = Path(name)
        parsed, report = parse_dump_file(path.read_bytes(), path.name)
        rows.extend(parsed)
        note = f" — ERREUR: {report.error}" if report.error else ""
        print(
            f"{report.file_name}: {report.rows} lignes, {report.rows_with_msgid} avec msgid, "
            f"{report.distinct_msgids} msgid distincts, montant={report.amount_column or '?'}{note}"
        )

    index, dump_duplicates = build_dump_index(rows)
    if dump_duplicates:
        print(
            f"{len(rows)} lignes lues, {dump_duplicates} en double entre fichiers "
            f"ignorées, {len(rows) - dump_duplicates} retenues"
        )
    controls, duplicates = control_all(movements, index)
    failures = [c for c in controls if not c.ok]

    print()
    breakdown = Counter(c.status for c in failures)
    print(
        f"CONTROLE: {len(controls) - len(failures)} OK / {len(failures)} en écart"
        + (f" [{', '.join(f'{k}={v}' for k, v in sorted(breakdown.items()))}]"
           if breakdown else "")
    )
    if breakdown.get(CTRL_NOT_SETTLED):
        # Not a gap: the batch is listed before Finacle prices it, and the next
        # extract carries the amount. Committing it now would be refused anyway.
        print(
            f"  dont {breakdown[CTRL_NOT_SETTLED]} lot(s) pas encore settlé(s) "
            "(montant 0.00 au classeur) — à reprendre sur un extrait plus récent"
        )
    if duplicates:
        print(f"msgid présents plusieurs fois dans le xlsx: {', '.join(duplicates)}")
    for control in controls:
        if control.ok and not args.all:
            continue
        print(
            f"  {control.msgid:<24} {control.service_id:<4} {control.status:<18} "
            f"{control.found_count}/{control.expected_count} lignes  "
            f"{control.found_amount} / {control.expected_amount} "
            f"(delta {control.delta_amount})  {', '.join(control.files) or '-'}"
        )

    if args.csv_out:
        with open(args.csv_out, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, delimiter=";")
            writer.writerow([
                "msgid", "status", "serviceid", "direction", "tran_id", "sp_date",
                "expected_count", "found_count", "delta_count",
                "expected_amount", "found_amount", "delta_amount", "files",
            ])
            for control in controls:
                writer.writerow([
                    control.msgid, control.status, control.service_id,
                    control.direction, control.tran_id,
                    control.sp_date, control.expected_count, control.found_count,
                    control.delta_count, control.expected_amount, control.found_amount,
                    control.delta_amount, "|".join(control.files),
                ])
        print(f"\nCSV écrit: {args.csv_out}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
