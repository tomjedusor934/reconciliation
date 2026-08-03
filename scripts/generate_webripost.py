#!/usr/bin/env python3
"""Generate synthetic WebriPost XLSX files for reconciliation testing.

Each file mirrors the structure of the real WebriPost export:
  TxnTypeRiposte | OperationType | GroupId | NodeId | Date | Time | User |
  TxnId | PostOfficeName | ReferenceRiposte | IdThaler | AnnulationReference |
  OperationDate | OperationAmount

Usage:
  python generate_webripost.py \\
      --count 200 \\
      --matched-ratio 0.8 \\
      --date 20260428 \\
      --output ./inbox/webripost/ \\
      --files 1

Arguments:
  --count          Total number of transaction rows to generate (default: 100)
  --matched-ratio  Fraction of rows that are balanced per ReferenceRiposte.
                   0.8 = 80% of reco_ids have a matching pair (sum=0).
                   Note: since OperationType direction is unknown, matching
                   is currently simulated by pairing positive/negative amounts.
                   (default: 0.8)
  --date           Operation date in YYYYMMDD format (default: today)
  --output         Output directory (default: ./inbox/webripost)
  --files          Number of output files (default: 1)
  --operation-type Operation type code to use (default: "02" — the known code)
  --seed           Random seed for reproducibility (default: 42)
  --prefix         File name prefix (default: WEBRIPOST_TEST)
  --group-id       GroupId to use (default: 102)
  --post-office    PostOfficeName to use (default: "Luxembourg Centre")

Output:
  One or more XLSX files named {prefix}_{date}_{N}.xlsx in the output directory.

Matching logic:
  - Matched pairs: same ReferenceRiposte, same amount but opposite sign.
    (sign convention follows the real file: positive = withdrawal by convention)
  - Unmatched: single row only.

Note on OperationType direction:
  In the real WebriPost file, all observed rows have OperationType='02'.
  The direction (debit/credit) of '02' is not yet confirmed.
  The generated file uses '02' for all rows. Once the direction is confirmed,
  update flow.parser_config.direction_map: {"02": "debit"} (or "credit").
"""
import argparse
import os
import random
import string
from datetime import datetime, time


# Transaction types observed in WebriPost
TXN_TYPES = [
    "CCPWithdrawWiiDebitThaler",
    "CCPDepositWiiCreditThaler",
    "CCPTransferWiiDebitThaler",
    "CCPTransferWiiCreditThaler",
]

POST_OFFICE_NAMES = [
    "Luxembourg Centre",
    "Esch-sur-Alzette",
    "Differdange",
    "Dudelange",
    "Pétange",
    "Ettelbruck",
    "Diekirch",
    "Wiltz",
    "Echternach",
    "Remich",
]

# Realistic amounts (EUR)
AMOUNTS = [
    50.0, 100.0, 150.0, 200.0, 300.0, 400.0, 450.0, 500.0,
    600.0, 700.0, 800.0, 900.0, 1000.0, 1200.0, 1500.0, 2000.0,
    2350.0, 2500.0, 3000.0, 5000.0,
]


def random_thaler_id(rng: random.Random) -> str:
    """Generate a random Thaler ID (16 alphanumeric chars)."""
    return "C" + "".join(rng.choices(string.ascii_uppercase + string.digits, k=15))


def random_reco_ref(date_str: str, group_id: int, node_id: int, seq: int) -> str:
    """Generate a ReferenceRiposte following observed pattern: GGGDDDDDDDDHHMMSS."""
    # Pattern from real data: 1020428046070047 → 102 (group) + 0428 (day) + 046070047 (seq)
    return f"{group_id:03d}{date_str[4:]}{seq:09d}"


def random_txn_id(group_id: int, node_id: int, seq: int, attempt: int) -> str:
    """Generate a TxnId following observed pattern: 352-102-4-11715290-2."""
    return f"352-{group_id}-{node_id}-{seq}-{attempt}"


def random_time(rng: random.Random) -> time:
    """Generate a random time within business hours (07:00 - 18:00)."""
    h = rng.randint(7, 17)
    m = rng.randint(0, 59)
    s = rng.randint(0, 59)
    return time(h, m, s)


