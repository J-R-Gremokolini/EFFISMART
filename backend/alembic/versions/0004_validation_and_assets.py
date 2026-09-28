"""Principe P1 (sorties algorithmiques expliquées et validées par un humain) et graphe physique des équipements.

Revision ID: 0004_validation_and_assets
Revises: 0003_integrations
Create Date: 2026-09-28
"""
import sqlalchemy as sa
from alembic import op

revision = "0004_validation_and_assets"
down_revision = "0003_integrations"
branch_labels = None
depends_on = None

ENUM = sa.String(32)
TS = sa.DateTime(timezone=True)


def _explanation_columns() -> list[sa.Column]:
    """Raisonnement, niveau de confiance et gain estimé : communs à toute sortie algorithmique."""
    return [
        sa.Column("reasoning", sa.JSON),
        sa.Column("confidence", sa.Float),
        sa.Column("confidence_factors", sa.JSON),
        sa.Column("gain_kwh", sa.Float),
        sa.Column("gain_eur", sa.Float),
        sa.Column("gain_kgco2e", sa.Float),
        sa.Column("gain_basis", sa.Text),
        sa.Column("algorithm", sa.String(120)),
    ]


def upgrade() -> None:
    op.add_column("users", sa.Column("is_energy_manager", sa.Boolean, nullable=False, server_default=sa.false()))
    for column in _explanation_columns():
        op.add_column("drifts", column)
    op.add_column("drifts", sa.Column("qualified_at", TS))

    op.create_table(
        "asset_nodes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("site_id", sa.Integer, sa.ForeignKey("sites.id"), nullable=False, index=True),
        sa.Column("kind", ENUM, nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("delivery_point_id", sa.Integer, sa.ForeignKey("delivery_points.id"), unique=True),
        sa.Column("power_kw", sa.Float),
        sa.Column("surface_m2", sa.Float),
        sa.Column("always_occupied", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("notes", sa.Text),
        sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "asset_relations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("source_id", sa.Integer, sa.ForeignKey("asset_nodes.id"), nullable=False, index=True),
        sa.Column("target_id", sa.Integer, sa.ForeignKey("asset_nodes.id"), nullable=False, index=True),
        sa.Column("kind", ENUM, nullable=False),
        sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("created_at", TS, nullable=False),
        sa.UniqueConstraint("source_id", "target_id", "kind", name="uq_asset_relation"),
    )
    op.create_table(
        "recommendations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("site_id", sa.Integer, sa.ForeignKey("sites.id"), nullable=False, index=True),
        sa.Column("delivery_point_id", sa.Integer, sa.ForeignKey("delivery_points.id")),
        sa.Column("drift_id", sa.Integer, sa.ForeignKey("drifts.id")),
        sa.Column("equipment_id", sa.Integer, sa.ForeignKey("asset_nodes.id")),
        sa.Column("supporting_drift_ids", sa.JSON),
        sa.Column("kind", ENUM, nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("status", ENUM, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("reviewed_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("reviewed_at", TS),
        sa.Column("review_comment", sa.Text),
        sa.Column("applied_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("applied_at", TS),
        sa.Column("applied_comment", sa.Text),
        *_explanation_columns(),
    )
    op.create_table(
        "predictions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("site_id", sa.Integer, sa.ForeignKey("sites.id"), nullable=False, index=True),
        sa.Column("fluid", ENUM, nullable=False),
        sa.Column("kind", ENUM, nullable=False),
        sa.Column("year", sa.Integer, nullable=False),
        sa.Column("data_as_of", sa.Date, nullable=False),
        sa.Column("measured_kwh", sa.Float, nullable=False),
        sa.Column("predicted_kwh", sa.Float, nullable=False),
        sa.Column("low_kwh", sa.Float, nullable=False),
        sa.Column("high_kwh", sa.Float, nullable=False),
        sa.Column("reference_kwh", sa.Float),
        sa.Column("monthly", sa.JSON),
        sa.Column("status", ENUM, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("reviewed_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("reviewed_at", TS),
        sa.Column("review_comment", sa.Text),
        *_explanation_columns(),
    )


def downgrade() -> None:
    op.drop_table("predictions")
    op.drop_table("recommendations")
    op.drop_table("asset_relations")
    op.drop_table("asset_nodes")
    op.drop_column("drifts", "qualified_at")
    for column in reversed(_explanation_columns()):
        op.drop_column("drifts", column.name)
    op.drop_column("users", "is_energy_manager")
