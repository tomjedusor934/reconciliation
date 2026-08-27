"""Unit tests for the claim-group split (pure functions, no I/O).

The invariant under test changed with claim groups:

    every ghost == its bucket's exact payment sum, emitted ONCE per claim group

N movements sharing an aggregate key must never duplicate a bucket's ghost N
times (160 LUXEMBOURG lots each carried 6 near-identical prorated ghosts), and
no largest-remainder cent may ever land in a lot (~330 lots pending at ±0,01).
Σ ghosts is NOT expected to equal Σ booked any more — that delta is the second
reconciliation's job (movement_lot.parent_mismatch), asserted in the service
tests.
"""
import random
import sys
from decimal import Decimal
from pathlib import Path

import pytest

DAGS_DIR = Path(__file__).resolve().parents[2] / "shared" / "dags"
if not DAGS_DIR.is_dir():
    pytest.skip(
        "shared/dags not mounted in this environment (backend-only container)",
        allow_module_level=True,
    )
sys.path.insert(0, str(DAGS_DIR))

from reco_datamart_bb import (  # noqa: E402
    BUCKET_PAIR,
    KEY_MSGID,
    KEY_PACS008,
    UNRESOLVED_RECO_ID,
    BucketKey,
    MovementResolution,
    PaymentGroup,
    PaymentRef,
    aggregate_key,
    bucket_id,
    casing_conflicts,
    count_aggregate_keys,
    group_ghost_ref,
    group_slices,
    plan_claim_group,
    plan_movement,
    split_push_batches,
)

D = Decimal
FSID = 3


def entry(amount="-1000.00", direction="debit", external_ref="PF0051006#2",
          value_date="2026-07-06T00:00:00"):
    """Entry dict in the exact movement_row_to_entry shape."""
    return {
        "reco_id": None,
        "account": "0010130015001",
        "currency": "EUR",
        "amount": amount,
        "value_date": value_date,
        "operation_date": value_date,
        "direction": direction,
        "event_type": "TR",
        "external_ref": external_ref,
        "transaction_particulars": "SCTXB/O/x",
        "ref_no": None,
        "remarks_1": "PACS1",
        "payload_raw": {},
    }


def groups_of(*pairs):
    """(msgid, amount, n_payments) → the partition the group planner consumes."""
    return {
        BucketKey(BUCKET_PAIR, pacs="PACS1", msgid=msgid): PaymentGroup(
            D(amount), [f"{msgid}-po{i}" for i in range(n)]
        )
        for msgid, amount, n in pairs
    }


def resolution_of(*pairs, claim=None):
    return MovementResolution(
        tuple(
            PaymentRef("PACS1", msgid, f"{msgid}-po{i}", D(amount) / n)
            for msgid, amount, n in pairs
            for i in range(n)
        ),
        BucketKey("PACS_ONLY", pacs="PACS1"),
        claim or (KEY_PACS008, "PACS1"),
    )


def plan_group(claim, partition, parents):
    """plan_claim_group with parents pre-ordered like _ingest_bb_source does."""
    ordered = sorted(
        parents, key=lambda p: (str(p.get("value_date"))[:10], str(p.get("external_ref")))
    )
    return plan_claim_group(claim, partition, ordered, flow_source_id=FSID)


def split_parent(res, **entry_overrides):
    """Run plan_movement on a splitting movement and return its parent payload."""
    plan = plan_movement(entry(**entry_overrides), "NDGB", res, flow_source_id=FSID)
    assert plan.parent is not None
    return plan


# ---------------------------------------------------------------------------
# group_slices — exact payment sums, nothing else
# ---------------------------------------------------------------------------

def test_every_slice_is_its_buckets_exact_payment_sum():
    """THE point of the redesign: both sides of a bucket price their ghosts from
    the same std.Payment rows, so they cancel to the cent. Prorating instead
    left ~330 lots stuck at ±0,01 (largest-remainder cents)."""
    payments = [D("100.00"), D("250.33"), D("37.10"), D("1000.00"), D("12.57")]
    partition = groups_of(*((f"PO{i}", str(s), 1) for i, s in enumerate(payments)))
    slices = group_slices(partition, sign=D(-1))
    assert sorted(abs(a) for _k, a, _g in slices) == sorted(payments)  # 5/5 exact
    assert all(a < 0 for _k, a, _g in slices)


