"""F2 à deux étages.

- F2a (seuils et dérive) : nouveau détecteur « consommation en période d'inoccupation » (OFF_HOURS), stocké
  dans la colonne VARCHAR existante `drifts.kind` : aucune modification de schéma ;
- F2b (anomalies contextualisées) : contexte physique de l'anomalie (équipements suspects, zones et usages
  potentiellement impactés), calculé à partir du graphe des équipements.

Revision ID: 0007_anomaly_context
Revises: 0006_scope_f1_f5
Create Date: 2026-10-07
"""
import sqlalchemy as sa
from alembic import op

revision = "0007_anomaly_context"
down_revision = "0006_scope_f1_f5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("drifts", sa.Column("context", sa.JSON))


def downgrade() -> None:
    op.drop_column("drifts", "context")
