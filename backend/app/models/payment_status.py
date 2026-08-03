from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)

from app.db.base import Base


class EntryPaymentStatus(Base):
    """Status of one payment (std.Payment) behind one reconciliation GROUP.

    Keyed by ``reco_id`` (NOT per entry): all entries sharing a reco_id are the
    same reconciliation — for the batch-booking flow ``reco_id`` is the lot uuid,
    so a lot's payments are stored ONCE instead of being fanned out per member
    (which produced tens of millions of duplicate rows). A bulk/aggregate reco
    settles N payments → N rows; a single reco has one. Synced from the datamart
    by the sync_payment_status DAG task (the app has no MSSQL access) and
    re-refreshed while the reco is still live. Read as a per-reco aggregate
    ("3 ACC · 2 PDNG") by the operational view and summed per lot by the lot view.
    """

    __tablename__ = "entry_payment_status"
    __table_args__ = (
        UniqueConstraint("reco_id", "po_id", name="uq_entry_payment_status_reco_po"),
        Index("ix_entry_payment_status_reco_id", "reco_id"),
        Index("ix_entry_payment_status_po_id", "po_id"),
        {"schema": "reco"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    # = reconciliation_entry.reco_id (logical reference, no FK). For the
    # batch-booking flow this equals movement_lot.id (the lot uuid); for the
    # standard flow it is the computed reconciliation key. Never the "Not
    # Supported"/NULL sentinels — those movements are not synced.
    reco_id = Column(String(128), nullable=False)
    po_id = Column(String(64), nullable=False)  # = std.Payment.PaymentNumber
    status = Column(String(32), nullable=True)  # = std.Payment.Status
    # = the payment's amount (std.Payment). Nullable until the DAG re-runs
    # (PS_FULL_SYNC backfills). The lot view sums this over a lot's PDNG payments
    # to show the real pending amount; the (reco_id, po_id) uniqueness means each
    # payment counts once per lot with no extra DISTINCT.
    amount = Column(Numeric(20, 4), nullable=True)
    # = std.Payment.CreatedOn. Nullable until the DAG re-runs
    # (PS_FULL_SYNC backfills). A reco can carry N payments hence N timestamps:
    # the operational view shows the min→max span and filters on "at least one
    # payment in range".
    #
    # WITHOUT TIME ZONE on purpose — unlike reconciliation_entry.value_date. The
    # whole chain (datamart -> DAG -> API -> UI -> filter) keeps the datamart wall
    # clock naive, so the hour shown in the table is exactly the hour the filter
    # matches. Tagging it UTC would offset the display from the filter by the
    # browser's offset, which defeats the point of an hour-level filter.
    payment_timestamp = Column(DateTime, nullable=True)
