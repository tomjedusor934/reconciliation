#!/usr/bin/env python3
"""Generate synthetic ATM MOSEL flat files for reconciliation testing.

Each file contains header (HEDTRA=0), data records (HEDTRA=1), and footer (HEDTRA=9).
Data records follow the ATM MOSEL format:
  HEDTRA(1) + REFCTR(30) + TMSEXT(30) + TYPEVT(8) + REFEXN(12) + DATOPN(8) + DEVISE(3) + AMOUNT(var)

Usage:
  python generate_atm.py \\
      --count 200 \\
      --matched-ratio 0.8 \\
      --date 20260428 \\
      --output ./inbox/atm/ \\
      --files 2

Arguments:
  --count          Total number of data records to generate (default: 100)
  --matched-ratio  Fraction of records that will be balanced (sum=0 per reco_id).
                   E.g. 0.8 = 80% of reco_ids will have matching pairs; 0.2 = 20% unmatched.
                   (default: 0.8)
  --date           Operation date in YYYYMMDD format (default: today)
  --output         Output directory (default: ./inbox/atm)
  --files          Number of files to generate (default: 1; records split evenly)
  --currency       Currency code (default: EUR)
  --seed           Random seed for reproducibility (default: 42)
  --prefix         File name prefix (default: ATM_TEST)

Output:
  One or more files named {prefix}_{date}_{N}.txt in the output directory.

Matching logic:
  - Each matched reco_id gets exactly 2 records: one positive (credit/deposit) and one
    negative (debit/withdrawal) of the same amount → sum = 0.
  - Unmatched reco_ids have only 1 record (positive or negative, randomly).
  - All reco_ids are unique 12-char strings.
"""
import argparse
import os
import random
import string
from datetime import datetime


# ATM event types per direction
WITHDRAWAL_EVENTS = ["ARACCMVT", "ARCLHSAN", "ARCLHSBK", "ARCLHSEU", "ARCLHSIN", "ARCLHSON", "AREXCMVT", "ARTRNLOA"]
DEPOSIT_EVENTS = ["SLFRECDP", "SLFRECRT", "SLFRECVP"]

# Realistic amounts (EUR)
AMOUNTS = [
    50, 100, 150, 200, 300, 400, 450, 500, 600, 700, 800, 900,
    1000, 1200, 1500, 2000, 2500, 3000, 5000,
]


def random_id(length: int = 12, rng: random.Random = None) -> str:
    """Generate a random alphanumeric ID of given length."""
    chars = string.ascii_uppercase + string.digits
    r = rng or random
    return "".join(r.choice(chars) for _ in range(length))


def random_contract_ref(rng: random.Random) -> str:
    """Generate a plausible REFCTR (30 chars, padded)."""
    ref = f"352-{rng.randint(100,999)}-{rng.randint(1,9)}-{rng.randint(10000000,99999999)}-{rng.randint(1,9)}"
    return ref[:30].ljust(30)


def format_amount(amount: float) -> str:
    """Format amount as signed string with comma decimal: '-90,00' or '890,00'."""
    if amount < 0:
        return f"-{abs(amount):.2f}".replace(".", ",")
    return f"{amount:.2f}".replace(".", ",")


def build_record(
    *,
    refctr: str,
    tmsext: str,
    typevt: str,
    refexn: str,
    datopn: str,
    devise: str,
    amount: float,
) -> str:
    """Build a single MOSEL data record (HEDTRA=1)."""
    hedtra = "1"
    refctr_f = refctr[:30].ljust(30)
    tmsext_f = tmsext[:30].ljust(30)
    typevt_f = typevt[:8].ljust(8)
    refexn_f = refexn[:12].ljust(12)
    datopn_f = datopn[:8]
    devise_f = devise[:3]
    amount_f = format_amount(amount)

    return f"{hedtra}{refctr_f}{tmsext_f}{typevt_f}{refexn_f}{datopn_f}{devise_f}{amount_f}"


