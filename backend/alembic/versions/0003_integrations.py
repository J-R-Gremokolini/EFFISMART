"""Intégrations d'API : sources de données, connecteurs génériques, clés d'API, webhooks.

Revision ID: 0003_integrations
Revises: 0002_documents
Create Date: 2026-09-28
"""
import sqlalchemy as sa
from alembic import op

revision = "0003_integrations"
down_revision = "0002_documents"
branch_labels = None
depends_on = None

ENUM = sa.String(32)
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "platform_integrations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("kind", ENUM, nullable=False, unique=True),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("settings", sa.JSON),
        sa.Column("secrets_encrypted", sa.Text),
        sa.Column("last_test_at", TS),
        sa.Column("last_test_ok", sa.Boolean),
        sa.Column("last_test_message", sa.Text),
        sa.Column("updated_at", TS),
        sa.Column("updated_by", sa.Integer, sa.ForeignKey("users.id")),
    )
    op.create_table(
        "connectors",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("auditor_id", sa.Integer, sa.ForeignKey("auditors.id"), nullable=False, index=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("url_template", sa.String(500), nullable=False),
        sa.Column("auth_type", ENUM, nullable=False),
        sa.Column("auth_header", sa.String(100)),
        sa.Column("username", sa.String(200)),
        sa.Column("secret_encrypted", sa.Text),
        sa.Column("records_path", sa.String(200), nullable=False),
        sa.Column("time_field", sa.String(100), nullable=False),
        sa.Column("value_field", sa.String(100), nullable=False),
        sa.Column("value_unit", ENUM, nullable=False),
        sa.Column("step", ENUM, nullable=False),
        sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("created_at", TS, nullable=False),
    )
    op.add_column("delivery_points", sa.Column("connector_id", sa.Integer, sa.ForeignKey("connectors.id")))
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("auditor_id", sa.Integer, sa.ForeignKey("auditors.id"), nullable=False, index=True),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id")),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("prefix", sa.String(16), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("last_used_at", TS),
        sa.Column("revoked_at", TS),
    )
    op.create_table(
        "webhooks",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("auditor_id", sa.Integer, sa.ForeignKey("auditors.id"), nullable=False, index=True),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id")),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("url", sa.String(500), nullable=False),
        sa.Column("secret_encrypted", sa.Text, nullable=False),
        sa.Column("events", sa.JSON, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("webhook_id", sa.Integer, sa.ForeignKey("webhooks.id"), nullable=False, index=True),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("status", ENUM, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("next_attempt_at", TS, nullable=False),
        sa.Column("last_status_code", sa.Integer),
        sa.Column("last_error", sa.Text),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("sent_at", TS),
    )


def downgrade() -> None:
    op.drop_table("webhook_deliveries")
    op.drop_table("webhooks")
    op.drop_table("api_keys")
    op.drop_column("delivery_points", "connector_id")
    op.drop_table("connectors")
    op.drop_table("platform_integrations")
