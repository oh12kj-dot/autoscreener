"""手動実行用CLI(typer)。開発時はサンプルモードで対象銘柄数を絞れる。

**出力は必ずUTF-8で書く(2026-08-30)。** 日本語Windowsの既定コンソールは
cp932 で、`run-backtest` がKPI不合格を報告する行に含まれるダッシュ(U+2014)を
エンコードできず、**受け入れ基準の判定そのものが UnicodeEncodeError で落ちて
いた**(終了コード2で正しく落ちるべき場面で、トレースバックになる)。
説明文は日本語で書く方針である以上、この事故はどの出力行でも起こりうるので、
個別の文字を避けるのではなくストリーム側を UTF-8 に固定する。
"""

from __future__ import annotations

import datetime
import logging
import sys

import typer

for _stream in (sys.stdout, sys.stderr):
    # `reconfigure` は TextIOWrapper にしか無い(パイプ/リダイレクト先が
    # 差し替えられている場合に備えて存在確認する)。
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from autoscreener.backtest.runner import (
    DEFAULT_HORIZON_DAYS,
    DEFAULT_REBALANCE_INTERVAL_DAYS,
    estimate_elasticity_over_history,
    run_backtest,
)
from autoscreener.batch.apply_gates import apply_gates
from autoscreener.batch.backfill_history import backfill_history
from autoscreener.batch.collect_concentration import collect_concentration
from autoscreener.batch.collect_filings import collect_filings
from autoscreener.batch.collect_filing_sections import collect_filing_sections
from autoscreener.batch.collect_guidance import collect_guidance
from autoscreener.batch.collect_litigation import collect_litigation
from autoscreener.batch.collect_macro import collect_macro
from autoscreener.batch.collect_xbrl_facts import collect_xbrl_facts
from autoscreener.batch.collect_consensus import collect_consensus
from autoscreener.batch.collect_investment_intelligence import collect_investment_intelligence
from autoscreener.batch.daily_pipeline import run_daily_pipeline
from autoscreener.batch.refresh_cik_map import refresh_cik_map
from autoscreener.batch.run_daily_collection import run_daily_collection, select_collectable_symbols
from autoscreener.batch.universe_refresh import refresh_universe
from autoscreener.config import load_collection_config
from autoscreener.dates import utc_today
from autoscreener.db.session import session_scope
from autoscreener.scoring.engine import run_scoring
from autoscreener.scoring.forward_validation import run_forward_validation

app = typer.Typer(add_completion=False)


@app.callback()
def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _parse_date(value: str) -> datetime.date | None:
    """`--date` の文字列を日付にする。空なら None(=呼び出し先が当日を使う)。"""
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"日付は YYYY-MM-DD 形式で指定してください: {value}") from exc


def _resolve_symbols(sample: int, symbols: str) -> list[str]:
    """収集対象のシンボルを決める。

    隔離済み銘柄を無条件に外していたが、それでは18.1の「週次で再挑戦し、復旧すれば
    自動復帰」が永久に起きない(`select_collectable_symbols` のdocstring参照)。
    再挑戦期限が来た隔離銘柄は対象に含める。
    """
    if symbols:
        return [s.strip() for s in symbols.split(",") if s.strip()]
    with session_scope() as session:
        collectable = select_collectable_symbols(session, load_collection_config())
    return collectable[:sample] if sample > 0 else collectable


@app.command("collect-universe")
def collect_universe_cmd() -> None:
    """NASDAQ/NYSE公開リストからユニバース候補を取得し、当日のスナップショットを記録する。"""
    count = refresh_universe()
    typer.echo(f"universe candidates recorded: {count}")


@app.command("collect")
def collect_cmd(
    sample: int = typer.Option(0, help="対象銘柄数を先頭からこの件数に絞る(0=全件)。開発時の動作確認用。"),
    symbols: str = typer.Option("", help="カンマ区切りのティッカーを直接指定する(sample指定より優先)。"),
) -> None:
    """既存ユニバースに対して日次データ収集バッチを実行する。"""
    target_symbols = _resolve_symbols(sample, symbols)
    if not target_symbols:
        typer.echo("対象銘柄が0件です。先に `collect-universe` を実行してください。")
        raise typer.Exit(code=1)

    typer.echo(f"collecting {len(target_symbols)} symbols...")
    status_counts = run_daily_collection(target_symbols, snapshot_date=utc_today())
    for status, count in sorted(status_counts.items()):
        typer.echo(f"  {status}: {count}")


@app.command("backfill-history")
def backfill_history_cmd(
    sample: int = typer.Option(0, help="対象銘柄数を先頭からこの件数に絞る(0=全件)。"),
    symbols: str = typer.Option("", help="カンマ区切りのティッカーを直接指定する(sample指定より優先)。"),
    period: str = typer.Option(
        "max", help="取得期間。B-4:既定を max に変更(1y/2y/5y/10y/max)。"
    ),
) -> None:
    """価格・株式数の履歴を一括取得し price_snapshots を埋める(1回限りのジョブ)。

    B-4(docs/defect_and_edge_audit_2026-08-28.md I-1):既定を `max` にした。評価日を
    増やすには履歴を伸ばすのが唯一の道(→ XBRL は2009年まで遡れる)。
    """
    target_symbols = _resolve_symbols(sample, symbols)
    if not target_symbols:
        typer.echo("対象銘柄が0件です。先に `collect-universe` を実行してください。")
        raise typer.Exit(code=1)

    typer.echo(f"backfilling history ({period}) for {len(target_symbols)} symbols...")
    status_counts = backfill_history(target_symbols, period=period)
    for status, count in sorted(status_counts.items()):
        typer.echo(f"  {status}: {count}")


@app.command("apply-gates")
def apply_gates_cmd(
    date: str = typer.Option("", help="対象日(YYYY-MM-DD)。既定は当日。"),
) -> None:
    """15.2の除外ゲートを最新データに適用し、その日の universe_snapshots を確定する。"""
    counts = apply_gates(_parse_date(date))
    for key, count in counts.items():
        typer.echo(f"  {key}: {count}")