def generate_records(
    *,
    count: int,
    matched_ratio: float,
    date_str: str,
    currency: str,
    rng: random.Random,
) -> list[str]:
    """Generate `count` data records with the requested matched/unmatched split.

    Returns a list of raw record strings (without newlines).
    """
    # Split target: how many reco_ids will have matched pairs vs. unmatched singles
    # A matched pair uses 2 records, an unmatched uses 1.
    # matched_ratio * count = records used for matched pairs → N_pairs * 2
    # (1 - matched_ratio) * count = records used for unmatched singles

    n_matched_records = int(count * matched_ratio)
    if n_matched_records % 2 != 0:
        n_matched_records -= 1  # ensure even
    n_pairs = n_matched_records // 2
    n_unmatched = count - n_matched_records

    records = []
    tmsext_base = f"{date_str}00000000"  # YYYYMMDD + time padding

    # --- Matched pairs (deposit + withdrawal with same reco_id) ---
    for _ in range(n_pairs):
        reco_id = random_id(12, rng)
        amount = float(rng.choice(AMOUNTS))
        refctr = random_contract_ref(rng)

        # Deposit record (positive)
        dep_event = rng.choice(DEPOSIT_EVENTS)
        records.append(build_record(
            refctr=refctr,
            tmsext=tmsext_base,
            typevt=dep_event,
            refexn=reco_id,
            datopn=date_str,
            devise=currency,
            amount=amount,
        ))

        # Withdrawal record (negative, same reco_id)
        wit_event = rng.choice(WITHDRAWAL_EVENTS)
        records.append(build_record(
            refctr=refctr,
            tmsext=tmsext_base,
            typevt=wit_event,
            refexn=reco_id,
            datopn=date_str,
            devise=currency,
            amount=-amount,
        ))

    # --- Unmatched singles (only one side) ---
    for _ in range(n_unmatched):
        reco_id = random_id(12, rng)
        amount = float(rng.choice(AMOUNTS))
        refctr = random_contract_ref(rng)
        # Randomly choose deposit or withdrawal
        if rng.random() < 0.5:
            event = rng.choice(DEPOSIT_EVENTS)
        else:
            event = rng.choice(WITHDRAWAL_EVENTS)
            amount = -amount
        records.append(build_record(
            refctr=refctr,
            tmsext=tmsext_base,
            typevt=event,
            refexn=reco_id,
            datopn=date_str,
            devise=currency,
            amount=amount,
        ))

    rng.shuffle(records)
    return records


def write_file(
    path: str,
    records: list[str],
    date_str: str,
    file_index: int,
    total_files: int,
) -> None:
    """Write header + records + footer to file."""
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("0\n")
        for rec in records:
            fh.write(rec + "\n")
        fh.write("9\n")
    print(f"  ✓ Written {path} ({len(records)} records)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate synthetic ATM MOSEL flat files for reconciliation testing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:")[0].strip(),
    )
    parser.add_argument("--count", type=int, default=100, help="Total records to generate (default: 100)")
    parser.add_argument("--matched-ratio", type=float, default=0.8, dest="matched_ratio",
                        help="Fraction of records that have a matching counterpart (default: 0.8)")
    parser.add_argument("--date", type=str, default=datetime.today().strftime("%Y%m%d"),
                        help="Operation date YYYYMMDD (default: today)")
    parser.add_argument("--output", type=str, default="./inbox/atm",
                        help="Output directory (default: ./inbox/atm)")
    parser.add_argument("--files", type=int, default=1,
                        help="Number of output files (default: 1)")
    parser.add_argument("--currency", type=str, default="EUR",
                        help="Currency code ISO 4217 (default: EUR)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for reproducibility (default: 42)")
    parser.add_argument("--prefix", type=str, default="ATM_TEST",
                        help="File name prefix (default: ATM_TEST)")
    args = parser.parse_args()

    if not 0.0 <= args.matched_ratio <= 1.0:
        parser.error("--matched-ratio must be between 0.0 and 1.0")

    rng = random.Random(args.seed)

    print(f"\n📄 Generating ATM MOSEL test files")
    print(f"   Count:         {args.count} records")
    print(f"   Matched ratio: {args.matched_ratio * 100:.0f}% will balance (sum=0)")
    print(f"   Unmatched:     {(1 - args.matched_ratio) * 100:.0f}% will NOT balance")
    print(f"   Date:          {args.date}")
    print(f"   Files:         {args.files}")
    print(f"   Output:        {args.output}\n")

    all_records = generate_records(
        count=args.count,
        matched_ratio=args.matched_ratio,
        date_str=args.date,
        currency=args.currency,
        rng=rng,
    )

    # Split records across files
    per_file = max(1, len(all_records) // args.files)
    for i in range(args.files):
        start = i * per_file
        end = start + per_file if i < args.files - 1 else len(all_records)
        chunk = all_records[start:end]
        fname = f"{args.prefix}_{args.date}_{i + 1:03d}.txt"
        fpath = os.path.join(args.output, fname)
        write_file(fpath, chunk, args.date, i + 1, args.files)

    matched = int(len(all_records) * args.matched_ratio // 2 * 2)
    unmatched = len(all_records) - matched
    print(f"\n✅ Done — {len(all_records)} records total")
    print(f"   ~{matched} balanced (will auto-reconcile if sum=0 per reco_id)")
    print(f"   ~{unmatched} unmatched (will remain pending after reconciliation run)")


if __name__ == "__main__":
    main()
