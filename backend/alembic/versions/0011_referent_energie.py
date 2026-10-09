"""Module référent énergie (formation PRO-REFEI) : plan d'actions, management de l'énergie, sensibilisation.

Revision ID: 0011_referent_energie
Revises: 0010_custom_ipe
Create Date: 2026-10-09
"""
import sqlalchemy as sa
from alembic import op

revision = "0011_referent_energie"
down_revision = "0010_custom_ipe"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    for name, column in (("revenue_eur", sa.Float), ("ebitda_eur", sa.Float), ("finance_year", sa.Integer),
                         ("economics", sa.JSON)):
        op.add_column("organizations", sa.Column(name, column))
    for name, column in (("category", sa.String(32)), ("cee_sheet", sa.String(20)), ("lifetime_years", sa.Integer)):
        op.add_column("action_templates", sa.Column(name, column))
    for name in ("target_value", "alert_threshold_pct"):
        op.add_column("ipe_definitions", sa.Column(name, sa.Float))
    op.create_table(
        "energy_actions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("site_id", sa.Integer, sa.ForeignKey("sites.id"), nullable=False, index=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("nature", sa.String(32), nullable=False),
        sa.Column("priority", sa.String(16), nullable=False),
        sa.Column("horizon", sa.String(16), nullable=False),
        sa.Column("savings", sa.JSON, nullable=False),
        sa.Column("recurring_eur", sa.Float, nullable=False),
        sa.Column("investment_eur", sa.Float, nullable=False),
        sa.Column("reference_investment_eur", sa.Float, nullable=False),
        sa.Column("lifetime_years", sa.Integer, nullable=False),
        sa.Column("cee_kwh_cumac", sa.Float),
        sa.Column("cee_sheet", sa.String(20)),
        sa.Column("score_economic", sa.Integer),
        sa.Column("score_technical", sa.Integer),
        sa.Column("score_risk", sa.Integer),
        sa.Column("cobenefits", sa.JSON),
        sa.Column("owner", sa.String(120)),
        sa.Column("location", sa.String(200)),
        sa.Column("equipment_id", sa.Integer, sa.ForeignKey("asset_nodes.id")),
        sa.Column("due_date", sa.Date),
        sa.Column("why", sa.Text),
        sa.Column("how_much", sa.Text),
        sa.Column("how", sa.Text),
        sa.Column("contacts", sa.JSON),
        sa.Column("needs", sa.JSON),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("progress_note", sa.Text),
        sa.Column("done_on", sa.Date),
        sa.Column("ipe_definition_id", sa.Integer, sa.ForeignKey("ipe_definitions.id")),
        sa.Column("recommendation_id", sa.Integer, sa.ForeignKey("recommendations.id")),
        sa.Column("template_id", sa.Integer, sa.ForeignKey("action_templates.id")),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("mv_plan", sa.JSON),
        sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("updated_at", TS),
    )
    op.create_table(
        "maturity_assessments",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("answers", sa.JSON, nullable=False),
        sa.Column("comments", sa.JSON),
        sa.Column("scores", sa.JSON, nullable=False),
        sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "energy_management",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False, unique=True),
        sa.Column("policy", sa.Text),
        sa.Column("policy_approved_on", sa.Date),
        sa.Column("scope", sa.Text),
        sa.Column("team", sa.JSON),
        sa.Column("objectives", sa.JSON),
        sa.Column("review_months", sa.Integer),
        sa.Column("last_review_on", sa.Date),
        sa.Column("updated_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("updated_at", TS),
    )
    op.create_table(
        "communication_messages",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("site_id", sa.Integer, sa.ForeignKey("sites.id"), nullable=False, index=True),
        sa.Column("theme", sa.String(32), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("call_to_action", sa.String(200), nullable=False),
        sa.Column("figures", sa.JSON),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("approved_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("approved_at", TS),
    )


def downgrade() -> None:
    op.drop_table("communication_messages")
    op.drop_table("energy_management")
    op.drop_table("maturity_assessments")
    op.drop_table("energy_actions")
    with op.batch_alter_table("ipe_definitions") as batch:
        batch.drop_column("alert_threshold_pct")
        batch.drop_column("target_value")
    with op.batch_alter_table("action_templates") as batch:
        batch.drop_column("lifetime_years")
        batch.drop_column("cee_sheet")
        batch.drop_column("category")
    with op.batch_alter_table("organizations") as batch:
        batch.drop_column("economics")
        batch.drop_column("finance_year")
        batch.drop_column("ebitda_eur")
        batch.drop_column("revenue_eur")