def generate_rows(
    *,
    count: int,
    matched_ratio: float,
    date_str: str,
    operation_type: str,
    group_id: int,
    post_office: str,
    rng: random.Random,
) -> list[dict]:
    """Generate `count` transaction rows.

    Returns a list of dicts (one per row), columns matching WebriPost headers.
    """
    n_matched_records = int(count * matched_ratio)
    if n_matched_records % 2 != 0:
        n_matched_records -= 1
    n_pairs = n_matched_records // 2
    n_unmatched = count - n_matched_records

    date_int = int(date_str)
    rows = []
    seq = 10000000

    node_ids = [4, 5, 6]

    def make_row(
        *,
        txn_type: str,
        op_type: str,
        node_id: int,
        txn_seq: int,
        attempt: int,
        reco_ref: str,
        thaler_id: str,
        annulation_ref,
        op_date: int,
        amount: str,
        user,
    ) -> dict:
        return {
            "TxnTypeRiposte":     txn_type,
            "OperationType":      op_type,
            "GroupId":            group_id,
            "NodeId":             node_id,
            "Date":               date_int,
            "Time":               random_time(rng),
            "User":               user,
            "TxnId":              txn_seq,
            "PostOfficeName":     post_office,
            "ReferenceRiposte":   reco_ref,
            "IdThaler":           thaler_id,
            "AnnulationReference": annulation_ref,
            "OperationDate":      op_date,
            "OperationAmount":    amount,
        }

    # --- Matched pairs ---
    # Both rows share the same ReferenceRiposte so the reconciliation engine
    # will group them together. Amounts are equal and opposite.
    for _ in range(n_pairs):
        node_id = rng.choice(node_ids)
        seq += rng.randint(10, 500)
        reco_ref = random_reco_ref(date_str, group_id, node_id, seq)
        thaler_1 = random_thaler_id(rng)
        thaler_2 = random_thaler_id(rng)
        amount = rng.choice(AMOUNTS)
        user = rng.choice([51802, 51796, "P013783", "P012456", "P014902"])
        txn_type = rng.choice(TXN_TYPES)

        # Row 1: positive amount (debit or credit — direction TBD via OperationType)
        rows.append(make_row(
            txn_type=txn_type,
            op_type=operation_type,
            node_id=node_id,
            txn_seq=seq,
            attempt=rng.randint(1, 7),
            reco_ref=reco_ref,
            thaler_id=thaler_1,
            annulation_ref=None,
            op_date=date_int,
            amount=f"{amount:.2f}",
            user=user,
        ))

        seq += rng.randint(5, 200)
        # Row 2: negative amount (counterpart) — same reco_ref
        # In practice, sign is embedded in the amount field.
        # We use '-' prefix to signal the opposite direction.
        rows.append(make_row(
            txn_type=txn_type,
            op_type=operation_type,
            node_id=node_id,
            txn_seq=seq,
            attempt=rng.randint(1, 7),
            reco_ref=reco_ref,
            thaler_id=thaler_2,
            annulation_ref=None,
            op_date=date_int,
            amount=f"-{amount:.2f}",
            user=user,
        ))

    # --- Unmatched singles ---
    for _ in range(n_unmatched):
        node_id = rng.choice(node_ids)
        seq += rng.randint(10, 500)
        reco_ref = random_reco_ref(date_str, group_id, node_id, seq)
        thaler_id = random_thaler_id(rng)
        amount = rng.choice(AMOUNTS)
        if rng.random() < 0.5:
            amount = -amount
        user = rng.choice([51802, 51796, "P013783", "P012456", "P014902"])
        txn_type = rng.choice(TXN_TYPES)
        rows.append(make_row(
            txn_type=txn_type,
            op_type=operation_type,
            node_id=node_id,
            txn_seq=seq,
            attempt=rng.randint(1, 7),
            reco_ref=reco_ref,
            thaler_id=thaler_id,
            annulation_ref=None,
            op_date=date_int,
            amount=f"{amount:.2f}" if amount >= 0 else f"-{abs(amount):.2f}",
            user=user,
        ))

    rng.shuffle(rows)
    return rows


