"""Interface : mode clair ou sombre choisi par chaque utilisateur, photo de site en tête du tableau de bord.

Revision ID: 0012_interface_claire
Revises: 0011_referent_energie
Create Date: 2026-10-11
"""
import sqlalchemy as sa
from alembic import op

revision = "0012_interface_claire"
down_revision = "0011_referent_energie"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("users", sa.Column("ui_theme", sa.String(8)))
    op.create_table(
        "site_photos",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("site_id", sa.Integer, sa.ForeignKey("sites.id"), nullable=False, unique=True),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(64), nullable=False),
        sa.Column("stored_name", sa.String(80), nullable=False),
        sa.Column("uploaded_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("uploaded_at", TS, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("site_photos")
    op.drop_column("users", "ui_theme")
