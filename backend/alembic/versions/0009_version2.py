"""Version 2 (F6 à F12) : fonctionnalités avancées.

- F6 : contrats de fourniture (grille tarifaire) ;
- F7 : variables d'ajustement, unité de production, année de référence ISO 50001, certification ISO 50001 ;
- F8 : plan 2D du site, positions des éléments du graphe P2 ;
- F9 : bibliothèque de gestes types, scénarios d'économies ;
- F10 : rapports trimestriels ;
- F11 : référence Décret Tertiaire, trajectoires ; le détecteur « écart au modèle » utilise la colonne
  VARCHAR existante `drifts.kind` ;
- F12 : durée de fonctionnement des équipements décalables ; type de recommandation LOAD_SHIFT (VARCHAR existant).

Revision ID: 0009_version2
Revises: 0008_alert_groups
Create Date: 2026-10-08
"""
import sqlalchemy as sa
from alembic import op

revision = "0009_version2"
down_revision = "0008_alert_groups"
branch_labels = None
depends_on = None

ENUM = sa.String(32)
TS = sa.DateTime(timezone=True)


def _explained() -> list[sa.Column]:
    return [
        sa.Column("reasoning", sa.JSON), sa.Column("confidence", sa.Float), sa.Column("confidence_factors", sa.JSON),
        sa.Column("gain_kwh", sa.Float), sa.Column("gain_eur", sa.Float), sa.Column("gain_kgco2e", sa.Float),
        sa.Column("gain_basis", sa.Text), sa.Column("algorithm", sa.String(120)),
    ]


def upgrade() -> None:
    op.add_column("organizations", sa.Column("iso50001_certified_until", sa.Date))
    for name, column in (("production_unit", sa.String(60)), ("energy_baseline_year", sa.Integer),
                         ("dt_reference_year", sa.Integer), ("dt_reference_kwh", sa.Float)):
        op.add_column("sites", sa.Column(name, column))
    for name in ("plan_x", "plan_y", "plan_w", "plan_h", "shift_hours"):
        op.add_column("asset_nodes", sa.Column(name, sa.Float))

    op.create_table(
        "supply_contracts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("delivery_point_id", sa.Integer, sa.ForeignKey("delivery_points.id"), nullable=False, index=True),
        sa.Column("supplier", sa.String(120), nullable=False),
        sa.Column("option", ENUM, nullable=False),
        sa.Column("subscription_eur_month", sa.Float, nullable=False),
        sa.Column("price_base", sa.Float), sa.Column("price_hp", sa.Float), sa.Column("price_hc", sa.Float),
        sa.Column("offpeak_start_hour", sa.Integer, nullable=False),
        sa.Column("offpeak_end_hour", sa.Integer, nullable=False),
        sa.Column("tempo_prices", sa.JSON), sa.Column("dynamic_margin_eur_kwh", sa.Float),
        sa.Column("valid_from", sa.Date, nullable=False),
        sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "adjustment_variables",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("site_id", sa.Integer, sa.ForeignKey("sites.id"), nullable=False, index=True),
        sa.Column("kind", ENUM, nullable=False),
        sa.Column("month", sa.Date, nullable=False),
        sa.Column("value", sa.Float, nullable=False),
        sa.Column("updated_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("updated_at", TS, nullable=False),
        sa.UniqueConstraint("site_id", "kind", "month", name="uq_adjustment_site_kind_month"),
    )
    op.create_table(
        "site_plans",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("site_id", sa.Integer, sa.ForeignKey("sites.id"), nullable=False, unique=True),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(64), nullable=False),
        sa.Column("stored_name", sa.String(80), nullable=False),
        sa.Column("aspect_ratio", sa.Float, nullable=False),
        sa.Column("uploaded_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("uploaded_at", TS, nullable=False),
    )
    op.create_table(
        "action_templates",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("auditor_id", sa.Integer, sa.ForeignKey("auditors.id"), index=True),
        sa.Column("code", sa.String(40), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("usage", sa.String(16), nullable=False),
        sa.Column("savings_pct", sa.Float, nullable=False),
        sa.Column("investment_eur_m2", sa.Float, nullable=False),
        sa.Column("investment_eur", sa.Float, nullable=False),
        sa.Column("notes", sa.Text),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "savings_scenarios",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("site_id", sa.Integer, sa.ForeignKey("sites.id"), nullable=False, index=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("template_ids", sa.JSON, nullable=False),
        sa.Column("recommendation_ids", sa.JSON, nullable=False),
        sa.Column("usage_shares", sa.JSON),
        sa.Column("results", sa.JSON, nullable=False),
        sa.Column("retained", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "trajectories",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("site_id", sa.Integer, sa.ForeignKey("sites.id"), nullable=False, index=True),
        sa.Column("data_as_of", sa.Date, nullable=False),
        sa.Column("reference_year", sa.Integer, nullable=False),
        sa.Column("reference_kwh", sa.Float, nullable=False),
        sa.Column("current_kwh", sa.Float, nullable=False),
        sa.Column("annual_rate", sa.Float, nullable=False),
        sa.Column("projected_2030_kwh", sa.Float, nullable=False),
        sa.Column("reduction_2030", sa.Float, nullable=False),
        sa.Column("reduction_2030_with_actions", sa.Float),
        sa.Column("points", sa.JSON, nullable=False),
        sa.Column("status", ENUM, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("reviewed_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("reviewed_at", TS),
        sa.Column("review_comment", sa.Text),
        *_explained(),
    )
    op.create_table(
        "quarterly_reports",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("auditor_id", sa.Integer, sa.ForeignKey("auditors.id")),
        sa.Column("year", sa.Integer, nullable=False),
        sa.Column("quarter", sa.Integer, nullable=False),
        sa.Column("status", ENUM, nullable=False),
        sa.Column("sections", sa.JSON, nullable=False),
        sa.Column("introduction", sa.Text),
        sa.Column("conclusion", sa.Text),
        sa.Column("generated_at", TS, nullable=False),
        sa.Column("validated_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("validated_at", TS),
        sa.Column("delivered_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("delivered_at", TS),
        sa.UniqueConstraint("organization_id", "year", "quarter", name="uq_report_org_quarter"),
    )


def downgrade() -> None:
    for table in ("quarterly_reports", "trajectories", "savings_scenarios", "action_templates", "site_plans",
                  "adjustment_variables", "supply_contracts"):
        op.drop_table(table)
    for name in ("shift_hours", "plan_h", "plan_w", "plan_y", "plan_x"):
        op.drop_column("asset_nodes", name)
    for name in ("dt_reference_kwh", "dt_reference_year", "energy_baseline_year", "production_unit"):
        op.drop_column("sites", name)
    op.drop_column("organizations", "iso50001_certified_until")