def test_zero_sum_buckets_are_never_emitted():
    """A 0,00 ghost settles nothing, and a bucket holding only zeros nets to
    zero and 'matches' on thin air. Prod carried 2 217 of them."""
    slices = group_slices(
        groups_of(("A", "1000", 1), ("B", "0", 1), ("C", "0", 2)), sign=D(1)
    )
    assert [abs(a) for _k, a, _g in slices] == [D("1000")]


def test_slices_are_ordered_deterministically():
    a = group_slices(groups_of(("A", "400", 1), ("B", "600", 1)), sign=D(-1))
    b = group_slices(groups_of(("B", "600", 1), ("A", "400", 1)), sign=D(-1))
    assert [k.label() for k, _a, _g in a] == [k.label() for k, _a, _g in b]
    assert [str(x) for _k, x, _g in a] == [str(x) for _k, x, _g in b]


@pytest.mark.parametrize("seed", range(25))
def test_slices_always_equal_their_payment_sums(seed):
    """No arithmetic can drift: a slice IS a sum read off std.Payment, signed."""
    rng = random.Random(seed)
    partition = groups_of(
        *(
            (f"MSG{i}", str(D(rng.randint(0, 400_000)) / 100), rng.randint(0, 5))
            for i in range(rng.randint(1, 8))
        )
    )
    sign = D(rng.choice([-1, 1]))
    slices = group_slices(partition, sign=sign)
    expected = sorted(abs(g.amount) for g in partition.values() if g.amount)
    assert sorted(abs(a) for _k, a, _g in slices) == expected
    assert all((a < 0) == (sign < 0) for _k, a, _g in slices)


# ---------------------------------------------------------------------------
# plan_movement — the three shapes
# ---------------------------------------------------------------------------

def test_unsupported_movement_keeps_the_sentinel():
    plan = plan_movement(entry(), "?", None, flow_source_id=FSID)
    assert plan.entries[0]["reco_id"] == UNRESOLVED_RECO_ID
    assert plan.members == [] and plan.parent is None


def test_transient_movement_keeps_a_null_reco_id():
    """Nothing resolved and no fallback → retried through /tasks/finacle/unresolved."""
    plan = plan_movement(
        entry(), "SCTXB", MovementResolution((), None), flow_source_id=FSID
    )
    assert plan.entries[0]["reco_id"] is None
    assert plan.members == [] and plan.buckets == {}


def test_single_bucket_movement_is_not_split():
    res = resolution_of(("A", "1000", 2))
    plan = plan_movement(entry(), "NDGB", res, flow_source_id=FSID)
    assert plan.parent is None
    assert len(plan.entries) == 1 and len(plan.members) == 1
    # Amount and identity untouched — it is still the real movement.
    assert plan.entries[0]["amount"] == "-1000.00"
    assert plan.entries[0]["external_ref"] == "PF0051006#2"
    assert plan.members[0]["claim_key_value"] is None


def test_one_nonzero_bucket_keeps_the_movement_whole():
    """B and C weigh nothing: splitting would withdraw the movement and stand in
    for it with a single ghost. It goes whole into A instead."""
    res = resolution_of(("A", "1000", 1), ("B", "0", 1), ("C", "0", 2))
    plan = plan_movement(entry(), "SCTXB", res, flow_source_id=FSID)
    assert plan.parent is None
    assert plan.entries[0]["amount"] == "-1000.00"
    assert len(plan.buckets) == 1


def test_multi_bucket_movement_registers_as_a_split_parent_only():
    """The movement contributes NO entry and NO member of its own: the group
    emits the ghosts, once, whatever the number of claiming movements."""
    res = resolution_of(("A", "700", 2), ("B", "300", 1))
    plan = plan_movement(entry(), "SCTXB", res, flow_source_id=FSID)

    assert plan.entries == [] and plan.members == [] and plan.buckets == {}
    assert plan.claim == (KEY_PACS008, "PACS1")
    assert plan.partition is not None and len(plan.partition) == 2
    parent = plan.parent
    assert parent["external_ref"] == "PF0051006#2"
    assert parent["amount"] == "-1000.00"
    assert parent["payment_count"] == 3
    assert D(parent["payment_amount"]) == D("-1000.00")
    assert "children" not in parent
    assert "payment_trusted" not in parent


