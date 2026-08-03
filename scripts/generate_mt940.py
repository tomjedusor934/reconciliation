#!/usr/bin/env python3
"""Generate synthetic MT940 BCEE files for reconciliation testing (IP flow).

Each file is a valid SWIFT MT940 Customer Statement with BCEE-specific :86:
sub-fields. Transactions use the SCRT reference in +21 as the reco_id.

Matching logic:
  - Matched pairs: a Debit and a Credit entry sharing the SAME SCRT reference
    and the SAME absolute amount  → stored amounts sum to 0 (−x + x = 0).
  - Unmatched: a single Debit or Credit entry with a unique SCRT reference.

Usage:
  python scripts/generate_mt940.py \\
      --count 100 \\
      --matched-ratio 0.7 \\
      --date 20260506 \\
      --output ./shared/inbox/mt940_ip/ \\
      --messages 3

Arguments:
  --count          Total number of transaction rows (default: 100)
  --matched-ratio  Fraction of rows that form balanced D/C pairs (default: 0.7)
  --date           Value date YYYYMMDD (default: today)
  --output         Output directory (default: ./shared/inbox/mt940_ip)
  --messages       Number of MT940 message blocks per file (default: 3)
  --currency       ISO 4217 currency code (default: EUR)
  --account        Account identifier in :25: (default: LU28BCEE12345678901234)
  --seed           Random seed for reproducibility (default: 42)
  --prefix         File name prefix (default: MT940_IP_TEST)
"""
import argparse
import os
import random
import string
from datetime import datetime

# Realistic EUR amounts in cents (stored as X,XX in MT940)
AMOUNTS_EUR = [
    200_00, 500_00, 1_000_00, 2_000_00, 5_000_00, 10_000_00,
    20_000_00, 50_000_00, 100_000_00, 200_000_00, 500_000_00,
    1_000_000_00, 2_000_000_00,
]

BANK_CODES = ["835", "836", "837", "838"]
NARRATIVE_TEMPLATES = [
    "Virement reçu", "Paiement sortant", "Virement SEPA",
    "Payment IP", "Transfer", "inter comptes",
]
CREDITOR_NAMES = [
    "WEBER JEAN-LUC", "KAUFMANN MARC", "LEKO LABS SA",
    "TRESORERIE DE L'ETAT", "DR PAGE G3 GARDES", "DOMAINE ALICE HARTMANN S.A.",
]
CREDITOR_ADDRS = [
    "L-1475 LUXEMBOURG", "L-8213 MAMER", "", "L-2241 LUXEMBOURG",
]
COUNTERPARTY_BICS = ["BCEELULL", "BGLLLULL", "CCPLLULL", "REVOLT21", "INGBNL2A"]


def random_scrt(rng: random.Random) -> str:
    """Generate a SCRT reference: SCRT + 10-digit number."""
    return "SCRT" + str(rng.randint(1_000_000_000, 9_999_999_999))


def random_ref(rng: random.Random, length: int = 16) -> str:
    """Generate a random alphanumeric reference."""
    return "".join(rng.choices(string.ascii_uppercase + string.digits, k=length))


def format_amount_mt940(cents: int) -> str:
    """Format integer cents as MT940 amount string: '12345,67'."""
    euros = cents // 100
    centimes = cents % 100
    return f"{euros},{centimes:02d}"


def build_transaction(
    *,
    val_date_yy: str,      # YYMMDD
    entry_date_mmdd: str,  # MMDD
    dc: str,               # 'D' or 'C'
    amount_cents: int,
    scrt: str,
    bank_ref: str,
    narrative: str,
    creditor_name: str,
    creditor_addr: str,
    counterparty_bic: str,
    bank_code: str,
    rng: random.Random,
) -> str:
    """Return the :61: + :86: block for one transaction."""
    amt = format_amount_mt940(amount_cents)
    acct_ref = random_ref(rng, 16)
    continuation_ref = random_ref(rng, 15)
    
    # :61: line — some have continuation line, some don't
    tag61 = f":61:{val_date_yy}{entry_date_mmdd}{dc}{amt}NMSC{acct_ref}  //CCI-SIR\n{continuation_ref}"
    
    # :86: BCEE structured sub-fields
    narrative_short = narrative[:20]
    tag86 = (
        f":86:{bank_code}"
        f"+00{continuation_ref}"
        f"+{random_ref(rng, 8)}"
        f"+20{narrative_short}"
        f"+21{scrt}"
        f"+22\n"
        f"+23+24\n"
        f"   +25+26/OCMT/{counterparty_bic[:3]}{amt}/BCEELULL\n"
        f"+27/CHGS/EUR0,/+28/EXCH/1,/\n"
        f"+32{creditor_name}\n"
        f"+33{creditor_addr}"
    )
    return tag61 + "\n" + tag86


