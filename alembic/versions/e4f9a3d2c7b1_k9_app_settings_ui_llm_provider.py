"""K-9:UIから編集できる実行時設定(app_settings)。

`docs/ui_llm_provider_selection_2026-08-30.md`。LLMの接続先(provider / base_url)・
モデル・effort・APIキーを、UI から保存できるようにするための1枚テーブル。

**コミット済みの `config/collection.yaml` / `.env` は既定値の唯一の出所で
あり続ける。** ここに行があればその上に重ねるだけで、行が無ければ何も
起きない(CLIやテストが app_settings を要求しない)。

`secret.*` は平文で入る(`.env` と同じ扱い)。`GET /api/v1/llm/settings` は
本体を返さず「設定済みか」のブール値だけを出す。

**この表はゲートにもスコアにも影響しない。** LLMの接続先を選ぶだけで、
`llm_analyses` への隔離(K-9)はそのまま。

Revision ID: e4f9a3d2c7b1
Revises: d2c7b1e4f9a3
Create Date: 2026-08-30 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e4f9a3d2c7b1"
down_revision: Union[str, Sequence[str], None] = "d2c7b1e4f9a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_table("app_settings")