def write_excel(path: str, rows: list[dict]) -> None:
    """Write rows to an XLSX file with the WebriPost header row."""
    try:
        import openpyxl
    except ImportError:
        print("ERROR: openpyxl is required. Install it: pip install openpyxl")
        raise

    headers = [
        "TxnTypeRiposte", "OperationType", "GroupId", "NodeId",
        "Date", "Time", "User", "TxnId", "PostOfficeName",
        "ReferenceRiposte", "IdThaler", "AnnulationReference",
        "OperationDate", "OperationAmount",
    ]

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(headers)
    for row in rows:
        ws.append([row.get(h) for h in headers])

    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    wb.save(path)
    print(f"  ✓ Written {path} ({len(rows)} rows)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate synthetic WebriPost XLSX files for reconciliation testing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--count", type=int, default=100, help="Total rows to generate (default: 100)")
    parser.add_argument("--matched-ratio", type=float, default=0.8, dest="matched_ratio",
                        help="Fraction of rows that form balanced pairs (default: 0.8)")
    parser.add_argument("--date", type=str, default=datetime.today().strftime("%Y%m%d"),
                        help="Operation date YYYYMMDD (default: today)")
    parser.add_argument("--output", type=str, default="./inbox/webripost",
                        help="Output directory (default: ./inbox/webripost)")
    parser.add_argument("--files", type=int, default=1,
                        help="Number of output files (default: 1)")
    parser.add_argument("--operation-type", type=str, default="02", dest="operation_type",
                        help="OperationType code to use (default: '02')")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for reproducibility (default: 42)")
    parser.add_argument("--prefix", type=str, default="WEBRIPOST_TEST",
                        help="File name prefix (default: WEBRIPOST_TEST)")
    parser.add_argument("--group-id", type=int, default=102, dest="group_id",
                        help="GroupId value (default: 102)")
    parser.add_argument("--post-office", type=str, default="Luxembourg Centre", dest="post_office",
                        help="PostOfficeName value (default: 'Luxembourg Centre')")
    args = parser.parse_args()

    if not 0.0 <= args.matched_ratio <= 1.0:
        parser.error("--matched-ratio must be between 0.0 and 1.0")

    rng = random.Random(args.seed)

    print(f"\n📊 Generating WebriPost XLSX test files")
    print(f"   Count:         {args.count} rows")
    print(f"   Matched ratio: {args.matched_ratio * 100:.0f}% will balance (same ReferenceRiposte, opposite amounts)")
    print(f"   Unmatched:     {(1 - args.matched_ratio) * 100:.0f}% will NOT balance")
    print(f"   Date:          {args.date}")
    print(f"   OperationType: {args.operation_type} (direction pending confirmation)")
    print(f"   Files:         {args.files}")
    print(f"   Output:        {args.output}\n")

    all_rows = generate_rows(
        count=args.count,
        matched_ratio=args.matched_ratio,
        date_str=args.date,
        operation_type=args.operation_type,
        group_id=args.group_id,
        post_office=args.post_office,
        rng=rng,
    )

    per_file = max(1, len(all_rows) // args.files)
    for i in range(args.files):
        start = i * per_file
        end = start + per_file if i < args.files - 1 else len(all_rows)
        chunk = all_rows[start:end]
        fname = f"{args.prefix}_{args.date}_{i + 1:03d}.xlsx"
        fpath = os.path.join(args.output, fname)
        write_excel(fpath, chunk)

    matched = int(len(all_rows) * args.matched_ratio // 2 * 2)
    unmatched = len(all_rows) - matched
    print(f"\n✅ Done — {len(all_rows)} rows total")
    print(f"   ~{matched} balanced pairs (will auto-reconcile)")
    print(f"   ~{unmatched} unmatched (will remain pending)")
    print(f"\n⚠️  Note: OperationType='{args.operation_type}' direction is unknown.")
    print(f"   Once confirmed, update flow config:")
    print(f'   direction_map: {{"{args.operation_type}": "debit"}}  or "credit"')


if __name__ == "__main__":
    main()
