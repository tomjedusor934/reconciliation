"""Manual exclusion of a reconciliation entry (mandatory comment)."""
from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Integer, Text
from sqlalchemy.orm import relationship

from app.db.base import Base


class Exclusion(Base):
    __tablename__ = "exclusion"
    __table_args__ = {"schema": "reco"}

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    # NOTE: reconciliation_entry is partitioned, so we keep a logical reference (no FK)
    # to avoid the cross-partition FK limitation. Integrity is enforced application-side.
    entry_id = Column(BigInteger, nullable=False, index=True)
    entry_value_date = Column(DateTime(timezone=True), nullable=False)
    reason = Column(Text, nullable=False)  # mandatory per spec
    created_by_user_id = Column(Integer, ForeignKey("user.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    # Cancellation fields (set when the exclusion is reversed / unexcluded)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_by_user_id = Column(Integer, ForeignKey("user.id"), nullable=True)

    def __repr__(self) -> str:
        return f"<Exclusion(id={self.id}, entry_id={self.entry_id}, reason={self.reason}, created_by_user_id={self.created_by_user_id}, created_at={self.created_at}, cancelled_at={self.cancelled_at}, cancelled_by_user_id={self.cancelled_by_user_id})>"
