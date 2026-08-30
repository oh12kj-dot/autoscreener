"""K-8:投資ノート下書き自動生成のテスト(draft-note)。

`build_note_draft` はDBセッションを要求するが、**中核のロジック(感応度分析・
保守化ルール・ノートの組み立て・YAMLレンダリング)はすべて純関数に切り出して
あり**、ここではDBにもネットワークにも一切触れずにそれらを厚くテストする。

`render_note` が生成した文字列が `research/notes.py` の実際のパーサ
(`load_note`)を通って `is_complete=True` になることも確認する——これが
本タスクのゴールそのもの(埋め漏れがある下書きは「下書き未完成」であり、
それを検出できないと自動化の意味が無い)。
"""

from __future__ import annotations

import datetime

import pytest

from autoscreener.config import PortfolioConfig, load_scoring_config
from autoscreener.research import notes
from autoscreener.research.draft import (
    CompanyInfo,
    DilutionInfo,
    ReconciliationInfo,
    RedFlagInfo,
    UniverseMedians,
    VerificationDate,
    assemble_note_draft,
    compute_sensitivity_factors,
    compute_universe_medians,
    draft_note,
    render_note,
)
from autoscreener.scoring.moic import CrossSection, MoicInputs, compute_moic
from autoscreener.screening.liquidity import (
    compute_liquidity_profile,
)


@pytest.fixture(scope="module")
def config():
    return load_scoring_config()


# 断面効果(ナウキャスト・σ縮小)を無効にした中立の断面。感応度分析は
# 因子を1つずつ動かすテストなので、これらの副作用を切り離しておく
# (test_moic.py の NEUTRAL と同じ設計)。
NEUTRAL = CrossSection(median_log_momentum=None, median_log_sigma=None, sample_size=1)


def make_inputs(**overrides) -> MoicInputs:
    """中庸な成長企業。1因子だけ動かして感応度をテストする土台。"""
    base = dict(
        market_cap=5.0e8,
        net_debt=0.0,
        revenue_latest=2.0e8,
        gross_profit_latest=1.0e8,
        revenue_cagr=0.30,
        revenue_yoy=0.30,
        revenue_growth_volatility=0.15,
        gross_margin_latest=0.55,
        gross_margin_prior=0.50,
        dilution_cagr=0.02,
        piotroski_ratio=0.6,
        cash_runway_quarters=12.0,
        equity_to_assets=0.5,
        fcf_margin=0.05,
        sector="Technology",
        log_momentum_12m=None,
    )
    base.update(overrides)
    return MoicInputs(**base)


def make_universe(config, n: int = 25) -> list[MoicInputs]:
    """感応度分析の置換先(ユニバース中央値)を作るための、対象より弱い母集団。

    growth/margin/EVマルチプル/希薄化のいずれも対象銘柄(make_inputs)より
    悪い値にしておく——「中央値へ置き換えると悪化する」という前提を
    テストデータ自身で保証するため。
    """
    return [
        make_inputs(
            market_cap=2.0e8,
            revenue_cagr=0.10,
            revenue_yoy=0.10,
            gross_margin_latest=0.35,
            gross_margin_prior=0.35,
            dilution_cagr=0.08,
        )
        for _ in range(n)
    ]


# ============================================================================
# 感応度分析
# ============================================================================


def test_universe_medians_are_computed_from_the_universe(config):
    universe = make_universe(config)
    medians = compute_universe_medians(universe, config)
    assert medians.revenue_growth == pytest.approx(0.10)
    assert medians.gross_margin == pytest.approx(0.35)
    assert medians.dilution_cagr == pytest.approx(0.08)
    assert medians.ev_to_gross_profit is not None


def test_sensitivity_factors_are_ranked_by_expected_moic_drop(config):
    """一番効いた因子ほど `delta` が大きく、順序どおり並ぶこと。

    このテストは合成データの数値そのものではなく、**返ってくる順序が
    実際に測定したdeltaの降順になっていること**を確認する——ここが
    premortemの中核(順位を人間の直感ではなくモデルの実測で決める)。
    """
    inputs = make_inputs()
    universe = make_universe(config)
    medians = compute_universe_medians(universe, config)

    factors = compute_sensitivity_factors(inputs, NEUTRAL, config, medians)

    assert len(factors) == 3
    deltas = [f.delta for f in factors]
    assert deltas == sorted(deltas, reverse=True)
    # 上位に来る因子は、必ず全候補中で最も delta が大きかったものである。
    assert factors[0].delta >= factors[-1].delta