@app.command("run-scoring")
def run_scoring_cmd(
    date: str = typer.Option("", help="対象日(YYYY-MM-DD)。既定は当日。"),
) -> None:
    """その日のゲート判定を通過した銘柄にスコアリングエンジン(7章・15.2)を適用する。

    `--date` を使うと過去日を再計算できる。モデルやバグ修正を入れた直後、
    当日の収集がまだ走っていない時間帯に、直近のスコアだけを書き直したい
    ——という場面で要る(既定の当日だけだと `universe_snapshots` が無くて
    何もできない)。
    """
    counts = run_scoring(_parse_date(date))
    for key, count in counts.items():
        typer.echo(f"  {key}: {count}")


@app.command("run-forward-validation")
def run_forward_validation_cmd() -> None:
    """成熟したスコア×ホライズンの実現リターンを forward_returns に記録する(14.3)。"""
    counts = run_forward_validation()
    for key, count in counts.items():
        typer.echo(f"  {key}: {count}")


@app.command("run-backtest")
def run_backtest_cmd(
    horizon_days: int = typer.Option(DEFAULT_HORIZON_DAYS, help="建玉から決済までの保有日数。"),
    interval_days: int = typer.Option(DEFAULT_REBALANCE_INTERVAL_DAYS, help="評価日の間隔(既定は四半期)。"),
    non_overlapping: bool = typer.Option(
        False,
        "--non-overlapping",
        help="D-2:評価日の間隔をホライズンと同じにして保有期間を重ねない(正直な検出力)。",
    ),
    bootstrap_resamples: int = typer.Option(
        300, help="D-2:評価日単位ブロック・ブートストラップのリサンプル回数(0で無効)。"
    ),
    persist: bool = typer.Option(True, help="結果を backtest_runs テーブルに保存する。"),
) -> None:
    """擬似バックテスト(27.8)を実行し、14.2のKPIを表示する。

    過去の各評価日について、その時点で開示済みだったデータだけからスコアを付け直し、
    以降の実現リターンと突き合わせる。前方検証(`run-forward-validation`)が実データで
    成熟するまでの唯一の較正手段。

    **D-3:KPIに FAIL があれば非ゼロ終了する。** D-1(生存者バイアス)/ D-2
    (有効標本不足)が直るまで多くのKPIは INSUFFICIENT_DATA になる想定。
    """
    if non_overlapping:
        interval_days = horizon_days
    metrics = run_backtest(
        horizon_days=horizon_days,
        interval_days=interval_days,
        persist=persist,
        bootstrap_resamples=bootstrap_resamples,
    )
    if metrics.observation_count == 0:
        typer.echo("観測が0件でした。price_snapshots の期間とホライズンを確認してください。")
        raise typer.Exit(code=1)

    typer.echo(f"観測数: {metrics.observation_count}  ホライズン: {metrics.horizon_years:.2f}年")
    typer.echo(f"オンペース閾値(10倍/7年と同じ年率): {metrics.on_pace_threshold:.3f}倍")
    typer.echo("")
    typer.echo("[14.2 KPI]")
    typer.echo(f"  デシル単調性(順位相関): {metrics.decile_monotonicity:+.3f}  完全単調: {metrics.strictly_monotonic}")
    typer.echo(
        f"  オンペース・リフト(参考): {metrics.lift_ratio:.2f}  "
        f"最悪の評価日 {metrics.lift_ratio_worst_date:.2f}"
    )
    typer.echo(f"  順位IC: {metrics.rank_ic:+.3f} (t {metrics.rank_ic_t_stat:+.1f})")
    typer.echo(
        f"  破綻回避率: 上位デシル {metrics.top_decile_loss_rate:.1%} vs ユニバース {metrics.universe_loss_rate:.1%}"
    )
    typer.echo(
        f"  較正: 予測平均 {metrics.mean_predicted_on_pace_rate:.2%} vs 実績 "
        f"{metrics.universe_on_pace_rate:.2%} (誤差 {metrics.calibration_error:+.2%})"
    )
    typer.echo(f"  上場廃止決済の割合: {metrics.delisted_settlement_rate:.2%}")
    typer.echo(f"  [S-8診断] ナウキャスト上限への張り付き率: {metrics.nowcast_cap_hit_rate:.1%}")
    if metrics.delisted_settlement_rate == 0.0:
        # 27.15:この値が0%というのは「1銘柄も上場廃止にならなかった」ではなく
        # 「廃止された銘柄が最初から標本に入っていない」ことを示す警告灯である。
        typer.echo(
            "  [警告] 上場廃止が1件も観測されていません。`tickers` はNASDAQ Traderの"
            "**現在の**上場一覧から作られるため、期間中に廃止された銘柄はマスタに"
            "存在せず、バックテストの母集団から丸ごと欠落しています。"
            "上のリターン・オンペース率は実態より良い方向へ偏っています(27.15)。"
        )
    typer.echo("")
    # 28.11:14.2の「リフト >= 2.0」は本来「10バガー達成率」という稀な事象への
    # 指標だった。27.12がそれを基準率25%の「オンペース」に読み替えたまま目標値
    # だけ持ち越したのは誤りなので、右裾へずらして測り直した表を主として出す。
    typer.echo("[右裾リフト]  当たりの定義を右へずらすほど、モデルの選別力は上がる")
    typer.echo("  当たりの定義      閾値リターン   上位10%の的中率   リフト   最悪の評価日")
    for tail in metrics.tail_lifts:
        typer.echo(
            f"  断面上位 {tail.quantile:>4.0%}      {tail.median_threshold_return:>+8.1%}   "
            f"{tail.top_decile_hit_rate:>13.1%}   {tail.lift:>6.2f}   {tail.worst_date_lift:>11.2f}"
        )
    typer.echo("")
    typer.echo("[評価日ごと]  平均だけを見ると検出力の低さが隠れる")
    typer.echo("  評価日        n    ユニバース   上位デシル   リフト   順位IC")
    for stat in metrics.per_date:
        typer.echo(
            f"  {stat.base_date}  {stat.count:>4}   {stat.universe_on_pace_rate:>9.1%}   "
            f"{stat.top_decile_on_pace_rate:>9.1%}   {stat.lift_ratio:>6.2f}   {stat.rank_ic:>+6.3f}"
        )
    typer.echo("")
    typer.echo("[デシル別]  1 = モデルが最も有望とした10%")
    typer.echo("  decile   n   平均予測P   中央値リターン   オンペース率   −50%以下率")
    for d in metrics.deciles:
        typer.echo(
            f"  {d.decile:>5}  {d.count:>4}   {d.mean_probability:>8.3%}   {d.median_return:>+12.1%}   "
            f"{d.on_pace_rate:>10.1%}   {d.loss_rate:>9.1%}"
        )

    # D-2:検出力の実態(実効評価日数・ブートストラップCI・重複の有無)。
    typer.echo("")
    typer.echo("[検出力(D-2)]")
    typer.echo(
        f"  評価日 {len(metrics.per_date)} 点 / 実効評価日数(Kish) {metrics.effective_dates:.1f}  "
        f"{'非重複' if metrics.non_overlapping else '重複あり(保有期間が重なる=独立点はさらに少ない)'}"
    )

    def _fmt_ci(ci: tuple[float, float] | None) -> str:
        return f"[{ci[0]:+.3f}, {ci[1]:+.3f}]" if ci else "n/a(評価日 < 3)"

    typer.echo(f"  順位IC        {metrics.rank_ic:+.3f}   95%CI {_fmt_ci(metrics.rank_ic_ci)}")
    typer.echo(f"  リフト         {metrics.lift_ratio:.2f}    95%CI {_fmt_ci(metrics.lift_ratio_ci)}")
    typer.echo(
        f"  デシル単調性  {metrics.decile_monotonicity:+.3f}   95%CI {_fmt_ci(metrics.decile_monotonicity_ci)}"
    )

    # D-5:取引コスト。コスト前後の主要KPI。
    if metrics.after_cost:
        typer.echo("")
        typer.echo(
            f"[取引コスト(D-5)]  平均往復コスト {metrics.mean_round_trip_cost_bps:.0f}bps"
            "(Corwin-Schultz スプレッド + 平方根則インパクト)"
        )
        ac = metrics.after_cost
        typer.echo(
            f"  コスト前 → コスト後:  リフト {metrics.lift_ratio:.2f} → {ac['lift_ratio']:.2f}   "
            f"単調性 {metrics.decile_monotonicity:+.3f} → {ac['decile_monotonicity']:+.3f}   "
            f"順位IC {metrics.rank_ic:+.3f} → {ac['rank_ic']:+.3f}"
        )

    # D-4:ポートフォリオ・シミュレーション(指数超過CAGR・最大ドローダウン)。
    if metrics.portfolio:
        p = metrics.portfolio
        typer.echo("")
        typer.echo(
            f"[ポートフォリオ(D-4)]  上位{p['holdings_per_rebalance']}銘柄・等金額・"
            f"非重複トランシェ {p['non_overlapping_tranche_count']} 本"
        )
        typer.echo(
            f"  CAGR {p['cagr']:+.1%}  最大DD {p['max_drawdown']:+.1%}  "
            f"年率ボラ {p['volatility']:.1%}  回転率 {p['turnover']:.0%}  "
            f"コスト差引 {p['realized_cost_drag']:+.2%}/年"
        )
        for symbol in sorted(p.get("excess_cagr", {})):
            typer.echo(
                f"  vs {symbol}: 指数CAGR {p['benchmark_cagr'].get(symbol, 0):+.1%}  "
                f"超過CAGR {p['excess_cagr'][symbol]:+.1%}  "
                f"勝率 {p.get('win_rate_vs_benchmark', {}).get(symbol, 0):.0%}"
            )
        if not p.get("excess_cagr"):
            typer.echo("  (ベンチマーク未登録。`register-benchmarks` → 価格収集後に再実行)")

    # D-8:単純ベースラインとの比較。v4 が momentum/growth の両方に勝てるか。
    if metrics.baselines:
        typer.echo("")
        typer.echo("[ベースライン比較(D-8)]  v4 がこれらに勝てないなら複雑さは正当化されない")
        typer.echo(f"  {'モデル':<20} {'リフト':>8} {'単調性':>8} {'順位IC':>8}")
        typer.echo(
            f"  {'v4 (this)':<20} {metrics.lift_ratio:>8.2f} "
            f"{metrics.decile_monotonicity:>+8.3f} {metrics.rank_ic:>+8.3f}"
        )
        for name, b in metrics.baselines.items():
            typer.echo(
                f"  {name:<20} {b['lift_ratio']:>8.2f} "
                f"{b['decile_monotonicity']:>+8.3f} {b['rank_ic']:>+8.3f}"
            )

    # D-10:ゲートの整合性。
    if metrics.gate_parity and "ratio" in metrics.gate_parity:
        typer.echo("")
        typer.echo(
            f"[ゲート整合(D-10)]  ライブ相当通過 / 旧ゲート通過 = "
            f"{metrics.gate_parity['ratio']:.3f}  "
            f"(live={metrics.gate_parity.get('live_pass')} legacy={metrics.gate_parity.get('legacy_pass')})"
        )

    # D-3:KPIの合否。
    typer.echo("")
    typer.echo("[KPI合否(D-3)]")
    for name, verdict in metrics.kpi_verdicts.items():
        typer.echo(f"  {name:<24} {verdict}")
    failed = [name for name, v in metrics.kpi_verdicts.items() if v == "FAIL"]
    if failed:
        typer.echo("")
        typer.echo(f"  [FAIL] {', '.join(failed)} — 受け入れ基準を満たしていません。")
        raise typer.Exit(code=2)


