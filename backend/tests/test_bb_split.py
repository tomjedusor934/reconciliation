"""Unit tests for the ghost-movement split (pure functions, no I/O).

The invariant under test is the one the whole design rests on:

    Σ ghosts == the amount finacle actually booked

If it ever breaks, a movement's amount silently leaks out of the reconciliation
and no lot-level check can see it — the sums would simply be wrong everywhere.
So it is asserted directly, and over randomised inputs.
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
    BUCKET_RESIDUAL,
    UNRESOLVED_RECO_ID,
    BucketKey,
    MovementResolution,
    PaymentGroup,
    PaymentRef,
    aggregate_key,
    allocate_amount,
    bucket_id,
    count_aggregate_keys,
    ghost_external_ref,
    plan_movement,
    split_push_batches,
)

D = Decimal
FSID = 3


def entry(amount="-1000.00", direction="debit", external_ref="PF0051006#2"):
    """Entry dict in the exact movement_row_to_entry shape."""
    return {
        "reco_id": None,
        "account": "0010130015001",
        "currency": "EUR",
        "amount": amount,
        "value_date": "2026-07-06T00:00:00",
        "operation_date": "2026-07-06T00:00:00",
        "direction": direction,
        "event_type": "TR",
        "external_ref": external_ref,
        "transaction_particulars": "SCTXB/O/x",
        "ref_no": None,
        "remarks_1": "PACS1",
        "payload_raw": {},
    }


def groups_of(*pairs):
    """(msgid, amount, n_payments) → the partition plan_movement consumes."""
    return {
        BucketKey(BUCKET_PAIR, pacs="PACS1", msgid=msgid): PaymentGroup(
            D(amount), [f"{msgid}-po{i}" for i in range(n)]
        )
        for msgid, amount, n in pairs
    }


def resolution_of(*pairs):
    return MovementResolution(
        tuple(
            PaymentRef("PACS1", msgid, f"{msgid}-po{i}", D(amount) / n)
            for msgid, amount, n in pairs
            for i in range(n)
        ),
        BucketKey("PACS_ONLY", pacs="PACS1"),
    )


# ---------------------------------------------------------------------------
# allocate_amount — the movement's own money, shared out
# ---------------------------------------------------------------------------

def test_the_movement_is_shared_out_in_proportion_to_the_payment_sums():
    debit, _ = allocate_amount(D("-1000"), groups_of(("A", "600", 2), ("B", "400", 1)))
    assert sorted(str(a) for _k, a, _g in debit) == ["-400.00", "-600.00"]
    credit, _ = allocate_amount(D("1000"), groups_of(("A", "600", 2), ("B", "400", 1)))
    assert sorted(str(a) for _k, a, _g in credit) == ["400.00", "600.00"]


def test_a_ghost_can_never_exceed_the_movement_it_comes_from():
    """THE regression. In prod, 184 NDGB movements each resolved the same
    20 000-payment group worth 1 817 225,60 and each emitted a ghost worth the
    FULL group, taking one bucket to -334 M€. A movement may only ever hand out
    what it actually carries."""
    group = groups_of(("PTEL-003842-SDD20260721R02", "1817225.60", 3))
    slices, payment_amount = allocate_amount(D("-9876.54"), group)
    assert sum(a for _k, a, _g in slices) == D("-9876.54")
    assert all(abs(a) <= D("9876.54") for _k, a, _g in slices)
    # What std.Payment says is kept, as a fact about the movement — not as money.
    assert payment_amount == D("-1817225.60")


def test_the_shared_group_case_end_to_end():
    """184 movements, one shared payment group: the bucket must end up worth the
    sum of the real movements, not 184x the group."""
    groups = groups_of(("PTEL", "1817225.60", 2), ("AUTRE", "200000.00", 1))
    booked = D("-9876.54")
    bucket_total = D(0)
    for _ in range(184):
        slices, _ = allocate_amount(booked, groups)
        assert sum(a for _k, a, _g in slices) == booked
        bucket_total += next(a for k, a, _g in slices if k.msgid == "PTEL")
    # ~90 % of 184 x -9876.54, and nowhere near 184 x -1 817 225,60.
    assert D("-1700000") < bucket_total < D("-1600000")
    assert abs(bucket_total) < abs(booked * 184)


def test_allocation_is_exact_to_the_cent_with_awkward_weights():
    """Plain rounding would drift a cent per bucket; largest remainder must not."""
    slices, _ = allocate_amount(D("-100.00"), groups_of(("A", "1", 1), ("B", "1", 1), ("C", "1", 1)))
    assert sum(a for _k, a, _g in slices) == D("-100.00")
    assert sorted(str(a) for _k, a, _g in slices) == ["-33.33", "-33.33", "-33.34"]


def test_a_movement_booked_in_whole_units_still_splits_into_cents():
    slices, _ = allocate_amount(D("-1000"), groups_of(("A", "1", 1), ("B", "2", 1)))
    assert sum(a for _k, a, _g in slices) == D("-1000")
    assert sorted(str(a) for _k, a, _g in slices) == ["-333.33", "-666.67"]


def test_zero_weights_fall_back_to_counts_then_to_equal_shares():
    """A movement must always land somewhere, even when std.Payment reports 0."""
    by_count, _ = allocate_amount(D("-90"), groups_of(("A", "0", 2), ("B", "0", 1)))
    assert sorted(str(a) for _k, a, _g in by_count) == ["-30.00", "-60.00"]
    equal, _ = allocate_amount(D("-90"), groups_of(("A", "0", 0), ("B", "0", 0)))
    assert sorted(str(a) for _k, a, _g in equal) == ["-45.00", "-45.00"]


def test_slices_are_ordered_deterministically():
    a, _ = allocate_amount(D("-1000"), groups_of(("A", "400", 1), ("B", "600", 1)))
    b, _ = allocate_amount(D("-1000"), groups_of(("B", "600", 1), ("A", "400", 1)))
    assert [k.label() for k, _a, _g in a] == [k.label() for k, _a, _g in b]
    assert [str(x) for _k, x, _g in a] == [str(x) for _k, x, _g in b]


@pytest.mark.parametrize("seed", range(50))
def test_conservation_is_exact_for_random_inputs(seed):
    """Σ slices == total EXACTLY. No tolerance, no residual: there is no longer
    any escape hatch through which a movement's amount could change."""
    rng = random.Random(seed)
    total = D(rng.randint(-5_000_000, 5_000_000)) / D(rng.choice([1, 100, 10000]))
    groups = groups_of(
        *(
            (f"MSG{i}", str(D(rng.randint(0, 400_000)) / 100), rng.randint(0, 5))
            for i in range(rng.randint(1, 8))
        )
    )
    slices, _ = allocate_amount(total, groups)
    assert sum(a for _k, a, _g in slices) == total
    # And no slice ever exceeds the movement.
    assert all(abs(a) <= abs(total) for _k, a, _g in slices)


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
    assert plan.members[0]["split_parent_external_ref"] is None