def test_sensitivity_factor_uses_only_implemented_indicators(config):
    """`indicator` は必ず `monitoring_metrics.py` に実在する定数の値であるか、
    実装が無いことを表す `None` のどちらかであること。実装されていない
    先行指標名を書いてはならない、という要件そのものを検証する。"""
    from autoscreener.screening import monitoring_metrics

    implemented_codes = {
        getattr(monitoring_metrics, name)
        for name in dir(monitoring_metrics)
        if name.isupper() and isinstance(getattr(monitoring_metrics, name), str)
    }

    inputs = make_inputs()
    universe = make_universe(config)
    medians = compute_universe_medians(universe, config)
    factors = compute_sensitivity_factors(inputs, NEUTRAL, config, medians)

    for factor in factors:
        assert factor.indicator is None or factor.indicator in implemented_codes
        if factor.indicator is None:
            assert "未実装" in factor.detail


def test_sensitivity_factor_detail_reports_measured_before_and_after(config):
    inputs = make_inputs()
    universe = make_universe(config)
    medians = compute_universe_medians(universe, config)
    factors = compute_sensitivity_factors(inputs, NEUTRAL, config, medians)

    baseline = compute_moic(inputs, NEUTRAL, config, enforce_min_expected_moic=False)
    for factor in factors:
        assert factor.expected_moic_before == pytest.approx(baseline.expected_moic)
        assert factor.expected_moic_after < factor.expected_moic_before
        assert f"{factor.expected_moic_before:.2f}" in factor.detail
        assert f"{factor.expected_moic_after:.2f}" in factor.detail


def test_sensitivity_returns_empty_when_baseline_is_unmeasurable(config):
    """測れない銘柄(例:粗利が無い)では空リストを返す。premortemを
    捏造しない——`assemble_note_draft` 側がこれを検出してエラーにする。"""
    unmeasurable = make_inputs(gross_profit_latest=0.0)
    medians = compute_universe_medians(make_universe(config), config)
    assert compute_sensitivity_factors(unmeasurable, NEUTRAL, config, medians) == []


# ============================================================================
# ノートの組み立て(assemble_note_draft)とレンダリング
# ============================================================================


def _build_sample_draft(config):
    inputs = make_inputs()
    universe = make_universe(config)
    medians = compute_universe_medians(universe, config)
    factors = compute_sensitivity_factors(inputs, NEUTRAL, config, medians)
    baseline = compute_moic(inputs, NEUTRAL, config, enforce_min_expected_moic=False)

    portfolio_config = PortfolioConfig(
        portfolio_value_usd=100_000.0,
        per_position_cap=0.04,
        binary_event_position_cap=0.02,
        adv_participation_cap=0.10,
        sector_cap=0.25,
        max_positions=30,
    )
    liquidity = compute_liquidity_profile(
        [(10.0, 500_000)] * 20,
        portfolio_value_usd=portfolio_config.portfolio_value_usd,
        adv_participation_cap=portfolio_config.adv_participation_cap,
        per_position_cap=portfolio_config.per_position_cap,
    )

    return assemble_note_draft(
        symbol="ABCD",
        created_on=datetime.date(2026, 8, 30),
        company=CompanyInfo(sector="Technology", industry="Software", listed_date=datetime.date(2019, 5, 1), cik="0001234567"),
        baseline=baseline,
        sensitivity_factors=factors,
        medians=medians,
        liquidity=liquidity,
        portfolio_config=portfolio_config,
        dilution_rate_model=0.02,
        verification=VerificationDate(value=datetime.date(2026, 11, 5), estimated=False),
        dilution=DilutionInfo(available=False),
        edgar_10k_url="https://www.sec.gov/Archives/edgar/data/1234567/000123456726000001/abcd-10k.htm",
        red_flags=RedFlagInfo(checked=True, lines=[]),
        reconciliation=ReconciliationInfo(available=False, lines=[]),
    )


def test_assemble_note_draft_requires_at_least_three_premortem_factors(config):
    baseline = compute_moic(make_inputs(), NEUTRAL, config, enforce_min_expected_moic=False)
    with pytest.raises(ValueError, match="premortem"):
        assemble_note_draft(
            symbol="ABCD",
            created_on=datetime.date(2026, 8, 30),
            company=CompanyInfo(sector=None, industry=None, listed_date=None, cik=None),
            baseline=baseline,
            sensitivity_factors=[],
            medians=UniverseMedians(None, None, None, None),
            liquidity=compute_liquidity_profile([], portfolio_value_usd=None, adv_participation_cap=0.1, per_position_cap=0.04),
            portfolio_config=PortfolioConfig(
                portfolio_value_usd=100_000.0,
                per_position_cap=0.04,
                binary_event_position_cap=0.02,
                adv_participation_cap=0.10,
                sector_cap=0.25,
                max_positions=30,
            ),
            dilution_rate_model=0.02,
            verification=VerificationDate(value=datetime.date(2026, 11, 5), estimated=False),
            dilution=DilutionInfo(available=False),
            edgar_10k_url=None,
            red_flags=RedFlagInfo(checked=False, lines=[]),
            reconciliation=ReconciliationInfo(available=False, lines=[]),
        )


