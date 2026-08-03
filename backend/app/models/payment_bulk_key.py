from sqlalchemy import Column, Index, Integer, String

from app.db.base import Base


class PaymentBulkKey(Base):
    """One payment (std.Payment.PaymentNumber) that belongs to a BULK booking.

    An instant payment is normally 1 PaymentNumber = 1 MessageID, which is why the
    finacle movement's ``ref_no`` can serve directly as ``reco_id`` (and why the
    MT940 leg resolves to that same PaymentNumber). Payments coming from multiline
    break the assumption: N instant payments share one MessageID and are booked in
    bulk, so the counterpart is a single line while our N movements sit on N
    distinct keys — a group that can never balance.

    The ingestion DAG detects those groups against std.Payment (>= 2 PaymentNumbers
    on one MessageID, or a multiline ``InitModule``) and stores the mapping here.
    It is read back at the start of every run so that a movement of a known bulk is
    keyed on the MessageID from the start, however late it arrives. Only bulk
    members are stored — the 1-1 majority never lands in this table.
    """

    __tablename__ = "payment_bulk_key"
    __table_args__ = (
        Index("ix_payment_bulk_key_reco_id", "reco_id"),
        {"schema": "reco"},
    )

    # = std.Payment.PaymentNumber, i.e. the movement's ref_no / PaymentOrderID_Ref.
    po_id = Column(String(64), primary_key=True)
    # = std.Payment.MessageID — the bulk's key, shared by every member.
    reco_id = Column(String(128), nullable=False)
    # = std.Payment.InitModule (e.g. 'MLTLIP'). Traceability only: a group detected
    # purely on cardinality has no multiline module and leaves this NULL.
    init_module = Column(String(32), nullable=True)
    # Number of PaymentNumbers seen on this MessageID at detection time — tells a
    # real bulk apart from a single-payment group flagged by init_module alone.
    member_count = Column(Integer, nullable=True)
    # created_at / updated_at come from Base.

    def __repr__(self) -> str:
        return f"<PaymentBulkKey(po_id={self.po_id}, reco_id={self.reco_id})>"
