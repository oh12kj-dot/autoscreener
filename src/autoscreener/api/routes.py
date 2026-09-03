"""API v1 ルーティング(6.5)。

- `GET /candidates`:ランキング一覧(フィルタ・ページネーション、`date`指定で過去日も取得可能)
- `GET /candidates/{ticker}`:個別詳細・スコア履歴・除外理由
- `GET /universe/status`:直近の収集・ゲート・スコアリングの実行状況
- `GET /pipeline/runs`・`GET /pipeline/runs/{run_id}`:日次ジョブの実行履歴・
  工程詳細(14.15、docs/daily_job_status_screen_2026-08-30.md)。読み取り専用(18.6)。
- `GET /excluded`:除外銘柄の検索(14.16:除外銘柄確認画面用)
- `GET /scores/dates`:スコアが存在する日付一覧(順位変動画面が比較対象日を選ぶために使う)
- `GET /watchlist`:Tier 2(監視対象)一覧(15.5の二層構成。ランキングに出ないが追跡する価値のある銘柄)
- `GET /backtest/latest`:直近の擬似バックテスト結果(27.8。モデルの検証状況をUIに常時出すため)
- `GET /llm/report`・`GET /llm/{ticker}`:LLM(Claude)による定性分析の**参照**(K-9)。
  生成はCLIのみ——HTTPリクエスト1本で課金が発生する導線を作らないため。
  この出力はゲートにもスコアにも入らない(`src/autoscreener/llm/__init__.py`)。
"""

from __future__ import annotations

import datetime
import math
import threading
import time
import uuid
from dataclasses import dataclass
from statistics import NormalDist
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from autoscreener.api.dependencies import get_session
from autoscreener.api.metric_explain import build_factor_breakdown
from autoscreener.coverage import CoverageReasonCode, CoverageStatus, latest_dataset_coverage
# K-9(ui_llm_provider_selection):UIから唯一の書き込み(レポート生成・LLM設定)は、
# 読み取り専用の API セッションではなくバッチ層の書き込みエンジンを通す。
from autoscreener.db.session import session_scope as _write_session_scope
from autoscreener.api.schemas import (
    TICKER_PATTERN,
    BacktestCalibrationBin,
    BacktestDecile,
    BacktestPerDate,
    BacktestSummary,
    BacktestTailLift,
    CalendarEvent,
    CalendarResponse,
    CompanyProfile,
    OfficerView,
    PriceRiskView,
    EvidenceGradeView,
    EarningsHistoryView,
    EarningsPeriodView,
    CustomerConcentrationView,
    GuidanceView,
    LitigationView,
    PeerResponse,
    PeerView,
    BenchmarkReferenceResponse,
    CorrelationView,
    FinancialHistoryDerivedView,
    FinancialHistoryResponse,
    FinancialPeriodView,
    FxRateResponse,
    PiotroskiCriterionView,
    PortfolioOutlook,
    TargetSpec,
    CandidateDetail,
    CandidateListResponse,
    CandidateSummary,
    ExcludedListResponse,
    ExcludedTicker,
    AlertsResponse,
    AlertView,
    DilutionOutlook,
    FilingListItem,
    FilingListResponse,
    FilingRef,
    GenerateReportRequest,
    GenerateReportResult,
    LlmConnectionCreate,
    LlmConnectionUpdate,
    LlmConnectionView,
    LlmConnectionsResponse,
    LlmFilingSummaryView,
    LlmProviderInfo,
    LlmProvidersResponse,
    LlmSettingsResponse,
    LlmQualitativeView,
    LlmReportResponse,
    LlmSourceRef,
    LlmTickerAnalysisResponse,
    LlmUsageView,
    MacroResponse,
    MacroSeriesPoint,
    MacroSeriesView,
    MonitoringMetricView,
    NextTrim,
    PipelineHealthFinding,
    PipelineRunDetail,
    PipelineRunListResponse,
    PipelineRunSummary,
    PipelineStageView,
    PortfolioSummary,
    PositionsResponse,
    PositionView,
    ReconciliationItemView,
    RedFlagView,
    ResearchNoteResponse,
    ScoreDatesResponse,
    ScoreHistoryPoint,
    SupplyView,
    UniverseStatusResponse,
    WatchlistEntry,
    WatchlistResponse,
    InvestmentIntelligenceResponse,
    ReverseValuationResponse,
    ReverseValuationScenarioView,
    DataCoverageResponse,
    DataCoverageRow,
    ModelV5RunView,
    ModelV5ScoreDetail,
    ModelV5ScoreListResponse,
    ModelV5ObjectiveDefinitionView,
    ModelV5ObjectivesResponse,
    ModelV5ScoreSummary,
    ModelV5ValidationStatusResponse,
    ModelV5ObjectiveScoreView,
)
from autoscreener.config import (
    ScoringConfig,
    UniverseCeilings,
    UniverseConfig,
    get_settings,
    load_execution_config,
    load_fred_config,
    load_monitoring_config,
    load_objectives_config,
    load_portfolio_config,
    load_positions_config,
    load_scoring_config,
    load_universe_config,
)
from autoscreener.scoring.engine import result_to_factors
from autoscreener.backtest.benchmark import rolling_moic_quantiles
from autoscreener.scoring.portfolio import pairwise_return_correlation, portfolio_outcome
from autoscreener.scoring.moic import (
    CrossSection,
    MoicInputs,
    build_cross_section,
    compute_moic,
    moic_quantiles,
)
from autoscreener.dates import business_days_between, utc_today
from autoscreener.db.models import (
    Alert,
    BacktestRun,
    CollectionLog,
    EventCalendar,
    Filing,
    FilingSection,
    InsiderTransaction,
    LlmAnalysis,
    LlmConnection,
    MacroSeries,
    PipelineRun,
    PipelineStageRun,
    ShortInterest,
    PriceSnapshot,
    CustomerConcentration,
    Guidance,
    LitigationEvent,
    RawSnapshot,
    Score,
    Ticker,
    UniverseSnapshot,
    XbrlFact,
    AnalystConsensusSnapshot,
    ManagementGuidanceSnapshot,
    MarketOpportunityEstimate,
    MarketOpportunityComponent,
    OperatingKpiDefinition,
    OperatingKpiObservation,
    CapitalAllocationEvent,
    ManagementIncentiveSnapshot,
    DebtInstrument,
    LiquidityFacility,
    ThesisMilestone,
    MacroExposureSnapshot,
    DelistingEvent,
    LiveDatasetCoverage,
    ModelRun,
    ModelScore,
    ModelV5ForwardReturn,
    ObjectiveScore,
)
from autoscreener.scoring.v5.feature_registry import FEATURES_BY_KEY
from autoscreener.research.notes import load_all_notes, load_note
from autoscreener.pipeline_stages import PIPELINE_STAGE_COUNT
from autoscreener.screening.dilution_outlook import (
    FilingRefView,
    NoteDilutionInputs,
    compute_dilution_outlook,
)
from autoscreener.screening.exclusion_gates import normalize_financial_currency_value
from autoscreener.screening.earnings_history import build_earnings_history
from autoscreener.screening.evidence_grade import compute_evidence_grade
from autoscreener.screening.financial_history import build_financial_history
from autoscreener.screening.liquidity import ADV_WINDOW_DAYS, LiquidityProfile, compute_execution_diagnostics, compute_liquidity_profile
from autoscreener.screening.peers import PeerCandidate, select_peers
from autoscreener.screening.price_risk import compute_price_risk
from autoscreener.screening.price_range import compute_price_range
from autoscreener.screening.monitoring_metrics import MonitoringThresholds, evaluate_monitoring
from autoscreener.screening.red_flags import evaluate_red_flags, filing_to_view
from autoscreener.validation.reconciliation import (
    MAGNITUDE_MISMATCH,
    MISMATCH,
    XbrlFactView,
    reconcile,
)
from autoscreener.validation.xbrl_facts import tag_to_concept
from autoscreener.screening.trading_cost import corwin_schultz_spread, round_trip_cost_bps
from autoscreener.screening.tradability import (
    TRADABLE,
    TradabilityResult,
    evaluate_tradability,
    get_cached_broker_coverage,
)
from autoscreener.screening.watchlist import REASON_LABELS, GateOutcome, build_tier2
from autoscreener.scoring.reverse_valuation import solve_scenarios
from autoscreener.scoring.model_router import CompanyModelProfile, classify_model_family
from autoscreener.scoring.investment_intelligence import (
    calculate_reinvestment_quality,
    jpy_after_tax_return,
    return_distribution,
    risk_sizing_preview,
)
from autoscreener.screening.accounting_quality import calculate_accounting_quality

router = APIRouter(prefix="/api/v1")

_NORMAL_DIST = NormalDist()

# 18.5:1リクエストで返す最大件数の上限(過大なlimitによる負荷を防止)
_MAX_LIMIT = 200
_DEFAULT_LIMIT = 50

# スコア履歴として1銘柄あたり返す最大件数
_SCORE_HISTORY_LIMIT = 30

# J-9:保有と重複チェックするランキング上位の件数
_RANKING_OVERLAP_TOP_N = 20

# 任意ホライズンの下限・上限(27.24)。
# 下限1年:これより短いと株価は決算よりセンチメントで動き、本モデルの前提
# (ファンダメンタルズの外挿)が成り立たない。
# 上限15年:年次決算は最大5期しか取れず(13.1)、3〜4年の実績から15年を外挿する
# のは無理がある。実測でも15年では既定7年との順位相関が+0.848まで落ちる(27.23)。
_MIN_HORIZON_YEARS = 1.0
_MAX_HORIZON_YEARS = 15.0
_MIN_TARGET_MOIC = 1.5
_MAX_TARGET_MOIC = 100.0


@dataclass(frozen=True)
class _ScoreView:
    """ある目標(何年で何倍)における1銘柄の値。保存済みか再計算かを問わない。"""

    probability: float
    expected_moic: float | None
    median_moic: float | None
    survival_probability: float | None
    calibrated_on_pace_probability: float | None = None
    # C-1(2026-08-26、docs/model_audit_v4_2026-08-26.md):下振れ確率の算出に使う。
    log_moic_mu: float | None = None
    log_moic_sigma: float | None = None
    # C-4:警告バッジの算出に使う診断フラグ群(result_to_factorsと同じ内容)。
    factors: dict | None = None
    # A-1(docs/defect_and_edge_audit_2026-08-28.md D-12):このスコアが読んだデータの日付。
    price_as_of: datetime.date | None = None
    financials_as_of: datetime.date | None = None


def _resolve_target(
    scoring_config: ScoringConfig,
    universe_config: UniverseConfig,
    horizon_years: float | None,
    target_moic: float | None,
) -> tuple[ScoringConfig, TargetSpec, UniverseCeilings]:
    """リクエストで指定された「何年で何倍」を反映した設定を作る(27.24)。

    どちらも未指定(または設定値と同じ)なら `is_default=True` を返し、呼び出し元は
    保存済みの確率をそのまま使える(再計算しない)。

    成長経路・希薄化の複利・生存確率は「年」単位で積むため、端数のホライズンは
    最も近い整数年に丸める。年次決算しか無いこのモデルに、0.5年刻みの精度は
    そもそも存在しない。

    **29章:規模の上限も一緒に決まる。** 「大きすぎる企業は算数上10倍になれない」
    (15.6)という除外の根拠は10倍という目標に依存しているため、目標を緩めたら
    上限も緩めなければ筋が通らない。バッチは最も緩い目標の母集団を materialize
    しており、ここで返す `UniverseCeilings` がその中から当該目標の分を切り出す。
    """
    default_horizon = float(scoring_config.horizon_years)
    effective_horizon = horizon_years if horizon_years is not None else default_horizon
    effective_target = target_moic if target_moic is not None else scoring_config.target_moic
    rounded_horizon = max(1, round(effective_horizon))

    is_default = rounded_horizon == scoring_config.horizon_years and (
        effective_target == scoring_config.target_moic
    )
    adjusted = scoring_config.model_copy(
        update={"horizon_years": rounded_horizon, "target_moic": effective_target}
    )
    ceilings = universe_config.ceilings_for_target(effective_target)
    spec = TargetSpec(
        horizon_years=rounded_horizon,
        target_moic=effective_target,
        required_cagr=effective_target ** (1 / rounded_horizon) - 1,
        is_default=is_default,
        market_cap_ceiling=ceilings.market_cap_usd,
        revenue_ceiling=ceilings.revenue_usd,
        universe_ceiling_capped=ceilings.widening_capped,
    )
    return adjusted, spec, ceilings


def _scale_from_info(info: dict) -> tuple[float | None, float | None]:
    """規模ゲートが見る2値(時価総額, 取引通貨建てのTTM売上高)を `info` から取る。"""
    return info.get("marketCap"), normalize_financial_currency_value(info.get("totalRevenue"), info)


def _scale_by_ticker(session: Session) -> dict[int, tuple[float | None, float | None]]:
    """全銘柄の最新スナップショットから規模の2値だけをSQLで抜く(29章)。

    payload 全体(1件あたり約13KB)を読まずに済ませるためのもの。詳細画面が
    断面統計を作り直すとき、母集団の判定に必要なのはこの2値だけである。
    """
    rows = session.execute(
        text(
            """
            SELECT DISTINCT ON (ticker_id)
                   ticker_id,
                   payload->'info'->>'marketCap' AS market_cap,
                   payload->'info'->>'totalRevenue' AS total_revenue,
                   payload->'info'->>'currency' AS currency,
                   payload->'info'->>'financialCurrency' AS financial_currency,
                   payload->'info'->>'_fx_rate_financial_to_trading' AS fx_rate
            FROM raw_snapshots
            ORDER BY ticker_id, snapshot_date DESC
            """
        )
    ).all()

    def _as_float(value: str | None) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except ValueError:
            return None

    scale: dict[int, tuple[float | None, float | None]] = {}
    for ticker_id, market_cap, total_revenue, currency, financial_currency, fx_rate in rows:
        info = {
            "marketCap": _as_float(market_cap),
            "totalRevenue": _as_float(total_revenue),
            "currency": currency,
            "financialCurrency": financial_currency,
            "_fx_rate_financial_to_trading": _as_float(fx_rate),
        }
        scale[ticker_id] = _scale_from_info(info)
    return scale


def _within_ceilings(
    market_cap: float | None, revenue: float | None, ceilings: UniverseCeilings
) -> bool:
    """規模の2値がその目標の上限に収まっているか(29章)。"""
    if market_cap is not None and market_cap >= ceilings.market_cap_usd:
        return False
    if revenue is not None and revenue >= ceilings.revenue_usd:
        return False
    return True


def _within_target_universe(info: dict, ceilings: UniverseCeilings) -> bool:
    """その目標の母集団に入るか(29章)。`evaluate_gates` の規模ゲートと同じ判定。

    値が欠損している場合に除外しないのは `evaluate_gates` と揃えるため——ただし
    実際には、時価総額・売上高が欠損している銘柄は日次バッチのゲートで
    `missing_market_cap` / `missing_revenue` として既に落ちているので、ここに
    到達するのは両方が揃っている銘柄だけである。
    """
    return _within_ceilings(*_scale_from_info(info), ceilings)


def _target_universe_scores(
    session: Session,
    score_date: datetime.date,
    scoring_config: ScoringConfig,
    ceilings: UniverseCeilings,
) -> list[Score]:
    """`score_date` 時点で、その目標の母集団に入る Score 行(29章)。

    確率が NULL の行(27.20:見通しがマイナス)も含める——σ の縮小中心を
    推定する母集団はランキング対象に限らない(`moic.build_cross_section`)。
    """
    scale = _scale_by_ticker(session)
    rows = (
        session.query(Score)
        .join(
            UniverseSnapshot,
            (UniverseSnapshot.ticker_id == Score.ticker_id)
            & (UniverseSnapshot.snapshot_date == score_date),
        )
        .filter(
            Score.score_date == score_date,
            Score.scoring_version == scoring_config.scoring_version,
            UniverseSnapshot.included.is_(True),
        )
        .all()
    )
    return [row for row in rows if _within_ceilings(*scale.get(row.ticker_id, (None, None)), ceilings)]


def _cross_section_for_target(
    scores: list[Score], config: ScoringConfig
) -> CrossSection | None:
    """その目標の母集団から断面統計を作り直す(29章)。

    **断面統計は母集団の中央値である以上、母集団が変われば変わる。**
    ナウキャストの基準線(市場全体の動き)も σ の縮小中心もそうであり、
    バックテスト(`backtest.runner._evaluate_one_date`)は「検証している目標の
    母集団」から断面を作っている。v4のKPIはその構成で測ったものなので、
    目標を変えたリクエストも同じ構成で計算しなければ、**検証していない設定で
    順位を出す**ことになる。

    `scores` には見通しがマイナスの行(`probability` が NULL)も含めて渡すこと。
    σ の中心を推定する母集団をランキング対象に限らないのは
    `moic.build_cross_section` の設計どおり。
    """
    inputs = [MoicInputs.from_dict(s.inputs) for s in scores if s.inputs]
    if not inputs:
        return None
    return build_cross_section(inputs, config)


def _score_view(
    score: Score,
    config: ScoringConfig,
    target: TargetSpec,
    cross_section: CrossSection | None = None,
) -> _ScoreView | None:
    """既定の目標なら保存値を、変更されていれば保存済み入力から厳密に再計算する。

    **近似で引き伸ばさない理由**(27.24):対数正規を `μ_f = μ·f, σ_f = σ·√f` で
    時間方向に伸ばす手は安いが、本モデルのドリフトは成長減衰により**前倒しに
    効く**ため、短いホライズンでは上昇を過小評価する。入力を保存してあるので、
    その年数で成長経路・希薄化・生存確率を計算し直すほうが正しく、かつ十分速い
    (1銘柄あたり数十回の浮動小数点演算)。
    """
    if target.is_default:
        if score.probability is None:
            return None
        return _ScoreView(
            probability=float(score.probability),
            expected_moic=(score.factors or {}).get("expected_moic"),
            median_moic=float(score.median_moic) if score.median_moic is not None else None,
            survival_probability=(
                float(score.survival_probability) if score.survival_probability is not None else None
            ),
            calibrated_on_pace_probability=(
                float(score.calibrated_on_pace_probability)
                if score.calibrated_on_pace_probability is not None
                else None
            ),
            log_moic_mu=float(score.log_moic_mu) if score.log_moic_mu is not None else None,
            log_moic_sigma=float(score.log_moic_sigma) if score.log_moic_sigma is not None else None,
            factors=score.factors,
            price_as_of=score.price_as_of,
            financials_as_of=score.financials_as_of,
        )

    stored = score.inputs or {}
    # 29章:目標を変えたときは、その目標の母集団から作り直した断面を使う
    # (`_cross_section_for_target`)。保存済みの断面は既定の目標の母集団のもの
    # なので、広い母集団の銘柄にそのまま当てると σ の縮小中心が小型株のものに
    # なる——バックテストで検証していない構成になってしまう。
    if cross_section is None:
        cross_section = CrossSection.from_dict(stored.get("cross_section"))
    if cross_section.sample_size == 0:
        # v3以前に書かれた行(保存形式が違う)。再計算しない——古い形式を
        # 無理に読み替えると、当時とは別の前提で計算した値を「その日のスコア」
        # として出すことになる。
        return None
    result = compute_moic(MoicInputs.from_dict(stored), cross_section, config)
    if result is None:
        # 指定した目標では「見通しがマイナス」になる銘柄(短いホライズンほど
        # 複利が効かず期待倍率が1.0を割りやすい)。順位は付けない。
        return None
    # 較正写像は既定のホライズンで学習したものなので、目標を変えて再計算した
    # 場合には**適用しない**(28.8)。「3年で3倍」に読み替えた確率へ、7年モデルの
    # 1年オンペース較正を当てるのは意味を成さない。
    return _ScoreView(
        probability=result.probability,
        expected_moic=result.expected_moic,
        median_moic=result.median_moic,
        survival_probability=result.survival_probability,
        calibrated_on_pace_probability=None,
        log_moic_mu=result.log_moic_mu,
        log_moic_sigma=result.log_moic_sigma,
        factors=result_to_factors(result),
    )


def _company_name(info: dict) -> str | None:
    return info.get("shortName") or info.get("longName")