def test_missing_dilution_capacity_is_null_not_zero(config):
    """dilution_capacity が0行のとき、各項目は0ではなくNoneであること。
    『枠が無い』と『まだ調べていない』を混同しない、という要件そのもの。"""
    draft = _build_sample_draft(config)
    dilution = draft.front_matter["dilution"]
    assert dilution["remaining_shelf_capacity_usd"] is None
    assert dilution["atm_remaining_usd"] is None
    assert dilution["unexercised_options_ratio"] is None
    assert dilution["has_variable_conversion_price"] is None
    assert "note" in dilution


def test_sizing_reports_which_constraint_bound(config):
    draft = _build_sample_draft(config)
    sizing = draft.front_matter["sizing"]
    assert sizing["amount_usd"] is not None
    # 上のフィクスチャは ADV=$10 x 500,000株 = $5,000,000/日 と巨大なので、
    # 規律側($100,000 x 4% = $4,000)が効くはずである。
    assert "規律側" in sizing["rationale"]


def test_thesis_is_a_placeholder_with_machine_factor_breakdown(config):
    draft = _build_sample_draft(config)
    thesis = draft.front_matter["thesis"]
    assert "(要記入)" in thesis
    assert "revenue_multiple" in thesis


def test_exit_plan_thesis_break_matches_premortem_indicators(config):
    draft = _build_sample_draft(config)
    premortem = draft.front_matter["premortem"]
    thesis_break = draft.front_matter["exit_plan"]["thesis_break"]
    assert len(thesis_break) == len(premortem) == 3
    for p, t in zip(premortem, thesis_break):
        assert p["indicator"] == t["indicator"]
        assert p["cause"] == t["condition"]


# ============================================================================
# render_note が notes.py の検証を通ること(ゴールそのもの)
# ============================================================================


def test_rendered_note_is_complete_per_notes_py(config, tmp_path):
    draft = _build_sample_draft(config)
    rendered = render_note(draft)

    path = tmp_path / "ABCD.md"
    path.write_text(rendered, encoding="utf-8")

    note = notes.load_note("ABCD", tmp_path)
    assert note is not None
    assert note.missing_fields == []
    assert note.is_complete is True


def test_rendered_note_round_trips_assumptions_and_premortem(config, tmp_path):
    draft = _build_sample_draft(config)
    rendered = render_note(draft)
    path = tmp_path / "ABCD.md"
    path.write_text(rendered, encoding="utf-8")

    note = notes.load_note("ABCD", tmp_path)
    assert note.front_matter["ticker"] == "ABCD"
    assert set(note.front_matter["assumptions"]) == {
        "revenue_growth",
        "terminal_margin",
        "terminal_multiple",
        "dilution_rate",
    }
    assert len(note.front_matter["premortem"]) >= 3


# ============================================================================
# draft_note:既存ノートを上書きしない
# ============================================================================


def test_draft_note_does_not_overwrite_an_existing_note(tmp_path, monkeypatch, capsys):
    out_dir = tmp_path / "research"
    out_dir.mkdir()
    existing = out_dir / "ABCD.md"
    existing.write_text("既存の手書きノート", encoding="utf-8")

    from autoscreener.research import draft as draft_module

    def _fake_build(session, symbol, *, as_of=None):
        config = load_scoring_config()
        return _build_sample_draft(config)

    # DBに触れずに `draft_note` の「既存ファイルを検出して逃がす」分岐だけを
    # 検証する。`session_scope` はテストDBが無い環境でも呼べるよう差し替える。
    monkeypatch.setattr(draft_module, "build_note_draft", _fake_build)
    monkeypatch.setattr(draft_module, "session_scope", lambda: _NullContext())

    result_path = draft_note("ABCD", out_dir=out_dir)

    assert result_path == out_dir / "ABCD.draft.md"
    assert existing.read_text(encoding="utf-8") == "既存の手書きノート"
    assert result_path.exists()
    captured = capsys.readouterr()
    assert "既存ノートがあるため" in captured.out


def test_draft_note_writes_directly_when_no_existing_note(tmp_path, monkeypatch):
    out_dir = tmp_path / "research"
    out_dir.mkdir()

    from autoscreener.research import draft as draft_module

    def _fake_build(session, symbol, *, as_of=None):
        config = load_scoring_config()
        return _build_sample_draft(config)

    monkeypatch.setattr(draft_module, "build_note_draft", _fake_build)
    monkeypatch.setattr(draft_module, "session_scope", lambda: _NullContext())

    result_path = draft_note("ABCD", out_dir=out_dir)

    assert result_path == out_dir / "ABCD.md"
    assert result_path.exists()


class _NullContext:
    """`session_scope()` の代わりに使う、何もしないコンテキストマネージャ。

    `draft_note` は `with session_scope() as session:` の形でしか `session` を
    使わない(`build_note_draft` をモックしているため中身は参照されない)。
    """

    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        return False
