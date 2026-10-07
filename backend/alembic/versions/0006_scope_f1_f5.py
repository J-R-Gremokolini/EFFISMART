"""Périmètre F1–F5 confirmé par l'étude de marché.

- F1 : consommations déclarées (données N0, factures et relevés) ; mesure avant / après des recommandations ;
- F2, F3 : notification immédiate par e-mail (file d'envoi, préférence par utilisateur), rappels d'échéances ;
- F4 : jeux de données énergie produits automatiquement.

Revision ID: 0006_scope_f1_f5
Revises: 0005_prediction_models
Create Date: 2026-10-07
"""
import sqlalchemy as sa
from alembic import op

revision = "0006_scope_f1_f5"
down_revision = "0005_prediction_models"
branch_labels = None
depends_on = None

ENUM = sa.String(32)
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "declared_consumptions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("delivery_point_id", sa.Integer, sa.ForeignKey("delivery_points.id"), nullable=False, index=True),
        sa.Column("period_start", sa.Date, nullable=False),
        sa.Column("period_end", sa.Date, nullable=False),
        sa.Column("kwh", sa.Float, nullable=False),
        sa.Column("amount_eur", sa.Float),
        sa.Column("source", ENUM, nullable=False),
        sa.Column("document_id", sa.Integer, sa.ForeignKey("documents.id")),
        sa.Column("notes", sa.Text),
        sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "outgoing_emails",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), index=True),
        sa.Column("to_address", sa.String(254), nullable=False),
        sa.Column("subject", sa.String(300), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("status", ENUM, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("next_attempt_at", TS, nullable=False),
        sa.Column("channel", sa.String(16)),
        sa.Column("last_error", sa.Text),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("sent_at", TS),
    )
    op.add_column("users", sa.Column("email_notifications", sa.Boolean, nullable=False, server_default=sa.true()))
    op.add_column("regulatory_deadlines", sa.Column("reminder_level", sa.Integer))
    op.add_column("export_jobs", sa.Column("automatic", sa.Boolean, nullable=False, server_default=sa.false()))
    op.add_column("recommendations", sa.Column("savings_snapshot", sa.JSON))
    op.add_column("recommendations", sa.Column("savings_validated_by", sa.Integer, sa.ForeignKey("users.id")))
    op.add_column("recommendations", sa.Column("savings_validated_at", TS))


def downgrade() -> None:
    for column in ("savings_validated_at", "savings_validated_by", "savings_snapshot"):
        op.drop_column("recommendations", column)
    op.drop_column("export_jobs", "automatic")
    op.drop_column("regulatory_deadlines", "reminder_level")
    op.drop_column("users", "email_notifications")
    op.drop_table("outgoing_emails")
    op.drop_table("declared_consumptions")