def build_message(
    *,
    statement_ref: str,
    account: str,
    statement_seq: str,
    opening_balance_tag: str,
    closing_balance_tag: str,
    transactions: list[str],
    session_ref: str,
) -> str:
    """Wrap transactions in a full MT940 message block."""
    body = "\n".join(transactions)
    return (
        f"{{1:F01BCEELULLAXXX0000000000}}"
        f"{{2:I940CCPLLULLXXXXN}}"
        f"{{3:{{108:{session_ref}}}}}"
        f"{{4:\n"
        f":20:{statement_ref}\n"
        f":25:{account}\n"
        f":28C:{statement_seq}\n"
        f"{opening_balance_tag}\n"
        f"{body}\n"
        f"{closing_balance_tag}\n"
        f"-}}"
    )


def generate_transactions(
    *,
    count: int,
    matched_ratio: float,
    date_yymmdd: str,
    date_mmdd: str,
    currency: str,
    rng: random.Random,
) -> list[str]:
    """Generate `count` transaction blocks, matched_ratio fraction as D/C pairs."""
    n_matched = int(count * matched_ratio)
    if n_matched % 2 != 0:
        n_matched -= 1
    n_pairs = n_matched // 2
    n_unmatched = count - n_matched

    txns = []

    # Matched pairs: Debit + Credit with same SCRT
    for _ in range(n_pairs):
        scrt = random_scrt(rng)
        amount = rng.choice(AMOUNTS_EUR)
        narrative = rng.choice(NARRATIVE_TEMPLATES)
        creditor = rng.choice(CREDITOR_NAMES)
        addr = rng.choice(CREDITOR_ADDRS)
        bic = rng.choice(COUNTERPARTY_BICS)
        bank_code = rng.choice(BANK_CODES)

        # Debit (money leaving the account, negative in our DB)
        txns.append(build_transaction(
            val_date_yy=date_yymmdd,
            entry_date_mmdd=date_mmdd,
            dc="D",
            amount_cents=amount,
            scrt=scrt,
            bank_ref=random_ref(rng, 15),
            narrative=narrative,
            creditor_name=creditor,
            creditor_addr=addr,
            counterparty_bic=bic,
            bank_code=bank_code,
            rng=rng,
        ))
        # Credit (money entering the account, positive in our DB) — same SCRT
        txns.append(build_transaction(
            val_date_yy=date_yymmdd,
            entry_date_mmdd=date_mmdd,
            dc="C",
            amount_cents=amount,
            scrt=scrt,
            bank_ref=random_ref(rng, 15),
            narrative=narrative,
            creditor_name=creditor,
            creditor_addr=addr,
            counterparty_bic=bic,
            bank_code=bank_code,
            rng=rng,
        ))

    # Unmatched singles (either D or C, unique SCRT)
    for _ in range(n_unmatched):
        scrt = random_scrt(rng)
        amount = rng.choice(AMOUNTS_EUR)
        dc = rng.choice(["D", "C"])
        txns.append(build_transaction(
            val_date_yy=date_yymmdd,
            entry_date_mmdd=date_mmdd,
            dc=dc,
            amount_cents=amount,
            scrt=scrt,
            bank_ref=random_ref(rng, 15),
            narrative=rng.choice(NARRATIVE_TEMPLATES),
            creditor_name=rng.choice(CREDITOR_NAMES),
            creditor_addr=rng.choice(CREDITOR_ADDRS),
            counterparty_bic=rng.choice(COUNTERPARTY_BICS),
            bank_code=rng.choice(BANK_CODES),
            rng=rng,
        ))

    rng.shuffle(txns)
    return txns


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate synthetic MT940 BCEE files for reconciliation testing (IP flow).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--count", type=int, default=100, help="Total transactions (default: 100)")
    parser.add_argument("--matched-ratio", type=float, default=0.7, dest="matched_ratio",
                        help="Fraction of transactions forming balanced D/C pairs (default: 0.7)")
    parser.add_argument("--date", type=str, default=datetime.today().strftime("%Y%m%d"),
                        help="Value date YYYYMMDD (default: today)")
    parser.add_argument("--output", type=str, default="./shared/inbox/mt940_ip",
                        help="Output directory (default: ./shared/inbox/mt940_ip)")
    parser.add_argument("--messages", type=int, default=3,
                        help="Number of MT940 message blocks per file (default: 3)")
    parser.add_argument("--currency", type=str, default="EUR",
                        help="ISO 4217 currency code (default: EUR)")
    parser.add_argument("--account", type=str, default="LU28BCEE12345678901234",
                        help="Account identifier for :25: tag (default: LU28BCEE12345678901234)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for reproducibility (default: 42)")
    parser.add_argument("--prefix", type=str, default="MT940_IP_TEST",
                        help="File name prefix (default: MT940_IP_TEST)")
    args = parser.parse_args()

    if not 0.0 <= args.matched_ratio <= 1.0:
        parser.error("--matched-ratio must be between 0.0 and 1.0")

    rng = random.Random(args.seed)
    date_str = args.date  # YYYYMMDD
    date_yymmdd = date_str[2:]   # YYMMDD
    date_mmdd = date_str[4:]     # MMDD

    print(f"\n📄 Generating MT940 BCEE test files")
    print(f"   Count:         {args.count} transactions")
    print(f"   Matched ratio: {args.matched_ratio * 100:.0f}% balanced D/C pairs")
    print(f"   Unmatched:     {(1 - args.matched_ratio) * 100:.0f}% will remain pending")
    print(f"   Date:          {date_str}")
    print(f"   Messages/file: {args.messages}")
    print(f"   Account:       {args.account}")
    print(f"   Output:        {args.output}\n")

    all_txns = generate_transactions(
        count=args.count,
        matched_ratio=args.matched_ratio,
        date_yymmdd=date_yymmdd,
        date_mmdd=date_mmdd,
        currency=args.currency,
        rng=rng,
    )

    # Split transactions across messages
    per_msg = max(1, len(all_txns) // args.messages)
    messages_content = []
    for i in range(args.messages):
        start = i * per_msg
        end = start + per_msg if i < args.messages - 1 else len(all_txns)
        chunk = all_txns[start:end]
        if not chunk:
            continue

        session_ref = f"ELS{date_str}{rng.randint(1000, 9999):04d}"
        stmt_ref = f"{rng.randint(1000, 9999)}EXT{rng.randint(1000, 9999):04d}"
        stmt_seq = f"{rng.randint(1, 999):05d}/{i + 1:05d}"
        # Anonymized balance values (same as real file format)
        open_bal = f":60{'F' if i == 0 else 'M'}:{rng.choice('DC')}{date_yymmdd}{args.currency}{rng.randint(100000, 9999999999):012d},"
        close_bal = f":62{'F' if i == args.messages - 1 else 'M'}:{rng.choice('DC')}{date_yymmdd}{args.currency}{rng.randint(100000, 9999999999):012d},"

        messages_content.append(build_message(
            statement_ref=stmt_ref,
            account=args.account,
            statement_seq=stmt_seq,
            opening_balance_tag=open_bal,
            closing_balance_tag=close_bal,
            transactions=chunk,
            session_ref=session_ref,
        ))

    os.makedirs(args.output, exist_ok=True)
    fname = f"{args.prefix}_{date_str}.txt"
    fpath = os.path.join(args.output, fname)
    with open(fpath, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(messages_content) + "\n")
    print(f"  ✓ Written {fpath} ({len(all_txns)} transactions in {len(messages_content)} messages)")

    n_matched = int(len(all_txns) * args.matched_ratio // 2 * 2)
    n_unmatched = len(all_txns) - n_matched
    print(f"\n✅ Done — {len(all_txns)} transactions total")
    print(f"   ~{n_matched} balanced pairs (will auto-reconcile, sum=0 per SCRT ref)")
    print(f"   ~{n_unmatched} unmatched (will remain PENDING after reconciliation)")


if __name__ == "__main__":
    main()