def test_the_claim_is_uppercased_like_every_identity():
    res = resolution_of(("A", "700", 1), ("B", "300", 1), claim=("MSGID", "ptel-x"))
    plan = plan_movement(entry(), "NDGB", res, flow_source_id=FSID)
    assert plan.claim == ("MSGID", "ptel-x")  # resolution already folded it…

    from reco_datamart_bb import _claim
    assert _claim("MSGID", "ptel-x") == ("MSGID", "PTEL-X")  # …at construction


# ---------------------------------------------------------------------------
# plan_claim_group — one ghost set per group
# ---------------------------------------------------------------------------

def test_a_single_parent_group_prices_its_ghosts_at_the_payment_sums():
    partition = groups_of(("A", "700.00", 2), ("B", "290.00", 1))  # payments: 990
    plan = split_parent(resolution_of(("A", "700.00", 2), ("B", "290.00", 1)))
    group, members, buckets = plan_group(plan.claim, partition, [plan.parent])

    children = {c["bucket_msgid"]: D(c["amount"]) for c in group["children"]}
    assert children == {"A": D("-700.00"), "B": D("-290.00")}
    # The 10€ of charges is NOT materialised anywhere: it is the group delta the
    # backend tags lots for. No RESIDUAL bucket exists any more.
    assert sum(D(c["amount"]) for c in group["children"]) == D("-990.00")
    assert {b.kind for b in buckets.values()} == {BUCKET_PAIR}
    assert len(members) == 2
    assert plan.parent["shared_key_movements"] == 1


def test_the_prod_case_184_parents_one_single_ghost_set():
    """184 NDGB movements share one MessageID and each resolves the WHOLE
    payment group. The old per-parent emission duplicated every bucket 184×;
    the group emits its ghosts ONCE, priced at the payment sums."""
    partition = groups_of(("PTEL", "1817225.60", 3), ("AUTRE", "200000.00", 1))
    res = resolution_of(("PTEL", "1817225.60", 3), ("AUTRE", "200000.00", 1),
                        claim=("MSGID", "PTEL-003842-SDD20260721R02"))
    parents = [
        split_parent(res, external_ref=f"PF{i:07d}#2", amount="-10963.19").parent
        for i in range(184)
    ]
    group, members, _buckets = plan_group(("MSGID", "PTEL-003842-SDD20260721R02"),
                                          partition, parents)

    assert len(group["children"]) == 2          # one per bucket — not 368
    assert len(members) == 2
    amounts = sorted(D(c["amount"]) for c in group["children"])
    assert amounts == [D("-1817225.60"), D("-200000.00")]
    # Every parent now knows the true group size.
    assert all(p["shared_key_movements"] == 184 for p in parents)
    # Σ parents ≈ Σ children: the delta is what the backend will measure.
    parent_total = sum(D(p["amount"]) for p in parents)
    child_total = sum(D(c["amount"]) for c in group["children"])
    assert parent_total == D("-10963.19") * 184
    assert child_total == D("-2017225.60")


def test_ghost_identity_is_a_pure_function_of_claim_and_bucket():
    """No parent reference and no date: a later run re-emitting the group — off
    different parents — must upsert the very same ghost rows."""
    partition = groups_of(("A", "700", 1), ("B", "300", 1))
    claim = ("MSGID", "PTEL-X")
    p1 = split_parent(resolution_of(("A", "700", 1), ("B", "300", 1)),
                      external_ref="RUN1").parent
    p2 = split_parent(resolution_of(("A", "700", 1), ("B", "300", 1)),
                      external_ref="RUN2-OTHER").parent
    g1, _m1, _b1 = plan_group(claim, partition, [p1])
    g2, _m2, _b2 = plan_group(claim, partition, [p2])

    refs1 = sorted(c["external_ref"] for c in g1["children"])
    refs2 = sorted(c["external_ref"] for c in g2["children"])
    assert refs1 == refs2                      # parent-independent
    assert len(set(refs1)) == 2                # bucket-specific
    assert all(r.startswith("KEY:PTEL-X~") for r in refs1)
    assert all(len(r) <= 128 for r in refs1)