@app.command("compare-configs")
def compare_configs_cmd(
    config_a: str = typer.Argument(..., help="比較元の scoring.yaml パス。"),
    config_b: str = typer.Argument(..., help="比較先の scoring.yaml パス。"),
    horizon_days: int = typer.Option(DEFAULT_HORIZON_DAYS),
    interval_days: int = typer.Option(DEFAULT_REBALANCE_INTERVAL_DAYS),
    bootstrap_resamples: int = typer.Option(500),
) -> None:
    """D-2:2つの `config/scoring.yaml` を同一データでバックテストし、KPI差を判定する。

    差が評価日単位ブロック・ブートストラップの95%CIを超えたときだけ **ADOPT** を、
    それ以外は **INDISTINGUISHABLE** を出力する。`config/scoring.yaml` のコメントに
    書く実測値は、この出力を貼ること(D-2 受け入れ基準)。
    """
    from pathlib import Path

    from autoscreener.backtest.metrics import bootstrap_kpi_interval, compute_metrics, per_date_stats, _weighted_mean
    from autoscreener.backtest.runner import collect_backtest_observations
    from autoscreener.config import load_scoring_config

    cfg_a = load_scoring_config(Path(config_a))
    cfg_b = load_scoring_config(Path(config_b))

    typer.echo(f"collecting observations for {config_a} ...")
    obs_a = collect_backtest_observations(horizon_days, interval_days, cfg_a)
    typer.echo(f"collecting observations for {config_b} ...")
    obs_b = collect_backtest_observations(horizon_days, interval_days, cfg_b)
    if not obs_a or not obs_b:
        typer.echo("観測が0件でした。")
        raise typer.Exit(code=1)

    hy = horizon_days / 365.25

    def _metrics(obs):
        return compute_metrics(obs, cfg_a.target_moic, hy, cfg_a.horizon_years)

    ma, mb = _metrics(obs_a), _metrics(obs_b)

    kpis = {
        "lift_ratio": (ma.lift_ratio, mb.lift_ratio, lambda o: _metrics(o).lift_ratio),
        "decile_monotonicity": (
            ma.decile_monotonicity,
            mb.decile_monotonicity,
            lambda o: _metrics(o).decile_monotonicity,
        ),
        "rank_ic": (
            ma.rank_ic,
            mb.rank_ic,
            lambda o: _weighted_mean(
                [d.rank_ic for d in per_date_stats(o, ma.on_pace_threshold)],
                [d.count for d in per_date_stats(o, ma.on_pace_threshold)],
            ),
        ),
    }

    typer.echo("")
    typer.echo(f"  {'KPI':<22} {'A':>10} {'B':>10} {'Δ(B-A)':>10}   判定(差の95%CI基準)")
    for name, (va, vb, fn) in kpis.items():
        delta = vb - va
        # 差の不確かさは、A の観測でのブートストラップCIの半幅で近似する。
        ci = bootstrap_kpi_interval(obs_a, fn, n_resamples=bootstrap_resamples)
        half_width = (ci[1] - ci[0]) / 2 if ci else float("inf")
        verdict = "ADOPT" if abs(delta) > half_width else "INDISTINGUISHABLE"
        typer.echo(f"  {name:<22} {va:>10.3f} {vb:>10.3f} {delta:>+10.3f}   {verdict}  (±{half_width:.3f})")


