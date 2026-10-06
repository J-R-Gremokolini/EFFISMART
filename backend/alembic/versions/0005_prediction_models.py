"""Prévisions v2 : modèles comparés en validation hors échantillon (Catalina et al., 2009 ; Paudel, 2016).

Revision ID: 0005_prediction_models
Revises: 0004_validation_and_assets
Create Date: 2026-10-06
"""
import sqlalchemy as sa
from alembic import op

revision = "0005_prediction_models"
down_revision = "0004_validation_and_assets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("predictions", sa.Column("model_comparison", sa.JSON))


def downgrade() -> None:
    op.drop_column("predictions", "model_comparison")
