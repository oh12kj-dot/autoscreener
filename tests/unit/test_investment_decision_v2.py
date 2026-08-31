import datetime

import pytest
from fastapi.testclient import TestClient

from autoscreener.api.main import app
from autoscreener.batch.collect_consensus import collect_consensus
from autoscreener.collectors.consensus import ConsensusSnapshot, YfinanceConsensusProvider
from autoscreener.config import load_scoring_config
from autoscreener.db.models import AnalystConsensusSnapshot, Ticker
from autoscreener.db.session import session_scope
from autoscreener.scoring.investment_intelligence import (
    calculate_reinvestment_quality, jpy_after_tax_return, risk_sizing_preview,
)
from autoscreener.scoring.model_router import CompanyModelProfile, classify_model_family
from autoscreener.scoring.moic import MoicInputs
from autoscreener.scoring.reverse_valuation import solve_implied_growth
from autoscreener.screening.accounting_quality import calculate_accounting_quality
from autoscreener.screening.investment_intelligence_extract import (
    extract_debt_maturities, extract_operating_kpis,
)


def _inputs(**overrides):
    values = dict(market_cap=500_000_000, net_debt=0, revenue_latest=200_000_000,
        gross_profit_latest=100_000_000, revenue_cagr=0.25, revenue_yoy=0.25,
        revenue_growth_volatility=0.1, gross_margin_latest=0.5, gross_margin_prior=0.5,
        dilution_cagr=0.0, piotroski_ratio=0.7, cash_runway_quarters=12,
        equity_to_assets=0.5, fcf_margin=0.05, sector="Technology")
    values.update(overrides)
    return MoicInputs(**values)


def test_reverse_valuation_is_monotonic_in_required_return():
    config = load_scoring_config()
    low = solve_implied_growth(_inputs(), config, 0.10)
    high = solve_implied_growth(_inputs(), config, 0.25)
    assert low.feasible and high.feasible
    assert high.implied_revenue_cagr > low.implied_revenue_cagr


def test_reverse_valuation_reports_infeasible_instead_of_unbounded_growth():
    result = solve_implied_growth(_inputs(market_cap=10**15), load_scoring_config(), 0.30)
    assert not result.feasible
    assert result.implied_revenue_cagr is None


def test_reinvestment_metrics_keep_company_growth_and_per_share_growth_separate():
    result = calculate_reinvestment_quality(years=3, revenue_start=100, revenue_end=200,
        gross_profit_start=50, gross_profit_end=100, fcf_start=10, fcf_end=20,
        shares_start=10, shares_end=20, nopat_start=8, nopat_end=16,
        invested_capital_start=50, invested_capital_end=90)
    assert result.revenue_cagr > 0
    assert result.revenue_per_share_cagr == pytest.approx(0)
    assert result.incremental_roic == pytest.approx(0.2)


def test_risk_sizing_never_expands_hard_cap():
    result = risk_sizing_preview(per_position_cap=0.04, liquidity_cap=0.03,
        realized_vol=1.2, target_vol=0.60, correlation_factor=0.7,
        sector_factor=1.0, uncertainty_factor=0.75)
    assert result.recommended_cap < result.base_cap == 0.03


def test_nisa_jpy_scenario_has_no_capital_gain_tax():
    taxable = jpy_after_tax_return(usd_moic=2, entry_usdjpy=150, exit_usdjpy=150, account_type="taxable")
    nisa = jpy_after_tax_return(usd_moic=2, entry_usdjpy=150, exit_usdjpy=150, account_type="NISA")
    assert nisa["jpy_after_tax_moic"] == pytest.approx(2)
    assert taxable["jpy_after_tax_moic"] < nisa["jpy_after_tax_moic"]


def test_model_router_classifies_special_models_without_scoring_them():
    route = classify_model_family(CompanyModelProfile(sector="Financial Services", industry="Banks"))
    assert route.model_family == "bank"
    assert not route.supported


def test_accounting_quality_preserves_missing_data_and_warns_on_cash_conversion():
    quality = calculate_accounting_quality(net_income=100, operating_cash_flow=50,
        average_assets=None, revenue_growth=0.1, receivables_growth=None,
        inventory_growth=None, stock_based_compensation=None, revenue=1000,
        goodwill=None, total_assets=None)
    assert quality.accrual_ratio is None
    assert "weak_cash_conversion" in quality.warnings


def test_filing_extractors_keep_source_excerpt():
    kpis = extract_operating_kpis("Net revenue retention was 118% and ARR was $420 million.")
    assert {item.code for item in kpis} == {"nrr", "arr"}
    assert next(item for item in kpis if item.code == "nrr").value == pytest.approx(1.18)
    debts = extract_debt_maturities("$190 million of senior notes mature in 2028.")
    assert debts[0].principal == 190_000_000
    assert debts[0].maturity_year == 2028