@app.command("estimate-elasticity")
def estimate_elasticity_cmd(
    interval_days: int = typer.Option(60, help="断面を取る間隔(日)。"),
) -> None:
    """マルチプルの成長弾力性 κ を断面から再推定する(28.2)。

    `config/scoring.yaml` の `multiple.growth_elasticity` に入れる値を測る。
    **これは較正ではなく測定である**——リターンには一切フィットさせず、
    「市場が成長に対していくら払っているか」という値づけ構造だけを見る。

    ユニバース(市場・時価総額レンジ・除外セクター)を変えたら測り直すこと。
    複数の断面で推定し、断面間のばらつきも一緒に出す。日を変えても同じ値が
    出るかどうかが、この構造パラメータを信用してよいかの判断材料になる。
    """
    from autoscreener.scoring.elasticity import MIN_POOLING_SAMPLE, pool_estimates

    estimates = estimate_elasticity_over_history(interval_days=interval_days)
    if not estimates:
        typer.echo("断面を1つも作れませんでした。price_snapshots と raw_snapshots を確認してください。")
        raise typer.Exit(code=1)

    typer.echo("ln(EV/粗利) = 定数 + kappa x 成長率")
    typer.echo("")
    typer.echo("  評価日          n    kappa     標準誤差    t値      切片     決定係数   クランプ除外n  クランプ除外kappa")
    for as_of, cs in estimates:
        estimate = cs.full
        if estimate is None:
            continue
        thin = "  ← 観測が薄いため平均から除外" if estimate.sample_size < MIN_POOLING_SAMPLE else ""
        unclamped_n = cs.unclamped.sample_size if cs.unclamped is not None else 0
        unclamped_kappa = f"{cs.unclamped.slope:>+8.3f}" if cs.unclamped is not None else "     n/a"
        typer.echo(
            f"  {as_of}  {estimate.sample_size:>4}  {estimate.slope:>+6.3f}  "
            f"{estimate.standard_error:>9.3f}  {estimate.t_statistic:>+6.1f}  "
            f"{estimate.intercept:>+7.3f}  {estimate.r_squared:>5.3f}  "
            f"{unclamped_n:>11}  {unclamped_kappa}{thin}"
        )

    pooled = pool_estimates([cs.full for _, cs in estimates if cs.full is not None])
    pooled_unclamped = pool_estimates([cs.unclamped for _, cs in estimates if cs.unclamped is not None])
    typer.echo("")
    if pooled is None:
        typer.echo("断面が1つしかないため、ばらつきは評価できません。")
        return
    mean_slope, spread = pooled
    typer.echo(f"平均 kappa(クランプ銘柄を含む)      = {mean_slope:+.4f}  断面間の標準偏差 = {spread:.4f}")
    if pooled_unclamped is not None:
        mean_unclamped, spread_unclamped = pooled_unclamped
        typer.echo(
            f"平均 kappa(クランプ銘柄を除く)      = {mean_unclamped:+.4f}  断面間の標準偏差 = {spread_unclamped:.4f}"
        )
        divergence = abs(mean_unclamped - mean_slope)
        typer.echo("")
        typer.echo(
            f"E-4(docs/defect_audit_2026-08-27.md):2値の乖離 = {divergence:.4f}(断面間の標準偏差 {spread:.4f} と比較する)。"
        )
        if divergence <= spread:
            typer.echo(
                "  → 乖離は断面間ばらつきの範囲内。クランプ済み成長率を説明変数に使う現状の測定方法は妥当。"
                "config/scoring.yaml の growth_elasticity コメントにこの判定を追記すること。"
            )
        else:
            typer.echo(
                "  → 乖離が大きい。打ち切り回帰(regression dilution)で kappa が減衰している可能性がある。"
                "estimate_elasticity_over_history の説明変数を raw_initial_growth(クランプ前)へ切り替え、"
                "再測定・run-backtest でのKPI確認を行うこと。"
            )
    else:
        typer.echo("クランプ銘柄を除いた推定は、有効な断面が足りず算出できませんでした。")
    typer.echo("")
    typer.echo(
        "断面ごとの標準誤差ではなく**断面間のばらつき**で判断すること。"
        "同じ日の数百銘柄は独立ではないので、その中での標準誤差は検出力を過大に見せる。"
    )
    typer.echo("")
    typer.echo(
        f"config/scoring.yaml の multiple.growth_elasticity に {mean_slope:.2f} を設定し、"
        "`run-backtest` でKPIの変化を確認してください。"
    )