def test_multi_bucket_movement_becomes_a_parent_and_ghosts():
    res = resolution_of(("A", "700", 2), ("B", "300", 1))
    plan = plan_movement(entry(), "SCTXB", res, flow_source_id=FSID)

    # The real movement is NOT pushed as an entry: the backend withdraws it and
    # the ghosts stand in for it, so it cannot double count against them.
    assert plan.entries == []
    assert plan.parent is not None
    assert len(plan.parent["children"]) == 2 and len(plan.members) == 2

    children = plan.parent["children"]
    assert sum(D(c["amount"]) for c in children) == D("-1000.00")
    assert {c["bucket_msgid"] for c in children} == {"A", "B"}
    assert all(c["direction"] == "debit" for c in children)
    assert plan.parent["payment_count"] == 3
    assert D(plan.parent["payment_amount"]) == D("-1000.00")
    assert plan.parent["shared_key_movements"] == 1


def test_ghosts_get_a_distinct_stable_identity_per_bucket():
    res = resolution_of(("A", "700", 1), ("B", "300", 1))
    plan = plan_movement(entry(), "SCTXB", res, flow_source_id=FSID)
    refs = [c["external_ref"] for c in plan.parent["children"]]
    assert len(set(refs)) == 2
    # Derived from the parent's, so the backend can hash both from one payload.
    assert all(r.startswith("PF0051006#2~") for r in refs)
    assert all(len(r) <= 128 for r in refs)
    # Stable across runs: same bucket, same ref.
    again = plan_movement(entry(), "SCTXB", res, flow_source_id=FSID)
    assert refs == [c["external_ref"] for c in again.parent["children"]]


