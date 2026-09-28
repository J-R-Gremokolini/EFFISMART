"""Dépôt de documents par les clients (factures, relevés).

Revision ID: 0002_documents
Revises: 0001_initial
Create Date: 2026-09-28
"""
import sqlalchemy as sa
from alembic import op

revision = "0002_documents"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("site_id", sa.Integer, sa.ForeignKey("sites.id")),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("original_name", sa.String(255), nullable=False),
        sa.Column("stored_name", sa.String(64), nullable=False, unique=True),
        sa.Column("content_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.Integer, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False, index=True),
        sa.Column("period_start", sa.Date),
        sa.Column("period_end", sa.Date),
        sa.Column("comment", sa.Text),
        sa.Column("review_note", sa.Text),
        sa.Column("uploaded_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("documents")