@app.command("run-daily-pipeline")
def run_daily_pipeline_cmd() -> None:
    """収集→ゲート適用→スコアリングを1回で実行する(スケジューラから呼び出す想定)。"""
    results = run_daily_pipeline()
    for stage, counts in results.items():
        typer.echo(f"[{stage}]")
        for key, count in counts.items():
            typer.echo(f"  {key}: {count}")


@app.command("refresh-cik-map")
def refresh_cik_map_cmd() -> None:
    """SECの company_tickers.json から `tickers.cik` を埋める(30.3.2)。週次実行を想定。"""
    counts = refresh_cik_map()
    for key, count in counts.items():
        typer.echo(f"  {key}: {count}")


@app.command("collect-filings")
def collect_filings_cmd(
    symbols: str = typer.Option("", help="カンマ区切りのティッカーを直接指定する(省略時は追跡対象を自動選定)。"),
) -> None:
    """追跡対象銘柄のSEC提出書類メタデータを取得する(30.3.6)。"""
    target_symbols = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else None
    counts = collect_filings(symbols=target_symbols)
    for key, count in counts.items():
        typer.echo(f"  {key}: {count}")


def _optional_symbols(symbols: str) -> list[str] | None:
    return [symbol.strip().upper() for symbol in symbols.split(",") if symbol.strip()] or None


@app.command("collect-concentration")
def collect_concentration_cmd(
    symbols: str = typer.Option("", help="Comma-separated ticker symbols; defaults to tracked tickers."),
    limit: int = typer.Option(300, min=1, help="Maximum tracked tickers when --symbols is omitted."),
) -> None:
    """Collect customer-concentration disclosures from filing sections and XBRL."""
    counts = collect_concentration(symbols=_optional_symbols(symbols), limit=limit)
    for key, count in counts.items():
        typer.echo(f"  {key}: {count}")


@app.command("collect-guidance")
def collect_guidance_cmd(
    symbols: str = typer.Option("", help="Comma-separated ticker symbols; defaults to tracked tickers."),
    limit: int = typer.Option(300, min=1, help="Maximum tracked tickers when --symbols is omitted."),
) -> None:
    """Extract forward guidance from earnings-release filing sections."""
    counts = collect_guidance(symbols=_optional_symbols(symbols), limit=limit)
    for key, count in counts.items():
        typer.echo(f"  {key}: {count}")


@app.command("collect-consensus")
def collect_consensus_cmd(
    symbols: str = typer.Option("", help="Comma-separated ticker symbols; defaults to tracked tickers."),
) -> None:
    """Append provider-neutral analyst consensus snapshots without overwriting history."""
    counts = collect_consensus(symbols=_optional_symbols(symbols))
    for key, count in counts.items():
        typer.echo(f"  {key}: {count}")


@app.command("collect-investment-intelligence")
def collect_investment_intelligence_cmd(
    symbols: str = typer.Option("", help="Comma-separated ticker symbols; defaults to all stored filing sections."),
) -> None:
    """Extract KPI, debt, allocation and proxy facts from stored SEC sections."""
    counts = collect_investment_intelligence(symbols=_optional_symbols(symbols))
    for key, count in counts.items():
        typer.echo(f"  {key}: {count}")


@app.command("collect-filing-sections")
def collect_filing_sections_cmd(
    symbols: str = typer.Option("", help="Comma-separated ticker symbols; defaults to tracked tickers."),
    limit: int = typer.Option(300, min=1, help="Maximum tracked tickers when --symbols is omitted."),
) -> None:
    """Store relevant 10-K, 10-Q, and earnings-release filing sections."""
    counts = collect_filing_sections(symbols=_optional_symbols(symbols), limit=limit)
    for key, count in counts.items():
        typer.echo(f"  {key}: {count}")


@app.command("collect-litigation")
def collect_litigation_cmd(
    symbols: str = typer.Option("", help="Comma-separated ticker symbols; defaults to tracked tickers."),
    limit: int = typer.Option(300, min=1, help="Maximum tracked tickers when --symbols is omitted."),
) -> None:
    """Collect litigation, regulatory-investigation, and short-report events."""
    counts = collect_litigation(symbols=_optional_symbols(symbols), limit=limit)
    for key, count in counts.items():
        typer.echo(f"  {key}: {count}")


