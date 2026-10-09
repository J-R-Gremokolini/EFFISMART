"""F7 : IPE personnalisés (créés par un humain) et proposés par l'IA de la plateforme (à valider, P1).

Revision ID: 0010_custom_ipe
Revises: 0009_version2
Create Date: 2026-10-09
"""
import sqlalchemy as sa
from alembic import op

revision = "0010_custom_ipe"
down_revision = "0009_version2"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "ipe_variables",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("site_id", sa.Integer, sa.ForeignKey("sites.id"), nullable=False, index=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("unit", sa.String(40), nullable=False),
        sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "ipe_variable_values",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("variable_id", sa.Integer, sa.ForeignKey("ipe_variables.id"), nullable=False, index=True),
        sa.Column("month", sa.Date, nullable=False),
        sa.Column("value", sa.Float, nullable=False),
        sa.Column("updated_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("updated_at", TS, nullable=False),
        sa.UniqueConstraint("variable_id", "month", name="uq_ipe_variable_month"),
    )
    op.create_table(
        "ipe_definitions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("site_id", sa.Integer, sa.ForeignKey("sites.id"), nullable=False, index=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("energy", sa.String(8), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("drivers", sa.JSON, nullable=False),
        sa.Column("model", sa.JSON),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("reviewed_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("reviewed_at", TS),
        sa.Column("review_comment", sa.Text),
        sa.Column("reasoning", sa.JSON), sa.Column("confidence", sa.Float), sa.Column("confidence_factors", sa.JSON),
        sa.Column("gain_kwh", sa.Float), sa.Column("gain_eur", sa.Float), sa.Column("gain_kgco2e", sa.Float),
        sa.Column("gain_basis", sa.Text), sa.Column("algorithm", sa.String(120)),
    )


def downgrade() -> None:
    op.drop_table("ipe_definitions")
    op.drop_table("ipe_variable_values")
    op.drop_table("ipe_variables")