def test_group_ghost_ref_is_bounded_and_distinct():
    long_claim = ("MSGID", "X" * 300)
    key_a = BucketKey(BUCKET_PAIR, pacs="P", msgid="M")
    key_b = BucketKey(BUCKET_PAIR, pacs="P", msgid="N")
    assert len(group_ghost_ref(long_claim, key_a)) <= 128
    assert group_ghost_ref(long_claim, key_a) != group_ghost_ref(long_claim, key_b)
    # Two long claims sharing a prefix stay distinct through the digest.
    other = ("MSGID", "X" * 299 + "Y")
    assert group_ghost_ref(long_claim, key_a) != group_ghost_ref(other, key_a)


def test_ghost_sign_follows_the_groups_booked_total():
    """std.Payment stores amounts unsigned; whether the group settles or
    receives is how finacle booked its movements."""
    partition = groups_of(("A", "700", 1), ("B", "300", 1))
    debit = split_parent(resolution_of(("A", "700", 1), ("B", "300", 1))).parent
    credit = split_parent(resolution_of(("A", "700", 1), ("B", "300", 1)),
                          amount="1000.00", direction="credit").parent

    g_debit, _m, _b = plan_group(("MSGID", "K"), partition, [debit])
    assert all(c["direction"] == "debit" and D(c["amount"]) < 0
               for c in g_debit["children"])
    g_credit, _m, _b = plan_group(("MSGID", "K"), partition, [credit])
    assert all(c["direction"] == "credit" and D(c["amount"]) > 0
               for c in g_credit["children"])


def test_the_canonical_parent_lends_its_fields_to_the_group():
    """First by (value_date, external_ref): the run-level anchor the backend
    keeps only for brand-new groups."""
    partition = groups_of(("A", "700", 1), ("B", "300", 1))
    older = split_parent(resolution_of(("A", "700", 1), ("B", "300", 1)),
                         external_ref="OLD", value_date="2026-07-01T00:00:00").parent
    newer = split_parent(resolution_of(("A", "700", 1), ("B", "300", 1)),
                         external_ref="NEW", value_date="2026-07-05T00:00:00").parent
    group, members, _b = plan_group(("MSGID", "K"), partition, [newer, older])

    assert group["value_date"] == "2026-07-01T00:00:00"
    assert group["account"] == "0010130015001"
    assert all(m["value_date"] == "2026-07-01T00:00:00" for m in members)
    assert all(m["claim_key_type"] == "MSGID" and m["claim_key_value"] == "K"
               for m in members)


def test_members_and_children_stay_aligned_per_bucket():
    partition = groups_of(("A", "700", 2), ("B", "300", 1))
    plan = split_parent(resolution_of(("A", "700", 2), ("B", "300", 1)))
    group, members, buckets = plan_group(plan.claim, partition, [plan.parent])

    for member, child in zip(
        sorted(members, key=lambda m: m["external_ref"]),
        sorted(group["children"], key=lambda c: c["external_ref"]),
    ):
        assert member["lot_id"] == child["lot_id"]
        assert member["external_ref"] == child["external_ref"]
        assert D(member["amount"]) == D(child["amount"])
        assert member["payment_count"] == child["payment_count"]
    assert set(buckets) == {c["lot_id"] for c in group["children"]}


