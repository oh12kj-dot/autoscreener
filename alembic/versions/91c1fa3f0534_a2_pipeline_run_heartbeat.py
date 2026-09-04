"""A-2(docs/racr_wp_a_operational_safety_2026-09-04.md、監査§10.2/10.3/10.4):
pipeline_runs.last_heartbeat_at

2026-09-03のgate stage FK違反はrun全体を包む outer try/finally が無く、
`pipeline_runs.status` が `running` のまま永久に残った。API層(§4.3)の
`_pipeline_is_orphaned` は `started_at` からの経過時間だけで孤児を推測して
おり、DBの状態そのものは書き換えない(表示専用)。A-2はDB側に実際の
生存確認(heartbeat)を持たせ、`sweep_orphan_runs()` が一定時間heartbeatの
進まない `running` runを `aborted` へ確定できるようにする。

`last_heartbeat_at` は `started_at` と同じ値で初期化し(既存行を「起動時刻を
最後のheartbeatとみなす」という保守的な扱いにする)、以後は
`PipelineRecorder.heartbeat()` が工程境界ごとに更新する。

Revision ID: 91c1fa3f0534
Revises: 2c4e6f8a1b3d
"""

from alembic import op
import sqlalchemy as sa


revision = "91c1fa3f0534"
down_revision = "2c4e6f8a1b3d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pipeline_runs",
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    # 既存行(過去の実行記録)は「起動時刻を最後の生存確認とみなす」。
    # 新規の `running` running行がまだあれば、次のsweeperで
    # `started_at` からの経過時間として扱われることになり、既存の
    # API層の孤児判定(§4.3、6時間閾値)と矛盾しない初期値になる。
    op.execute("UPDATE pipeline_runs SET last_heartbeat_at = started_at")


def downgrade() -> None:
    op.drop_column("pipeline_runs", "last_heartbeat_at")