def test_ghost_members_point_back_at_the_real_movement():
    res = resolution_of(("A", "700", 2), ("B", "300", 1))
    plan = plan_movement(entry(), "SCTXB", res, flow_source_id=FSID)
    assert all(m["split_parent_external_ref"] == "PF0051006#2" for m in plan.members)
    assert sorted(m["payment_count"] for m in plan.members) == [1, 2]
    # Each member sits in the bucket its ghost was priced for.
    for member, child in zip(
        sorted(plan.members, key=lambda m: m["amount"]),
        sorted(plan.parent["children"], key=lambda c: c["amount"]),
    ):
        assert member["lot_id"] == child["lot_id"]
        assert member["external_ref"] == child["external_ref"]


def test_a_payment_gap_is_recorded_and_never_becomes_a_ghost():
    """'finacle booked more than the payments explain' is a fact about the
    movement, not money to place in a bucket. A residual ghost used to carry it —
    and that is precisely the door through which a 1,8 M€ slice walked in."""
    res = resolution_of(("A", "700", 1), ("B", "290", 1))  # payments explain 990
    plan = plan_movement(entry(), "SCTXB", res, flow_source_id=FSID)

    assert not any(c["bucket_kind"] == BUCKET_RESIDUAL for c in plan.parent["children"])
    assert D(plan.parent["payment_amount"]) == D("-990.00")
    assert D(plan.parent["amount"]) - D(plan.parent["payment_amount"]) == D("-10.00")
    # The whole booked amount is still placed — the gap changes the weights, not
    # the total.
    assert sum(D(c["amount"]) for c in plan.parent["children"]) == D("-1000.00")


def test_no_residual_bucket_is_ever_emitted():
    """Whatever the input, plan_movement must not invent a bucket to dump a
    leftover into: there is no leftover any more."""
    for res in (
        resolution_of(("A", "700", 1), ("B", "290", 1)),
        resolution_of(("A", "0", 1), ("B", "0", 1)),
        resolution_of(("A", "999999", 3), ("B", "0.01", 1)),
    ):
        plan = plan_movement(entry(), "SCTXB", res, flow_source_id=FSID)
        kinds = {c["bucket_kind"] for c in plan.parent["children"]}
        assert BUCKET_RESIDUAL not in kinds


def test_shared_key_count_is_carried_onto_the_parent():
    res = resolution_of(("A", "700", 1), ("B", "300", 1))
    plan = plan_movement(entry(), "NDGB", res, flow_source_id=FSID, shared_key_movements=184)
    assert plan.parent["shared_key_movements"] == 184
    # Signalled, never acted on: the allocation is the same either way.
    plain = plan_movement(entry(), "NDGB", res, flow_source_id=FSID)
    assert [c["amount"] for c in plan.parent["children"]] == [
        c["amount"] for c in plain.parent["children"]
    ]


def test_credit_movement_splits_into_credit_ghosts():
    res = resolution_of(("A", "700", 1), ("B", "300", 1))
    plan = plan_movement(
        entry(amount="1000.00", direction="credit"), "NDGB", res, flow_source_id=FSID
    )
    assert all(c["direction"] == "credit" for c in plan.parent["children"])
    assert sum(D(c["amount"]) for c in plan.parent["children"]) == D("1000.00")


def test_a_ghost_and_its_counterpart_land_in_the_same_bucket():
    """The end-to-end property: an SP bulk sliced by MessageID meets the NDGB
    booked for that MessageID, and the pair nets to zero."""
    bulk = plan_movement(
        entry(amount="-1000.00"), "SCTXB",
        resolution_of(("A", "700", 2), ("B", "300", 1)),
        flow_source_id=FSID,
    )
    ndgb = plan_movement(
        entry(amount="700.00", direction="credit", external_ref="NDGB-1"), "NDGB",
        resolution_of(("A", "700", 2)),
        flow_source_id=FSID,
    )
    assert ndgb.parent is None  # one bucket → no split
    lot = ndgb.entries[0]["reco_id"]
    ghost = next(c for c in bulk.parent["children"] if c["lot_id"] == lot)
    assert D(ghost["amount"]) + D(ndgb.entries[0]["amount"]) == D("0.00")