def test_filing_extractors_ignore_punctuation_that_is_not_a_number():
    assert extract_operating_kpis("We serve enterprise, customers across many markets.") == []


def test_yfinance_consensus_uses_annual_rows_only():
    class EstimateTable:
        empty = False

        def iterrows(self):
            return iter((
                ("0q", {"avg": 10, "low": 9, "high": 11, "numberOfAnalysts": 3}),
                ("+1q", {"avg": 12, "low": 11, "high": 13, "numberOfAnalysts": 3}),
                ("0y", {"avg": 40, "low": 38, "high": 42, "numberOfAnalysts": 5}),
                ("+1y", {"avg": 50, "low": 48, "high": 52, "numberOfAnalysts": 5}),
            ))

    class ProviderTicker:
        revenue_estimate = EstimateTable()
        earnings_estimate = None
        info = {}

    observed_at = datetime.datetime(2026, 8, 31, tzinfo=datetime.timezone.utc)
    rows = YfinanceConsensusProvider(lambda _: ProviderTicker()).fetch("TEST", observed_at)

    assert [row.raw_payload["provider_period"] for row in rows] == ["0y", "+1y"]
    assert len({(row.source, row.period_end) for row in rows}) == len(rows)


def test_consensus_conflict_is_isolated_and_recorded():
    symbol = "ZZCONFLICT"
    observed_at = datetime.datetime(2097, 7, 1, tzinfo=datetime.timezone.utc)

    class ConflictingProvider:
        name = "test-conflict"

        def fetch(self, ticker, as_of):
            common = dict(
                observed_at=as_of,
                source=self.name,
                period_type="FY",
                period_end=datetime.date(2097, 12, 31),
            )
            return [
                ConsensusSnapshot(**common, revenue_mean=100),
                ConsensusSnapshot(**common, revenue_mean=200),
            ]

    with session_scope() as session:
        old = session.query(Ticker).filter_by(symbol=symbol).one_or_none()
        if old:
            session.query(AnalystConsensusSnapshot).filter_by(ticker_id=old.id).delete()
            session.delete(old)
        session.add(Ticker(symbol=symbol, market="US", sector="Technology"))

    try:
        stats = collect_consensus(ConflictingProvider(), as_of=observed_at, symbols=[symbol])
        assert stats["processed"] == 1
        assert stats["failed"] == 1
        assert stats["inserted"] == 0
        with session_scope() as session:
            ticker = session.query(Ticker).filter_by(symbol=symbol).one()
            rows = session.query(AnalystConsensusSnapshot).filter_by(ticker_id=ticker.id).all()
            assert len(rows) == 1
            assert rows[0].coverage_status == "collection_failed"
            assert rows[0].raw_payload["error_type"] == "ValueError"
    finally:
        with session_scope() as session:
            ticker = session.query(Ticker).filter_by(symbol=symbol).one_or_none()
            if ticker:
                session.query(AnalystConsensusSnapshot).filter_by(ticker_id=ticker.id).delete()
                session.delete(ticker)


def test_consensus_endpoint_is_point_in_time():
    symbol = "ZZPITV2"
    with session_scope() as session:
        old = session.query(Ticker).filter_by(symbol=symbol).one_or_none()
        if old:
            session.query(AnalystConsensusSnapshot).filter_by(ticker_id=old.id).delete()
            session.delete(old)
        ticker = Ticker(symbol=symbol, market="US", sector="Technology")
        session.add(ticker); session.flush()
        for day, value in ((30, 120.0), (31, 180.0)):
            observed = datetime.datetime(2026, 6, day, 12, tzinfo=datetime.timezone.utc) if day == 30 else datetime.datetime(2026, 7, 1, 12, tzinfo=datetime.timezone.utc)
            session.add(AnalystConsensusSnapshot(ticker_id=ticker.id, observed_at=observed,
                source="test", period_type="FY", period_end=datetime.date(2026, 12, 31),
                revenue_mean=value, raw_payload={}, coverage_status="collected_with_data",
                confidence="high", content_hash=f"h-{day}"))
    try:
        body = TestClient(app).get(f"/api/v1/candidates/{symbol}/consensus?as_of=2026-06-30").json()
        assert len(body["data"]) == 1
        assert float(body["data"][0]["revenue_mean"]) == 120.0
    finally:
        with session_scope() as session:
            ticker = session.query(Ticker).filter_by(symbol=symbol).one_or_none()
            if ticker:
                session.query(AnalystConsensusSnapshot).filter_by(ticker_id=ticker.id).delete()
                session.delete(ticker)
