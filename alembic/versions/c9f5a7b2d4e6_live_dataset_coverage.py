"""Add generic no-finding/failed collection status.

Revision ID: c9f5a7b2d4e6
Revises: b8e4f6a1c2d3
"""

from alembic import op
from autoscreener.db.models import Base

revision = "c9f5a7b2d4e6"
down_revision = "b8e4f6a1c2d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.tables["live_dataset_coverage"].create(bind=op.get_bind(), checkfirst=False)


def downgrade() -> None:
    Base.metadata.tables["live_dataset_coverage"].drop(bind=op.get_bind(), checkfirst=False)