def test_a_group_ghost_meets_its_real_counterpart_in_the_bucket():
    """End to end: the NDGB side splits (label MessageID spanning pacs008) and
    its ghost, priced at the pacs's exact payment sum, cancels the real SCTXB
    booked for that very pacs008."""
    # NDGB group over two pacs008 under one label.
    partition = {
        BucketKey(BUCKET_PAIR, pacs="P1", msgid="LUXEMBOURG"): PaymentGroup(D("700"), ["a"]),
        BucketKey(BUCKET_PAIR, pacs="P2", msgid="LUXEMBOURG"): PaymentGroup(D("300"), ["b"]),
    }
    res = MovementResolution(
        (PaymentRef("P1", "LUXEMBOURG", "a", D("700")),
         PaymentRef("P2", "LUXEMBOURG", "b", D("300"))),
        BucketKey("MSGID_ONLY", msgid="LUXEMBOURG"),
        ("MSGID", "LUXEMBOURG"),
    )
    ndgb = plan_movement(entry(amount="1000.00", direction="credit",
                               external_ref="NDGB-1"), "NDGB", res, flow_source_id=FSID)
    assert ndgb.partition == partition  # the movement stashed the same partition
    group, _members, _b = plan_group(ndgb.claim, partition, [ndgb.parent])

    # SCTXB side: pacs P1 has only the label msgid → one bucket → whole.
    sctxb = plan_movement(
        entry(amount="-700.00", external_ref="PF-SP"),
        "SCTXB",
        MovementResolution(
            (PaymentRef("P1", "LUXEMBOURG", "a", D("700")),),
            BucketKey("PACS_ONLY", pacs="P1"),
            (KEY_PACS008, "P1"),
        ),
        flow_source_id=FSID,
    )
    assert sctxb.parent is None
    lot = sctxb.entries[0]["reco_id"]
    ghost = next(c for c in group["children"] if c["lot_id"] == lot)
    assert D(ghost["amount"]) + D(sctxb.entries[0]["amount"]) == D("0.00")


# ---------------------------------------------------------------------------
# aggregate_key — how many movements claim the same payment group
# ---------------------------------------------------------------------------

def _row(tp, ref_no=None, remarks_1=None):
    return {"TransactionParticulars": tp, "PaymentOrderID_Ref": ref_no, "Remarks_1": remarks_1}


def test_aggregate_key_mirrors_how_a_movement_resolves_its_payments():
    assert aggregate_key(_row("SCTXB/O/x", remarks_1="PACS1")) == ("PACS008", "PACS1")
    assert aggregate_key(_row("NDGB/agg", remarks_1="MSGA")) == ("MSGID", "MSGA")
    assert aggregate_key(_row("SCTXB/NCP/I/PO1/x", ref_no="PO1")) == ("PO", "PO1")
    assert aggregate_key(_row("NDRJ/rej", ref_no="paysis##PO9")) == ("PO", "PO9")
    assert aggregate_key(_row("SWIFT/x/y", ref_no="R1")) == ("PO", "R1")
    assert aggregate_key(_row("GLXYZ/other")) is None
    assert aggregate_key(_row("NDGB/agg", remarks_1=None)) is None


def test_the_two_sides_of_a_pair_meet_despite_the_datamarts_casing():
    """End to end on the exact prod pair. The NDGB takes the MessageID off the
    movement (upper case, as Finacle writes Remarks_1) and the SCTXB off
    std.Payment (real casing); the two must still land in ONE bucket and net to
    zero — they were +1 573,80 and -1 573,80 in two lots."""
    pacs, msgid_real = "26080309550401355", "2412-20260731-Rumelange"

    sctxb = MovementResolution(
        (PaymentRef(pacs, msgid_real, "po1", D("1573.80")),),
        BucketKey("PACS_ONLY", pacs=pacs),
        (KEY_PACS008, pacs),
    )
    ndgb = MovementResolution(
        (PaymentRef(pacs, msgid_real.upper(), "po1", D("1573.80")),),
        BucketKey("MSGID_ONLY", msgid=msgid_real.upper()),
        (KEY_MSGID, msgid_real.upper()),
    )

    debit = plan_movement(
        entry(amount="-1573.80", external_ref="PF0024071"), "SCTXB", sctxb, flow_source_id=16
    )
    credit = plan_movement(
        entry(amount="1573.80", direction="credit", external_ref="PF0136426#2"),
        "NDGB", ndgb, flow_source_id=16,
    )
    # One bucket each (no split), and the SAME bucket.
    assert debit.parent is None and credit.parent is None
    assert debit.entries[0]["reco_id"] == credit.entries[0]["reco_id"]
    assert D(debit.entries[0]["amount"]) + D(credit.entries[0]["amount"]) == D("0.00")


def test_aggregate_key_and_its_count_ignore_case():
    assert aggregate_key(_row("NDGB/agg", remarks_1="2412-Rumelange")) == (
        "MSGID", "2412-RUMELANGE"
    )
    counts = count_aggregate_keys([
        _row("NDGB/agg", remarks_1="2412-Rumelange"),
        _row("NDGB/agg", remarks_1="2412-RUMELANGE"),
    ])
    assert counts == {("MSGID", "2412-RUMELANGE"): 2}


