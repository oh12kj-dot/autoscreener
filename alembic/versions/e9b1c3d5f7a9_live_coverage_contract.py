"""Add truthful Live Intelligence coverage attempt metadata.

Revision ID: e9b1c3d5f7a9
Revises: c9f5a7b2d4e6
"""

from alembic import op
import sqlalchemy as sa


revision = "e9b1c3d5f7a9"
down_revision = "c9f5a7b2d4e6"
branch_labels = None
depends_on = None


_STATUSES = "'not_collected', 'collected_no_finding', 'collected_with_data', 'collection_failed', 'not_applicable'"


def upgrade() -> None:
    # Earlier revisions intentionally create tables from metadata.  That made a
    # fresh install see the current columns before this revision runs, whereas
    # an upgraded existing database does not.  Introspection keeps both paths
    # valid and is safe to rerun after an interrupted local verification.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    coverage_columns = {column["name"] for column in inspector.get_columns("live_dataset_coverage")}
    for column in (
        sa.Column("reason_code", sa.String(length=80), nullable=True),
        sa.Column("reason_detail", sa.Text(), nullable=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_scope", sa.Text(), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=True),
    ):
        if column.name not in coverage_columns:
            op.add_column("live_dataset_coverage", column)
    check_names = {item.get("name") for item in inspector.get_check_constraints("live_dataset_coverage")}
    if "ck_live_dataset_coverage_status" not in check_names:
        op.create_check_constraint("ck_live_dataset_coverage_status", "live_dataset_coverage", f"coverage_status IN ({_STATUSES})")
    op.execute("UPDATE live_dataset_coverage SET attempted_at = observed_at WHERE attempted_at IS NULL")
    capital_columns = {column["name"] for column in inspector.get_columns("capital_allocation_events")}
    if "source_excerpt" not in capital_columns:
        op.add_column("capital_allocation_events", sa.Column("source_excerpt", sa.Text(), nullable=True))
    if "content_hash" not in capital_columns:
        op.add_column("capital_allocation_events", sa.Column("content_hash", sa.String(length=64), nullable=True))
    unique_names = {item.get("name") for item in inspector.get_unique_constraints("capital_allocation_events")}
    if "uq_capital_allocation_event_evidence" not in unique_names:
        op.create_unique_constraint("uq_capital_allocation_event_evidence", "capital_allocation_events",
            ["ticker_id", "source_accession", "event_type", "content_hash"])


def downgrade() -> None:
    op.drop_constraint("uq_capital_allocation_event_evidence", "capital_allocation_events", type_="unique")
    op.drop_column("capital_allocation_events", "content_hash")
    op.drop_column("capital_allocation_events", "source_excerpt")
    op.drop_constraint("ck_live_dataset_coverage_status", "live_dataset_coverage", type_="check")
    for name in ("retryable", "source_scope", "attempted_at", "reason_detail", "reason_code"):
        op.drop_column("live_dataset_coverage", name)
