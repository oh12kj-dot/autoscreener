"""K-9:名前付きLLM接続プロファイル(llm_connections)。

`ui_llm_provider_selection_2026-08-30.md`。provider / base_url / model / APIキーを
名前付きで何件でも保存し、1件をアクティブにする。アクティブな行が
`config/collection.yaml` / `.env` の上に重なる(CLIも同じ解決)。

前段の `app_settings`(単一スロットの上書き)はこのプロファイル方式に置き換える。
`app_settings` は本セッションで追加したばかりで LLM 設定専用だったので、ここで
drop する(downgrade で作り直す)。

`api_key` は平文で入る(`.env` と同じ扱い)。API は本体を返さない。
`is_active` は `WHERE is_active` 付き部分ユニークで最大1件に制限する。

**この表はゲートにもスコアにも影響しない。**

Revision ID: f7a1c3e9b5d2
Revises: e4f9a3d2c7b1
Create Date: 2026-08-30 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f7a1c3e9b5d2"
down_revision: Union[str, Sequence[str], None] = "e4f9a3d2c7b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "llm_connections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=80), nullable=False, unique=True),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=True),
        sa.Column("model", sa.String(length=120), nullable=True),
        sa.Column("effort", sa.String(length=10), nullable=True),
        sa.Column(
            "send_effort", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        # 平文。API は返さない。
        sa.Column("api_key", sa.Text(), nullable=True),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "uq_llm_connections_active",
        "llm_connections",
        ["is_active"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )

    op.drop_table("app_settings")


def downgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=80), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )
    op.drop_index("uq_llm_connections_active", table_name="llm_connections")
    op.drop_table("llm_connections")
