"""Add Finacle movement context columns (transaction_particulars, ref_no, remarks_1).

These varchar columns carry the raw movement context used to (re)compute the
reconciliation key in the ingest_finacle DAG; they also let the backend return
unresolved entries for daily retry. All nullable, no data backfill.

Revision ID: 004
Revises: 003
Create Date: 2026-06-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("reconciliation_entry", "reconciliation_entry_emargement")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("transaction_particulars", sa.String(512), nullable=True), schema="reco")
        op.add_column(table, sa.Column("ref_no", sa.String(128), nullable=True), schema="reco")
        op.add_column(table, sa.Column("remarks_1", sa.String(255), nullable=True), schema="reco")


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "remarks_1", schema="reco")
        op.drop_column(table, "ref_no", schema="reco")
        op.drop_column(table, "transaction_particulars", schema="reco")
