"""投資ノートの下書き自動生成(K-8)。

元文書 第08〜11節・`research/TEMPLATE.md` が要求する項目のうち、**大半は既に
`scores.inputs` / `event_calendar` / `dilution_capacity` / `filings` に機械的に
存在する数字の転記でしかない**。ここでは、その転記をゼロにする。

**人間に残すのは「事業の理解」(`thesis`)と「モデルへの不同意」(`assumptions.mine`
の追認・修正)だけ**にする。それ以外の項目——premortem・sizing・
verification_date・exit_plan・dilution——はすべて既存データからの機械的な導出で
埋め、人間は直すだけにする。

**日次パイプラインからは呼ばない。** 理由は2つ:

1. これは「1銘柄を検討する」という人間主導のワークフローの入口であり、
   全銘柄を毎日下書きしても誰も読まない(コスト対効果が無い)。
2. `research/` はアプリが**読むだけで書き換えない**ディレクトリである
   (`research/README.md`)。「建てる前に書くこと」「後から書き換えないこと」は
   gitのコミット履歴が担保する要件であり、バッチが無人で書き込むと、
   その担保が崩れる(いつ・誰が・どの意図で書いたかが曖昧になる)。

したがって本モジュールの唯一のエントリポイントである `draft_note` は、人間が
明示的に叩く CLI(`draft-note TICKER`)からのみ呼ばれる。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from autoscreener.config import (
    PROJECT_ROOT,
    PortfolioConfig,
    ScoringConfig,
    load_portfolio_config,
    load_scoring_config,
)
from autoscreener.dates import utc_today
from autoscreener.db.models import (
    DilutionCapacity,
    EventCalendar,
    Filing,
    PriceSnapshot,
    RawSnapshot,
    Score,
    Ticker,
    XbrlFact,
)
from autoscreener.db.session import session_scope
from autoscreener.scoring.moic import (
    CrossSection,
    MoicInputs,
    MoicResult,
    base_initial_growth,
    compute_moic,
    moic_quantiles,
)
from autoscreener.screening import monitoring_metrics
from autoscreener.screening.exclusion_gates import normalize_financial_currency_value
from autoscreener.screening.liquidity import (
    ADV_WINDOW_DAYS,
    LIQUIDITY_BINDING,
    LiquidityProfile,
    compute_liquidity_profile,
)
from autoscreener.screening.red_flags import (
    FLAG_LABELS,
    evaluate_red_flags,
    filing_to_view,
)
from autoscreener.validation.reconciliation import XbrlFactView, reconcile
from autoscreener.validation.xbrl_facts import tag_to_concept

# J-8/元文書 第11節と同じ文言。`exit_plan.trim_rule` に添えて、機械的な売り
# シグナルとして誤用されないようにする(TEMPLATE.md のコメントと同一の注意書き)。
_TRIM_RULE_NOTE = (
    "買う前に降り方を決める。閾値は売却条件ではない——点灯は「価格に関係なく"
    "判断をやり直す」合図であって、機械的な売りシグナルとして使ってはならない。"
)

MAX_HOLD_REVIEW_MONTHS_DEFAULT = 24

# 成長率の保守化倍率(モデル値に対して)。29.7/K-8の「明示的な保守化ルール」の例
# そのものを採用する——中央値との中点だと、モデルが既にユニバース最下位の
# 成長率を出している銘柄では逆に強気化してしまうため、成長率だけは常に
# モデル値を下回る単純な倍率にする。
REVENUE_GROWTH_CONSERVATISM = 0.75


class _LiteralStr(str):
    """複数行文字列をYAMLの `|` ブロックスタイルで出力させるためのマーカー型。

    `thesis` / `model_divergence` はどちらも複数行の日本語文になるため、
    `assemble_note_draft` が組み立てる時点でこの型に包む。実際のダンパー登録
    (`_NoteDumper`)は下の「YAML出力」節にあるが、クラス自体は
    `assemble_note_draft` から先に使われるためここで定義する。
    """


class _NoteDumper(yaml.SafeDumper):
    """既定の `yaml.Dumper` を汚さないための専用ダンパー。"""


def _literal_str_representer(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="|")


_NoteDumper.add_representer(_LiteralStr, _literal_str_representer)


# ============================================================================
# 純関数 ①:感応度分析(premortemの中核)
# ============================================================================


@dataclass(frozen=True)
class UniverseMedians:
    """同日にスコアリングされた全銘柄から作る中央値(感応度分析の置換先)。"""

    revenue_growth: float | None
    gross_margin: float | None
    ev_to_gross_profit: float | None
    dilution_cagr: float | None


def compute_universe_medians(inputs_list: list[MoicInputs], config: ScoringConfig) -> UniverseMedians:
    """`inputs_list` からユニバース中央値を作る。DBには触らない純関数。

    成長率は `base_initial_growth`(モデルが実際に外挿へ使う値と同じ経路)で
    揃える——素の `revenue_cagr` を使うと、モデルの上限クランプを経ていない
    値と比較することになり、中央値の意味がモデルの挙動とずれる。
    """
    growths = [g for g in (base_initial_growth(i, config) for i in inputs_list) if g is not None]
    margins = [i.gross_margin_latest for i in inputs_list if i.gross_margin_latest is not None]
    multiples = [
        (i.market_cap + i.net_debt) / i.gross_profit_latest
        for i in inputs_list
        if i.gross_profit_latest is not None
        and i.gross_profit_latest > 0
        and (i.market_cap + i.net_debt) > 0
    ]
    dilutions = [i.dilution_cagr for i in inputs_list if i.dilution_cagr is not None]
    return UniverseMedians(
        revenue_growth=statistics.median(growths) if growths else None,
        gross_margin=statistics.median(margins) if margins else None,
        ev_to_gross_profit=statistics.median(multiples) if multiples else None,
        dilution_cagr=statistics.median(dilutions) if dilutions else None,
    )


@dataclass(frozen=True)
class SensitivityFactor:
    """1つの premortem 因子。感応度分析(compute_moicの再計算)から機械的に作る。"""

    key: str  # "revenue_growth" / "gross_margin" / "terminal_multiple" / "dilution_rate"
    cause: str  # 失敗要因の日本語説明(premortem.cause / exit_plan.thesis_break.condition 共用)
    indicator: str | None  # monitoring_metrics.py に実装済みの先行指標コード。無ければ None
    detail: str  # 実測値入りの説明文
    expected_moic_before: float
    expected_moic_after: float
    delta: float  # before - after。大きいほど深刻な因子


# 因子キー → monitoring_metrics.py の実装済み定数への動的参照。
#
# **`getattr` で実際のモジュール属性を引く**(文字列を直接書かない)。これは
# 「実装されていない指標名を書いてはならない」を構造的に守るためで、
# `research/TEMPLATE.md` が要求していた `customer_concentration_disclosed_drop`
# を誰も監視していなかった穴(K-1で `CustomerConcentration` テーブルは追加
# されたが、`monitoring_metrics.py` への実装は本タスク時点でまだ別担当が
# 作業中)を再発させないため。属性が実装された時点で、このモジュールを
# 一切変更せずに自動で拾われる。
_INDICATOR_BY_FACTOR: dict[str, str | None] = {
    "revenue_growth": getattr(monitoring_metrics, "REVENUE_GROWTH_DECELERATION", None),
    "gross_margin": getattr(monitoring_metrics, "GROSS_MARGIN_DECLINE", None),
    "dilution_rate": getattr(monitoring_metrics, "SHARE_COUNT_GROWTH", None),
    # 評価倍率(EV/粗利)の再評価リスクに対応する監視指標は無い。市場参加者の
    # センチメント変化を機械的に先行検知する手段が無いため、意図的に null。
    "terminal_multiple": None,
}

_FACTOR_LABELS: dict[str, str] = {
    "revenue_growth": "売上成長率がユニバース中央値まで鈍化する",
    "gross_margin": "価格競争などで粗利率がユニバース中央値まで低下する",
    "terminal_multiple": "評価倍率(EV/粗利)がユニバース中央値まで再評価(圧縮)される",
    "dilution_rate": "資金繰りに追われた増資により希薄化ペースがユニバース中央値まで悪化する",
}


def _sensitivity_detail(cause: str, before: float, after: float, delta: float, indicator: str | None) -> str:
    base = f"{cause}と、期待倍率(expected MOIC)は{before:.2f}倍→{after:.2f}倍に低下する(Δ{delta:.2f})。"
    if indicator is None:
        return base + "この因子を自動監視する先行指標は未実装(手動確認が必要)。"
    return base


def compute_sensitivity_factors(
    inputs: MoicInputs,
    cross_section: CrossSection,
    config: ScoringConfig,
    medians: UniverseMedians,
    *,
    top_n: int = 3,
) -> list[SensitivityFactor]:
    """`compute_moic` の感応度分析でpremortemを機械的に導出する(K-8の核心)。

    4つの入力(売上成長率・粗利率・評価倍率・希薄化率)をそれぞれ単独で
    ユニバース中央値へ置き換え、`expected_moic` の低下幅が大きい順に上位
    `top_n` 件を返す。**人間に「失敗要因を3つ考えろ」とは言わせない**——
    モデルが実際にどの前提へ最も敏感かを実測する。

    `enforce_min_expected_moic=False` で呼ぶのは、感応度分析はランキングの
    可否判定ではなく「どれだけ動くか」の実測だから(27.20と同じ理由付け)。
    """
    baseline = compute_moic(inputs, cross_section, config, enforce_min_expected_moic=False)
    if baseline is None:
        return []

    candidates: list[SensitivityFactor] = []

    def _try(key: str, perturbed: MoicInputs) -> None:
        result = compute_moic(perturbed, cross_section, config, enforce_min_expected_moic=False)
        if result is None:
            return
        delta = baseline.expected_moic - result.expected_moic
        indicator = _INDICATOR_BY_FACTOR.get(key)
        cause = _FACTOR_LABELS[key]
        candidates.append(
            SensitivityFactor(
                key=key,
                cause=cause,
                indicator=indicator,
                detail=_sensitivity_detail(cause, baseline.expected_moic, result.expected_moic, delta, indicator),
                expected_moic_before=baseline.expected_moic,
                expected_moic_after=result.expected_moic,
                delta=delta,
            )
        )

    if medians.revenue_growth is not None:
        _try(
            "revenue_growth",
            replace(inputs, revenue_cagr=medians.revenue_growth, revenue_yoy=medians.revenue_growth),
        )
    if medians.gross_margin is not None:
        # prior も同値にして「中央値でフラットに推移する」保守シナリオにする
        # (元のtrendを引きずると、中央値への低下に加えてさらなる下落トレンドが
        # 二重に乗ってしまい、感応度が過大になる)。
        _try(
            "gross_margin",
            replace(inputs, gross_margin_latest=medians.gross_margin, gross_margin_prior=medians.gross_margin),
        )
    if medians.ev_to_gross_profit is not None and inputs.gross_profit_latest > 0:
        target_ev = medians.ev_to_gross_profit * inputs.gross_profit_latest
        # `MoicInputs` はEV/粗利を直接持たないため、粗利一定のまま market_cap を
        # 動かして目標EVへ寄せる(net_debtは名目一定とみなす)。
        _try("terminal_multiple", replace(inputs, market_cap=max(target_ev - inputs.net_debt, 1.0)))
    dilution_median = medians.dilution_cagr
    if dilution_median is not None:
        _try("dilution_rate", replace(inputs, dilution_cagr=dilution_median))

    candidates.sort(key=lambda f: f.delta, reverse=True)
    return candidates[:top_n]


# ============================================================================
# 純関数 ②:保守化ルール(assumptions.mine)
# ============================================================================


def _midpoint(a: float, b: float) -> float:
    return (a + b) / 2.0


def _conservative_downward(model_value: float, universe_median: float | None, fallback_ratio: float) -> float:
    """高いほど楽観的な仮定(利益率・評価倍率)を保守化する。

    中央値とモデル値の中点を採るが、**モデル値を上回らないようにクランプ**する
    (保守化のつもりが中央値のほうが高くて逆に強気化する事故を防ぐ)。
    中央値が無い銘柄(ユニバードが薄い日)は `fallback_ratio` を掛けるだけの
    単純な保守化にする。
    """
    if universe_median is None:
        return model_value * fallback_ratio
    return min(model_value, _midpoint(model_value, universe_median))


def _conservative_upward(model_value: float, universe_median: float | None, fallback_ratio: float) -> float:
    """高いほど悲観的な仮定(希薄化率)を保守化する。中央値とモデル値の中点を
    採るが、**モデル値を下回らないようにクランプ**する。"""
    if universe_median is None:
        return model_value * fallback_ratio if model_value > 0 else 0.05
    return max(model_value, _midpoint(model_value, universe_median))


def _effective_dilution_rate(inputs: MoicInputs, cross_section: CrossSection, config: ScoringConfig) -> float:
    """`compute_moic` が実際に使う希薄化レートの読み取り専用の写し(表示用)。

    `scoring/` 配下は書き換え禁止のため新しい戻り値を追加できない。同じ規則
    (A-1、model_audit_v4_2026-08-26.md:欠損は断面中央値で補完)をここで
    再現するだけで、モデルの計算そのものには一切触れない。
    """
    dilution = config.dilution
    source = inputs.dilution_cagr
    if source is None:
        source = cross_section.median_dilution_cagr if cross_section.median_dilution_cagr is not None else 0.0
    return max(dilution.min_annual_rate, min(dilution.max_annual_rate, source))


# ============================================================================
# ノート本体の組み立て(純関数)
# ============================================================================


@dataclass(frozen=True)
class NoteDraft:
    ticker: str
    front_matter: dict
    body: str


@dataclass(frozen=True)
class CompanyInfo:
    sector: str | None
    industry: str | None
    listed_date: date | None
    cik: str | None


@dataclass(frozen=True)
class VerificationDate:
    value: date
    estimated: bool  # True なら「次回決算日」が event_calendar に無く推定した


@dataclass(frozen=True)
class DilutionInfo:
    """`dilution_capacity` の最新行。`available=False` は「0行(まだ調べていない)」
    を表し、各項目は必ず None にする(0を入れない——29.7/K-8要件)。"""

    available: bool
    shelf_remaining_usd: float | None = None
    atm_remaining_usd: float | None = None
    unexercised_options_ratio: float | None = None
    has_variable_conversion: bool | None = None


@dataclass(frozen=True)
class RedFlagInfo:
    checked: bool  # filings が1件以上あるか(「未確認」と「該当なし」を区別する)
    lines: list[str]


@dataclass(frozen=True)
class ReconciliationInfo:
    available: bool  # XBRLデータが1件でもあるか
    lines: list[str]


def assemble_note_draft(
    *,
    symbol: str,
    created_on: date,
    company: CompanyInfo,
    baseline: MoicResult,
    sensitivity_factors: list[SensitivityFactor],
    medians: UniverseMedians,
    liquidity: LiquidityProfile,
    portfolio_config: PortfolioConfig,
    dilution_rate_model: float,
    verification: VerificationDate,
    dilution: DilutionInfo,
    edgar_10k_url: str | None,
    red_flags: RedFlagInfo,
    reconciliation: ReconciliationInfo,
) -> NoteDraft:
    """すべて計算済みの値を受け取って `NoteDraft` を組み立てる純関数。

    DB・yfinance・EDGARには一切触れない——`build_note_draft` がそれらを集めて
    ここに渡す。感応度分析と並び、テストで最も厚く当てるべき関数。
    """
    if len(sensitivity_factors) < 3:
        raise ValueError(
            f"'{symbol}': premortemに必要な感応度分析が{len(sensitivity_factors)}件しか"
            "導出できませんでした(3件以上必要)。ユニバード中央値が算出できる"
            "銘柄数が少なすぎる可能性があります。"
        )

    # --- assumptions ---------------------------------------------------------
    model_revenue_growth = baseline.initial_growth_rate
    model_terminal_margin = baseline.terminal_gross_margin
    model_terminal_multiple = baseline.target_ev_to_gross_profit
    model_dilution_rate = dilution_rate_model

    mine_revenue_growth = model_revenue_growth * REVENUE_GROWTH_CONSERVATISM
    mine_terminal_margin = _conservative_downward(model_terminal_margin, medians.gross_margin, 0.90)
    mine_terminal_multiple = _conservative_downward(model_terminal_multiple, medians.ev_to_gross_profit, 0.75)
    mine_dilution_rate = _conservative_upward(model_dilution_rate, medians.dilution_cagr, 1.25)

    assumptions = {
        "revenue_growth": {"model": round(model_revenue_growth, 4), "mine": round(mine_revenue_growth, 4)},
        "terminal_margin": {"model": round(model_terminal_margin, 4), "mine": round(mine_terminal_margin, 4)},
        "terminal_multiple": {"model": round(model_terminal_multiple, 2), "mine": round(mine_terminal_multiple, 2)},
        "dilution_rate": {"model": round(model_dilution_rate, 4), "mine": round(mine_dilution_rate, 4)},
    }

    model_divergence = _LiteralStr(
        "保守化ルール(機械が適用、人間は同意/不同意だけ判断する):\n"
        f"- revenue_growth: モデル値の{REVENUE_GROWTH_CONSERVATISM:.0%}"
        f"({model_revenue_growth:.1%} → {mine_revenue_growth:.1%})。\n"
        "- terminal_margin / terminal_multiple: モデル値とユニバース中央値の中点、"
        "ただしモデル値を上回らない側に丸める"
        f"(margin {model_terminal_margin:.1%} → {mine_terminal_margin:.1%}、"
        f"multiple {model_terminal_multiple:.1f}x → {mine_terminal_multiple:.1f}x)。\n"
        "- dilution_rate: モデル値とユニバース中央値の中点、ただしモデル値を"
        f"下回らない側に丸める({model_dilution_rate:.1%} → {mine_dilution_rate:.1%})。"
    )

    # --- premortem / exit_plan.thesis_break -----------------------------------
    premortem = [
        {"cause": f.cause, "indicator": f.indicator, "detail": f.detail} for f in sensitivity_factors
    ]
    thesis_break = [
        {"condition": f.cause, "indicator": f.indicator} for f in sensitivity_factors
    ]

    # --- sizing ----------------------------------------------------------------
    if liquidity.max_position_usd is not None:
        amount_usd = round(liquidity.max_position_usd, 2)
        if liquidity.binding_constraint == LIQUIDITY_BINDING:
            rationale = (
                f"ADV制約(20日平均売買代金${liquidity.adv_usd:,.0f}の"
                f"{portfolio_config.adv_participation_cap:.0%}=${liquidity.max_position_adv_usd:,.0f})"
                "が規律側の上限より小さく、これが効いた。"
            )
        else:
            rationale = (
                f"規律側(総資産${portfolio_config.portfolio_value_usd:,.0f}の"
                f"1銘柄上限{portfolio_config.per_position_cap:.0%}="
                f"${liquidity.max_position_portfolio_usd:,.0f})が"
                "ADV制約より小さく、これが効いた。"
            )
    else:
        amount_usd = None
        rationale = (
            f"直近{ADV_WINDOW_DAYS}営業日の価格データが不足しており、"
            "ADVベースの上限を計算できない(手動で流動性を確認すること)。"
        )
    sizing = {"amount_usd": amount_usd, "rationale": rationale}

    # --- verification_date ------------------------------------------------------
    verification_date_value = verification.value

    # --- exit_plan.trim_rule ----------------------------------------------------
    quantiles = moic_quantiles(baseline.log_moic_mu, baseline.log_moic_sigma, baseline.survival_probability)
    p50 = quantiles.get(0.50, 0.0)
    p90 = quantiles.get(0.90, 0.0)
    trim_rule = [
        {"at_moic": round(p50, 2), "action": "1/3を売却して原資を回収"},
        {"at_moic": round(p90, 2), "action": "さらに1/3"},
    ]

    exit_plan: dict = {
        "note": _TRIM_RULE_NOTE,
        "thesis_break": thesis_break,
        "trim_rule": trim_rule,
        "max_hold_review_months": MAX_HOLD_REVIEW_MONTHS_DEFAULT,
    }

    # --- dilution -----------------------------------------------------------------
    if dilution.available:
        dilution_block = {
            "remaining_shelf_capacity_usd": dilution.shelf_remaining_usd,
            "atm_remaining_usd": dilution.atm_remaining_usd,
            "unexercised_options_ratio": dilution.unexercised_options_ratio,
            "has_variable_conversion_price": dilution.has_variable_conversion,
        }
    else:
        dilution_block = {
            "remaining_shelf_capacity_usd": None,
            "atm_remaining_usd": None,
            "unexercised_options_ratio": None,
            "has_variable_conversion_price": None,
            "note": "dilution_capacityが未取得(0行)。空欄は『枠が無い』ではなく『まだ調べていない』。",
        }

    # --- thesis(人間が書く場所。直下に機械の因子分解を補助情報として置く) -------
    thesis_lines = [
        "(要記入)なぜこの会社が7年で10倍になり得るのか。3文以内。",
        "",
        "[参考:モデルが検出した因子分解(15.1の恒等式)。機械が書いた補助情報であり、",
        " thesisの代わりにはならない]",
        f"- 売上倍率(revenue_multiple): {baseline.revenue_multiple:.2f}x",
        f"- 利益率変化(margin_multiple): {baseline.margin_multiple:.2f}x",
        f"- 評価倍率変化(multiple_change): {baseline.multiple_change:.2f}x",
        f"- レバレッジ効果(leverage_effect): {baseline.leverage_effect:.2f}x",
        f"- 希薄化ドラッグ(dilution_drag): {baseline.dilution_drag:.2f}x(÷)",
        f"- 期待倍率(expected_moic): {baseline.expected_moic:.2f}x",
        f"- P(MOIC >= target): {baseline.probability:.2%}",
    ]
    thesis = _LiteralStr("\n".join(thesis_lines))

    front_matter: dict = {
        "ticker": symbol,
        "created_on": created_on,
        "thesis": thesis,
        "assumptions": assumptions,
        "model_divergence": model_divergence,
        "premortem": premortem,
        "sizing": sizing,
        "verification_date": verification_date_value,
        "exit_plan": exit_plan,
        "dilution": dilution_block,
        "review": None,
    }
    if verification.estimated:
        front_matter["verification_date_note"] = (
            "(推定)event_calendarに次回決算日が未収集のため、最新10-K/10-Q提出日+90日で推定した。"
        )

    body = _render_body(
        company=company,
        edgar_10k_url=edgar_10k_url,
        red_flags=red_flags,
        reconciliation=reconciliation,
    )

    return NoteDraft(ticker=symbol, front_matter=front_matter, body=body)


def _render_body(
    *,
    company: CompanyInfo,
    edgar_10k_url: str | None,
    red_flags: RedFlagInfo,
    reconciliation: ReconciliationInfo,
) -> str:
    lines = [
        "## 会社概要",
        f"- セクター: {company.sector or '(不明)'}",
        f"- 業種: {company.industry or '(不明)'}",
        f"- 上場日: {company.listed_date.isoformat() if company.listed_date else '(不明)'}",
        f"- CIK: {company.cik or '(未取得)'}",
        "",
        "## EDGAR",
        f"- 10-K直リンク: {edgar_10k_url or '(未取得)'}",
        "",
        "## 直近の赤旗(filings.analysis)",
    ]
    if not red_flags.checked:
        lines.append("- 未確認(EDGAR提出書類が未収集)。")
    elif not red_flags.lines:
        lines.append("- 該当なし。")
    else:
        lines.extend(f"- {line}" for line in red_flags.lines)

    lines += ["", "## SEC突合(yfinance値とXBRL値)"]
    if not reconciliation.available:
        lines.append("- 未実施(XBRLデータ未取得)。")
    else:
        lines.extend(f"- {line}" for line in reconciliation.lines)

    lines += ["", "## 事業の理解", ""]
    return "\n".join(lines)


# ============================================================================
# YAML出力
# ============================================================================


def render_note(draft: NoteDraft) -> str:
    """`NoteDraft` を `research/<TICKER>.md` 形式の文字列(YAMLフロントマター+本文)にする。"""
    front_yaml = yaml.dump(
        draft.front_matter,
        Dumper=_NoteDumper,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    return f"---\n{front_yaml}---\n\n{draft.body}\n"


# ============================================================================
# DB連携(build_note_draft / draft_note)
# ============================================================================


def build_note_draft(session: Session, symbol: str, *, as_of: date | None = None) -> NoteDraft:
    """1銘柄ぶんの `NoteDraft` をDBから組み立てる。

    前提として `run-scoring` が既にその銘柄を採点済みであること
    (`scores.inputs` が無ければ導出のしようがない)。まだ採点されていない
    銘柄には `ValueError` を送出する——空のノートを書いて「埋めた気になる」
    ほうが害があるため。
    """
    as_of = as_of or utc_today()
    symbol = symbol.upper()
    scoring_config = load_scoring_config()
    portfolio_config = load_portfolio_config()

    ticker_row = session.query(Ticker).filter_by(symbol=symbol).one_or_none()
    if ticker_row is None:
        raise ValueError(f"ticker '{symbol}' が見つかりません。")

    latest_score = (
        session.query(Score)
        .filter(Score.ticker_id == ticker_row.id, Score.scoring_version == scoring_config.scoring_version)
        .order_by(Score.score_date.desc())
        .first()
    )
    if latest_score is None or not latest_score.inputs:
        raise ValueError(
            f"'{symbol}' のスコアが見つかりません。先に `run-scoring` を実行してください。"
        )

    inputs = MoicInputs.from_dict(latest_score.inputs)
    cross_section = CrossSection.from_dict(latest_score.inputs.get("cross_section"))
    score_date = latest_score.score_date

    baseline = compute_moic(inputs, cross_section, scoring_config, enforce_min_expected_moic=False)
    if baseline is None:
        raise ValueError(
            f"'{symbol}': 保存済みの入力からMOICを再計算できませんでした"
            "(モデルの前提条件を満たしません)。"
        )

    # 同日にスコアリングされた全銘柄からユニバード中央値を作る(感応度分析の置換先)。
    universe_rows = (
        session.query(Score.inputs)
        .filter(Score.score_date == score_date, Score.scoring_version == scoring_config.scoring_version)
        .all()
    )
    universe_inputs = [MoicInputs.from_dict(row[0]) for row in universe_rows if row[0]]
    medians = compute_universe_medians(universe_inputs, scoring_config)

    sensitivity_factors = compute_sensitivity_factors(inputs, cross_section, scoring_config, medians)

    dilution_rate_model = _effective_dilution_rate(inputs, cross_section, scoring_config)

    # --- 会社概要 ---------------------------------------------------------------
    company = CompanyInfo(
        sector=ticker_row.sector,
        industry=ticker_row.industry,
        listed_date=ticker_row.listed_date,
        cik=ticker_row.cik,
    )

    # --- sizing:流動性(30.2.2)。直近20営業日ぶんの終値・出来高。 ---------------
    cutoff = as_of - timedelta(days=40)
    price_rows = (
        session.query(PriceSnapshot.close, PriceSnapshot.volume)
        .filter(
            PriceSnapshot.ticker_id == ticker_row.id,
            PriceSnapshot.trade_date <= as_of,
            PriceSnapshot.trade_date > cutoff,
        )
        .order_by(PriceSnapshot.trade_date.desc())
        .limit(ADV_WINDOW_DAYS)
        .all()
    )
    closes_and_volumes = [
        (float(close) if close is not None else None, volume) for close, volume in price_rows
    ]
    liquidity = compute_liquidity_profile(
        closes_and_volumes,
        portfolio_value_usd=portfolio_config.portfolio_value_usd,
        adv_participation_cap=portfolio_config.adv_participation_cap,
        per_position_cap=portfolio_config.per_position_cap,
    )

    # --- verification_date:次回決算日(event_calendar)。無ければ推定。 -----------
    next_earnings = (
        session.query(EventCalendar)
        .filter(
            EventCalendar.ticker_id == ticker_row.id,
            EventCalendar.event_type == "earnings",
            EventCalendar.event_date >= as_of,
        )
        .order_by(EventCalendar.event_date.asc())
        .first()
    )
    filing_rows = session.query(Filing).filter_by(ticker_id=ticker_row.id).all()
    if next_earnings is not None:
        verification = VerificationDate(value=next_earnings.event_date, estimated=False)
    else:
        annual_quarterly = [f for f in filing_rows if f.form in ("10-K", "10-Q")]
        if annual_quarterly:
            latest_filing_date = max(f.filed_date for f in annual_quarterly)
            verification = VerificationDate(value=latest_filing_date + timedelta(days=90), estimated=True)
        else:
            verification = VerificationDate(value=as_of + timedelta(days=90), estimated=True)

    # --- dilution:dilution_capacity の最新行。0行なら null(29.7要件)。 ----------
    dilution_row = (
        session.query(DilutionCapacity)
        .filter_by(ticker_id=ticker_row.id)
        .order_by(DilutionCapacity.as_of_date.desc())
        .first()
    )
    if dilution_row is not None:
        dilution = DilutionInfo(
            available=True,
            shelf_remaining_usd=(
                float(dilution_row.shelf_remaining_usd) if dilution_row.shelf_remaining_usd is not None else None
            ),
            atm_remaining_usd=(
                float(dilution_row.atm_remaining_usd) if dilution_row.atm_remaining_usd is not None else None
            ),
            unexercised_options_ratio=(
                float(dilution_row.unexercised_options_ratio)
                if dilution_row.unexercised_options_ratio is not None
                else None
            ),
            has_variable_conversion=dilution_row.has_variable_conversion,
        )
    else:
        dilution = DilutionInfo(available=False)

    # --- EDGAR 10-K 直リンク -----------------------------------------------------
    ten_k_filings = [f for f in filing_rows if f.form in ("10-K", "10-K/A")]
    if ten_k_filings:
        latest_10k = max(ten_k_filings, key=lambda f: f.filed_date)
        edgar_10k_url = latest_10k.document_url
    elif ticker_row.cik:
        edgar_10k_url = (
            f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker_row.cik}&type=10-K"
        )
    else:
        edgar_10k_url = None

    # --- 直近の赤旗(30.4) ------------------------------------------------------
    if filing_rows:
        flags = evaluate_red_flags([filing_to_view(f) for f in filing_rows], as_of=as_of)
        red_flags = RedFlagInfo(
            checked=True,
            lines=[
                f"[{flag.severity}] {FLAG_LABELS.get(flag.code, flag.code)}"
                f"({flag.detected_on}): {flag.detail}"
                for flag in flags
            ],
        )
    else:
        red_flags = RedFlagInfo(checked=False, lines=[])

    # --- SEC突合(30.5.3) -------------------------------------------------------
    xbrl_rows = session.query(XbrlFact).filter_by(ticker_id=ticker_row.id).all()
    if xbrl_rows:
        raw = (
            session.query(RawSnapshot)
            .filter_by(ticker_id=ticker_row.id)
            .order_by(RawSnapshot.snapshot_date.desc())
            .first()
        )
        info = (raw.payload.get("info") or {}) if raw else {}
        balance_sheet = (raw.payload.get("balance_sheet") or {}) if raw else {}
        liabilities_series = balance_sheet.get("Total Liabilities Net Minority Interest") or {}
        model_inputs = {
            "revenue": normalize_financial_currency_value(info.get("totalRevenue"), info),
            "shares_outstanding": info.get("sharesOutstanding"),
            "cash": info.get("totalCash"),
            "liabilities": (
                normalize_financial_currency_value(
                    next(iter(sorted(liabilities_series.items(), reverse=True)), (None, None))[1], info
                )
                if liabilities_series
                else None
            ),
        }
        xbrl_facts_views = [
            XbrlFactView(
                concept=tag_to_concept(row.taxonomy, row.tag) or "",
                tag=row.tag,
                value=float(row.value),
                period_end=row.period_end,
                filed_date=row.filed_date,
            )
            for row in xbrl_rows
            if tag_to_concept(row.taxonomy, row.tag) is not None
        ]
        items = reconcile(model_inputs, xbrl_facts_views, as_of=as_of)
        reconciliation = ReconciliationInfo(
            available=True,
            lines=[
                f"{item.concept}: {item.status}"
                + (f"(相対差{item.relative_diff:.1%})" if item.relative_diff is not None else "")
                for item in items
            ],
        )
    else:
        reconciliation = ReconciliationInfo(available=False, lines=[])

    return assemble_note_draft(
        symbol=symbol,
        created_on=as_of,
        company=company,
        baseline=baseline,
        sensitivity_factors=sensitivity_factors,
        medians=medians,
        liquidity=liquidity,
        portfolio_config=portfolio_config,
        dilution_rate_model=dilution_rate_model,
        verification=verification,
        dilution=dilution,
        edgar_10k_url=edgar_10k_url,
        red_flags=red_flags,
        reconciliation=reconciliation,
    )


def draft_note(symbol: str, *, out_dir: Path | None = None, as_of: date | None = None) -> Path:
    """人間が明示的に叩くエントリポイント。`draft-note TICKER` CLIから呼ばれる。

    `research/<TICKER>.md` が存在しなければそこに書く。**既に存在する場合は
    絶対に上書きしない**——`research/<TICKER>.draft.md` に書き、その旨を
    標準出力に出す(`research/README.md`「アプリはこのディレクトリを読むだけで、
    書き換えない」)。差分を既存ノートへ取り込むのは人間の仕事。
    """
    out_dir = out_dir or (PROJECT_ROOT / "research")
    out_dir.mkdir(parents=True, exist_ok=True)
    symbol = symbol.upper()

    with session_scope() as session:
        draft = build_note_draft(session, symbol, as_of=as_of)

    rendered = render_note(draft)
    target = out_dir / f"{symbol}.md"
    if target.exists():
        target = out_dir / f"{symbol}.draft.md"
        print(
            f"既存ノートがあるため下書きを別ファイルに出しました: {target}"
            "(差分は人間が取り込むこと)"
        )
    target.write_text(rendered, encoding="utf-8")
    return target