@app.command("collect-delistings")
def collect_delistings_cmd(
    start: str = typer.Option("", help="走査開始日(YYYY-MM-DD)。既定は 2022-08-01。"),
    end: str = typer.Option("", help="走査終了日(YYYY-MM-DD)。既定は今日。"),
) -> None:
    """D-1 / I-2:SEC フルインデックスから上場廃止銘柄を復元し tickers に登録する。

    擬似バックテストの母集団は現在100%が生存銘柄(実測)。Form 25 / 15 を全期間
    走査して、期間中に破綻・買収・上場基準抵触で消えた企業を `delisted_at` 付きで
    マスタへ戻す。SEC のレート制限は 10 req/s、`.env` の `EDGAR_USER_AGENT` が必須。
    """
    from autoscreener.batch.collect_delistings import collect_delistings

    counts = collect_delistings(
        start=_parse_date(start), end=_parse_date(end)
    )
    for key, count in counts.items():
        typer.echo(f"  {key}: {count}")


@app.command("estimate-hazard")
def estimate_hazard_cmd() -> None:
    """D-9(D-1 完了後):health_index から上場廃止ハザードを実測較正する。

    `config/scoring.yaml` の `survival.base_annual_hazard` / `health_sensitivity` は
    公表基準率からの事前値で、標本に廃止が0件(D-1)のため検証手段がゼロだった。
    `collect-delistings` で廃止銘柄を入れた後にこのコマンドで実測値を出す。
    """
    from autoscreener.backtest.runner import collect_backtest_observations  # noqa: F401
    from autoscreener.db.models import Ticker
    from autoscreener.scoring.hazard import estimate_hazard

    # health_index を持つ観測と「評価日から1年以内に廃止されたか」の組を作るには
    # バックテスト観測に health_index と ticker の delisted_at を突き合わせる必要が
    # ある。廃止銘柄が母集団に入る(I-1 段階2)まで、ここは events=0 を報告する。
    with session_scope() as session:
        n_delisted = session.query(Ticker).filter(Ticker.delisted_at.isnot(None)).count()
    if n_delisted == 0:
        typer.echo(
            "delisted_at を持つ銘柄が0件です。先に `collect-delistings` を実行し、"
            "I-1 段階2(XBRL ポイントインタイム)で母集団へ投入してください。"
        )
        raise typer.Exit(code=1)
    typer.echo(
        f"{n_delisted} 銘柄が delisted_at を持っています。"
        "health_index × 1年廃止フラグの観測作成は I-1 段階2 の実装後に有効化されます "
        "(scoring/hazard.py::estimate_hazard は実装済み)。"
    )
    _ = estimate_hazard  # 実装済みであることを明示


@app.command("collect-macro")
def collect_macro_cmd() -> None:
    """FREDマクロ系列(米10年債利回り・実質金利・ハイイールドOAS)を取得する(30.8.2)。"""
    counts = collect_macro()
    for key, count in counts.items():
        typer.echo(f"  {key}: {count}")


@app.command("collect-xbrl")
def collect_xbrl_cmd(
    symbols: str = typer.Option("", help="カンマ区切りのティッカーを直接指定する(省略時は追跡対象を自動選定)。"),
) -> None:
    """追跡対象銘柄のSEC XBRL実績値(売上・株式数・現金・負債)を取得する(30.5.5)。"""
    target_symbols = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else None
    counts = collect_xbrl_facts(symbols=target_symbols)
    for key, count in counts.items():
        typer.echo(f"  {key}: {count}")


