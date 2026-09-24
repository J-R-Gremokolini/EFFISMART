"""Schéma initial V1 + hypertable TimescaleDB pour les mesures.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-24
"""
import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

ENUM = sa.String(32)  # énumérations stockées en VARCHAR (cf. app.models._enum)
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    is_postgres = op.get_bind().dialect.name == "postgresql"
    if is_postgres:
        op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")

    op.create_table(
        "auditors",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("email", sa.String(254), nullable=False, unique=True),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "organizations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("siren", sa.String(9)),
        sa.Column("address", sa.String(300)),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("auditor_id", sa.Integer, sa.ForeignKey("auditors.id"), index=True),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), index=True),
        sa.Column("role", ENUM, nullable=False),
        sa.Column("email", sa.String(254), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(200), nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "auditor_client_links",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("auditor_id", sa.Integer, sa.ForeignKey("auditors.id"), nullable=False, index=True),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("start_date", sa.Date, nullable=False),
        sa.Column("end_date", sa.Date),
        sa.Column("active", sa.Boolean, nullable=False),
    )
    op.create_table(
        "sites",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("address", sa.String(300)),
        sa.Column("surface_m2", sa.Float),
        sa.Column("is_tertiary_decret", sa.Boolean, nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "delivery_points",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("site_id", sa.Integer, sa.ForeignKey("sites.id"), nullable=False, index=True),
        sa.Column("fluid", ENUM, nullable=False),
        sa.Column("external_ref", sa.String(32), nullable=False, unique=True),
        sa.Column("provider", ENUM, nullable=False),
        sa.Column("is_primary", sa.Boolean, nullable=False),
        sa.Column("subscribed_power_kva", sa.Float),
        sa.Column("power_threshold_kw", sa.Float),
        sa.Column("daily_threshold_kwh", sa.Float),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "consents",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("delivery_point_id", sa.Integer, sa.ForeignKey("delivery_points.id"), nullable=False, index=True),
        sa.Column("granted_at", TS, nullable=False),
        sa.Column("expires_at", TS),
        sa.Column("revoked_at", TS),
        sa.Column("scope", sa.String(200), nullable=False),
        sa.Column("proof_ref", sa.String(200), nullable=False),
        sa.Column("granted_by", sa.Integer, sa.ForeignKey("users.id")),
    )
    op.create_table(
        "measurements",
        sa.Column("delivery_point_id", sa.Integer, sa.ForeignKey("delivery_points.id"), primary_key=True),
        sa.Column("time", TS, primary_key=True),
        sa.Column("value_kwh", sa.Float, nullable=False),
        sa.Column("avg_power_kw", sa.Float),
        sa.Column("step", ENUM, nullable=False),
    )
    if is_postgres:
        op.execute(
            "SELECT create_hypertable('measurements', 'time', "
            "chunk_time_interval => INTERVAL '30 days', if_not_exists => TRUE)"
        )
    op.create_table(
        "regulatory_deadlines",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("site_id", sa.Integer, sa.ForeignKey("sites.id"), nullable=False, index=True),
        sa.Column("obligation", ENUM, nullable=False),
        sa.Column("due_date", sa.Date, nullable=False),
        sa.Column("status", ENUM, nullable=False),
        sa.Column("notes", sa.Text),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "action_logs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("site_id", sa.Integer, sa.ForeignKey("sites.id"), nullable=False, index=True),
        sa.Column("obligation", ENUM, nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("performed_at", TS, nullable=False),
        sa.Column("performed_by", sa.Integer, sa.ForeignKey("users.id")),
    )
    op.create_table(
        "emission_factors",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("fluid", ENUM, nullable=False),
        sa.Column("factor_kgco2_per_kwh", sa.Float, nullable=False),
        sa.Column("valid_from", sa.Date, nullable=False),
        sa.Column("source", sa.String(300), nullable=False),
        sa.Column("version", sa.String(100), nullable=False),
    )
    op.create_table(
        "export_jobs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("format", ENUM, nullable=False),
        sa.Column("period_start", sa.Date, nullable=False),
        sa.Column("period_end", sa.Date, nullable=False),
        sa.Column("status", ENUM, nullable=False),
        sa.Column("file_ref", sa.String(300)),
        sa.Column("factors_used", sa.JSON),
        sa.Column("error", sa.Text),
        sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "drifts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("delivery_point_id", sa.Integer, sa.ForeignKey("delivery_points.id"), nullable=False, index=True),
        sa.Column("kind", ENUM, nullable=False),
        sa.Column("day", sa.Date, nullable=False),
        sa.Column("measured_value", sa.Float, nullable=False),
        sa.Column("reference_value", sa.Float, nullable=False),
        sa.Column("deviation_pct", sa.Float, nullable=False),
        sa.Column("unit", sa.String(16), nullable=False),
        sa.Column("details", sa.Text, nullable=False),
        sa.Column("status", ENUM, nullable=False),
        sa.Column("comment", sa.Text),
        sa.Column("detected_at", TS, nullable=False),
        sa.Column("qualified_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.UniqueConstraint("delivery_point_id", "kind", "day", name="uq_drift_dp_kind_day"),
    )
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("drift_id", sa.Integer, sa.ForeignKey("drifts.id")),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id")),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )


def downgrade() -> None:
    for table in (
        "notifications",
        "drifts",
        "export_jobs",
        "emission_factors",
        "action_logs",
        "regulatory_deadlines",
        "measurements",
        "consents",
        "delivery_points",
        "sites",
        "auditor_client_links",
        "users",
        "organizations",
        "auditors",
    ):
        op.drop_table(table)
