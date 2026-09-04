"""Point-in-time input builder for Model v5."""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy.orm import Session

from autoscreener.db.models import PriceSnapshot, RawSnapshot, Ticker, UniverseSnapshot
from autoscreener.scoring.engine import build_inputs_for_ticker
from autoscreener.scoring.moic import MoicInputs
from autoscreener.scoring.v5.path_risk import PriceObservation
from autoscreener.screening.financial_history import FinancialPeriod, build_financial_history
from autoscreener.validation.rules import sanitize_info


@dataclass(frozen=True)
class V5PitInput:
    ticker_id: int
    symbol: str
    as_of: datetime.date
    moic_inputs: MoicInputs | None
    raw_snapshot_id: int | None
    raw_available_from: datetime.date | None
    price_as_of: datetime.date | None
    input_status: str
    # Model v5 Phase 4 (docs/model_v5_phase4_handoff_2026-09-03.md 4.2): the
    # same raw_snapshots.payload already loaded above, reduced to annual
    # FinancialPeriod rows visible as of ``as_of``. Keeping the PIT filter
    # (period_end <= as_of) here means quality.py never re-derives it and
    # never re-queries raw_snapshots.
    financial_annual: tuple[FinancialPeriod, ...] = ()
    currency_conversion_unavailable: bool = False
    # WP-D (docs/racr_wp_d_reliability_layer_2026-09-04.md): universal,
    # per-ticker evidence-quantity/quality signals for
    # ``reliability.core_evidence_reliability`` -- available for every
    # ticker regardless of optional Live Intelligence coverage, unlike the
    # growth/quality/capital/tail signals which are gated at 0.24%-25%
    # universe coverage. Deliberately NOT ``available_from`` (WP-D trap 1:
    # that is the ingestion date, near-constant across the universe, not a
    # freshness signal).
    sector: str | None = None
    price_row_count: int = 0
    price_first_date: datetime.date | None = None
    raw_is_valid: bool = True
    raw_validation_error_count: int = 0
    # WP-F1 (docs/racr_wp_f1_path_risk_2026-09-04.md): the same PIT-filtered
    # ``price_snapshots`` rows already queried below (``trade_date <= as_of``),
    # reduced to the three fields ``path_risk.py`` needs. Carried here so
    # ``path_risk.estimate_path_risk`` never re-queries the DB and can never
    # see a price row this PIT filter would have excluded -- single source
    # of truth for "which prices this ticker's run may see" (same rationale
    # as ``financial_annual`` above).
    price_observations: tuple[PriceObservation, ...] = ()

    def evidence(self) -> dict:
        return {
            "as_of": self.as_of.isoformat(),
            "raw_snapshot_id": self.raw_snapshot_id,
            "raw_available_from": self.raw_available_from.isoformat() if self.raw_available_from else None,
            "price_as_of": self.price_as_of.isoformat() if self.price_as_of else None,
            "input_status": self.input_status,
            "pit_rules": {
                "raw_snapshot": "available_from <= as_of",
                "price": "trade_date <= as_of",
                "financial_annual": "period_end <= as_of",
            },
            "financial_annual_periods": len(self.financial_annual),
            "currency_conversion_unavailable": self.currency_conversion_unavailable,
        }


def build_v5_pit_inputs(session: Session, *, as_of: datetime.date) -> list[V5PitInput]:
    """Build exactly the universe visible on ``as_of`` without current-value fallback."""

    tickers = (
        session.query(Ticker)
        .join(UniverseSnapshot, UniverseSnapshot.ticker_id == Ticker.id)
        .filter(UniverseSnapshot.snapshot_date == as_of, UniverseSnapshot.included.is_(True))
        .order_by(Ticker.symbol)
        .all()
    )
    built: list[V5PitInput] = []
    for ticker in tickers:
        raw = (
            session.query(RawSnapshot)
            .filter(RawSnapshot.ticker_id == ticker.id, RawSnapshot.available_from <= as_of)
            .order_by(RawSnapshot.available_from.desc(), RawSnapshot.snapshot_date.desc(), RawSnapshot.id.desc())
            .first()
        )
        prices = (
            session.query(PriceSnapshot)
            .filter(PriceSnapshot.ticker_id == ticker.id, PriceSnapshot.trade_date <= as_of)
            .order_by(PriceSnapshot.trade_date.asc())
            .all()
        )
        price_as_of = prices[-1].trade_date if prices else None
        price_first_date = prices[0].trade_date if prices else None
        price_observations = tuple(
            PriceObservation(
                trade_date=row.trade_date,
                close=float(row.close) if row.close is not None else None,
                dividend=float(row.dividend) if row.dividend is not None else None,
            )
            for row in prices
        )
        if raw is None:
            built.append(V5PitInput(
                ticker.id, ticker.symbol, as_of, None, None, None,
                price_as_of, "not_collected",
                sector=ticker.sector, price_row_count=len(prices),
                price_first_date=price_first_date,
                price_observations=price_observations,
            ))
            continue
        info = sanitize_info(raw.payload.get("info") or {})
        sector = info.get("sector") or ticker.sector
        inputs = build_inputs_for_ticker(raw.payload, prices, as_of, sector)
        history = build_financial_history(raw.payload)
        financial_annual = tuple(
            period for period in history.annual if period.period_end <= as_of
        )
        built.append(V5PitInput(
            ticker.id,
            ticker.symbol,
            as_of,
            inputs,
            raw.id,
            raw.available_from,
            price_as_of,
            "collected_with_data" if inputs is not None else "collected_no_finding",
            financial_annual,
            history.currency_conversion_unavailable,
            sector=sector,
            price_row_count=len(prices),
            price_first_date=price_first_date,
            raw_is_valid=bool(raw.is_valid),
            raw_validation_error_count=len(raw.validation_errors or []),
            price_observations=price_observations,
        ))
    return built
