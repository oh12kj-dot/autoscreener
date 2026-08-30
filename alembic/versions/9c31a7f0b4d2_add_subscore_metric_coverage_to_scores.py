"""add subscore_metric_coverage to scores

Revision ID: 9c31a7f0b4d2
Revises: 805471bbebdb
Create Date: 2026-08-24 20:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '9c31a7f0b4d2'
down_revision: Union[str, Sequence[str], None] = '805471bbebdb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'scores',
        sa.Column('subscore_metric_coverage', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('scores', 'subscore_metric_coverage')