def _num_or_none(value: object) -> float | None:
    """`factors` JSONB から数値を安全に取り出す(文字列メタ情報が同居するため)。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _int_or_none(value: object) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _company_profile(
    info: dict, ticker_row: Ticker, profile_as_of: datetime.date | None
) -> CompanyProfile | None:
    """J-1:`raw_snapshots.payload.info` から会社概要を組み立てる。

    `info` が空(185行が該当)なら None を返し、**例外にしない**。
    `industry` / `listed_date` / `cik` は `tickers` を正とし、無ければ `info` で補う。
    """
    if not info:
        # tickers 側の上場情報だけでも出せるなら出す(info 欠損 ≠ 何も無い)。
        if not (ticker_row.listed_date or ticker_row.cik or ticker_row.industry):
            return None
    officers = info.get("companyOfficers") or []
    normalized_officers = [o for o in officers if isinstance(o, dict)] if isinstance(officers, list) else []
    normalized_officers.sort(key=lambda o: _num_or_none(o.get("totalPay")) or 0, reverse=True)
    shares = _num_or_none(info.get("sharesOutstanding"))
    float_shares = _num_or_none(info.get("floatShares"))
    return CompanyProfile(
        business_summary=info.get("longBusinessSummary") or None,
        website=info.get("website") or info.get("irWebsite") or None,
        industry=ticker_row.industry or info.get("industry") or None,
        country=info.get("country") or None,
        full_time_employees=_int_or_none(info.get("fullTimeEmployees")),
        exchange=info.get("exchange") or info.get("fullExchangeName") or None,
        listed_date=ticker_row.listed_date,
        cik=ticker_row.cik,
        profile_as_of=profile_as_of,
        held_percent_insiders=_num_or_none(info.get("heldPercentInsiders")),
        held_percent_institutions=_num_or_none(info.get("heldPercentInstitutions")),
        float_ratio=float_shares / shares if float_shares is not None and shares not in (None, 0) else None,
        officers=[
            OfficerView(name=str(o.get("name") or ""), title=o.get("title"), age=_int_or_none(o.get("age")), total_pay=_num_or_none(o.get("totalPay")))
            for o in normalized_officers[:5] if o.get("name")
        ],
    )


def _price_series(session: Session, ticker_id: int, *, limit: int = 800) -> list[tuple[datetime.date, float]]:
    rows = (
        session.query(PriceSnapshot.trade_date, PriceSnapshot.close)
        .filter(PriceSnapshot.ticker_id == ticker_id, PriceSnapshot.close.isnot(None))
        .order_by(PriceSnapshot.trade_date.desc()).limit(limit).all()
    )
    return [(d, float(c)) for d, c in reversed(rows) if c is not None and float(c) > 0]


def _execution_diagnostics(session: Session, ticker_id: int, max_position_usd: float | None) -> dict[str, float | int | None]:
    rows = (
        session.query(PriceSnapshot.close, PriceSnapshot.volume)
        .filter(PriceSnapshot.ticker_id == ticker_id)
        .order_by(PriceSnapshot.trade_date.desc()).limit(252).all()
    )
    return compute_execution_diagnostics(
        [(float(close) if close is not None else None, volume) for close, volume in rows],
        max_position_usd=max_position_usd,
        adv_participation_cap=load_portfolio_config().adv_participation_cap,
    )


def _summary_evidence_grade(raw: RawSnapshot | None, warnings: list[str], data_age_days: int | None) -> EvidenceGradeView:
    history = build_financial_history(raw.payload if raw else {})
    grade = compute_evidence_grade(
        warnings=warnings, reconciliation=[], annual_period_count=len(history.annual),
        quarterly_period_count=len(history.quarterly), data_age_days=data_age_days,
    )
    return EvidenceGradeView(**vars(grade))


_MOIC_QUANTILE_LEVELS: dict[str, float] = {
    "p10": 0.10,
    "p25": 0.25,
    "p50": 0.50,
    "p75": 0.75,
    "p90": 0.90,
}


def _moic_quantile_map(
    mu: float | None, sigma: float | None, survival: float | None
) -> dict[str, float] | None:
    """J-4:生存確率込みの混合分布から P10〜P90 を出す。生の対数正規から算出
    (較正は掛けない)。入力が欠ければ None。"""
    if mu is None or sigma is None or sigma <= 0 or survival is None:
        return None
    raw = moic_quantiles(mu, sigma, survival, tuple(_MOIC_QUANTILE_LEVELS.values()))
    return {name: raw[level] for name, level in _MOIC_QUANTILE_LEVELS.items()}


def _downside_probability(
    mu: float | None, sigma: float | None, survival: float | None, threshold: float
) -> float | None:
    """C-1(2026-08-26、docs/model_audit_v4_2026-08-26.md): P(MOIC < threshold)。

    `log_moic_mu` / `log_moic_sigma` は保存済みなので新規計算は不要。生存確率が
    低い銘柄は「上場廃止=実現倍率0」も下振れに含める必要があるため、
    (1-survival) を無条件の下振れ確率として足し、生存できた場合の条件付き
    下振れ確率に survival を掛けて合成する。
    """
    if mu is None or sigma is None or sigma <= 0:
        return None
    conditional = _NORMAL_DIST.cdf((math.log(threshold) - mu) / sigma)
    survival_rate = survival if survival is not None else 1.0
    return _clamp01((1.0 - survival_rate) + survival_rate * conditional)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


# C-4(2026-08-26、docs/model_audit_v4_2026-08-26.md):ランキング上位に偏っていた
# クランプ到達・欠損データ・高レバレッジを利用者に見せるための警告バッジ。
# 条件は docs/model_audit_v4_2026-08-26.md の該当項目(S-5〜S-9・A-1)に対応する。
_WARNING_RULES: list[tuple[str, str]] = [
    ("growth_rate_clamped", "初期成長率が上限(または下限)に張り付いています(S-6)。この銘柄の成長力は測れておらず、モデルの外挿限界で丸められた値です。"),
    ("dilution_data_missing", "希薄化データが欠損しているため、断面の中央値で補完しています(A-1)。"),
    ("net_debt_data_missing", "有利子負債または現金の行が財務データに無く、ネットデットをゼロとして計算しています(E-1)。無借金の優良企業として過大評価されている可能性があります。"),
    ("terminal_multiple_capped", "終端EV/粗利が断面由来の上限に当たっています(30.3)。入口のバリュエーションが断面の最上位帯にあり、7年後もその水準が続く前提は取っていません。"),
]


def _compute_warnings(
    factors: dict | None,
    survival_probability: float | None,
    external: list[str] | None = None,
) -> list[str]:
    """30.5.4:`external` は突合(30.5)・レッドフラグ(30.4)由来の警告コード。
    `factors` が無い(見通しマイナス・古い行等)銘柄でも、外部由来の警告は
    出す必要がある——SEC原本との不一致はモデルのランキング結果とは独立した
    事実だから、`factors is None` を理由に握りつぶしてはならない。
    """
    if factors is None:
        return list(external or [])
    warnings: list[str] = list(external or [])
    for key, _ in _WARNING_RULES:
        if factors.get(key, 0.0) and float(factors[key]) >= 1.0:
            warnings.append(key)
    leverage = factors.get("leverage_effect")
    if leverage is not None and float(leverage) >= 1.5:
        warnings.append("high_leverage")
    lease_share = factors.get("lease_share_of_net_debt")
    if lease_share is not None and float(lease_share) >= 0.5:
        warnings.append("lease_heavy")
    margin_multiple = factors.get("margin_multiple")
    if margin_multiple is not None and float(margin_multiple) >= 1.5:
        warnings.append("large_margin_extrapolation")
    # 30.1:粗利率の履歴が上下に振れているのに、直近2期の差分で大きく外挿している。
    # 「構造的な改善」ではなく「循環の一局面」を7年分に引き伸ばしている可能性。
    margin_consistency = factors.get("gross_margin_consistency")
    if (
        margin_multiple is not None
        and float(margin_multiple) >= 1.2
        and margin_consistency is not None
        and float(margin_consistency) < 0.4
    ):
        warnings.append("cyclical_margin_extrapolation")
    nowcast_adj = factors.get("growth_nowcast_adjustment")
    if nowcast_adj is not None and float(nowcast_adj) >= 0.10:
        warnings.append("nowcast_upward")
    if survival_probability is not None and survival_probability < 0.50:
        warnings.append("low_survival_probability")
    return warnings


def _latest_raw_snapshots_by_ticker(session: Session, ticker_ids: list[int]) -> dict[int, RawSnapshot]:
    """指定ティッカー群それぞれの最新raw_snapshotを1クエリで取得する(N+1回避)。

    **最新の1行だけをDBに絞らせる**(`DISTINCT ON`)。以前は該当ティッカーの
    **全スナップショット行**を読んでPython側で先頭を取っていたため、1銘柄あたり
    平均3行 × 約13KB の payload を捨てるために転送していた。29章で母集団が
    約1.9倍に広がるぶん、この無駄がそのまま応答時間に乗る。
    """
    if not ticker_ids:
        return {}
    rows = (
        session.query(RawSnapshot)
        .filter(RawSnapshot.ticker_id.in_(ticker_ids))
        .order_by(RawSnapshot.ticker_id, RawSnapshot.snapshot_date.desc())
        .distinct(RawSnapshot.ticker_id)
        .all()
    )
    return {row.ticker_id: row for row in rows}


def _liquidity_profiles_by_ticker(
    session: Session, ticker_ids: list[int], as_of: datetime.date
) -> dict[int, LiquidityProfile]:
    """指定ティッカー群それぞれの流動性プロファイルを1クエリで取得する(30.2.2)。

    `cutoff` は「基準日 − 40暦日」(20営業日を確実に含む)。Python側で
    ティッカーごとに新しい順で先頭20件を取る——`_latest_raw_snapshots_by_ticker`
    と同じくN+1を避けるのが目的で、`DISTINCT ON` ではなく単純な
    `ORDER BY ticker_id, trade_date DESC` の全件取得+Python側グルーピングに
    したのは、DISTINCT ONが「各ticker_idにつき1行」しか返せず20行必要な
    このクエリの形に合わないため。
    """
    if not ticker_ids:
        return {}
    portfolio_config = load_portfolio_config()
    cutoff = as_of - datetime.timedelta(days=40)
    rows = (
        session.query(PriceSnapshot.ticker_id, PriceSnapshot.close, PriceSnapshot.volume)
        .filter(PriceSnapshot.ticker_id.in_(ticker_ids), PriceSnapshot.trade_date > cutoff)
        .order_by(PriceSnapshot.ticker_id, PriceSnapshot.trade_date.desc())
        .all()
    )
    by_ticker: dict[int, list[tuple[float | None, int | None]]] = {}
    for ticker_id, close, volume in rows:
        bucket = by_ticker.setdefault(ticker_id, [])
        if len(bucket) < ADV_WINDOW_DAYS:
            bucket.append((float(close) if close is not None else None, volume))

    return {
        ticker_id: compute_liquidity_profile(
            closes_and_volumes,
            portfolio_value_usd=portfolio_config.portfolio_value_usd,
            adv_participation_cap=portfolio_config.adv_participation_cap,
            per_position_cap=portfolio_config.per_position_cap,
        )
        for ticker_id, closes_and_volumes in by_ticker.items()
    }


def _round_trip_cost_by_ticker(
    session: Session,
    ticker_ids: list[int],
    as_of: datetime.date,
    liquidity_by_ticker: dict[int, LiquidityProfile],
) -> dict[int, float]:
    """D-5(docs/defect_and_edge_audit_2026-08-28.md):銘柄ごとの推定往復取引コスト(bps)。

    Corwin–Schultz 実効スプレッド(直近の日次 high/low)+ 平方根則インパクト
    (ADV と `per_position_cap` の名目建玉サイズ)。
    """
    if not ticker_ids:
        return {}
    execution_config = load_execution_config()
    portfolio_config = load_portfolio_config()
    nominal_position_usd = portfolio_config.portfolio_value_usd * portfolio_config.per_position_cap
    cutoff = as_of - datetime.timedelta(days=40)
    rows = (
        session.query(PriceSnapshot.ticker_id, PriceSnapshot.high, PriceSnapshot.low)
        .filter(
            PriceSnapshot.ticker_id.in_(ticker_ids),
            PriceSnapshot.trade_date > cutoff,
            PriceSnapshot.trade_date <= as_of,
        )
        .order_by(PriceSnapshot.ticker_id, PriceSnapshot.trade_date.asc())
        .all()
    )
    bars_by_ticker: dict[int, list[tuple[float, float]]] = {}
    for ticker_id, high, low in rows:
        if high is None or low is None:
            continue
        bars_by_ticker.setdefault(ticker_id, []).append((float(high), float(low)))

    costs: dict[int, float] = {}
    for ticker_id in ticker_ids:
        spread = corwin_schultz_spread(bars_by_ticker.get(ticker_id, []))
        adv = liquidity_by_ticker.get(ticker_id).adv_usd if liquidity_by_ticker.get(ticker_id) else None
        costs[ticker_id] = round_trip_cost_bps(
            spread,
            nominal_position_usd,
            adv,
            execution_config.impact_coefficient,
            commission_bps=execution_config.commission_bps,
            min_half_spread_bps=execution_config.min_half_spread_bps,
        )
    return costs


def _red_flag_counts_by_ticker(
    session: Session, ticker_ids: list[int], as_of: datetime.date
) -> dict[int, tuple[int, int]]:
    """指定ティッカー群それぞれの (blocking件数, warning件数) を1クエリで取得する(30.4.3)。

    一覧では件数だけを見せれば足り、`RedFlag.detail`/`document_url` は要らない
    ——`CandidateSummary` を膨らませないため(30.4.3)。
    """
    if not ticker_ids:
        return {}
    rows = session.query(Filing).filter(Filing.ticker_id.in_(ticker_ids)).all()
    by_ticker: dict[int, list] = {}
    for row in rows:
        by_ticker.setdefault(row.ticker_id, []).append(filing_to_view(row))

    counts: dict[int, tuple[int, int]] = {}
    for ticker_id, views in by_ticker.items():
        flags = evaluate_red_flags(views, as_of=as_of)
        blocking = sum(1 for f in flags if f.severity == "blocking")
        warning = sum(1 for f in flags if f.severity == "warning")
        counts[ticker_id] = (blocking, warning)
    return counts


@router.get("/scores/dates", response_model=ScoreDatesResponse)
def list_score_dates(
    limit: int = Query(30, ge=1, le=_MAX_LIMIT),
    session: Session = Depends(get_session),
) -> ScoreDatesResponse:
    """スコアが存在する日付一覧(新しい順)。順位変動画面が比較対象日を選ぶために使う。

    `GET /candidates` は現行 `scoring_version` の行しか返さないため、ここも同じ
    バージョンに絞る。絞らないと、旧バージョンでしかスコアが無い日付を「比較可能な
    過去日」として返してしまい、順位変動画面がその日付で空のランキングを取得して
    全銘柄「前回順位なし」と表示される(14.6のバージョニングと整合させる)。"""
    scoring_version = load_scoring_config().scoring_version
    rows = (
        session.query(Score.score_date)
        .filter(Score.scoring_version == scoring_version)
        .distinct()
        .order_by(Score.score_date.desc())
        .limit(limit)
        .all()
    )
    return ScoreDatesResponse(dates=[row[0] for row in rows])


@router.get("/candidates", response_model=CandidateListResponse)
def list_candidates(
    date: datetime.date | None = None,
    sector: str | None = None,
    min_market_cap: float | None = Query(None, ge=0),
    max_market_cap: float | None = Query(None, ge=0),
    horizon_years: float | None = Query(
        None,
        ge=_MIN_HORIZON_YEARS,
        le=_MAX_HORIZON_YEARS,
        description="目標達成までの年数。未指定なら設定値(既定7年)。",
    ),
    target_moic: float | None = Query(
        None,
        ge=_MIN_TARGET_MOIC,
        le=_MAX_TARGET_MOIC,
        description="目標倍率。未指定なら設定値(既定10倍)。",
    ),
    limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    offset: int = Query(0, ge=0),
    # 30.2.1:True のとき取扱可否リストで "tradable" と判定された銘柄だけを返す。
    # 既定を False にするのは、リスト未整備の利用者に空の画面を見せないため。
    tradable_only: bool = False,
    session: Session = Depends(get_session),
) -> CandidateListResponse:
    """ランキング。`horizon_years` / `target_moic` で目標を自由に変えられる(27.24)。

    「3年で3倍」「5年で5倍」のように指定すると、保存済みの入力から**その年数で
    厳密に再計算**して並べ直す。年数と倍率の組み合わせが決める**必要年率**も
    一緒に返す——「3年で3倍」(年率44.2%)は「7年で10倍」(年率38.9%)より
    実は厳しい、という関係は年率にしないと見えないため。
    """
    scoring_config = load_scoring_config()
    scoring_version = scoring_config.scoring_version
    adjusted_config, target_spec, ceilings = _resolve_target(
        scoring_config, load_universe_config(), horizon_years, target_moic
    )
    target_score_date = (
        date
        or session.query(func.max(Score.score_date)).filter(Score.scoring_version == scoring_version).scalar()
    )
    if target_score_date is None:
        return CandidateListResponse(
            score_date=None, total=0, limit=limit, offset=offset, target=target_spec, items=[]
        )

    # scoring_versionを跨いだ新旧スコアが同じscore_dateに共存し得るため(14.6)、
    # 現行バージョンのみに絞る。絞らないと同一ティッカーが新旧スコアで二重に
    # 表示される(実データ検証で発見:v1→v2移行後にランキング一覧で重複行が
    # 出ていた)。
    #
    # さらにScoreはuniverse_snapshots.includedを一切見ずに書き込まれるため
    # (run_scoringはスコアリング時点の「最新」included集合を対象にするが、
    # 後からapply_gatesを同日中に再実行して判定が変わっても古いScore行は
    # 削除されない)、同日中にapply-gates→run-scoringを複数回回すと既に対象外
    # になった銘柄のScoreがランキングに残り続ける不具合があった(実データで
    # 発見、2026-08-24)。target_score_date時点のuniverse_snapshotsで
    # included=Trueであることを明示的に確認する。
    query = (
        session.query(Score, Ticker)
        .join(Ticker, Ticker.id == Score.ticker_id)
        .join(
            UniverseSnapshot,
            (UniverseSnapshot.ticker_id == Score.ticker_id)
            & (UniverseSnapshot.snapshot_date == target_score_date),
        )
        .filter(
            Score.score_date == target_score_date,
            Score.scoring_version == scoring_version,
            UniverseSnapshot.included.is_(True),
        )
    )
    # セクター絞り込みはSQLではなく表示側で行う(29章)。断面統計は**母集団
    # 全体**から作らなければならないため、母集団を確定する前に絞ってしまうと、
    # セクターを選んだ瞬間に σ の縮小中心とナウキャスト基準線がその1セクターの
    # ものに変わり、確率そのものが動いてしまう(絞り込みは表示の操作であって、
    # モデルの前提を変える操作ではない)。
    # 27.20の「見通しがマイナス」(確率NULL)の行もここでは落とさない。29章の
    # 断面統計を作り直すのに必要な母集団だからである(`build_cross_section` は
    # ランキング対象に限らず母集団全体からσの中心を推定する)。ランキングからは
    # 下の `_score_view` が None を返すことで除かれる。
    rows = query.all()
    raw_by_ticker = _latest_raw_snapshots_by_ticker(session, [t.id for _, t in rows])

    # 29章:この目標の母集団(=規模の上限で切った集合)を先に確定させる。
    in_universe = [
        (score, ticker)
        for score, ticker in rows
        if _within_target_universe(
            (raw_by_ticker[ticker.id].payload.get("info") or {}) if ticker.id in raw_by_ticker else {},
            ceilings,
        )
    ]
    target_cross_section = (
        None
        if target_spec.is_default
        else _cross_section_for_target([score for score, _ in in_universe], adjusted_config)
    )

    filtered: list[tuple[Ticker, float | None, float | None, str | None, _ScoreView]] = []
    for score, ticker in in_universe:
        raw = raw_by_ticker.get(ticker.id)
        info = raw.payload.get("info", {}) if raw else {}
        if sector and ticker.sector != sector:
            continue
        market_cap = info.get("marketCap")
        if min_market_cap is not None and (market_cap is None or market_cap < min_market_cap):
            continue
        if max_market_cap is not None and (market_cap is None or market_cap > max_market_cap):
            continue
        view = _score_view(score, adjusted_config, target_spec, target_cross_section)
        if view is None:
            # 順位を付けない銘柄。理由は3つある:27.20の「見通しがマイナス」
            # (確率NULLで保存されている行。29章で断面統計の母集団として
            # 読み込むようになったのでここまで来る)、指定された目標では期待倍率が
            # 1.0を割る銘柄(年数が短いほど複利が効かず増える)、そして
            # 入力を保存する前に書かれた古い行。
            continue
        price = info.get("currentPrice") or info.get("regularMarketPrice")
        filtered.append((ticker, market_cap, price, _company_name(info), view))

    # 30.2.1:取扱可否はDBに依存しない(ファイルベース)ので、ページングの前に
    # 母集団全体へ適用してよい——`tradable_only` は絞り込みであり、後段の
    # `total`/ページングに反映されるべきため。coverageの読み込みは
    # mtimeキャッシュ済みで1リクエストにつき実質1回のファイル走査で済む。
    coverage = get_cached_broker_coverage()
    if tradable_only:
        filtered = [row for row in filtered if evaluate_tradability(row[0].symbol, coverage).status == TRADABLE]

    # 再計算した場合は順序が変わるので、SQLではなくここで並べ替える。
    filtered.sort(key=lambda row: row[4].probability, reverse=True)
    total = len(filtered)
    page = filtered[offset : offset + limit]

    # 30.2.2 / 30.4.3:流動性とレッドフラグ件数はDBクエリを伴うため、
    # ページング後の表示対象(page)だけに絞って計算する——`filtered` 全体
    # (数百〜数千件)に対して行うと無駄なクエリコストになる。
    page_ticker_ids = [ticker.id for ticker, *_ in page]
    liquidity_by_ticker = _liquidity_profiles_by_ticker(session, page_ticker_ids, target_score_date)
    red_flag_counts = _red_flag_counts_by_ticker(session, page_ticker_ids, target_score_date)
    cost_by_ticker = _round_trip_cost_by_ticker(
        session, page_ticker_ids, target_score_date, liquidity_by_ticker
    )
    benchmark = session.query(Ticker).filter(Ticker.symbol == "IWM", Ticker.is_benchmark.is_(True)).one_or_none()
    benchmark_series = _price_series(session, benchmark.id) if benchmark is not None else None
    risk_by_ticker = {
        ticker_id: compute_price_risk(_price_series(session, ticker_id), benchmark_series)
        for ticker_id in page_ticker_ids
    }

    items = []
    max_data_age_days: int | None = None
    for i, (ticker, market_cap, price, company_name, view) in enumerate(page):
        tradability_result = evaluate_tradability(ticker.symbol, coverage)
        liquidity = liquidity_by_ticker.get(ticker.id)
        blocking_count, warning_count = red_flag_counts.get(ticker.id, (0, 0))
        data_age_days = (
            business_days_between(view.price_as_of, target_score_date)
            if view.price_as_of is not None
            else None
        )
        if data_age_days is not None:
            max_data_age_days = max(max_data_age_days or 0, data_age_days)
        moic_q = _moic_quantile_map(view.log_moic_mu, view.log_moic_sigma, view.survival_probability) or {}
        items.append(
            CandidateSummary(
                rank=offset + i + 1,
                ticker=ticker.symbol,
                company_name=company_name,
                sector=ticker.sector,
                market_cap=market_cap,
                price=price,
                probability=view.probability,
                expected_moic=view.expected_moic,
                median_moic=view.median_moic,
                survival_probability=view.survival_probability,
                calibrated_on_pace_probability=view.calibrated_on_pace_probability,
                probability_below_half=_downside_probability(
                    view.log_moic_mu, view.log_moic_sigma, view.survival_probability, 0.5
                ),
                probability_below_one=_downside_probability(
                    view.log_moic_mu, view.log_moic_sigma, view.survival_probability, 1.0
                ),
                moic_p10=moic_q.get("p10"),
                moic_p90=moic_q.get("p90"),
                warnings=_compute_warnings(view.factors, view.survival_probability),
                tradability=tradability_result.status,
                tradable_brokers=tradability_result.brokers,
                adv_usd=liquidity.adv_usd if liquidity else None,
                adv_observation_days=liquidity.observation_days if liquidity else None,
                max_position_usd=liquidity.max_position_usd if liquidity else None,
                position_binding_constraint=liquidity.binding_constraint if liquidity else None,
                estimated_round_trip_cost_bps=cost_by_ticker.get(ticker.id),
                blocking_flag_count=blocking_count,
                warning_flag_count=warning_count,
                price_as_of=view.price_as_of,
                financials_as_of=view.financials_as_of,
                data_age_days=data_age_days,
                realized_vol_1y=risk_by_ticker.get(ticker.id).realized_vol_1y if risk_by_ticker.get(ticker.id) else None,
                max_drawdown_3y=risk_by_ticker.get(ticker.id).max_drawdown_3y if risk_by_ticker.get(ticker.id) else None,
                evidence_grade=_summary_evidence_grade(
                    raw_by_ticker.get(ticker.id), _compute_warnings(view.factors, view.survival_probability), data_age_days
                ),
            )
        )

    return CandidateListResponse(
        score_date=target_score_date,
        total=total,
        limit=limit,
        offset=offset,
        target=target_spec,
        items=items,
        portfolio=_portfolio_outlook(session, [view.probability for _, _, _, _, view in page]),
        max_data_age_days=max_data_age_days,
    )


def _portfolio_outlook(session: Session, probabilities: list[float]) -> PortfolioOutlook | None:
    """表示中の上位銘柄をまとめて持ったときの見通し(28.12)。

    資産相関は直近の擬似バックテストが推定した値を使う。まだ推定できていない
    場合は**この節を出さない**——相関が分からないまま「少なくとも1つ当たる確率」
    を出すと、結局は独立性の仮定を隠して表示することになる。
    """
    if not probabilities:
        return None
    # 新旧のモデルが同じDBに共存しうる(14.6)。別バージョンの実行から相関を
    # 借りてくると、いま表示している確率とは別のモデルの性質を混ぜることになる。
    run = (
        session.query(BacktestRun)
        .filter(
            BacktestRun.scoring_version == load_scoring_config().scoring_version,
            BacktestRun.asset_correlation.isnot(None),
        )
        .order_by(BacktestRun.run_at.desc())
        .first()
    )
    if run is None:
        return None
    outcome = portfolio_outcome(probabilities, float(run.asset_correlation))
    return PortfolioOutlook(
        holdings=outcome.holdings,
        asset_correlation=outcome.asset_correlation,
        expected_hits=outcome.expected_hits,
        probability_at_least_one=outcome.probability_at_least_one,
        probability_at_least_one_if_independent=outcome.probability_at_least_one_if_independent,
        probability_at_least_two=outcome.probability_at_least_two,
    )


@router.get("/candidates/{ticker}", response_model=CandidateDetail)
def get_candidate_detail(
    ticker: str = Path(..., pattern=TICKER_PATTERN),
    horizon_years: float | None = Query(None, ge=_MIN_HORIZON_YEARS, le=_MAX_HORIZON_YEARS),
    target_moic: float | None = Query(None, ge=_MIN_TARGET_MOIC, le=_MAX_TARGET_MOIC),
    session: Session = Depends(get_session),
) -> CandidateDetail:
    """銘柄詳細。ランキングと同じ `horizon_years` / `target_moic` を受け付ける(27.24)。

    一覧で「3年で3倍」を選んだまま詳細に入ったのに、詳細だけ既定の7年で表示される
    ——という食い違いが起きないよう、同じパラメータで再計算できるようにする。
    5因子の内訳も、その年数で計算し直した値になる。
    """
    symbol = ticker.upper()
    ticker_row = session.query(Ticker).filter(Ticker.symbol == symbol).one_or_none()
    if ticker_row is None:
        raise HTTPException(status_code=404, detail=f"ticker '{symbol}' is not in the tracked universe")

    raw = (
        session.query(RawSnapshot)
        .filter_by(ticker_id=ticker_row.id)
        .order_by(RawSnapshot.snapshot_date.desc())
        .first()
    )
    info = raw.payload.get("info", {}) if raw else {}

    latest_universe_date = session.query(func.max(UniverseSnapshot.snapshot_date)).scalar()
    universe_row = None
    if latest_universe_date is not None:
        universe_row = (
            session.query(UniverseSnapshot)
            .filter_by(snapshot_date=latest_universe_date, ticker_id=ticker_row.id)
            .one_or_none()
        )
    scoring_config = load_scoring_config()
    adjusted_config, target_spec, ceilings = _resolve_target(
        scoring_config, load_universe_config(), horizon_years, target_moic
    )

    # 29章:規模の上限は目標の関数なので、「候補かどうか」も目標ごとに変わる。
    # 日次バッチは最も緩い目標の母集団を materialize しているため、そこを通って
    # いても**この目標では大きすぎる**銘柄がありうる。その場合は日次バッチと
    # 同じ理由コードを付けて「この目標では対象外」と返す——そうしないと、
    # ランキングに出てこない銘柄の詳細が「候補です」と表示される。
    passed_daily_gate = bool(universe_row and universe_row.included)
    over_target_ceiling = passed_daily_gate and not _within_target_universe(info, ceilings)
    is_candidate = passed_daily_gate and not over_target_ceiling
    if over_target_ceiling:
        market_cap = info.get("marketCap")
        revenue = normalize_financial_currency_value(info.get("totalRevenue"), info)
        exclusion_reason = [
            reason
            for reason, over in (
                ("market_cap_ceiling", market_cap is not None and market_cap >= ceilings.market_cap_usd),
                ("revenue_ceiling", revenue is not None and revenue >= ceilings.revenue_usd),
            )
            if over
        ]
    else:
        exclusion_reason = (
            universe_row.exclusion_reason.split(",")
            if universe_row and not universe_row.included and universe_row.exclusion_reason
            else None
        )

    score_history = (
        session.query(Score)
        .filter_by(ticker_id=ticker_row.id, scoring_version=scoring_config.scoring_version)
        .order_by(Score.score_date.desc())
        .limit(_SCORE_HISTORY_LIMIT)
        .all()
    )
    latest_score = score_history[0] if score_history else None

    # J-3:52週レンジ内の位置。直近約1年(400暦日)の終値から算出する。
    today = utc_today()
    range_closes = [
        float(c)
        for (c,) in session.query(PriceSnapshot.close)
        .filter(
            PriceSnapshot.ticker_id == ticker_row.id,
            PriceSnapshot.trade_date > today - datetime.timedelta(days=400),
            PriceSnapshot.trade_date <= today,
            PriceSnapshot.close.isnot(None),
        )
        .order_by(PriceSnapshot.trade_date.asc())
        .all()
    ]
    price_range = compute_price_range(range_closes) if range_closes else None
    # L-1: display observed risk independently of the model's sigma.
    risk_series = _price_series(session, ticker_row.id)
    benchmark_symbol = "IWC" if (info.get("marketCap") or 0) < 300_000_000 else "IWM"
    benchmark_ticker = session.query(Ticker).filter(Ticker.symbol == benchmark_symbol, Ticker.is_benchmark.is_(True)).one_or_none()
    price_risk_result = compute_price_risk(
        risk_series,
        _price_series(session, benchmark_ticker.id) if benchmark_ticker is not None else None,
    )

    # 30.2.1 / 30.2.2:取扱可否と流動性(フェーズ1)。一覧と同じロジックを
    # 1銘柄だけに適用する。
    tradability_result = evaluate_tradability(ticker_row.symbol, get_cached_broker_coverage())
    liquidity = _liquidity_profiles_by_ticker(session, [ticker_row.id], today).get(ticker_row.id)
    estimated_cost_bps = _round_trip_cost_by_ticker(
        session, [ticker_row.id], today, {ticker_row.id: liquidity} if liquidity else {}
    ).get(ticker_row.id)
    execution_diagnostics = _execution_diagnostics(
        session, ticker_row.id, liquidity.max_position_usd if liquidity else None
    )

    # 30.4.3:提出書類から読み取れるレッドフラグ。`filings` が1件も無い銘柄
    # (追跡対象外でEDGARを一度も見ていない)は red_flags=[] / filings_checked_on=None
    # を返し、UIが「未確認」と表示できるようにする(空リストと区別する)。
    filing_rows = session.query(Filing).filter_by(ticker_id=ticker_row.id).all()
    if filing_rows:
        filings_checked_on = max(f.created_at for f in filing_rows).date()
        red_flags = evaluate_red_flags([filing_to_view(f) for f in filing_rows], as_of=today)
    else:
        filings_checked_on = None
        red_flags = []

    # 30.5.3:yfinance値とSEC XBRL値の突合。model_inputsは`raw_snapshots.payload`
    # の生値そのもの(MoicInputsはnet_debtしか保持せず現金・負債を分離して
    # 持たないため、モデルが実際に読んでいる生値をそのまま使う)。
    xbrl_rows = session.query(XbrlFact).filter_by(ticker_id=ticker_row.id).all()
    balance_sheet = (raw.payload.get("balance_sheet") or {}) if raw else {}
    liabilities_series = balance_sheet.get("Total Liabilities Net Minority Interest") or {}
    model_inputs = {
        "revenue": normalize_financial_currency_value(info.get("totalRevenue"), info),
        "shares_outstanding": info.get("sharesOutstanding"),
        "cash": info.get("totalCash"),
        "liabilities": normalize_financial_currency_value(
            next(iter(sorted(liabilities_series.items(), reverse=True)), (None, None))[1], info
        )
        if liabilities_series
        else None,
    }
    xbrl_facts_views = [
        XbrlFactView(
            concept=tag_to_concept(row.taxonomy, row.tag) or "",
            tag=row.tag,
            value=float(row.value),
            period_end=row.period_end,
            filed_date=row.filed_date,
            period_start=row.period_start,
        )
        for row in xbrl_rows
        if tag_to_concept(row.taxonomy, row.tag) is not None
    ]
    # 30.5.3(2026-08-30 修正):`liabilities` は時点を合わせて比べる。
    # CLI の `reconcile` コマンドと同じ理由(詳細は reconciliation.py)。
    model_period_ends: dict[str, datetime.date] = {}
    _liab_period = next(iter(sorted(liabilities_series, reverse=True)), None) if liabilities_series else None
    if _liab_period:
        try:
            model_period_ends["liabilities"] = datetime.date.fromisoformat(str(_liab_period)[:10])
        except ValueError:
            pass
    sec_reconciliation = reconcile(
        model_inputs, xbrl_facts_views, as_of=today, model_period_ends=model_period_ends
    )
    external_warnings: list[str] = []
    if any(item.status == MAGNITUDE_MISMATCH for item in sec_reconciliation):
        external_warnings.append("sec_magnitude_mismatch")
    elif any(item.status == MISMATCH for item in sec_reconciliation):
        external_warnings.append("sec_value_mismatch")

    # 30.6:将来の希薄化見通し。追跡対象外(filingsが1件も無い)銘柄では
    # 提出履歴は空になるが、ノートの手入力があれば reserved_dilution_ratio は
    # 計算できる——「追跡対象外」と「未入力」を混同しない。
    try:
        note_for_dilution = load_note(symbol)
    except Exception:
        note_for_dilution = None
    note_dilution_block = (note_for_dilution.front_matter.get("dilution") or {}) if note_for_dilution else {}
    dilution_outlook_result = compute_dilution_outlook(
        [
            FilingRefView(
                accession_number=f.accession_number, form=f.form, filed_date=f.filed_date, document_url=f.document_url
            )
            for f in filing_rows
        ],
        as_of=today,
        historical_dilution_rate=(latest_score.inputs or {}).get("dilution_cagr") if latest_score else None,
        market_cap=info.get("marketCap"),
        note=NoteDilutionInputs(
            remaining_shelf_capacity_usd=note_dilution_block.get("remaining_shelf_capacity_usd"),
            atm_remaining_usd=note_dilution_block.get("atm_remaining_usd"),
            unexercised_options_ratio=note_dilution_block.get("unexercised_options_ratio"),
            has_variable_conversion_price=note_dilution_block.get("has_variable_conversion_price"),
        ),
    )
    if dilution_outlook_result.heavy_reserved_dilution:
        external_warnings.append("heavy_reserved_dilution")

    # L-7: reasons are explanatory only and never feed probability.
    financial_history = build_financial_history(raw.payload if raw else {})
    evidence_grade = compute_evidence_grade(
        warnings=_compute_warnings((latest_score.factors or {}) if latest_score else {}, latest_score.survival_probability if latest_score else None, external_warnings),
        reconciliation=sec_reconciliation,
        annual_period_count=len(financial_history.annual), quarterly_period_count=len(financial_history.quarterly),
        data_age_days=business_days_between(latest_score.price_as_of, latest_score.score_date) if latest_score and latest_score.price_as_of else None,
    )

    concentration_rows = session.query(CustomerConcentration).filter_by(ticker_id=ticker_row.id).order_by(CustomerConcentration.period_end.desc()).all()
    guidance_rows = session.query(Guidance).filter_by(ticker_id=ticker_row.id).order_by(Guidance.filed_date.desc()).limit(20).all()
    litigation_rows = session.query(LitigationEvent).filter_by(ticker_id=ticker_row.id).order_by(LitigationEvent.event_date.desc()).limit(20).all()
    collected_sections = {
        section
        for (section,) in session.query(FilingSection.section)
        .filter(FilingSection.ticker_id == ticker_row.id)
        .all()
    }
    concentration_collected = bool({"item1", "item7"} & collected_sections)
    guidance_collected = "ex99" in collected_sections
    litigation_collected = "item3" in collected_sections

    # J-7:需給(インサイダー・空売り残・浮動株)。**ゲート・スコアには一切
    # 入っていない**——表示のみ(原則3)。
    supply = _supply_view(session, ticker_row.id, xbrl_facts_views, info.get("marketCap"), today)

    # J-6:直近のカタリスト。event_calendar(次回決算日)とノートの verification_date
    # のうち、`today` 以降で最も近いもの。
    next_event: CalendarEvent | None = None
    event_candidates: list[tuple[datetime.date, str, bool, str, datetime.date | None]] = []
    ec_row = (
        session.query(EventCalendar)
        .filter(EventCalendar.ticker_id == ticker_row.id, EventCalendar.event_date >= today)
        .order_by(EventCalendar.event_date.asc())
        .first()
    )
    if ec_row is not None:
        event_candidates.append(
            (ec_row.event_date, ec_row.event_type, ec_row.is_estimated, ec_row.source, ec_row.collected_on)
        )
    vdate = _note_verification_date(note_for_dilution.front_matter) if note_for_dilution else None
    if vdate is not None and vdate >= today:
        event_candidates.append((vdate, "verification", False, "note", None))
    if event_candidates:
        event_candidates.sort(key=lambda c: c[0])
        ev_date, ev_type, ev_est, ev_src, ev_collected = event_candidates[0]
        next_event = _calendar_event_view(
            ticker_row.symbol, _company_name(info), ev_type, ev_date, today,
            is_estimated=ev_est, source=ev_src, collected_on=ev_collected,
        )

    # 29章:この目標の母集団に入っていない銘柄は、**モデルの出力を一切出さない**。
    #
    # バッチは最も緩い目標の母集団すべてにスコアを付けるが、その計算に使う断面
    # 統計(σ の縮小中心・ナウキャストの基準線)は**既定の目標の母集団**のもの
    # である(`engine.primary_universe_inputs`)。$7B の銘柄に対して保存されて
    # いる確率は「小型株のプールに属していたと仮定したときの値」であり、
    # この目標では意味を持たない。27.20 と同じ方針で、測っていないものを
    # 測ったことにしない——過去のスコア履歴も同じ理由で出さない。
    if over_target_ceiling:
        score_history = []
        latest_score = None

    # 指定された目標で計算し直す。既定なら保存値をそのまま使う(27.24)。
    recomputed = None
    if latest_score is not None and not target_spec.is_default:
        stored = latest_score.inputs or {}
        # 29章:断面統計も**その目標の母集団**から作り直す。ランキング一覧
        # (`list_candidates`)と同じ断面を使わないと、一覧と詳細で同じ銘柄の
        # 確率が食い違う——27.24が「一覧で3年3倍を選んだのに詳細だけ7年」を
        # 避けたのと同じ理由で、母集団の食い違いも避ける必要がある。
        cross_section = _cross_section_for_target(
            _target_universe_scores(session, latest_score.score_date, scoring_config, ceilings),
            adjusted_config,
        ) or CrossSection.from_dict(stored.get("cross_section"))
        if cross_section.sample_size > 0:
            recomputed = compute_moic(MoicInputs.from_dict(stored), cross_section, adjusted_config)

    if recomputed is not None:
        factors = result_to_factors(recomputed)
        probability = recomputed.probability
        expected_moic = recomputed.expected_moic
        median_moic = recomputed.median_moic
        log_moic_mu = recomputed.log_moic_mu
        log_moic_sigma = recomputed.log_moic_sigma
        survival_probability = recomputed.survival_probability
    else:
        # 目標を変えたのに再計算できなかった場合、保存済みの `factors` は**既定の
        # 7年/10倍で計算された内訳**である。それを指定された目標のラベルの下に
        # 出すと、UIが「この内訳は『3年で3倍』で計算し直した値です」と明記して
        # いる横に、7年の値が並ぶ——27.20が防ごうとした「測っていないものを
        # 測ったことにする」表示そのもの。確率・期待倍率を None にしている以上、
        # 同じ理由で内訳も出さない(`unranked_reason` も既定の目標に対する判定
        # なので一緒に落とす。フロントは「この年数では対象外」と正しく出す)。
        factors = latest_score.factors if latest_score and target_spec.is_default else None
        probability = (
            float(latest_score.probability)
            if latest_score and latest_score.probability is not None and target_spec.is_default
            else None
        )
        expected_moic = (factors or {}).get("expected_moic") if target_spec.is_default else None
        median_moic = (
            float(latest_score.median_moic)
            if latest_score and latest_score.median_moic is not None and target_spec.is_default
            else None
        )
        log_moic_mu = (
            float(latest_score.log_moic_mu)
            if latest_score and latest_score.log_moic_mu is not None and target_spec.is_default
            else None
        )
        log_moic_sigma = (
            float(latest_score.log_moic_sigma)
            if latest_score and latest_score.log_moic_sigma is not None and target_spec.is_default
            else None
        )
        survival_probability = (
            float(latest_score.survival_probability)
            if latest_score and latest_score.survival_probability is not None and target_spec.is_default
            else None
        )

    return CandidateDetail(
        ticker=ticker_row.symbol,
        company_name=_company_name(info),
        is_candidate=is_candidate,
        sector=ticker_row.sector,
        market_cap=info.get("marketCap"),
        price=info.get("currentPrice") or info.get("regularMarketPrice"),
        probability=probability,
        expected_moic=expected_moic,
        median_moic=median_moic,
        log_moic_sigma=log_moic_sigma,
        survival_probability=survival_probability,
        calibrated_on_pace_probability=(
            float(latest_score.calibrated_on_pace_probability)
            if latest_score
            and latest_score.calibrated_on_pace_probability is not None
            and target_spec.is_default
            else None
        ),
        probability_below_half=_downside_probability(log_moic_mu, log_moic_sigma, survival_probability, 0.5),
        probability_below_one=_downside_probability(log_moic_mu, log_moic_sigma, survival_probability, 1.0),
        moic_quantiles=_moic_quantile_map(log_moic_mu, log_moic_sigma, survival_probability),
        warnings=_compute_warnings(factors, survival_probability, external_warnings),
        scoring_version=latest_score.scoring_version if latest_score else None,
        price_as_of=latest_score.price_as_of if latest_score else None,
        financials_as_of=latest_score.financials_as_of if latest_score else None,
        data_age_days=(
            business_days_between(latest_score.price_as_of, latest_score.score_date)
            if latest_score is not None and latest_score.price_as_of is not None
            else None
        ),
        target=target_spec,
        factors=factors,
        unranked_reason=(factors or {}).get("unranked_reason"),
        factor_breakdown=build_factor_breakdown(factors),
        exclusion_reason=exclusion_reason,
        score_history=[
            ScoreHistoryPoint(
                score_date=s.score_date,
                probability=float(s.probability) if s.probability is not None else None,
                ev_to_gross_profit=_num_or_none((s.factors or {}).get("current_ev_to_gross_profit")),
            )
            for s in score_history
        ],
        last_updated=raw.created_at if raw else None,
        tradability=tradability_result.status,
        tradable_brokers=tradability_result.brokers,
        adv_usd=liquidity.adv_usd if liquidity else None,
        adv_observation_days=liquidity.observation_days if liquidity else None,
        max_position_usd=liquidity.max_position_usd if liquidity else None,
        **execution_diagnostics,
        position_binding_constraint=liquidity.binding_constraint if liquidity else None,
        estimated_round_trip_cost_bps=estimated_cost_bps,
        red_flags=[
            RedFlagView(
                code=f.code,
                severity=f.severity,
                detected_on=f.detected_on,
                detail=f.detail,
                document_url=f.document_url,
            )
            for f in red_flags
        ],
        filings_checked_on=filings_checked_on,
        dilution_outlook=DilutionOutlook(
            shelf_filings=[
                FilingRef(accession_number=f.accession_number, form=f.form, filed_date=f.filed_date, document_url=f.document_url)
                for f in dilution_outlook_result.shelf_filings
            ],
            offering_filings=[
                FilingRef(accession_number=f.accession_number, form=f.form, filed_date=f.filed_date, document_url=f.document_url)
                for f in dilution_outlook_result.offering_filings
            ],
            offerings_last_3y=dilution_outlook_result.offerings_last_3y,
            historical_dilution_rate=dilution_outlook_result.historical_dilution_rate,
            remaining_shelf_capacity_usd=dilution_outlook_result.remaining_shelf_capacity_usd,
            atm_remaining_usd=dilution_outlook_result.atm_remaining_usd,
            unexercised_options_ratio=dilution_outlook_result.unexercised_options_ratio,
            has_variable_conversion_price=dilution_outlook_result.has_variable_conversion_price,
            reserved_dilution_ratio=dilution_outlook_result.reserved_dilution_ratio,
        ),
        sec_reconciliation=[
            ReconciliationItemView(
                concept=item.concept,
                model_value=item.model_value,
                sec_value=item.sec_value,
                sec_tag=item.sec_tag,
                sec_period_end=item.sec_period_end,
                sec_filed_date=item.sec_filed_date,
                relative_diff=item.relative_diff,
                status=item.status,
            )
            for item in sec_reconciliation
        ],
        profile=_company_profile(info, ticker_row, raw.snapshot_date if raw else None),
        week52_high=price_range.week52_high if price_range else None,
        week52_low=price_range.week52_low if price_range else None,
        week52_position=price_range.position_in_range if price_range else None,
        next_event=next_event,
        supply=supply,
        price_risk=(
            PriceRiskView(**{**vars(price_risk_result), "benchmark_symbol": benchmark_symbol})
            if price_risk_result is not None else None
        ),
        evidence_grade=EvidenceGradeView(**vars(evidence_grade)),
        customer_concentration=(
            [CustomerConcentrationView(period_end=r.period_end, customer_label=r.customer_label, revenue_pct=float(r.revenue_pct)) for r in concentration_rows]
            if concentration_rows else ([] if concentration_collected else None)
        ),
        guidance=(
            [GuidanceView(filed_date=r.filed_date, period_label=r.period_label, metric=r.metric, low_usd=float(r.low_usd) if r.low_usd is not None else None, high_usd=float(r.high_usd) if r.high_usd is not None else None) for r in guidance_rows]
            if guidance_rows else ([] if guidance_collected else None)
        ),
        litigation=(
            [LitigationView(event_date=r.event_date, kind=r.kind, title=r.title, detail=r.detail, source_url=r.source_url) for r in litigation_rows]
            if litigation_rows else ([] if litigation_collected else None)
        ),
    )


@router.get("/candidates/{ticker}/financials", response_model=FinancialHistoryResponse)
def get_candidate_financials(
    ticker: str = Path(..., pattern=TICKER_PATTERN),
    session: Session = Depends(get_session),
) -> FinancialHistoryResponse:
    """J-2(docs/investment_decision_gap_2026-08-29.md):財務三表の推移。

    詳細本体(`GET /candidates/{ticker}`)を重くしないため別エンドポイントにする。
    `raw_snapshots.payload` に既に入っている値を整形するだけ——追加収集はしない。
    財務三表が1行も無い銘柄でも 200 を返す(空の系列)。
    """
    symbol = ticker.upper()
    ticker_row = session.query(Ticker).filter(Ticker.symbol == symbol).one_or_none()
    if ticker_row is None:
        raise HTTPException(status_code=404, detail=f"ticker '{symbol}' is not in the tracked universe")

    raw = (
        session.query(RawSnapshot)
        .filter_by(ticker_id=ticker_row.id)
        .order_by(RawSnapshot.snapshot_date.desc())
        .first()
    )
    payload = raw.payload if raw else {}
    monitoring = load_monitoring_config()
    history = build_financial_history(payload, runway_floor_months=monitoring.cash_runway_floor_months)
    earnings = build_earnings_history(payload)

    return FinancialHistoryResponse(
        ticker=ticker_row.symbol,
        currency=history.currency,
        currency_conversion_unavailable=history.currency_conversion_unavailable,
        annual=[FinancialPeriodView.model_validate(p) for p in history.annual],
        quarterly=[FinancialPeriodView.model_validate(p) for p in history.quarterly],
        derived=FinancialHistoryDerivedView(
            **{
                k: v
                for k, v in vars(history.derived).items()
                if k != "piotroski_criteria"
            },
            piotroski_criteria=[
                PiotroskiCriterionView.model_validate(c) for c in history.derived.piotroski_criteria
            ],
        ),
        as_of=history.as_of,
        earnings=EarningsHistoryView(
            **{k: v for k, v in vars(earnings).items() if k != "periods"},
            periods=[EarningsPeriodView(**vars(period)) for period in earnings.periods],
        ),
    )


def _note_verification_date(front_matter: dict) -> datetime.date | None:
    """ノートの `verification_date`(手入力)を date にする。書き込みはしない
    (J-6:ノートが正、DB は索引)。"""
    value = front_matter.get("verification_date")
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


_INSIDER_LOOKBACK_DAYS = 180
_INSIDER_BUY_CODES = {"P"}
_INSIDER_SELL_CODES = {"S"}


def _supply_view(
    session: Session,
    ticker_id: int,
    xbrl_facts_views: list,
    market_cap: float | None,
    today: datetime.date,
) -> SupplyView:
    """J-7:需給の集計。**ゲート・スコアには入れない**——表示のみ(原則3)。

    データが無い項目は None(「未取得」)であり 0 とは区別する。
    """
    window_start = today - datetime.timedelta(days=_INSIDER_LOOKBACK_DAYS)
    insider_rows = (
        session.query(InsiderTransaction)
        .filter(
            InsiderTransaction.ticker_id == ticker_id,
            InsiderTransaction.transaction_date >= window_start,
            InsiderTransaction.is_derivative.is_(False),
        )
        .all()
    )
    net_shares: float | None = None
    buyer_count: int | None = None
    insider_as_of: datetime.date | None = None
    if insider_rows:
        buys = sells = 0.0
        buyers: set[str] = set()
        for row in insider_rows:
            shares = float(row.shares)
            if row.transaction_code in _INSIDER_BUY_CODES:
                buys += shares
                buyers.add(row.insider_name)
            elif row.transaction_code in _INSIDER_SELL_CODES:
                sells += shares
        net_shares = buys - sells
        buyer_count = len(buyers)
        insider_as_of = max(r.transaction_date for r in insider_rows)

    short_row = (
        session.query(ShortInterest)
        .filter(ShortInterest.ticker_id == ticker_id)
        .order_by(ShortInterest.settlement_date.desc())
        .first()
    )
    short_shares = days_to_cover = None
    short_as_of = short_lag = None
    if short_row is not None:
        short_shares = float(short_row.short_interest_shares)
        days_to_cover = float(short_row.days_to_cover) if short_row.days_to_cover is not None else None
        short_as_of = short_row.settlement_date
        short_lag = (today - short_row.settlement_date).days

    public_float = None
    float_facts = sorted(
        (v for v in xbrl_facts_views if v.concept == "public_float"),
        key=lambda v: v.period_end,
    )
    if float_facts:
        public_float = float(float_facts[-1].value)
    float_ratio = (
        public_float / market_cap
        if public_float is not None and market_cap not in (None, 0)
        else None
    )

    return SupplyView(
        insider_net_shares_180d=net_shares,
        insider_buyer_count_180d=buyer_count,
        insider_as_of=insider_as_of,
        short_interest_shares=short_shares,
        days_to_cover=days_to_cover,
        short_as_of=short_as_of,
        short_lag_days=short_lag,
        public_float_usd=public_float,
        float_ratio=float_ratio,
    )


def _calendar_event_view(
    ticker: str, company_name: str | None, event_type: str, event_date: datetime.date,
    today: datetime.date, *, is_estimated: bool, source: str, collected_on: datetime.date | None,
) -> CalendarEvent:
    return CalendarEvent(
        ticker=ticker,
        company_name=company_name,
        event_type=event_type,
        event_date=event_date,
        is_estimated=is_estimated,
        source=source,
        days_until=(event_date - today).days,
        collected_on=collected_on,
    )


@router.get("/calendar", response_model=CalendarResponse)
def list_calendar(
    days: int = Query(30, ge=1, le=365),
    session: Session = Depends(get_session),
) -> CalendarResponse:
    """J-6:近いカタリスト(次回決算日・検証日)を近い順に返す。

    `event_calendar`(`collect-events` が書く)に、投資ノートの `verification_date`
    (手入力)をクエリ時に合流させる——ノートには書き戻さない。
    """
    today = utc_today()
    horizon = today + datetime.timedelta(days=days)

    rows = (
        session.query(EventCalendar, Ticker)
        .join(Ticker, Ticker.id == EventCalendar.ticker_id)
        .filter(EventCalendar.event_date >= today, EventCalendar.event_date <= horizon)
        .all()
    )
    events: list[CalendarEvent] = [
        _calendar_event_view(
            ticker.symbol, None, ev.event_type, ev.event_date, today,
            is_estimated=ev.is_estimated, source=ev.source, collected_on=ev.collected_on,
        )
        for ev, ticker in rows
    ]
    # company_name は info 由来なので join では取れない。symbol だけで足りるので
    # ここでは付けない(詳細画面へ遷移すれば出る)。

    seen = {(e.ticker, e.event_type, e.event_date) for e in events}
    for symbol, note in load_all_notes().items():
        vdate = _note_verification_date(note.front_matter)
        if vdate is None or not (today <= vdate <= horizon):
            continue
        key = (symbol.upper(), "verification", vdate)
        if key in seen:
            continue
        events.append(
            _calendar_event_view(
                symbol.upper(), None, "verification", vdate, today,
                is_estimated=False, source="note", collected_on=None,
            )
        )

    events.sort(key=lambda e: e.event_date)
    return CalendarResponse(as_of=today, items=events)


# B-6(2026-08-26、docs/model_audit_v4_2026-08-26.md):バッチ単位のマーカー
# (`ticker_id IS NULL`)。銘柄ごとの収集結果ではないので `collection_status_counts`
# には出さず、進捗の算出専用に使う。
_BATCH_MARKER_STATUSES = ("run_started", "run_finished")


@router.get("/universe/status", response_model=UniverseStatusResponse)
def universe_status(session: Session = Depends(get_session)) -> UniverseStatusResponse:
    """日次バッチの実行状況(6.5)。**目標に依存しない数**を返す。

    29章以降、`gate_status_counts.included` と `scoring_status_counts` は
    **materialize した母集団**(最も緩い目標=3倍、時価総額 $11.7B まで)の数で
    あって、特定の目標のランキング件数ではない。目標ごとの件数は
    `GET /candidates` の `total` が返す。ここはバッチが最後まで走ったかを見る
    ための画面なので、母集団の数のままにしてある。
    """
    latest_run_id = session.query(CollectionLog.run_id).order_by(CollectionLog.created_at.desc()).limit(1).scalar()
    collection_status_counts: dict[str, int] = {}
    last_collection_run_at = None
    collection_target_count: int | None = None
    collection_complete: bool | None = None
    if latest_run_id is not None:
        rows = (
            session.query(CollectionLog.status, func.count())
            .filter(CollectionLog.run_id == latest_run_id, CollectionLog.status.notin_(_BATCH_MARKER_STATUSES))
            .group_by(CollectionLog.status)
            .all()
        )
        collection_status_counts = dict(rows)
        started = (
            session.query(CollectionLog.detail)
            .filter(CollectionLog.run_id == latest_run_id, CollectionLog.status == "run_started")
            .scalar()
        )
        if started is not None:
            collection_target_count = started.get("target_count")
            collection_complete = (
                session.query(CollectionLog.id)
                .filter(CollectionLog.run_id == latest_run_id, CollectionLog.status == "run_finished")
                .first()
                is not None
            )
        # `run_started` が無い実行はマーカー導入(B-5/B-6、2026-08-26)より前のもの。
        # 完了したかどうかを**知る手段が無い**ので、False(=未完了)ではなく
        # None(=不明)のままにする。完了済みの過去実行を「実行中」と誤表示すると、
        # B-6で直したかった誤読を別の形で再生産することになる。
        last_collection_run_at = (
            session.query(func.max(CollectionLog.created_at)).filter(CollectionLog.run_id == latest_run_id).scalar()
        )

    latest_universe_date = session.query(func.max(UniverseSnapshot.snapshot_date)).scalar()
    gate_status_counts: dict[str, int] = {}
    if latest_universe_date is not None:
        rows = (
            session.query(UniverseSnapshot.included, func.count())
            .filter(UniverseSnapshot.snapshot_date == latest_universe_date)
            .group_by(UniverseSnapshot.included)
            .all()
        )
        gate_status_counts = {("included" if included else "excluded"): count for included, count in rows}

    # ランキング一覧と同じく現行 scoring_version に絞る(14.6)。絞らないと
    # v1/v2が同居する日に scored が二重計上され、included との差から求める
    # unmeasurable が0に潰れる。
    scoring_version = load_scoring_config().scoring_version
    latest_score_date = (
        session.query(func.max(Score.score_date)).filter(Score.scoring_version == scoring_version).scalar()
    )
    scored_count = 0
    if latest_score_date is not None:
        # ランキング一覧(`GET /candidates`)と同じ集合を数える:その日の
        # universe_snapshots で included=True の銘柄に限る。ここを揃えないと、
        # included との差から求める unmeasurable が実際とずれる。
        scored_count = (
            session.query(func.count(Score.id))
            .join(
                UniverseSnapshot,
                (UniverseSnapshot.ticker_id == Score.ticker_id)
                & (UniverseSnapshot.snapshot_date == latest_score_date),
            )
            .filter(
                Score.score_date == latest_score_date,
                Score.scoring_version == scoring_version,
                UniverseSnapshot.included.is_(True),
                Score.probability.isnot(None),
            )
            .scalar()
        )
    # 27.20:ランキング外を1つの数にまとめない。「見通しがマイナス」(測れた)と
    # 「測れない」は利用者にとって意味がまったく違う。
    negative_outlook_count = 0
    if latest_score_date is not None:
        negative_outlook_count = (
            session.query(func.count(Score.id))
            .join(
                UniverseSnapshot,
                (UniverseSnapshot.ticker_id == Score.ticker_id)
                & (UniverseSnapshot.snapshot_date == latest_score_date),
            )
            .filter(
                Score.score_date == latest_score_date,
                Score.scoring_version == scoring_version,
                UniverseSnapshot.included.is_(True),
                Score.probability.is_(None),
            )
            .scalar()
        )
    included_count = gate_status_counts.get("included", 0)
    unmeasurable = max(included_count - scored_count - negative_outlook_count, 0)

    universe_size = session.query(func.count(Ticker.id)).scalar()

    return UniverseStatusResponse(
        last_collection_run_at=last_collection_run_at,
        universe_size=universe_size,
        collection_status_counts=collection_status_counts,
        collection_target_count=collection_target_count,
        collection_complete=collection_complete,
        gate_status_counts=gate_status_counts,
        scoring_status_counts={
            "scored": scored_count,
            "negative_outlook": negative_outlook_count,
            "unmeasurable": unmeasurable,
        },
    )


# §4.3:8.1の想定所要時間(実測は数分程度)に十分な余裕を持たせた孤児判定閾値。
# Task Schedulerがタイムアウトで殺す・Dockerが落ちる等で finished_at が
# NULLのまま残った実行を、DBを書き換えずAPI応答でのみ「failed」に見せる
# (死亡を検知する主体がバッチ内に存在しないため、DBの状態を「修復」すると
# 嘘になりうる。docs/daily_job_status_screen_2026-08-30.md §4.3)。
_PIPELINE_ORPHAN_THRESHOLD = datetime.timedelta(hours=6)

# 過去実行のバックフィルはしない(§2の「やらないこと」)。半分だけ埋まった
# 履歴は「その日は他の工程が動かなかった」という誤読を生むため。この画面が
# 記録し始めた日より前は「記録なし」と明示する。
_PIPELINE_HISTORY_STARTS_AT = datetime.date(2026, 8, 30)


def _pipeline_is_orphaned(run: PipelineRun) -> bool:
    if run.finished_at is not None:
        return False
    started_at = run.started_at
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=datetime.UTC)
    return datetime.datetime.now(datetime.UTC) - started_at > _PIPELINE_ORPHAN_THRESHOLD


def _pipeline_headline(stage_results: dict[str, dict]) -> dict[str, int | None]:
    """B節(成果の推移)の主要成果を工程のresultから抽出する(§5.1)。

    キーが存在しない(その工程がまだ記録されていない・resultがNone)場合は
    None のままにする——**0で埋めない**(§2「やらないこと」と同じ判断。
    「測れなかった」と「0件だった」を混ぜると、B節の値を読み違える)。
    """
    collection = stage_results.get("collection")
    gates = stage_results.get("gates")
    scoring = stage_results.get("scoring")
    collected = quarantined = universe_size = None
    if collection is not None:
        # E-2と同じ定義(sanitizedも成功として数える)。収集工程の「成果」を
        # 表す数なので、健全性判定の成功率分子と揃える。
        collected = collection.get("success", 0) + collection.get("sanitized", 0)
        quarantined = collection.get("quarantined")
        universe_size = collection.get("universe_size")
    return {
        "collected": collected,
        "gated_in": gates.get("included") if gates is not None else None,
        "scored": scoring.get("scored") if scoring is not None else None,
        "quarantined": quarantined,
        "universe_size": universe_size,
    }


def _pipeline_stage_summary(stages: list[PipelineStageRun]) -> dict[str, int]:
    summary = {"succeeded": 0, "failed": 0, "skipped": 0, "running": 0}
    for s in stages:
        summary[s.status] = summary.get(s.status, 0) + 1
    return summary


def _pipeline_run_summary(run: PipelineRun, stages: list[PipelineStageRun]) -> PipelineRunSummary:
    orphaned = _pipeline_is_orphaned(run)
    status = "failed" if orphaned else run.status
    health = list(run.health or [])
    if orphaned:
        health = [
            *health,
            {
                "code": "run_orphaned",
                "severity": "error",
                "message": "実行が終了記録を残さないまま6時間以上経過しました(プロセスが強制終了した可能性があります)",
                "detail": {"started_at": run.started_at.isoformat()},
            },
        ]
    duration_seconds = None
    if run.finished_at is not None:
        duration_seconds = (run.finished_at - run.started_at).total_seconds()
    stage_results = {s.stage: s.result for s in stages if s.result is not None}
    return PipelineRunSummary(
        run_id=str(run.run_id),
        run_date=run.run_date,
        is_weekly=run.is_weekly,
        trigger=run.trigger,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_seconds=duration_seconds,
        status=status,
        health=[PipelineHealthFinding(**f) for f in health],
        headline=_pipeline_headline(stage_results),
        stage_summary=_pipeline_stage_summary(stages),
        expected_stage_count=PIPELINE_STAGE_COUNT,
    )


@router.get("/pipeline/runs", response_model=PipelineRunListResponse)
def list_pipeline_runs(
    limit: int = Query(14, ge=1, le=_MAX_LIMIT),
    session: Session = Depends(get_session),
) -> PipelineRunListResponse:
    """日次ジョブの実行履歴(14.15)。

    **「終了コード0」と「正常」を同一視しない**ための画面(daily_job_status_
    screen_2026-08-30.md)が使う一覧。履歴ストリップと最新実行ヘッダの両方の
    データ源になる。
    """
    runs = (
        session.query(PipelineRun)
        .order_by(PipelineRun.run_date.desc(), PipelineRun.started_at.desc())
        .limit(limit)
        .all()
    )
    stages_by_run: dict[uuid.UUID, list[PipelineStageRun]] = {}
    if runs:
        all_stages = (
            session.query(PipelineStageRun)
            .filter(PipelineStageRun.run_id.in_([r.run_id for r in runs]))
            .order_by(PipelineStageRun.sequence.asc())
            .all()
        )
        for s in all_stages:
            stages_by_run.setdefault(s.run_id, []).append(s)

    return PipelineRunListResponse(
        runs=[_pipeline_run_summary(r, stages_by_run.get(r.run_id, [])) for r in runs],
        history_starts_at=_PIPELINE_HISTORY_STARTS_AT,
    )


@router.get("/pipeline/runs/{run_id}", response_model=PipelineRunDetail)
def get_pipeline_run(run_id: str, session: Session = Depends(get_session)) -> PipelineRunDetail:
    """工程の詳細。`run_id` に `latest` を渡すと最新実行を返す(初回描画で
    2往復させないため。§5.2)。

    **記録ゼロ件のときの `latest` は404ではなく空を返す**——初回導入直後は
    まだ1件も実行されていない状態が正常にありうるため、それをエラー扱い
    すると画面が白紙のエラーになる(§6.4「この画面自身がAPI障害を映す場」
    と紛らわしくなるのを避ける)。
    """
    if run_id == "latest":
        run = (
            session.query(PipelineRun)
            .order_by(PipelineRun.run_date.desc(), PipelineRun.started_at.desc())
            .first()
        )
        if run is None:
            return PipelineRunDetail(run=None, stages=[])
    else:
        try:
            parsed_run_id = uuid.UUID(run_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="run_idの形式が不正です") from None
        run = session.query(PipelineRun).filter_by(run_id=parsed_run_id).one_or_none()
        if run is None:
            raise HTTPException(status_code=404, detail="指定された実行が見つかりません")

    stages = (
        session.query(PipelineStageRun)
        .filter(PipelineStageRun.run_id == run.run_id)
        .order_by(PipelineStageRun.sequence.asc())
        .all()
    )
    return PipelineRunDetail(
        run=_pipeline_run_summary(run, stages),
        stages=[
            PipelineStageView(
                stage=s.stage,
                sequence=s.sequence,
                status=s.status,
                started_at=s.started_at,
                finished_at=s.finished_at,
                duration_seconds=(s.finished_at - s.started_at).total_seconds() if s.finished_at else None,
                result=s.result,
                reason=s.reason,
                error_message=s.error_message,
                error_traceback=s.error_traceback,
            )
            for s in stages
        ],
    )


@router.get("/excluded", response_model=ExcludedListResponse)
def list_excluded(
    reason: str | None = None,
    horizon_years: float | None = Query(None, ge=_MIN_HORIZON_YEARS, le=_MAX_HORIZON_YEARS),
    target_moic: float | None = Query(None, ge=_MIN_TARGET_MOIC, le=_MAX_TARGET_MOIC),
    limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> ExcludedListResponse:
    """除外銘柄の一覧(14.16)。ランキングと同じ `horizon_years` / `target_moic` を受け付ける。

    **29章:規模の上限が目標の関数になったため、この一覧も目標に依存する。**
    日次バッチは最も緩い目標の母集団(時価総額 11.7B まで)を materialize して
    いるので、既定の目標(7年で10倍、上限 3.5B)では「バッチのゲートは通ったが
    この目標には大きすぎる」銘柄が生じる。それを一覧に出さないと、**ランキング
    にも除外一覧にも現れない銘柄**ができてしまい、「この銘柄はどこへ行ったのか」
    に答えられなくなる。バッチが出す理由コードと同じ名前で合成して並べる。
    """
    latest_universe_date = session.query(func.max(UniverseSnapshot.snapshot_date)).scalar()
    if latest_universe_date is None:
        return ExcludedListResponse(total=0, limit=limit, offset=offset, items=[])

    _, _, ceilings = _resolve_target(
        load_scoring_config(), load_universe_config(), horizon_years, target_moic
    )
    scale = _scale_by_ticker(session)

    rows = (
        session.query(UniverseSnapshot, Ticker)
        .join(Ticker, Ticker.id == UniverseSnapshot.ticker_id)
        .filter(UniverseSnapshot.snapshot_date == latest_universe_date)
        .order_by(Ticker.symbol)
        .all()
    )

    excluded: list[tuple[Ticker, list[str]]] = []
    for us, ticker in rows:
        if not us.included:
            excluded.append((ticker, us.exclusion_reason.split(",") if us.exclusion_reason else []))
            continue
        market_cap, revenue = scale.get(ticker.id, (None, None))
        if _within_ceilings(market_cap, revenue, ceilings):
            continue
        excluded.append(
            (
                ticker,
                [
                    code
                    for code, over in (
                        ("market_cap_ceiling", market_cap is not None and market_cap >= ceilings.market_cap_usd),
                        ("revenue_ceiling", revenue is not None and revenue >= ceilings.revenue_usd),
                    )
                    if over
                ],
            )
        )

    if reason:
        excluded = [(ticker, reasons) for ticker, reasons in excluded if any(reason in r for r in reasons)]

    total = len(excluded)
    page = excluded[offset : offset + limit]
    raw_by_ticker = _latest_raw_snapshots_by_ticker(session, [ticker.id for ticker, _ in page])

    items = [
        ExcludedTicker(
            ticker=ticker.symbol,
            company_name=_company_name(raw_by_ticker[ticker.id].payload.get("info", {}))
            if ticker.id in raw_by_ticker
            else None,
            sector=ticker.sector,
            exclusion_reason=reasons,
        )
        for ticker, reasons in page
    ]
    return ExcludedListResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/watchlist", response_model=WatchlistResponse)
def list_watchlist(
    # 未知の値を黙って受けると、タイプミスが「該当0件」として返る——利用者には
    # 「そういう銘柄が無い」と読めてしまい、絞り込みの誤りに気づけない。
    reason: Literal["single_gate_miss", "recent_listing", "insufficient_data", "negative_outlook"]
    | None = None,
    gate: str | None = None,
    horizon_years: float | None = Query(None, ge=_MIN_HORIZON_YEARS, le=_MAX_HORIZON_YEARS),
    target_moic: float | None = Query(None, ge=_MIN_TARGET_MOIC, le=_MAX_TARGET_MOIC),
    limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> WatchlistResponse:
    """15.5の二層構成のうち Tier 2(監視対象)を返す。

    Tier 1(`GET /candidates`)は「全ゲート通過・スコア算出済み」のランキング。
    ここではそこに出てこないが追跡する価値がある銘柄を、理由つきで返す
    (ゲート1つ未達・新規上場・データ不足・高成長だが総合で沈んだ銘柄)。

    **29章:規模の上限が目標の関数になったため、ここも目標に依存する。**
    その目標には大きすぎる銘柄には、日次バッチと同じ理由コードを足してから
    分類する。そうしないと、流動性だけを落としている $7B の銘柄が既定の目標で
    「あと一歩(ゲート1つ未達)」として並ぶ——実際には流動性を満たしても
    規模で外れるので、`WATCHABLE_SINGLE_GATES` が除きたかったものそのものになる。
    """
    scoring_config = load_scoring_config()
    scoring_version = scoring_config.scoring_version
    _, _, ceilings = _resolve_target(
        scoring_config, load_universe_config(), horizon_years, target_moic
    )
    # ゲート判定とスコアは同じ日付で突き合わせる(run_scoring が同日の
    # universe_snapshots のみを対象にするため、両者は必ず揃っている)。
    target_date = (
        session.query(func.max(Score.score_date)).filter(Score.scoring_version == scoring_version).scalar()
        or session.query(func.max(UniverseSnapshot.snapshot_date)).scalar()
    )
    if target_date is None:
        return WatchlistResponse(
            snapshot_date=None,
            total=0,
            limit=limit,
            offset=offset,
            counts_by_reason={},
            counts_by_gate={},
            items=[],
        )

    scale = _scale_by_ticker(session)
    gates: list[GateOutcome] = []
    for row in session.query(UniverseSnapshot).filter(UniverseSnapshot.snapshot_date == target_date).all():
        reasons = row.exclusion_reason.split(",") if row.exclusion_reason else []
        included = row.included
        market_cap, revenue = scale.get(row.ticker_id, (None, None))
        over = [
            code
            for code, hit in (
                ("market_cap_ceiling", market_cap is not None and market_cap >= ceilings.market_cap_usd),
                ("revenue_ceiling", revenue is not None and revenue >= ceilings.revenue_usd),
            )
            if hit and code not in reasons
        ]
        if over:
            # 除外済みの銘柄にも足す。**「あと一歩」の判定に効くのはこちら**——
            # 流動性だけを落としている $7B の銘柄は、バッチの理由が
            # `liquidity_floor` 1つだけなので、そのままだと既定の目標で
            # 「ゲート1つ未達」に並ぶ。実際には流動性を満たしても規模で外れる。
            included = False
            reasons = [*reasons, *over]
        gates.append(
            GateOutcome(ticker_id=row.ticker_id, included=included, exclusion_reasons=reasons)
        )

    score_rows = (
        session.query(Score.ticker_id, Score.probability)
        .filter(Score.score_date == target_date, Score.scoring_version == scoring_version)
        .all()
    )
    ranked_ticker_ids = {tid for tid, probability in score_rows if probability is not None}
    negative_outlook_ticker_ids = {tid for tid, probability in score_rows if probability is None}

    entries = build_tier2(gates, ranked_ticker_ids, negative_outlook_ticker_ids)
    counts_by_reason: dict[str, int] = {}
    for entry in entries:
        counts_by_reason[entry.reason] = counts_by_reason.get(entry.reason, 0) + 1

    counts_by_gate: dict[str, int] = {}
    for entry in entries:
        if entry.gate:
            counts_by_gate[entry.gate] = counts_by_gate.get(entry.gate, 0) + 1

    if reason:
        entries = [e for e in entries if e.reason == reason]
    if gate:
        entries = [e for e in entries if e.gate == gate]

    tickers_by_id = (
        {t.id: t for t in session.query(Ticker).filter(Ticker.id.in_([e.ticker_id for e in entries])).all()}
        if entries
        else {}
    )
    entries.sort(key=lambda e: (*e.sort_key(), tickers_by_id[e.ticker_id].symbol))

    total = len(entries)
    page = entries[offset : offset + limit]
    raw_by_ticker = _latest_raw_snapshots_by_ticker(session, [e.ticker_id for e in page])

    items = [
        WatchlistEntry(
            ticker=tickers_by_id[e.ticker_id].symbol,
            company_name=_company_name(raw_by_ticker[e.ticker_id].payload.get("info", {}))
            if e.ticker_id in raw_by_ticker
            else None,
            sector=tickers_by_id[e.ticker_id].sector,
            reason=e.reason,
            reason_label=REASON_LABELS.get(e.reason, e.reason),
            detail=e.detail,
            gate=e.gate,
        )
        for e in page
    ]

    return WatchlistResponse(
        snapshot_date=target_date,
        total=total,
        limit=limit,
        offset=offset,
        counts_by_reason=counts_by_reason,
        counts_by_gate=counts_by_gate,
        items=items,
    )


@router.get("/backtest/latest", response_model=BacktestSummary)
def latest_backtest(session: Session = Depends(get_session)) -> BacktestSummary:
    """直近の擬似バックテスト結果(27.8)。

    **この結果をUIに常時出すのは意図的である。** 14.2は「上位デシルでも大半は
    外れる前提をUI上にも明示すること」を要件としており、リフト倍率や単調性が
    目標に届いていない事実を隠したままランキングだけを見せるのは、ツールとして
    誤った確信を与える。留保事項(`caveats`)も一緒に返す。
    """
    # 14.6:新旧のモデルが同じDBに共存しうる。バージョンで絞らないと、v3の実行結果を
    # 「v4の検証状況」として出してしまう——ランキング(`GET /candidates`)も
    # 資産相関(`_portfolio_outlook`)も較正写像(`load_calibration_map`)も現行
    # バージョンだけを見ているので、このページだけ別モデルの数字になる。
    # 検証状況の表示でモデルを取り違えるのは、何も表示しないより悪い。
    scoring_version = load_scoring_config().scoring_version
    run = (
        session.query(BacktestRun)
        .filter(BacktestRun.scoring_version == scoring_version)
        .order_by(BacktestRun.run_at.desc())
        .first()
    )
    if run is None:
        return BacktestSummary(
            caveats=[
                f"現行モデル({scoring_version})の擬似バックテストがまだ実行されていません"
                "(`run-backtest`)。"
            ]
        )

    metrics = run.metrics or {}
    caveats = [
        "年次財務諸表は現在時点で修正済みの値であり、当時開示されていた数値とは異なりうる(リステートメントの先読み)。",
        "実際の提出日は取得できないため、期末から90日後に開示されたと近似している。",
        "四半期FCFベースのキャッシュランウェイ・ゲートは過去に遡れないため、バックテストでは適用していない(ライブのほうが厳しい)。",
        "保有期間が重複しているため、独立な観測期間の数は評価日数よりはるかに少なく、統計的な検出力は低い。",
        "「オンペース率」の閾値(10倍/7年と同じ年率)は基準率が約25%あり、右裾の事象ではない。"
        "10バガー探索としての性能は、下の『右裾リフト』のほうが実態に近い(28.11)。",
    ]
    if not run.calibration_map:
        caveats.insert(
            0,
            "較正写像が学習されていない(観測数不足、または `calibration.enabled: false`)。"
            "確率はモデルの対数正規仮定そのままの値であり、実測頻度で裏打ちされていない。",
        )
    if (metrics.get("delisted_settlement_rate") or 0) == 0:
        caveats.insert(
            0,
            "上場廃止が1件も観測されていない。`tickers` は現在の上場一覧から作られるため、"
            "期間中に廃止された銘柄が母集団に存在せず、リターンは実態より良い方向へ偏っている(27.15)。",
        )

    validation_reasons: list[str] = []
    if (metrics.get("delisted_settlement_rate") or 0) <= 0:
        validation_reasons.append("delisted_settlement_rate_zero")
    if run.observation_count <= 0:
        validation_reasons.append("no_backtest_observations")
    for key, verdict in (metrics.get("kpi_verdicts") or {}).items():
        if verdict == "FAIL":
            validation_reasons.append(f"kpi_failed:{key}")
        elif verdict == "INSUFFICIENT_DATA":
            validation_reasons.append(f"kpi_insufficient:{key}")
    run_date = run.run_at.date() if run.run_at else None
    stale = run_date is None or (utc_today() - run_date).days > 14
    validation_status = "STALE" if stale else ("FAIL" if validation_reasons else "PASS")

    return BacktestSummary(
        run_at=run.run_at,
        scoring_version=run.scoring_version,
        horizon_years=metrics.get("horizon_years"),
        observation_count=run.observation_count,
        decile_monotonicity=metrics.get("decile_monotonicity"),
        lift_ratio=metrics.get("lift_ratio"),
        universe_on_pace_rate=metrics.get("universe_on_pace_rate"),
        top_decile_loss_rate=metrics.get("top_decile_loss_rate"),
        universe_loss_rate=metrics.get("universe_loss_rate"),
        calibration_error=metrics.get("calibration_error"),
        delisted_settlement_rate=metrics.get("delisted_settlement_rate"),
        delisted_count=metrics.get("delisted_count", 0),
        delisted_settled_count=metrics.get("delisted_settled_count", 0),
        bankruptcy_count=metrics.get("bankruptcy_count", 0),
        mna_count=metrics.get("mna_count", 0),
        unknown_delisting_count=metrics.get("unknown_delisting_count", 0),
        effective_independent_periods=metrics.get(
            "effective_independent_periods", metrics.get("effective_dates")
        ),
        validation_status=validation_status,
        validation_reasons=validation_reasons,
        rank_ic=metrics.get("rank_ic"),
        rank_ic_t_stat=metrics.get("rank_ic_t_stat"),
        lift_ratio_worst_date=metrics.get("lift_ratio_worst_date"),
        nowcast_cap_hit_rate=metrics.get("nowcast_cap_hit_rate"),
        asset_correlation=float(run.asset_correlation) if run.asset_correlation is not None else None,
        is_calibrated=bool(run.calibration_map),
        effective_dates=metrics.get("effective_dates"),
        non_overlapping=metrics.get("non_overlapping"),
        rank_ic_ci=metrics.get("rank_ic_ci"),
        lift_ratio_ci=metrics.get("lift_ratio_ci"),
        decile_monotonicity_ci=metrics.get("decile_monotonicity_ci"),
        mean_round_trip_cost_bps=metrics.get("mean_round_trip_cost_bps"),
        after_cost=metrics.get("after_cost"),
        portfolio=metrics.get("portfolio"),
        baselines=metrics.get("baselines"),
        gate_parity=metrics.get("gate_parity"),
        kpi_verdicts=metrics.get("kpi_verdicts") or {},
        per_date=[
            BacktestPerDate(
                base_date=d["base_date"],
                count=d["count"],
                universe_on_pace_rate=d["universe_on_pace_rate"],
                top_decile_on_pace_rate=d["top_decile_on_pace_rate"],
                lift_ratio=d["lift_ratio"],
                rank_ic=d["rank_ic"],
            )
            for d in metrics.get("per_date", [])
        ],
        tail_lifts=[
            BacktestTailLift(
                quantile=t["quantile"],
                median_threshold_return=t["median_threshold_return"],
                top_decile_hit_rate=t["top_decile_hit_rate"],
                lift=t["lift"],
                worst_date_lift=t["worst_date_lift"],
            )
            for t in metrics.get("tail_lifts", [])
        ],
        calibration_curve=[
            BacktestCalibrationBin(
                lower=b["lower"],
                upper=b["upper"],
                count=b["count"],
                mean_predicted=b["mean_predicted"],
                realized_rate=b["realized_rate"],
            )
            for b in metrics.get("calibration_curve", [])
        ],
        deciles=[
            BacktestDecile(
                decile=d["decile"],
                count=d["count"],
                mean_probability=d["mean_probability"],
                median_return=d["median_return"],
                on_pace_rate=d["on_pace_rate"],
                loss_rate=d["loss_rate"],
            )
            for d in metrics.get("deciles", [])
        ],
        caveats=caveats,
    )


@router.get("/candidates/{ticker}/peers", response_model=PeerResponse)
def get_candidate_peers(
    ticker: str = Path(..., pattern=TICKER_PATTERN), session: Session = Depends(get_session)
) -> PeerResponse:
    """L-2: compare only the current score-date cross-section."""
    target = session.query(Ticker).filter(Ticker.symbol == ticker.upper()).one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail=f"ticker '{ticker.upper()}' is not in the tracked universe")
    score_date = session.query(func.max(Score.score_date)).filter(Score.scoring_version == load_scoring_config().scoring_version).scalar()
    if score_date is None:
        return PeerResponse(ticker=target.symbol, peer_basis="none", peer_count=0)
    rows = session.query(Score, Ticker).join(Ticker, Ticker.id == Score.ticker_id).filter(Score.score_date == score_date, Score.scoring_version == load_scoring_config().scoring_version).all()
    raw = _latest_raw_snapshots_by_ticker(session, [t.id for _, t in rows])
    def candidate(t: Ticker) -> PeerCandidate:
        return PeerCandidate(t.symbol, t.industry, t.sector, _num_or_none((raw.get(t.id).payload.get("info") or {}).get("marketCap")) if raw.get(t.id) else None)
    target_candidate = candidate(target)
    selection = select_peers(target_candidate, [candidate(t) for _, t in rows])
    by_symbol = {t.symbol: (s, t) for s, t in rows}
    ranked = sorted((pair for pair in rows if pair[0].probability is not None), key=lambda pair: float(pair[0].probability), reverse=True)
    ranks = {t.id: i + 1 for i, (_, t) in enumerate(ranked)}
    items: list[PeerView] = []
    for peer in selection.peers:
        score, row_ticker = by_symbol[peer.ticker]
        factors = score.factors or {}
        items.append(PeerView(
            ticker=row_ticker.symbol, company_name=_company_name((raw.get(row_ticker.id).payload.get("info") or {}) if raw.get(row_ticker.id) else {}),
            market_cap=peer.market_cap, probability=float(score.probability) if score.probability is not None else None,
            rank=ranks.get(row_ticker.id), expected_moic=_num_or_none(factors.get("expected_moic")),
            revenue_growth=_num_or_none(factors.get("revenue_growth")), gross_margin=_num_or_none(factors.get("gross_margin")),
            ev_to_gross_profit=_num_or_none(factors.get("current_ev_to_gross_profit")),
            net_debt_to_gross_profit=_num_or_none(factors.get("net_debt_to_gross_profit")), share_growth_rate=_num_or_none(factors.get("dilution_cagr")),
        ))
    return PeerResponse(ticker=target.symbol, peer_basis=selection.peer_basis, peer_count=len(items), items=items)


@router.get("/benchmark/reference", response_model=BenchmarkReferenceResponse)
def get_benchmark_reference(
    horizon_years: float = Query(7.0, ge=_MIN_HORIZON_YEARS, le=_MAX_HORIZON_YEARS),
    session: Session = Depends(get_session),
) -> BenchmarkReferenceResponse:
    """L-9: historical IWM rolling MOIC distribution, not a forecast."""
    ticker = session.query(Ticker).filter(Ticker.symbol == "IWM", Ticker.is_benchmark.is_(True)).one_or_none()
    if ticker is None:
        return BenchmarkReferenceResponse(symbol="IWM", horizon_years=horizon_years, quantiles=None)
    return BenchmarkReferenceResponse(symbol="IWM", horizon_years=horizon_years, quantiles=rolling_moic_quantiles(_price_series(session, ticker.id, limit=8000), horizon_years))


@router.get("/filings/{ticker}", response_model=FilingListResponse)
def list_filings(
    ticker: str = Path(..., pattern=TICKER_PATTERN),
    limit: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> FilingListResponse:
    """その銘柄の `filings` を新しい順に返す(30.4.3)。

    詳細画面の「一次情報へ」の導線であり、判定の透明性を担保する——
    レッドフラグの`document_url`だけでなく、フラグの立たなかった提出も含めた
    全件を見られることが、機械判定を人間が検算するための最低条件になる。
    """
    symbol = ticker.upper()
    ticker_row = session.query(Ticker).filter(Ticker.symbol == symbol).one_or_none()
    if ticker_row is None:
        raise HTTPException(status_code=404, detail=f"ticker '{symbol}' is not in the tracked universe")

    total = session.query(Filing).filter_by(ticker_id=ticker_row.id).count()
    rows = (
        session.query(Filing)
        .filter_by(ticker_id=ticker_row.id)
        .order_by(Filing.filed_date.desc())
        .limit(limit)
        .all()
    )
    return FilingListResponse(
        ticker=symbol,
        total=total,
        items=[
            FilingListItem(
                accession_number=f.accession_number,
                form=f.form,
                filed_date=f.filed_date,
                report_date=f.report_date,
                items=f.items or [],
                document_url=f.document_url,
            )
            for f in rows
        ],
    )


# 30.8.1:表示ラベル。系列IDだけではUIで意味が伝わらない。
_MACRO_SERIES_LABELS: dict[str, str] = {
    "DGS10": "米10年債利回り",
    "DFII10": "米10年実質金利",
    "BAMLH0A0HYM2": "ハイイールドOAS",
    "DEXJPUS": "ドル円(USD/JPY)",
}
_MACRO_HISTORY_DAYS = 365


@router.get("/fx/usdjpy", response_model=FxRateResponse)
def get_usdjpy(session: Session = Depends(get_session)) -> FxRateResponse:
    """J-10(docs/investment_decision_gap_2026-08-29.md):円換算表示のための USD/JPY レート。

    まず `macro_series` の `DEXJPUS`(FRED、`collect-macro` が入れる)を見る。
    無ければ yfinance の `JPY=X` にフォールバックする。どちらも取れなければ
    `rate=None`(UI は通貨トグルを無効化して理由を出す)。
    **表示用の換算のみ**——税務・取得為替での損益計算はスコープ外(30.1.3)。
    """
    row = (
        session.query(MacroSeries)
        .filter(MacroSeries.series_id == "DEXJPUS", MacroSeries.value.isnot(None))
        .order_by(MacroSeries.observation_date.desc())
        .first()
    )
    if row is not None:
        return FxRateResponse(
            rate=float(row.value), as_of=row.observation_date, source="fred:DEXJPUS"
        )
    try:
        from autoscreener.collectors.yfinance_client import fetch_fx_rate

        rate = fetch_fx_rate("USD", "JPY")
    except Exception:  # noqa: BLE001
        rate = None
    if rate is not None and rate > 0:
        return FxRateResponse(rate=float(rate), as_of=utc_today(), source="yfinance:JPY=X")
    return FxRateResponse(rate=None, as_of=None, source="unavailable")


@router.get("/macro", response_model=MacroResponse)
def get_macro(session: Session = Depends(get_session)) -> MacroResponse:
    """マクロ系列の現在値・変化・直近1年の推移(30.8)。

    `FRED_API_KEY` 未設定でも200を返し `enabled=False` を明示する(500にしない、
    30.8.4の受け入れ基準)。**この値からスコアを自動調整する経路はコード上どこにも
    無い**(30.8.3)——表示と人間への示唆に留める。
    """
    fred_config = load_fred_config()
    settings = get_settings()
    if not fred_config.enabled or not settings.fred_api_key:
        return MacroResponse(enabled=False, series=[])

    today = utc_today()
    cutoff = today - datetime.timedelta(days=_MACRO_HISTORY_DAYS)
    series_views: list[MacroSeriesView] = []
    for series_id in fred_config.series_ids:
        rows = (
            session.query(MacroSeries)
            .filter(MacroSeries.series_id == series_id, MacroSeries.observation_date >= cutoff)
            .order_by(MacroSeries.observation_date.asc())
            .all()
        )
        if not rows:
            series_views.append(
                MacroSeriesView(series_id=series_id, label=_MACRO_SERIES_LABELS.get(series_id, series_id))
            )
            continue

        latest = rows[-1]

        def _value_near(days_ago: int) -> float | None:
            target_date = latest.observation_date - datetime.timedelta(days=days_ago)
            # 完全一致が無くても直近の観測(祝日・週末を考慮して±5日)で代用する。
            candidates = [r for r in rows if abs((r.observation_date - target_date).days) <= 5]
            if not candidates:
                return None
            closest = min(candidates, key=lambda r: abs((r.observation_date - target_date).days))
            return float(closest.value) if closest.value is not None else None

        value_3m_ago = _value_near(90)
        value_1y_ago = _value_near(365)
        latest_value = float(latest.value) if latest.value is not None else None

        series_views.append(
            MacroSeriesView(
                series_id=series_id,
                label=_MACRO_SERIES_LABELS.get(series_id, series_id),
                latest_value=latest_value,
                latest_observation_date=latest.observation_date,
                change_3m=(latest_value - value_3m_ago) if latest_value is not None and value_3m_ago is not None else None,
                change_1y=(latest_value - value_1y_ago) if latest_value is not None and value_1y_ago is not None else None,
                history=[
                    MacroSeriesPoint(observation_date=r.observation_date, value=float(r.value))
                    for r in rows
                    if r.value is not None
                ],
            )
        )

    return MacroResponse(enabled=True, series=series_views)


@router.get("/alerts", response_model=AlertsResponse)
def list_alerts(
    days: int = Query(30, ge=1, le=365),
    severity: str | None = Query(None, pattern="^(blocking|warning|info)$"),
    include_acknowledged: bool = False,
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> AlertsResponse:
    """直近アラート(新しい順、30.7.5)。既定では未解消のものだけを返す。"""
    cutoff = utc_today() - datetime.timedelta(days=days)
    query = session.query(Alert, Ticker).join(Ticker, Ticker.id == Alert.ticker_id).filter(
        Alert.triggered_on >= cutoff
    )
    if severity is not None:
        query = query.filter(Alert.severity == severity)
    if not include_acknowledged:
        query = query.filter(Alert.acknowledged_at.is_(None))
    rows = query.order_by(Alert.triggered_on.desc()).limit(limit).all()
    return AlertsResponse(
        total=len(rows),
        items=[
            AlertView(
                id=alert.id,
                ticker=ticker.symbol,
                code=alert.code,
                severity=alert.severity,
                source=alert.source,
                triggered_on=alert.triggered_on,
                detail=alert.detail,
                acknowledged_at=alert.acknowledged_at,
            )
            for alert, ticker in rows
        ],
    )


@router.get("/research/{ticker}", response_model=ResearchNoteResponse)
def get_research_note(ticker: str = Path(..., pattern=TICKER_PATTERN)) -> ResearchNoteResponse:
    """投資ノートのフロントマターと記入漏れ項目(30.7.5)。本文はMarkdownのまま返す。

    ノートが存在しない銘柄は404ではなく `exists=False` で200を返す——
    「まだ書いていない」は正常な状態であり、エラーではない(30.1.1 原則2)。
    """
    symbol = ticker.upper()
    try:
        note = load_note(symbol)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"ノートのフロントマターが壊れています: {exc}") from exc
    if note is None:
        return ResearchNoteResponse(ticker=symbol, exists=False)
    return ResearchNoteResponse(
        ticker=symbol,
        exists=True,
        front_matter=note.front_matter,
        body=note.body,
        missing_fields=note.missing_fields,
        is_complete=note.is_complete,
    )


def _exit_plan_view(
    note, achieved_moic: float | None, triggered_codes: set[str]
) -> tuple[NextTrim | None, list[str]]:
    """J-8:投資ノートの `exit_plan` から次の利食い段とテーゼ点灯を導く。

    **閾値は売却条件ではない。** 点灯は「価格に関係なく判断をやり直す」合図で
    あって、機械的な売りシグナルとして使ってはならない(`config/monitoring.yaml`
    冒頭と同じ立場)。
    """
    if note is None:
        return None, []
    exit_plan = note.front_matter.get("exit_plan")
    exit_plan = exit_plan if isinstance(exit_plan, dict) else {}

    next_trim: NextTrim | None = None
    rules = [r for r in (exit_plan.get("trim_rule") or []) if isinstance(r, dict) and r.get("at_moic") is not None]
    pending = sorted(
        (r for r in rules if achieved_moic is None or float(r["at_moic"]) > achieved_moic),
        key=lambda r: float(r["at_moic"]),
    )
    if pending:
        at_moic = float(pending[0]["at_moic"])
        next_trim = NextTrim(
            at_moic=at_moic,
            action=pending[0].get("action"),
            remaining_multiple=(at_moic - achieved_moic) if achieved_moic is not None else None,
        )

    thesis_break_indicators = {
        item.get("indicator")
        for item in (exit_plan.get("thesis_break") or [])
        if isinstance(item, dict) and item.get("indicator")
    }
    hits = sorted(thesis_break_indicators & triggered_codes)
    return next_trim, hits


def _sector_weights(rows: list[tuple]) -> dict[str, float]:
    total = sum(value for _, value in rows)
    if total <= 0:
        return {}
    by_sector: dict[str, float] = {}
    for sector, value in rows:
        key = sector or "(不明)"
        by_sector[key] = by_sector.get(key, 0.0) + value / total
    return by_sector


@router.get("/positions", response_model=PositionsResponse)
def list_positions(session: Session = Depends(get_session)) -> PositionsResponse:
    """保有一覧(30.7.5)。各行に最新スコア・監視指標・未解消アラート・ノートの
    記入状況・現在のポジション比率を含める。

    `config/positions.yaml` が存在しない状態では空リストと200を返す
    (30.7.6の受け入れ基準。保有が無い状態は正常)。
    """
    positions_config = load_positions_config()
    portfolio_config = load_portfolio_config()
    monitoring_config = load_monitoring_config()
    thresholds = MonitoringThresholds(
        revenue_growth_deceleration_quarters=monitoring_config.revenue_growth_deceleration_quarters,
        gross_margin_decline_quarters=monitoring_config.gross_margin_decline_quarters,
        share_count_annual_growth_ceiling=monitoring_config.share_count_annual_growth_ceiling,
        cash_runway_floor_months=monitoring_config.cash_runway_floor_months,
    )
    scoring_version = load_scoring_config().scoring_version

    # closed_on が入っている行はモニタリング対象から外れるが、記録としては残る
    # (30.7.1)。一覧には closed_on の有無に関わらずすべて出す——事後レビュー
    # (元文書 第13節)の材料として売却済みの行も見える必要があるため。
    open_positions = [p for p in positions_config.positions if p.closed_on is None]

    items: list[PositionView] = []
    sector_value_rows: list[tuple] = []
    total_cost = 0.0
    unprofitable_cost = 0.0

    for position in positions_config.positions:
        symbol = position.ticker.upper()
        ticker_row = session.query(Ticker).filter_by(symbol=symbol).one_or_none()
        cost_usd = position.shares * position.cost_basis_usd
        total_cost += cost_usd

        current_price = None
        probability = None
        monitoring_metrics: list[MonitoringMetricView] = []
        open_alert_count = 0
        sector = None
        net_income_negative = False

        if ticker_row is not None:
            sector = ticker_row.sector
            raw = (
                session.query(RawSnapshot)
                .filter_by(ticker_id=ticker_row.id)
                .order_by(RawSnapshot.snapshot_date.desc())
                .first()
            )
            info = (raw.payload.get("info") or {}) if raw else {}
            current_price = info.get("currentPrice") or info.get("regularMarketPrice")
            net_income = info.get("netIncomeToCommon")
            net_income_negative = net_income is not None and net_income < 0

            latest_score = (
                session.query(Score)
                .filter_by(ticker_id=ticker_row.id, scoring_version=scoring_version)
                .order_by(Score.score_date.desc())
                .first()
            )
            if latest_score is not None and latest_score.probability is not None:
                probability = float(latest_score.probability)

            if position.closed_on is None and raw is not None:
                share_rows = (
                    session.query(PriceSnapshot.trade_date, PriceSnapshot.shares_outstanding)
                    .filter(PriceSnapshot.ticker_id == ticker_row.id, PriceSnapshot.shares_outstanding.isnot(None))
                    .order_by(PriceSnapshot.trade_date.asc())
                    .all()
                )
                metrics = evaluate_monitoring(
                    raw.payload.get("quarterly_income_stmt") or {},
                    raw.payload.get("quarterly_cash_flow") or {},
                    info.get("totalCash"),
                    [(d, s) for d, s in share_rows],
                    thresholds,
                )
                monitoring_metrics = [
                    MonitoringMetricView(
                        code=m.code, label=m.label, current_value=m.current_value,
                        previous_value=m.previous_value, triggered=m.triggered,
                    )
                    for m in metrics
                ]

            open_alert_count = (
                session.query(Alert)
                .filter(Alert.ticker_id == ticker_row.id, Alert.acknowledged_at.is_(None))
                .count()
            )

        current_value_usd = current_price * position.shares if current_price is not None else None
        unrealized_return = (
            (current_price - position.cost_basis_usd) / position.cost_basis_usd
            if current_price is not None
            else None
        )
        # J-8:達成倍率 = 現在値 ÷ 取得単価。
        achieved_moic = (
            current_price / position.cost_basis_usd
            if current_price is not None and position.cost_basis_usd > 0
            else None
        )
        target_moic = load_scoring_config().target_moic
        horizon_years = load_scoring_config().horizon_years
        remaining_years = max(0.0, horizon_years - (utc_today() - position.opened_on).days / 365.25)
        remaining_moic = target_moic / achieved_moic if achieved_moic not in (None, 0) else None
        required_from_here = (
            remaining_moic ** (1 / remaining_years) - 1
            if remaining_moic is not None and remaining_moic > 1 and remaining_years > 0 else None
        )
        required_at_entry = target_moic ** (1 / horizon_years) - 1

        try:
            note = load_note(symbol)
        except Exception:
            note = None

        triggered_codes = {m.code for m in monitoring_metrics if m.triggered}
        next_trim, thesis_break_hits = _exit_plan_view(note, achieved_moic, triggered_codes)
        external_monitoring_available = ticker_row is not None and any(
            session.query(model.id).filter_by(ticker_id=ticker_row.id).first() is not None
            for model in (CustomerConcentration, Guidance, LitigationEvent)
        )
        thesis_evaluation_state = (
            "triggered" if thesis_break_hits else "none" if external_monitoring_available else "unassessed"
        )

        items.append(
            PositionView(
                ticker=symbol,
                opened_on=position.opened_on,
                closed_on=position.closed_on,
                shares=position.shares,
                cost_basis_usd=position.cost_basis_usd,
                binary_event=position.binary_event,
                current_price=current_price,
                current_value_usd=current_value_usd,
                unrealized_return=unrealized_return,
                probability=probability,
                monitoring_metrics=monitoring_metrics,
                open_alert_count=open_alert_count,
                note_exists=note is not None,
                note_is_complete=note.is_complete if note is not None else False,
                note_missing_fields=note.missing_fields if note is not None else [],
                achieved_moic=achieved_moic,
                next_trim=next_trim,
                thesis_break_hits=thesis_break_hits,
                thesis_evaluation_state=thesis_evaluation_state,
                remaining_moic_to_target=remaining_moic,
                remaining_years=remaining_years if current_price is not None else None,
                required_cagr_from_here=required_from_here,
                required_cagr_at_entry=required_at_entry if current_price is not None else None,
            )
        )

        if position.closed_on is None:
            sector_value_rows.append((sector, cost_usd))
            if net_income_negative:
                unprofitable_cost += cost_usd

    sector_weights = _sector_weights(sector_value_rows)
    open_cost_total = sum(v for _, v in sector_value_rows)
    sector_cap_breaches = [s for s, w in sector_weights.items() if w > portfolio_config.sector_cap]
    position_cap_breaches = [
        item.ticker
        for item, (sector, cost) in zip(
            [p for p in items if p.closed_on is None], sector_value_rows
        )
        if open_cost_total > 0 and cost / open_cost_total > portfolio_config.per_position_cap
    ]
    unprofitable_share = (unprofitable_cost / open_cost_total) if open_cost_total > 0 else None

    # J-9:保有群をまとめて持ったときの見通し(相関込み)。ランキング画面の
    # `_portfolio_outlook` をそのまま使う——保有0件では None。
    open_symbols = {p.ticker.upper() for p in open_positions}
    open_probabilities = [
        item.probability
        for item in items
        if item.closed_on is None and item.probability is not None
    ]
    portfolio_outlook = _portfolio_outlook(session, open_probabilities)

    portfolio_value = portfolio_config.portfolio_value_usd
    cash_ratio = (
        (portfolio_value - open_cost_total) / portfolio_value if portfolio_value > 0 else None
    )

    # 保有と現在のランキング上位の重複(同じテーゼに二重に賭けていないか)。
    ranking_overlap: list[str] = []
    if open_symbols:
        latest_score_date = (
            session.query(func.max(Score.score_date))
            .filter(Score.scoring_version == scoring_version)
            .scalar()
        )
        if latest_score_date is not None:
            top_symbols = [
                sym
                for (sym,) in session.query(Ticker.symbol)
                .join(Score, Score.ticker_id == Ticker.id)
                .filter(
                    Score.score_date == latest_score_date,
                    Score.scoring_version == scoring_version,
                    Score.probability.isnot(None),
                )
                .order_by(Score.probability.desc())
                .limit(_RANKING_OVERLAP_TOP_N)
                .all()
            ]
            ranking_overlap = [s for s in top_symbols if s in open_symbols]

    # L-8: observed pair correlations, kept separate from backtest rho.
    open_tickers = session.query(Ticker).filter(Ticker.symbol.in_(open_symbols)).all() if open_symbols else []
    pairwise = pairwise_return_correlation({t.symbol: _price_series(session, t.id) for t in open_tickers})
    correlations = [
        CorrelationView(a=a, b=b, correlation=corr, overlap_days=overlap)
        for (a, b), (corr, overlap) in sorted(pairwise.items(), key=lambda item: item[1][0], reverse=True)[:10]
    ]

    return PositionsResponse(
        items=items,
        summary=PortfolioSummary(
            total_cost_usd=total_cost,
            position_count=len(open_positions),
            sector_weights=sector_weights,
            sector_cap_breaches=sector_cap_breaches,
            position_cap_breaches=position_cap_breaches,
            unprofitable_share=unprofitable_share,
        ),
        portfolio=portfolio_outlook,
        cash_ratio=cash_ratio,
        ranking_overlap=ranking_overlap,
        correlations=correlations,
    )


# ---------------------------------------------------------------------------
# K-9:LLM(Claude / OpenAI互換)の定性分析。**参照は読み取り専用**(18.6)。
#
# 要約(`summarize-filings`)と定性評価(`score-qualitative`)の生成は CLI 専用。
# レポート(`generate-report`)だけは、UIからモデル/プロバイダを選んで実行できる
# `POST /llm/report/generate` を持つ(docs/ui_llm_provider_selection_2026-08-30.md)。
# これは API 層で唯一の書き込みで、原則18.6を意図的に破る。**HTTP1本で課金が
# 発生する**ので、`confirm=true` 必須・短間隔レート制限・同時実行ロックで守る。
#
# どの応答にも `advisory` と `disclaimer` が付く。ゲートにもスコアにも
# 入らない値であることを、表を分けたサーバ側の事情を知らない利用者
# (自作スクリプト等)にも伝えるため。
# ---------------------------------------------------------------------------

# 生成の同時実行ロックと最終実行時刻。プロセス内だけの防御で十分——このAPIは
# 個人ローカル利用前提(11.1解釈A)であり、複数ワーカーでは動かさない。
_REPORT_GEN_LOCK = threading.Lock()
_REPORT_GEN_MIN_INTERVAL_SECONDS = 30.0
_report_gen_last_at: float = 0.0


def _llm_usage_view(raw: dict | None) -> LlmUsageView | None:
    return LlmUsageView(**raw) if isinstance(raw, dict) else None


def _llm_source_refs(raw) -> list[LlmSourceRef]:
    """`source_refs` は kind によって形が違う(リスト/dict)。リスト形だけを扱う。"""
    if not isinstance(raw, list):
        return []
    return [LlmSourceRef(**item) for item in raw if isinstance(item, dict)]


def _report_row_to_response(row: LlmAnalysis | None) -> LlmReportResponse:
    """`llm_analyses`(kind='daily_report')の1行を応答に整形する。未生成なら
    `exists=False`——404にしないのは `GET /research/{ticker}` と同じ立場で、
    生成には課金が伴うので**作っていないのが既定**だから。"""
    if row is None:
        return LlmReportResponse(exists=False)

    refs = row.source_refs if isinstance(row.source_refs, dict) else {}
    ranked = [
        item["symbol"]
        for item in (refs.get("ranked_symbols") or [])
        if isinstance(item, dict) and item.get("symbol")
    ]
    try:
        score_date: datetime.date | None = datetime.date.fromisoformat(row.source_key)
    except ValueError:
        # `source_key` の形が将来変わっても、レポート本文は返せるようにする。
        score_date = None

    return LlmReportResponse(
        exists=True,
        score_date=score_date,
        as_of=row.as_of,
        model=row.model,
        effort=row.effort,
        content=row.content,
        ranked_symbols=ranked,
        usage=_llm_usage_view(row.usage),
        created_at=row.created_at,
    )


def _latest_report_row(session: Session, date: datetime.date | None) -> LlmAnalysis | None:
    query = session.query(LlmAnalysis).filter(
        LlmAnalysis.kind == "daily_report", LlmAnalysis.ticker_id.is_(None)
    )
    if date is not None:
        query = query.filter(LlmAnalysis.source_key == date.isoformat())
    return query.order_by(LlmAnalysis.as_of.desc(), LlmAnalysis.id.desc()).first()


# **宣言順が意味を持つ。** `/llm/report` と `/llm/providers` を `/llm/{ticker}` より
# 先に置く。後に置くと "REPORT" が TICKER_PATTERN に一致してしまい、レポートを
# 取りに行ったつもりが「REPORT という銘柄」を探して404になる。
@router.get("/llm/report", response_model=LlmReportResponse)
def get_llm_report(
    date: datetime.date | None = Query(None, description="対象のscore_date。省略時は最新。"),
    session: Session = Depends(get_session),
) -> LlmReportResponse:
    """当日ランキングの説明文(K-9)。未生成なら `exists=False` で200を返す。"""
    return _report_row_to_response(_latest_report_row(session, date))


# --- プロバイダ一覧(UIのモデル選択が読む) --------------------------------
_SUGGESTED_MODELS: dict[str, list[str]] = {
    "anthropic": ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"],
    # openai_compat は接続先次第。よく使うものを出すが、UIは自由入力も許す。
    "openai_compat": ["gpt-5", "gpt-5-mini", "o4-mini"],
}


@router.get("/llm/providers", response_model=LlmProvidersResponse)
def get_llm_providers() -> LlmProvidersResponse:
    """選べるプロバイダと、それぞれ実際に呼べるか(APIキーの有無)。

    `POST /llm/report/generate` の前にUIがこれを引き、未設定のプロバイダを
    選ばせないようにする。キーはアクティブな接続プロファイルと .env の両方を見る。
    """
    from autoscreener.runtime_settings import get_active_connection, resolve_llm_config, secret_is_set

    active = get_active_connection()
    cfg = resolve_llm_config(active=active)
    default_model = cfg.model
    providers = [
        LlmProviderInfo(
            provider="anthropic",
            configured=secret_is_set("anthropic", active=active),
            default_model=default_model if cfg.provider == "anthropic" else "claude-opus-5",
            suggested_models=_SUGGESTED_MODELS["anthropic"],
        ),
        LlmProviderInfo(
            provider="openai_compat",
            configured=secret_is_set("openai_compat", active=active),
            default_model=default_model if cfg.provider == "openai_compat" else "gpt-5",
            suggested_models=_SUGGESTED_MODELS["openai_compat"],
        ),
    ]
    return LlmProvidersResponse(current=cfg.provider, providers=providers)


# --- 名前付きLLM接続プロファイル(一覧・作成・編集・削除・アクティブ切替) ----


def _connection_view(row: LlmConnection) -> LlmConnectionView:
    """1行を応答に整形する。**`api_key` の本体は決して載せない。**"""
    return LlmConnectionView(
        id=row.id,
        name=row.name,
        provider=row.provider,
        base_url=row.base_url,
        model=row.model,
        effort=row.effort,
        send_effort=bool(row.send_effort),
        api_key_set=bool(row.api_key) and row.api_key != "CHANGE_ME",
        is_active=bool(row.is_active),
    )


def _validate_connection_shape(provider: str, effort: str | None) -> None:
    """provider / effort を LlmConfig のバリデータに通す(不正なら 422)。"""
    from autoscreener.config import LlmConfig, load_llm_config

    fields = load_llm_config().model_dump()
    fields["provider"] = provider
    if effort:
        fields["effort"] = effort
    try:
        LlmConfig(**fields)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"設定が不正です: {exc}") from exc


@router.get("/llm/settings", response_model=LlmSettingsResponse)
def get_llm_settings() -> LlmSettingsResponse:
    """いま実際に使われる LLM 接続の実効値(yaml/.env にアクティブなプロファイルを
    重ねた結果)。**APIキー本体は返さない。**"""
    from autoscreener.runtime_settings import get_active_connection, resolve_llm_config, secret_is_set

    active = get_active_connection()
    cfg = resolve_llm_config(active=active)
    return LlmSettingsResponse(
        provider=cfg.provider,
        base_url=cfg.base_url,
        model=cfg.model,
        effort=cfg.effort,
        send_effort=cfg.send_effort,
        anthropic_api_key_set=secret_is_set("anthropic", active=active),
        openai_api_key_set=secret_is_set("openai_compat", active=active),
        active_connection_id=active.id if active else None,
        active_connection_name=active.name if active else None,
    )


@router.get("/llm/connections", response_model=LlmConnectionsResponse)
def list_llm_connections(session: Session = Depends(get_session)) -> LlmConnectionsResponse:
    """保存済みの接続プロファイル一覧(APIキー本体は含まない)。"""
    rows = session.query(LlmConnection).order_by(LlmConnection.name).all()
    return LlmConnectionsResponse(connections=[_connection_view(r) for r in rows])


@router.post("/llm/connections", response_model=LlmConnectionView, status_code=201)
def create_llm_connection(body: LlmConnectionCreate) -> LlmConnectionView:
    """接続プロファイルを新規保存する。`name` は一意。`activate=true` で作成と同時に有効化。"""
    _validate_connection_shape(body.provider, body.effort)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="name は必須です。")

    with _write_session_scope() as session:
        if session.query(LlmConnection).filter(LlmConnection.name == name).first():
            raise HTTPException(status_code=409, detail=f"'{name}' は既に存在します。")
        if body.activate:
            session.query(LlmConnection).filter(LlmConnection.is_active.is_(True)).update(
                {"is_active": False}, synchronize_session=False
            )
        row = LlmConnection(
            name=name,
            provider=body.provider,
            base_url=(body.base_url or None),
            model=(body.model or None),
            effort=(body.effort or None),
            send_effort=bool(body.send_effort),
            api_key=(body.api_key or None),
            is_active=bool(body.activate),
        )
        session.add(row)
        session.flush()
        return _connection_view(row)


@router.post("/llm/connections/deactivate", response_model=LlmConnectionsResponse)
def deactivate_llm_connections(session: Session = Depends(get_session)) -> LlmConnectionsResponse:
    """アクティブを解除する(以後は collection.yaml / .env のまま)。"""
    with _write_session_scope() as write:
        write.query(LlmConnection).filter(LlmConnection.is_active.is_(True)).update(
            {"is_active": False}, synchronize_session=False
        )
    rows = session.query(LlmConnection).order_by(LlmConnection.name).all()
    return LlmConnectionsResponse(connections=[_connection_view(r) for r in rows])


@router.put("/llm/connections/{conn_id}", response_model=LlmConnectionView)
def update_llm_connection(conn_id: int, body: LlmConnectionUpdate) -> LlmConnectionView:
    """接続プロファイルを編集する。

    `None` のフィールドは触らない。`base_url` / `model` / `effort` に `""` を
    渡すとその項目をクリア(= yaml の既定へフォールバック)。`api_key` に `""`
    で保存済みキーを削除。
    """
    with _write_session_scope() as session:
        row = session.get(LlmConnection, conn_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"接続 id={conn_id} は存在しません。")

        if body.name is not None:
            new_name = body.name.strip()
            if not new_name:
                raise HTTPException(status_code=422, detail="name は空にできません。")
            clash = (
                session.query(LlmConnection)
                .filter(LlmConnection.name == new_name, LlmConnection.id != conn_id)
                .first()
            )
            if clash:
                raise HTTPException(status_code=409, detail=f"'{new_name}' は既に存在します。")
            row.name = new_name

        provider = body.provider if body.provider is not None else row.provider
        effort = row.effort if body.effort is None else (body.effort or None)
        _validate_connection_shape(provider, effort)
        row.provider = provider
        row.effort = effort
        if body.base_url is not None:
            row.base_url = body.base_url or None
        if body.model is not None:
            row.model = body.model or None
        if body.send_effort is not None:
            row.send_effort = bool(body.send_effort)
        if body.api_key is not None:
            row.api_key = body.api_key or None

        session.flush()
        return _connection_view(row)


@router.post("/llm/connections/{conn_id}/activate", response_model=LlmConnectionView)
def activate_llm_connection(conn_id: int) -> LlmConnectionView:
    """このプロファイルをアクティブにする(他の is_active は下ろす)。"""
    with _write_session_scope() as session:
        row = session.get(LlmConnection, conn_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"接続 id={conn_id} は存在しません。")
        session.query(LlmConnection).filter(
            LlmConnection.is_active.is_(True), LlmConnection.id != conn_id
        ).update({"is_active": False}, synchronize_session=False)
        row.is_active = True
        session.flush()
        return _connection_view(row)


@router.delete("/llm/connections/{conn_id}", status_code=204)
def delete_llm_connection(conn_id: int) -> None:
    """接続プロファイルを削除する。アクティブだった場合は以後 yaml / .env に戻る。"""
    with _write_session_scope() as session:
        row = session.get(LlmConnection, conn_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"接続 id={conn_id} は存在しません。")
        session.delete(row)


@router.post("/llm/report/generate", response_model=GenerateReportResult)
def post_generate_llm_report(
    body: GenerateReportRequest,
    session: Session = Depends(get_session),
) -> GenerateReportResult:
    """当日ランキングの説明文をUIから生成する(K-9)。

    **API層で唯一の書き込みで、課金が発生する。** `confirm=true` が無ければ 400。
    直近30秒以内の再要求は 429。別の生成が進行中なら 409。`provider` /
    `model` / `effort` は省略時 `config/collection.yaml` の既定を使う。
    """
    global _report_gen_last_at

    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="confirm=true が必要です(レポート生成はLLM APIの課金が発生します)。",
        )

    from autoscreener.batch.generate_report import generate_report
    from autoscreener.llm.client import build_provider
    from autoscreener.llm.errors import LlmDisabled, LlmError
    from autoscreener.runtime_settings import resolve_llm_config

    try:
        # collection.yaml に app_settings(UI保存の base_url / model / provider)を重ねた設定。
        cfg = resolve_llm_config()
    except Exception as exc:  # noqa: BLE001 — 設定不正はそのまま伝える
        raise HTTPException(status_code=500, detail=f"LLM設定の読み込みに失敗: {exc}") from exc

    overrides: dict[str, object] = {}
    if body.provider is not None:
        overrides["provider"] = body.provider
    if body.model is not None:
        overrides["model"] = body.model
    if body.effort is not None:
        overrides["effort"] = body.effort
    try:
        # 新インスタンスとして作り直すことで provider/effort のバリデータを通す
        # (model_copy(update=...) は再検証しない)。
        cfg = type(cfg)(**{**cfg.model_dump(), **overrides})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"指定が不正です: {exc}") from exc

    # プロバイダをここで組み立てる(ネットワークには触れない)。`generate_report`
    # は内部で LlmDisabled を握って0件で正常終了してしまうので、UIに「未設定」を
    # 伝えるにはこちらで先に捕まえる必要がある。
    try:
        client = build_provider(cfg)
    except LlmDisabled as exc:
        raise HTTPException(
            status_code=409,
            detail=f"選択したプロバイダは利用できません(APIキー未設定など): {exc}",
        ) from exc

    if not _REPORT_GEN_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="別のレポート生成が進行中です。")
    try:
        elapsed = time.monotonic() - _report_gen_last_at
        if elapsed < _REPORT_GEN_MIN_INTERVAL_SECONDS:
            retry_after = int(_REPORT_GEN_MIN_INTERVAL_SECONDS - elapsed) + 1
            raise HTTPException(
                status_code=429,
                detail=f"レポート生成は{int(_REPORT_GEN_MIN_INTERVAL_SECONDS)}秒に1回までです。",
                headers={"Retry-After": str(retry_after)},
            )
        try:
            counts = generate_report(
                score_date=body.score_date,
                top_n=body.top_n,
                config=cfg,
                client=client,
                raise_on_llm_error=True,
            )
        except LlmError as exc:
            raise HTTPException(status_code=502, detail=f"LLM呼び出しに失敗: {exc}") from exc
        _report_gen_last_at = time.monotonic()
    finally:
        _REPORT_GEN_LOCK.release()

    if counts.get("failures"):
        raise HTTPException(status_code=502, detail="レポート生成に失敗しました(ログを確認してください)。")

    row = _latest_report_row(session, body.score_date)
    return GenerateReportResult(created=bool(counts.get("new_rows")), report=_report_row_to_response(row))


@router.get("/llm/{ticker}", response_model=LlmTickerAnalysisResponse)
def get_llm_analysis(
    ticker: str = Path(..., pattern=TICKER_PATTERN),
    session: Session = Depends(get_session),
) -> LlmTickerAnalysisResponse:
    """その銘柄の要約(複数)と定性評価(最新1件)。

    要約は提出書類のセクションごとに1行あるので複数返す。定性評価は
    **最新の1件だけ**を返す——過去の評価を並べても、どれが今の見解かが
    かえって分かりにくくなる(履歴が要るときはDBを直接見る)。
    """
    symbol = ticker.upper()
    ticker_row = session.query(Ticker).filter(Ticker.symbol == symbol).one_or_none()
    if ticker_row is None:
        raise HTTPException(
            status_code=404, detail=f"ticker '{symbol}' is not in the tracked universe"
        )

    rows = (
        session.query(LlmAnalysis)
        .filter(LlmAnalysis.ticker_id == ticker_row.id)
        .order_by(LlmAnalysis.as_of.desc(), LlmAnalysis.id.desc())
        .all()
    )

    summaries = [
        LlmFilingSummaryView(
            source_key=row.source_key,
            as_of=row.as_of,
            model=row.model,
            effort=row.effort,
            prompt_fingerprint=row.prompt_fingerprint,
            content=row.content or "",
            source_refs=_llm_source_refs(row.source_refs),
            usage=_llm_usage_view(row.usage),
            created_at=row.created_at,
        )
        for row in rows
        if row.kind == "filing_summary" and row.content
    ]

    qualitative: LlmQualitativeView | None = None
    for row in rows:
        if row.kind != "qualitative" or not isinstance(row.data, dict):
            continue
        data = row.data
        qualitative = LlmQualitativeView(
            source_key=row.source_key,
            as_of=row.as_of,
            model=row.model,
            effort=row.effort,
            prompt_fingerprint=row.prompt_fingerprint,
            business_summary=data.get("business_summary"),
            moat_evidence=list(data.get("moat_evidence") or []),
            key_risks=list(data.get("key_risks") or []),
            evidence_gaps=list(data.get("evidence_gaps") or []),
            conviction=data.get("conviction"),
            conviction_rationale=data.get("conviction_rationale"),
            source_refs=_llm_source_refs(row.source_refs),
            usage=_llm_usage_view(row.usage),
            created_at=row.created_at,
        )
        break  # 並びは新しい順なので、最初に見つかったものが最新。

    return LlmTickerAnalysisResponse(
        ticker=symbol, summaries=summaries, qualitative=qualitative
    )


# ---------------------------------------------------------------------------
# TENX Investment Decision v2 — independent Live Intelligence endpoints.
# These routes never update or reinterpret Score.probability.
# ---------------------------------------------------------------------------

def _intelligence_ticker(session: Session, ticker: str) -> Ticker:
    row = session.query(Ticker).filter(func.upper(Ticker.symbol) == ticker.upper()).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="ticker not found")
    return row


def _pit_cutoff(as_of: datetime.date) -> datetime.datetime:
    return datetime.datetime.combine(
        as_of + datetime.timedelta(days=1), datetime.time.min, tzinfo=datetime.timezone.utc
    )


def _model_row_dict(row) -> dict:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns if column.name != "ticker_id"}


def _intelligence_response(ticker: str, as_of: datetime.date, rows: list, *, source: str | None = None,
                           data=None, coverage_status: CoverageStatus | str | None = None,
                           coverage=None, reason_code: CoverageReasonCode | str | None = None,
                           reason_detail: str | None = None, retryable: bool | None = None) -> InvestmentIntelligenceResponse:
    """Build a PIT-safe response without mistaking an empty result for zero."""
    observed = [getattr(row, "observed_at", None) for row in rows]
    observed = [value for value in observed if value is not None]
    statuses = [getattr(row, "coverage_status", None) for row in rows]
    status = coverage_status or getattr(coverage, "coverage_status", None)
    if status is None and rows:
        status = CoverageStatus.COLLECTED_WITH_DATA if any(
            item == CoverageStatus.COLLECTED_WITH_DATA for item in statuses
        ) else (statuses[0] or CoverageStatus.COLLECTED_WITH_DATA)
    status = CoverageStatus(status or CoverageStatus.NOT_COLLECTED)
    latest = getattr(coverage, "observed_at", None) or (max(observed) if observed else None)
    if latest is not None and latest.date() > as_of:
        latest = None
    resolved_source = source or getattr(coverage, "source", None) or (getattr(rows[0], "source", None) if rows else None)
    resolved_source_url = getattr(coverage, "source_url", None) or (getattr(rows[0], "source_url", None) if rows else None)
    return InvestmentIntelligenceResponse(
        ticker=ticker, as_of=as_of, coverage_status=status,
        reason_code=str(reason_code or getattr(coverage, "reason_code", None) or "") or None,
        reason_detail=reason_detail or getattr(coverage, "reason_detail", None),
        observed_at=latest,
        source=resolved_source, source_url=resolved_source_url,
        data_age_days=(as_of - latest.date()).days if latest else None,
        retryable=retryable if retryable is not None else getattr(coverage, "retryable", None),
        data=data if data is not None else [_model_row_dict(row) for row in rows],
    )


def _dataset_coverage(session: Session, ticker_id: int, dataset: str, as_of: datetime.date):
    return latest_dataset_coverage(session, ticker_id, dataset, as_of)


@router.get("/candidates/{ticker}/reverse-valuation", response_model=ReverseValuationResponse)
def get_reverse_valuation(
    ticker: str = Path(..., pattern=TICKER_PATTERN),
    horizon_years: int = Query(7, ge=1, le=15),
    as_of: datetime.date | None = Query(None),
    session: Session = Depends(get_session),
) -> ReverseValuationResponse:
    date = as_of or utc_today()
    ticker_row = _intelligence_ticker(session, ticker)
    route = classify_model_family(CompanyModelProfile(ticker_row.sector, ticker_row.industry))
    score = session.query(Score).filter(
        Score.ticker_id == ticker_row.id, Score.score_date <= date
    ).order_by(Score.score_date.desc()).first()
    if score is None or not score.inputs:
        return ReverseValuationResponse(
            ticker=ticker_row.symbol, as_of=date, horizon_years=horizon_years,
            coverage_status="not_collected", model_family=route.model_family,
            model_supported=route.supported,
        )
    config = load_scoring_config().model_copy(update={"horizon_years": horizon_years})
    inputs = MoicInputs.from_dict(score.inputs)
    scenario_rows = solve_scenarios(inputs, config)
    consensus = session.query(AnalystConsensusSnapshot).filter(
        AnalystConsensusSnapshot.ticker_id == ticker_row.id,
        AnalystConsensusSnapshot.observed_at < _pit_cutoff(date),
        AnalystConsensusSnapshot.revenue_mean.isnot(None),
    ).order_by(AnalystConsensusSnapshot.observed_at.desc(), AnalystConsensusSnapshot.period_end.asc()).first()
    guidance = session.query(ManagementGuidanceSnapshot).filter(
        ManagementGuidanceSnapshot.ticker_id == ticker_row.id,
        ManagementGuidanceSnapshot.observed_at < _pit_cutoff(date),
        ManagementGuidanceSnapshot.metric.ilike("%revenue%"),
    ).order_by(ManagementGuidanceSnapshot.observed_at.desc()).first()
    consensus_growth = (
        float(consensus.revenue_mean) / inputs.revenue_latest - 1
        if consensus and consensus.revenue_mean is not None and inputs.revenue_latest > 0 else None
    )
    guidance_mid = None
    if guidance and inputs.revenue_latest > 0 and (guidance.low is not None or guidance.high is not None):
        values = [float(v) for v in (guidance.low, guidance.high) if v is not None]
        guidance_mid = sum(values) / len(values) / inputs.revenue_latest - 1
    tenx_growth = (score.factors or {}).get("initial_growth_rate")
    scenarios = [ReverseValuationScenarioView(
        **vars(item),
        tenx_gap=(tenx_growth - item.implied_revenue_cagr if tenx_growth is not None and item.implied_revenue_cagr is not None else None),
        consensus_gap=(consensus_growth - item.implied_revenue_cagr if consensus_growth is not None and item.implied_revenue_cagr is not None else None),
        guidance_gap=(guidance_mid - item.implied_revenue_cagr if guidance_mid is not None and item.implied_revenue_cagr is not None else None),
    ) for item in scenario_rows]
    dist = None
    if score.log_moic_mu is not None and score.log_moic_sigma is not None and score.survival_probability is not None:
        dist = return_distribution(float(score.log_moic_mu), float(score.log_moic_sigma), float(score.survival_probability), horizon_years)
    return ReverseValuationResponse(
        ticker=ticker_row.symbol, as_of=date, horizon_years=horizon_years,
        coverage_status="collected_with_data", model_family=route.model_family,
        model_supported=route.supported, tenx_initial_growth=tenx_growth,
        consensus_growth=consensus_growth, management_guidance_growth=guidance_mid,
        scenarios=scenarios, return_distribution=dist,
    )


@router.get("/candidates/{ticker}/consensus", response_model=InvestmentIntelligenceResponse)
def get_consensus(ticker: str = Path(..., pattern=TICKER_PATTERN), as_of: datetime.date | None = Query(None),
                  session: Session = Depends(get_session)) -> InvestmentIntelligenceResponse:
    date = as_of or utc_today(); t = _intelligence_ticker(session, ticker)
    rows = session.query(AnalystConsensusSnapshot).filter(
        AnalystConsensusSnapshot.ticker_id == t.id,
        AnalystConsensusSnapshot.observed_at < _pit_cutoff(date),
    ).order_by(AnalystConsensusSnapshot.observed_at.desc(), AnalystConsensusSnapshot.period_end.asc()).limit(50).all()
    return _intelligence_response(t.symbol, date, rows)


@router.get("/candidates/{ticker}/reinvestment-quality", response_model=InvestmentIntelligenceResponse)
def get_reinvestment_quality(ticker: str = Path(..., pattern=TICKER_PATTERN), as_of: datetime.date | None = Query(None),
                             session: Session = Depends(get_session)) -> InvestmentIntelligenceResponse:
    date = as_of or utc_today(); t = _intelligence_ticker(session, ticker)
    raw = session.query(RawSnapshot).filter(
        RawSnapshot.ticker_id == t.id, RawSnapshot.available_from <= date
    ).order_by(RawSnapshot.available_from.desc()).first()
    if raw is None:
        return _intelligence_response(t.symbol, date, [], reason_code=CoverageReasonCode.NO_RAW_SNAPSHOT)
    history = build_financial_history(raw.payload)
    annual = [p for p in history.annual if p.period_end <= date]
    if len(annual) < 2:
        return InvestmentIntelligenceResponse(ticker=t.symbol, as_of=date, coverage_status=CoverageStatus.NOT_COLLECTED,
            reason_code=CoverageReasonCode.INSUFFICIENT_ANNUAL_HISTORY, source=raw.source, data=[])
    start, end = annual[0], annual[-1]
    years = max(1.0, (end.period_end - start.period_end).days / 365.25)
    nopat_start = start.operating_income * 0.79 if start.operating_income is not None and start.operating_income > 0 else None
    nopat_end = end.operating_income * 0.79 if end.operating_income is not None and end.operating_income > 0 else None
    ic_start = (start.total_debt or 0) - (start.cash_and_equivalents or 0) if start.total_debt is not None and start.cash_and_equivalents is not None else None
    ic_end = (end.total_debt or 0) - (end.cash_and_equivalents or 0) if end.total_debt is not None and end.cash_and_equivalents is not None else None
    quality = calculate_reinvestment_quality(
        years=years, revenue_start=start.revenue, revenue_end=end.revenue,
        gross_profit_start=start.gross_profit, gross_profit_end=end.gross_profit,
        fcf_start=start.free_cash_flow, fcf_end=end.free_cash_flow,
        shares_start=start.shares_outstanding, shares_end=end.shares_outstanding,
        nopat_start=nopat_start, nopat_end=nopat_end,
        invested_capital_start=ic_start, invested_capital_end=ic_end,
    )
    return InvestmentIntelligenceResponse(ticker=t.symbol, as_of=date, coverage_status="collected_with_data",
        source=raw.source, data_age_days=(date - raw.available_from).days,
        data={"period_years": years, **vars(quality)})


def _rows_endpoint(session: Session, ticker: str, as_of: datetime.date, model, *, limit: int = 100, order_column=None):
    t = _intelligence_ticker(session, ticker)
    query = session.query(model).filter(model.ticker_id == t.id)
    if hasattr(model, "observed_at"):
        query = query.filter(model.observed_at < _pit_cutoff(as_of))
    order = order_column if order_column is not None else getattr(
        model, "observed_at", getattr(model, "id")
    )
    rows = query.order_by(order.desc()).limit(limit).all()
    return t, rows


@router.get("/candidates/{ticker}/market-opportunity", response_model=InvestmentIntelligenceResponse)
def get_market_opportunity(ticker: str = Path(..., pattern=TICKER_PATTERN), as_of: datetime.date | None = Query(None), session: Session = Depends(get_session)):
    date=as_of or utc_today(); t, rows=_rows_endpoint(session,ticker,date,MarketOpportunityEstimate,limit=10)
    rows = [row for row in rows if row.as_of <= date]
    data=[]
    for row in rows:
        item=_model_row_dict(row)
        item["components"]=[_model_row_dict(c) for c in session.query(MarketOpportunityComponent).filter_by(estimate_id=row.id).all()]
        data.append(item)
    return _intelligence_response(t.symbol,date,rows,data=data,coverage=_dataset_coverage(session,t.id,"market_opportunity",date))


@router.get("/candidates/{ticker}/operating-kpis", response_model=InvestmentIntelligenceResponse)
def get_operating_kpis(ticker: str = Path(..., pattern=TICKER_PATTERN), as_of: datetime.date | None = Query(None), session: Session = Depends(get_session)):
    date=as_of or utc_today(); t, rows=_rows_endpoint(session,ticker,date,OperatingKpiObservation)
    labels={d.id:d for d in session.query(OperatingKpiDefinition).all()}
    data=[]
    for row in rows:
        item=_model_row_dict(row); definition=labels.get(row.kpi_definition_id)
        item.update({"code":definition.code if definition else None,"label":definition.label if definition else None,"unit":definition.unit if definition else None})
        data.append(item)
    return _intelligence_response(t.symbol,date,rows,data=data,coverage=_dataset_coverage(session,t.id,"operating_kpis",date))


@router.get("/candidates/{ticker}/capital-allocation", response_model=InvestmentIntelligenceResponse)
def get_capital_allocation(ticker: str = Path(..., pattern=TICKER_PATTERN), as_of: datetime.date | None = Query(None), session: Session = Depends(get_session)):
    date=as_of or utc_today(); t,rows=_rows_endpoint(session,ticker,date,CapitalAllocationEvent)
    totals={}
    for row in rows:
        if row.announced_at.date() >= date - datetime.timedelta(days=365 * 3) and row.amount is not None:
            totals[row.event_type]=totals.get(row.event_type,0.0)+float(row.amount)
    return _intelligence_response(t.symbol,date,rows,data={"three_year_totals":totals,"events":[_model_row_dict(r) for r in rows]},coverage=_dataset_coverage(session,t.id,"capital_allocation",date))


@router.get("/candidates/{ticker}/management-incentives", response_model=InvestmentIntelligenceResponse)
def get_management_incentives(ticker: str = Path(..., pattern=TICKER_PATTERN), as_of: datetime.date | None = Query(None), session: Session = Depends(get_session)):
    date=as_of or utc_today(); t,rows=_rows_endpoint(session,ticker,date,ManagementIncentiveSnapshot)
    return _intelligence_response(t.symbol,date,rows,coverage=_dataset_coverage(session,t.id,"management_incentives",date))


@router.get("/candidates/{ticker}/debt-profile", response_model=InvestmentIntelligenceResponse)
def get_debt_profile(ticker: str = Path(..., pattern=TICKER_PATTERN), as_of: datetime.date | None = Query(None), session: Session = Depends(get_session)):
    date=as_of or utc_today(); t,rows=_rows_endpoint(session,ticker,date,DebtInstrument)
    _, facilities=_rows_endpoint(session,ticker,date,LiquidityFacility,limit=1)
    maturity={}
    for row in rows:
        if row.maturity_date and row.principal is not None: maturity[str(row.maturity_date.year)]=maturity.get(str(row.maturity_date.year),0.0)+float(row.principal)
    cash=float(facilities[0].cash_balance) if facilities and facilities[0].cash_balance is not None else None
    available=float(facilities[0].revolver_available) if facilities and facilities[0].revolver_available is not None else None
    debt_coverage = _dataset_coverage(session,t.id,"debt_profile",date)
    maturity_scanned = debt_coverage is not None and debt_coverage.coverage_status in {CoverageStatus.COLLECTED_NO_FINDING, CoverageStatus.COLLECTED_WITH_DATA}
    due_12m=sum(float(r.principal) for r in rows if r.principal is not None and r.maturity_date and r.maturity_date <= date+datetime.timedelta(days=365)) if maturity_scanned else None
    return _intelligence_response(t.symbol,date,rows or facilities,data={"maturity_ladder":maturity,"cash_balance":cash,"revolver_available":available,
        "debt_due_12m":due_12m,"financing_review_required":(due_12m > cash+(available or 0)) if due_12m is not None and cash is not None else None,
        "instruments":[_model_row_dict(r) for r in rows]},coverage=debt_coverage)


@router.get("/candidates/{ticker}/accounting-quality", response_model=InvestmentIntelligenceResponse)
def get_accounting_quality(ticker: str = Path(..., pattern=TICKER_PATTERN), as_of: datetime.date | None = Query(None), session: Session = Depends(get_session)):
    date=as_of or utc_today(); t=_intelligence_ticker(session,ticker)
    raw=session.query(RawSnapshot).filter(RawSnapshot.ticker_id==t.id,RawSnapshot.available_from<=date).order_by(RawSnapshot.available_from.desc()).first()
    if raw is None: return _intelligence_response(t.symbol,date,[],reason_code=CoverageReasonCode.NO_RAW_SNAPSHOT)
    history=build_financial_history(raw.payload); annual=history.annual
    if not annual: return InvestmentIntelligenceResponse(ticker=t.symbol,as_of=date,coverage_status=CoverageStatus.NOT_COLLECTED,
        reason_code=CoverageReasonCode.INSUFFICIENT_ANNUAL_HISTORY,source=raw.source,data=[])
    latest=annual[-1]; prior=annual[-2] if len(annual)>1 else None
    revenue_growth=(latest.revenue/prior.revenue-1) if prior and latest.revenue is not None and prior.revenue else None
    quality=calculate_accounting_quality(net_income=latest.net_income,operating_cash_flow=latest.operating_cash_flow,
        average_assets=None,revenue_growth=revenue_growth,receivables_growth=None,inventory_growth=None,
        stock_based_compensation=None,revenue=latest.revenue,goodwill=None,total_assets=None)
    return InvestmentIntelligenceResponse(ticker=t.symbol,as_of=date,coverage_status="collected_with_data",source=raw.source,
        data_age_days=(date-raw.available_from).days,data=vars(quality))


@router.get("/candidates/{ticker}/thesis-milestones", response_model=InvestmentIntelligenceResponse)
def get_thesis_milestones(ticker: str = Path(..., pattern=TICKER_PATTERN), as_of: datetime.date | None = Query(None), session: Session = Depends(get_session)):
    date=as_of or utc_today(); t,rows=_rows_endpoint(session,ticker,date,ThesisMilestone,order_column=ThesisMilestone.due_date)
    data=[]
    for row in rows:
        item=_model_row_dict(row); item["days_until"]=(row.due_date-date).days; data.append(item)
    return _intelligence_response(t.symbol,date,rows,data=data,coverage=_dataset_coverage(session,t.id,"thesis_milestones",date))


@router.get("/candidates/{ticker}/macro-exposure", response_model=InvestmentIntelligenceResponse)
def get_macro_exposure(ticker: str = Path(..., pattern=TICKER_PATTERN), as_of: datetime.date | None = Query(None), session: Session = Depends(get_session)):
    date=as_of or utc_today(); t,rows=_rows_endpoint(session,ticker,date,MacroExposureSnapshot)
    vintage_supported = bool(rows) and all(
        bool((row.raw_payload or {}).get("fred_vintage_supported")) for row in rows
    )
    data={"historical_backtest_supported":vintage_supported,
          "forward_shadow_only":not vintage_supported,
          "snapshots":[_model_row_dict(row) for row in rows]}
    return _intelligence_response(t.symbol,date,rows,data=data,
        coverage=_dataset_coverage(session,t.id,"macro_exposure",date))


@router.get("/candidates/{ticker}/mna-history", response_model=InvestmentIntelligenceResponse)
def get_mna_history(ticker: str = Path(..., pattern=TICKER_PATTERN), as_of: datetime.date | None = Query(None), session: Session = Depends(get_session)):
    date=as_of or utc_today(); t,rows=_rows_endpoint(session,ticker,date,DelistingEvent,order_column=DelistingEvent.event_date)
    peers=session.query(DelistingEvent).filter(DelistingEvent.event_date<=date).all()
    acquisition=[r for r in peers if r.event_type in {"cash_acquisition","stock_acquisition"}]
    # Unknown is an unclassified competing-risk observation, not evidence of
    # "no acquisition".  Dividing acquisitions by all rows previously turned
    # 100% unknown coverage into an apparently precise 0% acquisition rate.
    classified=[r for r in peers if r.event_type != "unknown"]
    unknown_count=len(peers)-len(classified)
    classification_coverage=len(classified)/len(peers) if peers else None
    data={"population_statistics":{"historical_acquisition_count":len(acquisition),"historical_delisting_count":len(peers),
          "classified_event_count":len(classified),"unknown_event_count":unknown_count,
          "classification_coverage":classification_coverage,
          "acquisition_share":len(acquisition)/len(classified) if classified else None,
          "historical_backtest_supported":bool(classified) and classification_coverage >= 0.8,
          "forward_shadow_only":not (bool(classified) and classification_coverage >= 0.8),
          "coverage_status": CoverageStatus.COLLECTED_WITH_DATA if peers else CoverageStatus.NOT_COLLECTED},
          "ticker_events":[_model_row_dict(r) for r in rows]}
    return _intelligence_response(t.symbol,date,rows,source="delisting_events",data=data,
        coverage_status=CoverageStatus.COLLECTED_WITH_DATA if peers else CoverageStatus.NOT_COLLECTED,
        reason_code=None if peers else CoverageReasonCode.SOURCE_NOT_SCANNED)


@router.get("/positions/risk-sizing", response_model=InvestmentIntelligenceResponse)
def get_risk_sizing(ticker: str = Query(..., pattern=TICKER_PATTERN), realized_vol: float | None = Query(None, gt=0),
                    liquidity_cap: float | None = Query(None, gt=0, le=1), evidence_grade: str = Query("C"),
                    session: Session = Depends(get_session)):
    t=_intelligence_ticker(session,ticker); config=load_portfolio_config()
    risk=getattr(config,"risk_sizing",None); target_vol=getattr(risk,"target_annual_vol",0.60); min_factor=getattr(risk,"min_vol_factor",0.35)
    evidence_factors=getattr(risk,"evidence_grade_factors",{"A":1.0,"B":0.9,"C":0.75,"D":0.5})
    preview=risk_sizing_preview(per_position_cap=config.per_position_cap,liquidity_cap=liquidity_cap or config.per_position_cap,
        realized_vol=realized_vol,target_vol=target_vol,uncertainty_factor=evidence_factors.get(evidence_grade.upper(),0.5),min_vol_factor=min_factor)
    return InvestmentIntelligenceResponse(ticker=t.symbol,as_of=utc_today(),coverage_status="collected_with_data",source="portfolio_config",data=vars(preview))


@router.get("/positions/jpy-return", response_model=InvestmentIntelligenceResponse)
def get_jpy_return(ticker: str = Query(..., pattern=TICKER_PATTERN), usd_moic: float = Query(..., gt=0),
                   entry_usdjpy: float = Query(..., gt=0), exit_usdjpy: float = Query(..., gt=0),
                   account_type: str = Query("taxable", pattern="^(taxable|NISA)$"), horizon_years: float = Query(7, gt=0),
                   session: Session = Depends(get_session)):
    t=_intelligence_ticker(session,ticker)
    return InvestmentIntelligenceResponse(ticker=t.symbol,as_of=utc_today(),coverage_status="collected_with_data",source="user_scenario",
        data=jpy_after_tax_return(usd_moic=usd_moic,entry_usdjpy=entry_usdjpy,exit_usdjpy=exit_usdjpy,account_type=account_type,horizon_years=horizon_years))


def _latest_v5_run(session: Session, as_of: datetime.date | None = None) -> ModelRun:
    """The most recent successful v5 run, preferring one with real output.

    Phase 11 fix(2026-09-03「v5のUIが見れたものではない」実機確認で発見):
    a same-day ``run-v5-shadow`` invocation for a date whose
    ``universe_snapshots`` row does not exist yet (e.g. today, before the
    daily pipeline has run) legitimately "succeeds" with
    ``population_count = 0`` (see ``run_v5_shadow``'s early-population
    branch). Ordering purely by ``as_of DESC`` let such an empty run mask a
    real, populated run from a prior date -- every v5 UI surface (ranking
    list, ticker detail, validation status's ``latest_run``) would then
    show "no candidates" even though real data exists one day earlier.
    Prefer the latest run that actually scored something; only fall back
    to an empty run when literally no non-empty run exists for the
    requested window (so a genuinely-empty history still 404s honestly
    rather than silently succeeding with nothing to show).
    """
    query = session.query(ModelRun).filter(
        ModelRun.model_version == "v5", ModelRun.status == "succeeded"
    )
    if as_of is not None:
        query = query.filter(ModelRun.as_of <= as_of)
    run = (
        query.filter(ModelRun.population_count > 0)
        .order_by(ModelRun.as_of.desc(), ModelRun.finished_at.desc())
        .first()
    )
    if run is None:
        run = query.order_by(ModelRun.as_of.desc(), ModelRun.finished_at.desc()).first()
    if run is None:
        raise HTTPException(status_code=404, detail="successful v5 model run not found")
    return run


def _v5_run_view(run: ModelRun) -> ModelV5RunView:
    return ModelV5RunView(
        run_id=str(run.id), model_version=run.model_version,
        config_hash=run.config_hash, as_of=run.as_of, mode=run.mode,
        status=run.status, population_count=run.population_count,
        started_at=run.started_at, finished_at=run.finished_at,
        metrics=run.metrics, warnings=run.warnings or [],
    )


def _v5_distribution_payload(score: ModelScore) -> dict:
    """Make older Phase 1 rows readable while retaining their explicit version."""
    payload = dict(score.distribution)
    payload.setdefault("model_confidence", float(score.confidence))
    payload.setdefault("scenarios", [])
    return payload


@router.get("/models/v5/runs/latest", response_model=ModelV5RunView)
def get_latest_v5_run(
    as_of: datetime.date | None = None,
    session: Session = Depends(get_session),
) -> ModelV5RunView:
    """Return the latest successful append-only v5 run, never a running/failed row."""
    return _v5_run_view(_latest_v5_run(session, as_of))


@router.get("/models/v5/scores", response_model=ModelV5ScoreListResponse)
def list_v5_scores(
    objective: str | None = None,
    as_of: datetime.date | None = None,
    limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> ModelV5ScoreListResponse:
    """Rank one immutable v5 distribution set by the selected objective."""
    objective_config = load_objectives_config()
    selected = objective or objective_config.default_objective
    definition = objective_config.objectives.get(selected)
    if definition is None or not definition.enabled:
        raise HTTPException(status_code=422, detail=f"objective is not enabled: {selected}")
    run = _latest_v5_run(session, as_of)
    base = (
        session.query(ObjectiveScore, ModelScore, Ticker)
        .join(ModelScore, (ModelScore.run_id == ObjectiveScore.run_id) &
              (ModelScore.ticker_id == ObjectiveScore.ticker_id))
        .join(Ticker, Ticker.id == ObjectiveScore.ticker_id)
        .filter(ObjectiveScore.run_id == run.id, ObjectiveScore.objective == selected)
    )
    total = base.count()
    rows = (
        base.order_by(ObjectiveScore.rank.asc().nullslast(), Ticker.symbol.asc())
        .offset(offset).limit(limit).all()
    )
    items = [
        ModelV5ScoreSummary(
            rank=objective_row.rank, ticker=ticker.symbol,
            selected_objective=selected,
            objective_value=(float(objective_row.score_value)
                             if objective_row.score_value is not None else None),
            distribution=_v5_distribution_payload(model_score),
            confidence=float(model_score.confidence),
            warnings=model_score.warnings or [],
        )
        for objective_row, model_score, ticker in rows
    ]
    return ModelV5ScoreListResponse(
        run=_v5_run_view(run), selected_objective=selected, total=total, items=items
    )


@router.get("/models/v5/scores/{ticker}", response_model=ModelV5ScoreDetail)
def get_v5_score(
    ticker: str = Path(..., pattern=TICKER_PATTERN),
    as_of: datetime.date | None = None,
    session: Session = Depends(get_session),
) -> ModelV5ScoreDetail:
    run = _latest_v5_run(session, as_of)
    row = (
        session.query(ModelScore, Ticker)
        .join(Ticker, Ticker.id == ModelScore.ticker_id)
        .filter(ModelScore.run_id == run.id, Ticker.symbol == ticker)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="ticker not found in latest v5 run")
    score, ticker_row = row
    objective_rows = (
        session.query(ObjectiveScore)
        .filter(ObjectiveScore.run_id == run.id, ObjectiveScore.ticker_id == ticker_row.id)
        .order_by(ObjectiveScore.objective.asc()).all()
    )
    objectives = [
        ModelV5ObjectiveScoreView(
            objective=item.objective,
            status=(item.explanation or {}).get("status", "unavailable"),
            score_value=(float(item.score_value) if item.score_value is not None else None),
            rank=item.rank,
            explanation=item.explanation or {},
        )
        for item in objective_rows
    ]
    return ModelV5ScoreDetail(
        run=_v5_run_view(run), ticker=ticker_row.symbol,
        target_horizon_years=score.target_horizon_years,
        target_moic=float(score.target_moic),
        distribution=_v5_distribution_payload(score), states=score.states, features=score.features,
        confidence=float(score.confidence), warnings=score.warnings or [],
        objectives=objectives,
    )


@router.get("/models/v5/objectives", response_model=ModelV5ObjectivesResponse)
def list_v5_objectives() -> ModelV5ObjectivesResponse:
    """Phase 8: the UI's objective selector reads this instead of a
    hardcoded list, so a disabled objective (quality_compounder,
    execution_adjusted -- config/objectives.yaml) can never be offered by
    construction, not by the frontend remembering to filter correctly.
    """
    config = load_objectives_config()
    return ModelV5ObjectivesResponse(
        default_objective=config.default_objective,
        objectives=[
            ModelV5ObjectiveDefinitionView(name=name, description=definition.description)
            for name, definition in config.objectives.items()
            if definition.enabled
        ],
    )


@router.get("/models/v5/validation-status", response_model=ModelV5ValidationStatusResponse)
def get_v5_validation_status(session: Session = Depends(get_session)) -> ModelV5ValidationStatusResponse:
    """Phase 8/9 (Issue #3 sections 28/29/34/36): live-measured validation
    status for the UI -- never a hardcoded "looks good" string. Mirrors
    docs/model_v5_validation.md Entry 1's decision (updated there, not
    recomputed here); everything else is queried live so it cannot drift
    stale the way a hardcoded copy would.
    """
    warnings = [
        "not_for_production", "forward_shadow_only",
        "no_realized_outcome_backtest_available_for_either_model",
    ]
    evaluation_dates = sorted(
        d for (d,) in session.query(UniverseSnapshot.snapshot_date)
        .filter(UniverseSnapshot.included.is_(True)).distinct().all()
    )
    realized_count = (
        session.query(ModelV5ForwardReturn)
        .filter(ModelV5ForwardReturn.realized_return.isnot(None))
        .count()
    )
    unsupported = sorted(
        key for key, spec in FEATURES_BY_KEY.items() if not spec.historical_backtest_supported
    )
    # Phase 11 fix(2026-09-03):以前はここだけ独自にクエリしており、
    # `_latest_v5_run()` の「population_count>0を優先する」補正(同フェーズの
    # 別修正)が反映されず、今日の空runが最終runとして出てしまっていた。
    try:
        latest_run_row = _latest_v5_run(session, None)
    except HTTPException:
        latest_run_row = None
    if realized_count == 0:
        warnings.append("forward_validation_zero_matured_observations")
    return ModelV5ValidationStatusResponse(
        decision="CONTINUE_SHADOW",
        decision_entry_date="2026-09-03",
        champion_model="v4", challenger_model="v5", challenger_mode="shadow",
        evaluation_dates_count=len(evaluation_dates),
        evaluation_date_range=(
            [evaluation_dates[0], evaluation_dates[-1]] if evaluation_dates else None
        ),
        realized_forward_validation_count=realized_count,
        unsupported_historical_features=unsupported,
        latest_run=_v5_run_view(latest_run_row) if latest_run_row is not None else None,
        warnings=warnings,
    )


@router.get("/data-coverage", response_model=DataCoverageResponse)
def get_data_coverage(session: Session = Depends(get_session)) -> DataCoverageResponse:
    date=utc_today(); ticker_count=session.query(Ticker).filter(Ticker.is_benchmark.is_(False)).count()
    tables=[("Consensus",AnalystConsensusSnapshot),("Guidance",ManagementGuidanceSnapshot),("TAM",MarketOpportunityEstimate),
        ("Operating KPI",OperatingKpiObservation),("Capital allocation",CapitalAllocationEvent),("Management incentives",ManagementIncentiveSnapshot),
        ("Debt",DebtInstrument),("Milestones",ThesisMilestone),("Macro exposure",MacroExposureSnapshot)]
    generic_names = {"TAM": "market_opportunity", "Operating KPI": "operating_kpis", "Capital allocation": "capital_allocation",
                     "Management incentives": "management_incentives", "Debt": "debt_profile", "Milestones": "thesis_milestones",
                     "Macro exposure": "macro_exposure"}
    datasets=[]
    for label,model in tables:
        status_model = LiveDatasetCoverage if label in generic_names else model
        query = session.query(
            status_model.ticker_id, status_model.observed_at,
            status_model.coverage_status, status_model.source,
        )
        if label in generic_names:
            query = query.filter(status_model.dataset == generic_names[label])
        latest_by_ticker = {}
        for item in query.all():
            current = latest_by_ticker.get(item.ticker_id)
            if current is None or item.observed_at > current.observed_at:
                latest_by_ticker[item.ticker_id] = item
        latest_rows = list(latest_by_ticker.values())
        successful = [item for item in latest_rows if item.coverage_status in {CoverageStatus.COLLECTED_WITH_DATA, CoverageStatus.COLLECTED_NO_FINDING}]
        with_data = sum(item.coverage_status == CoverageStatus.COLLECTED_WITH_DATA for item in latest_rows)
        no_finding = sum(item.coverage_status == CoverageStatus.COLLECTED_NO_FINDING for item in latest_rows)
        failed = sum(item.coverage_status == CoverageStatus.COLLECTION_FAILED for item in latest_rows)
        not_applicable = sum(item.coverage_status == CoverageStatus.NOT_APPLICABLE for item in latest_rows)
        not_collected = sum(item.coverage_status == CoverageStatus.NOT_COLLECTED for item in latest_rows)
        covered = len(successful)
        latest_item = max(successful, key=lambda item: item.observed_at) if successful else None
        latest = latest_item.observed_at if latest_item else None
        source = latest_item.source if latest_item else None
        stale = sum(item.observed_at.date() < date - datetime.timedelta(days=90) for item in successful)
        denominator=max(ticker_count,1)
        targeted = len(latest_rows)
        attempted = targeted - not_collected
        operational_denominator = max(targeted - not_applicable, 1)
        last_attempted = max((item.observed_at for item in latest_rows), default=None)
        datasets.append(DataCoverageRow(dataset=label,coverage=covered/denominator,stale=stale/denominator,failed=failed/denominator,
            universe_count=ticker_count, eligible_count=ticker_count, targeted_count=targeted, attempted_count=attempted,
            with_data_count=with_data, no_finding_count=no_finding, failed_count=failed, not_applicable_count=not_applicable,
            not_collected_count=not_collected, stale_count=stale, operational_coverage=covered/operational_denominator,
            universe_coverage=with_data/denominator, last_successful=latest, last_attempted=last_attempted, source=source))
    return DataCoverageResponse(as_of=date,ticker_count=ticker_count,datasets=datasets)
