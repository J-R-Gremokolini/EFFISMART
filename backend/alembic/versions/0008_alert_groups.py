"""F2 : regroupement des alertes de même cause (une cause, une alerte).

Revision ID: 0008_alert_groups
Revises: 0007_anomaly_context
Create Date: 2026-10-08
"""
import sqlalchemy as sa
from alembic import op

revision = "0008_alert_groups"
down_revision = "0007_anomaly_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("drifts", sa.Column("grouped_with_id", sa.Integer, sa.ForeignKey("drifts.id")))
    op.create_index("ix_drifts_grouped_with_id", "drifts", ["grouped_with_id"])
    op.add_column("drifts", sa.Column("grouping_rule", sa.String(16)))
    op.add_column("drifts", sa.Column("grouping_reason", sa.Text))


def downgrade() -> None:
    op.drop_column("drifts", "grouping_reason")
    op.drop_column("drifts", "grouping_rule")
    op.drop_index("ix_drifts_grouped_with_id", "drifts")
    op.drop_column("drifts", "grouped_with_id")
