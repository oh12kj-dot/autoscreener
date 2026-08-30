"""store model inputs on scores so any horizon/target can be recomputed exactly

要件定義書27.24。`scores.inputs` に `MoicInputs` とセクター中央値を保存し、
APIが任意のホライズン・目標倍率(「3年で3倍」等)で厳密に再計算できるようにする。

Revision ID: d5a91c3e6b74
Revises: c41f8a2e07b3
Create Date: 2026-08-25

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d5a91c3e6b74"
down_revision: str | Sequence[str] | None = "c41f8a2e07b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("scores", sa.Column("inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("scores", "inputs")