# ---------------------------------------------------------------------------
# identity helpers
# ---------------------------------------------------------------------------

def test_ghost_external_ref_is_bounded_and_bucket_specific():
    long_ref = "X" * 300
    key = BucketKey(BUCKET_PAIR, pacs="P", msgid="M")
    ref = ghost_external_ref(long_ref, key)
    assert len(ref) <= 128
    assert ref != ghost_external_ref(long_ref, BucketKey(BUCKET_PAIR, pacs="P", msgid="N"))
    assert ghost_external_ref(None, key)  # a movement with no TransactionID still splits


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


def test_counting_exposes_the_prod_case():
    """184 NDGB carrying one MessageID: that is the number the run must surface,
    because every one of them then resolves the whole 20 000-payment group."""
    rows = [_row("NDGB/agg", remarks_1="PTEL-003842-SDD20260721R02") for _ in range(184)]
    rows += [_row("NDGB/agg", remarks_1="AUTRE"), _row("GLXYZ/other")]
    counts = count_aggregate_keys(rows)
    assert counts[("MSGID", "PTEL-003842-SDD20260721R02")] == 184
    assert counts[("MSGID", "AUTRE")] == 1
    assert len(counts) == 2  # the unhandled shape contributes nothing


# ---------------------------------------------------------------------------
# push batching — sized by ghosts, never by parents
# ---------------------------------------------------------------------------

def _parent(name, n_children):
    return {"external_ref": name, "children": [{"external_ref": f"{name}~{i}"} for i in range(n_children)]}


def test_batches_are_bounded_by_the_ghost_count_not_the_parent_count():
    """A parent is not a unit of constant cost: one pacs008 spread over ~100
    MessageIDs yields ~100 ghosts. Counting parents put 50 000 rows in a single
    request and timed the backend out in prod."""
    parents = [_parent(f"P{i}", 100) for i in range(20)]  # 2000 ghosts total
    batches = list(split_push_batches(parents, max_children=250))
    assert all(sum(len(p["children"]) for p in b) <= 250 for b in batches)
    assert sum(len(b) for b in batches) == 20  # nothing lost


def test_a_parents_ghosts_are_never_split_across_two_pushes():
    """The backend reaps, for each parent it is given, the ghosts absent from
    the payload — so a parent cut in half would have the second push delete what
    the first created."""
    parents = [_parent("A", 3), _parent("B", 3), _parent("C", 3)]
    for batch in split_push_batches(parents, max_children=4):
        refs = [p["external_ref"] for p in batch]
        assert len(refs) == len(set(refs))
    # Every parent appears exactly once, with all of its ghosts.
    seen = [p for b in split_push_batches(parents, max_children=4) for p in b]
    assert [p["external_ref"] for p in seen] == ["A", "B", "C"]
    assert all(len(p["children"]) == 3 for p in seen)


def test_an_oversized_parent_is_pushed_alone_rather_than_cut():
    """Going over budget is survivable; losing ghosts is not."""
    parents = [_parent("SMALL", 1), _parent("HUGE", 5000), _parent("SMALL2", 1)]
    batches = list(split_push_batches(parents, max_children=10))
    huge = [b for b in batches if any(p["external_ref"] == "HUGE" for p in b)]
    assert len(huge) == 1 and len(huge[0]) == 1
    assert len(huge[0][0]["children"]) == 5000


def test_batching_handles_empty_and_childless_input():
    assert list(split_push_batches([])) == []
    childless = [{"external_ref": "X", "children": []}]
    assert list(split_push_batches(childless, max_children=10)) == [childless]


def test_bucket_id_still_separates_residual_refs():
    """RESIDUAL is no longer produced, but the kind stays addressable so the lots
    inherited from the previous behaviour remain readable and filterable."""
    assert bucket_id(FSID, BucketKey(BUCKET_RESIDUAL, ref="a")) != bucket_id(
        FSID, BucketKey(BUCKET_RESIDUAL, ref="b")
    )
