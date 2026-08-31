"""TENX investment decision v2 live-intelligence store.

Revision ID: b8e4f6a1c2d3
Revises: f7a1c3e9b5d2
Create Date: 2026-08-31
"""

from alembic import op

from autoscreener.db.models import Base

revision = "b8e4f6a1c2d3"
down_revision = "f7a1c3e9b5d2"
branch_labels = None
depends_on = None

TABLES = (
    "delisting_events",
    "analyst_consensus_snapshots",
    "management_guidance_snapshots",
    "market_opportunity_estimates",
    "market_opportunity_components",
    "operating_kpi_definitions",
    "operating_kpi_observations",
    "capital_allocation_events",
    "management_incentive_snapshots",
    "debt_instruments",
    "liquidity_facilities",
    "thesis_milestones",
    "macro_exposure_snapshots",
)


def upgrade() -> None:
    bind = op.get_bind()
    for name in TABLES:
        Base.metadata.tables[name].create(bind=bind, checkfirst=False)


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(TABLES):
        Base.metadata.tables[name].drop(bind=bind, checkfirst=False)