def test_casing_conflicts_reports_what_the_folding_papered_over():
    """A datamart-quality signal: folded into one bucket, but worth knowing."""
    conflicts = casing_conflicts([
        _row("NDGB/agg", remarks_1="2412-Rumelange"),
        _row("NDGB/agg", remarks_1="2412-RUMELANGE"),
        _row("NDGB/agg", remarks_1="CLEAN"),
    ])
    assert conflicts == {"MSGID:2412-RUMELANGE": ["2412-RUMELANGE", "2412-Rumelange"]}


def test_counting_exposes_the_prod_case():
    """184 NDGB carrying one MessageID: the number the run log must surface."""
    rows = [_row("NDGB/agg", remarks_1="PTEL-003842-SDD20260721R02") for _ in range(184)]
    rows += [_row("NDGB/agg", remarks_1="AUTRE"), _row("GLXYZ/other")]
    counts = count_aggregate_keys(rows)
    assert counts[("MSGID", "PTEL-003842-SDD20260721R02")] == 184
    assert counts[("MSGID", "AUTRE")] == 1
    assert len(counts) == 2  # the unhandled shape contributes nothing


# ---------------------------------------------------------------------------
# push batching — sized by ghosts, groups always travel whole
# ---------------------------------------------------------------------------

def _group(name, n_children, n_parents=1):
    return {
        "claim_key_type": "MSGID",
        "claim_key_value": name,
        "parents": [{"external_ref": f"{name}-p{i}"} for i in range(n_parents)],
        "children": [{"external_ref": f"KEY:{name}~{i}"} for i in range(n_children)],
    }


def test_batches_are_bounded_by_the_ghost_count_not_the_group_count():
    """A group is not a unit of constant cost: one label MessageID spread over
    ~100 pacs008 yields ~100 ghosts. Counting groups put 50 000 rows in a single
    request and timed the backend out in prod (per-parent era, same lesson)."""
    groups = [_group(f"G{i}", 100) for i in range(20)]  # 2000 ghosts total
    batches = list(split_push_batches(groups, max_children=250))
    assert all(sum(len(g["children"]) for g in b) <= 250 for b in batches)
    assert sum(len(b) for b in batches) == 20  # nothing lost


def test_a_groups_ghosts_are_never_split_across_two_pushes():
    """The backend reaps, for each group it is given, the ghosts absent from
    the payload — so a group cut in half would have the second push delete what
    the first created."""
    groups = [_group("A", 3), _group("B", 3), _group("C", 3)]
    for batch in split_push_batches(groups, max_children=4):
        keys = [g["claim_key_value"] for g in batch]
        assert len(keys) == len(set(keys))
    seen = [g for b in split_push_batches(groups, max_children=4) for g in b]
    assert [g["claim_key_value"] for g in seen] == ["A", "B", "C"]
    assert all(len(g["children"]) == 3 for g in seen)


def test_an_oversized_group_is_pushed_alone_rather_than_cut():
    """Going over budget is survivable; losing ghosts is not."""
    groups = [_group("SMALL", 1), _group("HUGE", 5000, n_parents=184), _group("SMALL2", 1)]
    batches = list(split_push_batches(groups, max_children=10))
    huge = [b for b in batches if any(g["claim_key_value"] == "HUGE" for g in b)]
    assert len(huge) == 1 and len(huge[0]) == 1
    assert len(huge[0][0]["children"]) == 5000


def test_batching_handles_empty_and_childless_input():
    assert list(split_push_batches([])) == []
    childless = [{"claim_key_value": "X", "children": []}]
    assert list(split_push_batches(childless, max_children=10)) == [childless]


def test_bucket_ids_are_stable_and_distinct_per_bucket():
    a = bucket_id(FSID, BucketKey(BUCKET_PAIR, pacs="P1", msgid="A"))
    assert a == bucket_id(FSID, BucketKey(BUCKET_PAIR, pacs="P1", msgid="A"))
    assert a != bucket_id(FSID, BucketKey(BUCKET_PAIR, pacs="P1", msgid="B"))