@app.command("reconcile")
def reconcile_cmd(ticker: str = typer.Argument(..., help="突合する銘柄のティッカー。")) -> None:
    """yfinance値とSEC XBRL値を突合し、人間可読の表を出す(30.5.5)。手作業の検算に使う。"""
    from autoscreener.dates import utc_today
    from autoscreener.db.models import RawSnapshot, Ticker as TickerModel, XbrlFact
    from autoscreener.screening.exclusion_gates import normalize_financial_currency_value
    from autoscreener.validation.reconciliation import reconcile as reconcile_fn
    from autoscreener.validation.reconciliation import XbrlFactView
    from autoscreener.validation.xbrl_facts import tag_to_concept

    symbol = ticker.upper()
    with session_scope() as session:
        ticker_row = session.query(TickerModel).filter_by(symbol=symbol).one_or_none()
        if ticker_row is None:
            typer.echo(f"ticker '{symbol}' not found")
            raise typer.Exit(code=1)
        raw = (
            session.query(RawSnapshot)
            .filter_by(ticker_id=ticker_row.id)
            .order_by(RawSnapshot.snapshot_date.desc())
            .first()
        )
        info = (raw.payload.get("info") or {}) if raw else {}
        balance_sheet = (raw.payload.get("balance_sheet") or {}) if raw else {}
        liabilities_series = balance_sheet.get("Total Liabilities Net Minority Interest") or {}
        liabilities_period_end = next(iter(sorted(liabilities_series, reverse=True)), None)
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
        xbrl_rows = session.query(XbrlFact).filter_by(ticker_id=ticker_row.id).all()
        facts = [
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

    # 30.5.3(2026-08-30 修正):`liabilities` だけはモデル側が「いつ時点の
    # 貸借対照表か」を持っている。時点を合わせないと、事業売却などで残高が
    # 動いた会社で「どちらも正しいのに不一致」が出る(DAN で 67% を実測)。
    model_period_ends: dict[str, datetime.date] = {}
    if liabilities_period_end:
        try:
            model_period_ends["liabilities"] = datetime.date.fromisoformat(
                str(liabilities_period_end)[:10]
            )
        except ValueError:
            pass

    items = reconcile_fn(
        model_inputs, facts, as_of=utc_today(), model_period_ends=model_period_ends
    )
    typer.echo(f"{symbol} の突合結果")
    typer.echo(f"{'概念':<20}{'モデル値':>18}{'SEC値':>18}{'差':>10}  タグ")
    for item in items:
        model_str = f"{item.model_value:,.0f}" if item.model_value is not None else "n/a"
        sec_str = f"{item.sec_value:,.0f}" if item.sec_value is not None else "n/a"
        diff_str = f"{item.relative_diff:.1%}" if item.relative_diff is not None else "n/a"
        typer.echo(f"{item.concept:<20}{model_str:>18}{sec_str:>18}{diff_str:>10}  {item.sec_tag or ''} [{item.status}]")


_BENCHMARK_SYMBOLS = ("IWM", "IWC", "IJR", "SPY")


@app.command("register-benchmarks")
def register_benchmarks_cmd(
    symbols: str = typer.Option(
        ",".join(_BENCHMARK_SYMBOLS),
        help="ベンチマークとして登録するETFのティッカー(カンマ区切り)。",
    ),
) -> None:
    """D-4(docs/defect_and_edge_audit_2026-08-28.md):ベンチマークETFを登録する。

    IWM(Russell 2000)/ IWC(マイクロキャップ)/ IJR(S&P600)/ SPY を
    `tickers` に `is_benchmark=True` で入れる。`universe_source.filter_candidates`
    は ETF を落とすので手動投入する。登録後:

        uv run python -m autoscreener.cli backfill-history --symbols IWM,IWC,IJR,SPY
        uv run python -m autoscreener.cli collect --symbols IWM,IWC,IJR,SPY

    で価格を収集する。`apply_gates` は `is_benchmark` を `included=False` で外すため
    ランキングには出ない。
    """
    from autoscreener.db.models import Ticker

    targets = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    with session_scope() as session:
        for symbol in targets:
            ticker = session.query(Ticker).filter_by(symbol=symbol).one_or_none()
            if ticker is None:
                session.add(Ticker(symbol=symbol, market="US", is_benchmark=True))
                typer.echo(f"  registered {symbol}")
            else:
                ticker.is_benchmark = True
                ticker.is_quarantined = False
                ticker.consecutive_failures = 0
                typer.echo(f"  marked existing {symbol} as benchmark")
    typer.echo(
        "next: backfill-history --symbols "
        + ",".join(targets)
        + "  then  collect --symbols "
        + ",".join(targets)
    )


@app.command("recover-quarantine")
def recover_quarantine_cmd(
    backup: bool = typer.Option(True, help="実行前に pg_dump バックアップを取る。"),
) -> None:
    """A-1(docs/defect_and_edge_audit_2026-08-28.md D-12):一斉隔離からの応急復旧。

    レート制限やネットワーク断で1回の収集実行が全滅すると、全銘柄が同時に隔離
    され、`select_collectable_symbols` は `retry_interval_days` 経過後にしか
    戻さないため復旧が遅延する。この復旧経路(サーキットブレーカーのロールバック)は
    A-1 で `run_daily_collection` に組み込んだが、**既に一斉隔離されている状態**は
    このコマンドで解除する。

    `is_quarantined` の銘柄すべてについて `is_quarantined=false` /
    `consecutive_failures=0` に戻す。`delisted_at` が設定済みの銘柄は触らない
    (そちらは B-5 の `empty_response_delisted` 経路で確定した実質消失)。
    """
    from sqlalchemy import text

    if backup:
        from autoscreener.batch.backup import run_backup

        typer.echo("taking backup before mass update...")
        path = run_backup()
        typer.echo(f"  backup written: {path}")

    with session_scope() as session:
        result = session.execute(
            text(
                "UPDATE tickers SET is_quarantined = false, consecutive_failures = 0 "
                "WHERE is_quarantined = true AND delisted_at IS NULL"
            )
        )
        typer.echo(f"un-quarantined {result.rowcount} tickers")


@app.command("run-monitoring")
def run_monitoring_cmd() -> None:
    """保有・追跡銘柄の四半期モニタリング指標とレッドフラグを評価し、新規点灯を alerts に記録する(30.7.4)。"""
    from autoscreener.batch.run_monitoring import run_monitoring

    counts = run_monitoring()
    for key, count in counts.items():
        typer.echo(f"  {key}: {count}")


@app.command("collect-events")
def collect_events_cmd(
    symbols: str = typer.Option("", help="カンマ区切りのティッカーを直接指定する(省略時は追跡対象を自動選定)。"),
) -> None:
    """J-6:追跡対象銘柄の次回決算日を event_calendar に収集する。

    yfinance `Ticker.calendar` の次回決算日のみ採用し、過去日は捨てる。
    `run-daily-pipeline` の週次(月曜)工程で回す想定。失敗しても止めない。
    """
    from autoscreener.batch.collect_events import collect_events

    target_symbols = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else None
    counts = collect_events(symbols=target_symbols)
    for key, count in counts.items():
        typer.echo(f"  {key}: {count}")


@app.command("collect-insider")
def collect_insider_cmd(
    symbols: str = typer.Option("", help="カンマ区切りのティッカー(省略時は追跡対象を自動選定)。"),
) -> None:
    """J-7:追跡対象銘柄の Form 4(インサイダー取引)を insider_transactions に収集する。

    **原則3:ゲート・スコアには入れない。** 表示とアラートのみ。
    """
    from autoscreener.batch.collect_supply import collect_insider

    target = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else None
    for key, count in collect_insider(symbols=target).items():
        typer.echo(f"  {key}: {count}")


@app.command("collect-short-interest")
def collect_short_interest_cmd(
    symbols: str = typer.Option("", help="カンマ区切りのティッカー(省略時は追跡対象を自動選定)。"),
) -> None:
    """J-7:FINRA の空売り残を short_interest に収集する。月2回・数営業日遅れ。

    **原則3:ゲート・スコアには入れない。** 遅延日数は API/UI が必ず表示する。
    """
    from autoscreener.batch.collect_supply import collect_short_interest

    target = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else None
    for key, count in collect_short_interest(symbols=target).items():
        typer.echo(f"  {key}: {count}")


@app.command("ack")
def ack_cmd(alert_id: int = typer.Argument(..., help="確認済みにするアラートのid(GET /alertsで確認できる)。")) -> None:
    """アラートを確認済みにする(30.7.4)。APIは読み取り専用なので、この書き込みはCLI経由でのみ行う(18.6)。"""
    from autoscreener.db.models import Alert

    with session_scope() as session:
        alert = session.query(Alert).filter_by(id=alert_id).one_or_none()
        if alert is None:
            typer.echo(f"alert id {alert_id} not found")
            raise typer.Exit(code=1)
        if alert.acknowledged_at is not None:
            typer.echo(f"alert id {alert_id} is already acknowledged at {alert.acknowledged_at}")
            return
        alert.acknowledged_at = datetime.datetime.now(datetime.UTC)
    typer.echo(f"alert id {alert_id} acknowledged")


# ---------------------------------------------------------------------------
# K-9:LLM(Claude API)を使う定性分析。
#
# **どれも日次パイプラインには入っていない。** 呼ぶたびに実費が出るためで、
# 人間が明示的に叩いたときだけ動く。1回あたりの上限は
# `config/collection.yaml` の `llm.max_tickers_per_run` が決める。
#
# 出力は `llm_analyses` に隔離され、ゲートにもスコアにも入らない
# (`src/autoscreener/llm/__init__.py` を参照)。
# ---------------------------------------------------------------------------


def _parse_sections(value: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """`--sections` のカンマ区切りを解く。空なら既定値。"""
    parsed = tuple(s.strip() for s in value.split(",") if s.strip())
    return parsed or default


@app.command("summarize-filings")
def summarize_filings_cmd(
    symbols: str = typer.Option("", help="カンマ区切りのティッカー(省略時は追跡対象を自動選定)。"),
    sections: str = typer.Option("", help="要約するItem(既定: item1a,item7)。"),
    limit: int = typer.Option(0, help="銘柄数の上限。0なら llm.max_tickers_per_run を使う。"),
) -> None:
    """K-9:直近の10-K/10-Q本文をClaudeに要約させ、llm_analyses に保存する。

    **課金が発生する。** ANTHROPIC_API_KEY 未設定なら何もせず0件で終わる。
    同じ提出書類・同じ指示文の要約が既にあれば作り直さない(existing で数える)。
    """
    from autoscreener.batch.summarize_filings import DEFAULT_SECTIONS, summarize_filings

    target_symbols = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else None
    counts = summarize_filings(
        symbols=target_symbols,
        sections=_parse_sections(sections, DEFAULT_SECTIONS),
        limit=limit or None,
    )
    for key, count in counts.items():
        typer.echo(f"  {key}: {count}")


@app.command("score-qualitative")
def score_qualitative_cmd(
    symbols: str = typer.Option("", help="カンマ区切りのティッカー(省略時は追跡対象を自動選定)。"),
    sections: str = typer.Option("", help="読ませるItem(既定: item1a,item7)。"),
    limit: int = typer.Option(0, help="銘柄数の上限。0なら llm.max_tickers_per_run を使う。"),
    batch_id: str = typer.Option("", help="投げ直さず、このbatch_idの結果回収だけを行う。"),
) -> None:
    """K-9:定性評価を Batch API(料金50%)で作り、llm_analyses に保存する。

    `conviction` は low/medium/high の順序尺度であり、点数ではない——
    ゲートにもスコアにも入れない前提の値である。

    待ち時間が llm.batch_timeout_seconds を超えて落ちても、ログに出た batch_id を
    `--batch-id` に渡せば回収だけをやり直せる(バッチはサーバ側で走り続ける)。
    """
    from autoscreener.batch.score_qualitative import score_qualitative
    from autoscreener.batch.summarize_filings import DEFAULT_SECTIONS

    target_symbols = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else None
    counts = score_qualitative(
        symbols=target_symbols,
        sections=_parse_sections(sections, DEFAULT_SECTIONS),
        limit=limit or None,
        batch_id=batch_id or None,
    )
    for key, count in counts.items():
        typer.echo(f"  {key}: {count}")


@app.command("generate-report")
def generate_report_cmd(
    date: str = typer.Option("", help="対象のscore_date(YYYY-MM-DD)。省略時はscoresの最新日。"),
    top_n: int = typer.Option(10, help="レポートに載せる上位銘柄数。"),
    provider: str = typer.Option("", help="anthropic | openai_compat。省略時は collection.yaml の既定。"),
    model: str = typer.Option("", help="使うモデルID。省略時は collection.yaml の既定。"),
    effort: str = typer.Option("", help="low|medium|high|xhigh|max。省略時は collection.yaml の既定。"),
    show: bool = typer.Option(False, "--show", help="生成したレポート本文を標準出力にも出す。"),
) -> None:
    """K-9:当日ランキングの説明文を生成し、llm_analyses に保存する。

    数値はすべて scores の値をそのまま使わせる(モデルに再計算させない)。
    データが古い銘柄があれば、レポート内に必ず列挙させる。

    `--provider` / `--model` / `--effort` は collection.yaml を編集せずに1回だけ
    別モデルを試すための上書き(UIの生成フォームと同じ)。
    """
    from autoscreener.batch.generate_report import KIND, generate_report
    from autoscreener.config import load_llm_config
    from autoscreener.db.models import LlmAnalysis

    cfg = load_llm_config()
    overrides = {
        k: v
        for k, v in {"provider": provider, "model": model, "effort": effort}.items()
        if v
    }
    if overrides:
        cfg = type(cfg)(**{**cfg.model_dump(), **overrides})

    counts = generate_report(score_date=_parse_date(date), top_n=top_n, config=cfg)
    for key, count in counts.items():
        typer.echo(f"  {key}: {count}")

    if show:
        with session_scope() as session:
            row = (
                session.query(LlmAnalysis)
                .filter(LlmAnalysis.kind == KIND)
                .order_by(LlmAnalysis.id.desc())
                .first()
            )
            if row is not None:
                typer.echo("")
                typer.echo(row.content or "")


if __name__ == "__main__":
    app()
