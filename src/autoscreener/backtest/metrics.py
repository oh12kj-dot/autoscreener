"""バックテストのKPI算出(14.2)。すべて純粋関数。

14.2は成功指標を5つ定めているが、**絶対値より単調性が重要**としている。
本モジュールもその優先順位に従い、デシル単調性を主指標、リフト倍率を副指標、
較正誤差を「モデルが出す確率の水準そのものが正しいか」の検査として扱う。

**「10倍達成率」を短いホライズンでどう測るか(27.12)**:価格ヒストリーは
3年分しかなく(13.1)、7年で10倍という最終判定はどう頑張っても今日は測れない。
代わりに「**その時点でのペース**」を見る。10倍/7年は年率38.9%なので、
ホライズン h 年での等価閾値は `10^(h/7)` になる(1年なら1.389倍)。
これを満たした観測を「オンペース」と数える。

オンペースは10倍の達成を意味しない(その後に失速する)。だが**モデルの序列が
正しいかどうか**を測るには十分であり、7年待つ理由にはならない。
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field, replace
from statistics import NormalDist

_NORMAL = NormalDist()

# D-2(defect_and_edge_audit_2026-08-28.md):観測数がこれ未満の評価日は、
# 資産相関推定(runner._MIN_DATES_FOR_CORRELATION)と同じ基準で「独立な1点」
# として扱わない。最悪日リフト・右裾リフトの母集団から外す。
MIN_DATE_OBSERVATIONS = 100


@dataclass(frozen=True)
class Observation:
    """バックテストの1観測=(ある評価日のある銘柄)。"""

    ticker_id: int
    base_date: str
    probability: float  # モデルが出した P(MOIC >= target) (ホライズン7年)
    log_moic_mu: float
    log_moic_sigma: float
    realized_return: float
    settlement: str
    # 28.9:層別の診断に使う補助情報。KPIの計算そのものには要らないが、
    # 「どの規模・どのセクターで効いていて、どこで効いていないか」を
    # 測れないと、改善の当てずっぽうが止まらない。
    expected_moic: float = 1.0
    market_cap: float = 0.0
    sector: str | None = None
    # S-8(2026-08-26、model_audit_v4_2026-08-26.md):価格ナウキャストが
    # 初期成長率に加えた補正量。`nowcast_cap_hit_rate` の算出に使う。
    growth_nowcast_adjustment: float = 0.0
    # D-5(defect_and_edge_audit_2026-08-28.md):この観測を建て・決済したときの
    # 往復取引コスト(bps)。Corwin–Schultz スプレッド + 平方根則インパクト。
    # `cost_adjusted_metrics` がコスト後KPIを出すのに使う。
    cost_bps: float = 0.0
    # D-8:単純ベースライン(モメンタム・成長率・割安・規模・ランダム)のスコア。
    # `backtest.baselines.baseline_metrics` が同一観測でKPIを出して v4 と比較する。
    baseline_scores: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DateStat:
    """1評価日分のKPI。**平均だけを見ないための内訳**(28.9)。

    リフト倍率の平均が1.3でも、9評価日のうち3日が1.0を下回っていれば
    「効いている」とは言えない。14.2が絶対値より単調性を重視するのと同じ理由で、
    **最悪の評価日**を必ず併記する。
    """

    base_date: str
    count: int
    universe_on_pace_rate: float
    top_decile_on_pace_rate: float
    lift_ratio: float
    rank_ic: float


@dataclass(frozen=True)
class TailLift:
    """右裾の事象に対するリフト(28.11)。**14.2のKPIの読み替え**。

    14.2は「リフト倍率 >= 2.0」を成功指標に挙げているが、その分子・分母は
    本来「10バガー達成率」——つまり極端に稀な事象——だった。価格ヒストリーが
    3年しかないため、27.12はこれを「1年で1.389倍(=10倍/7年と同じ年率)」に
    読み替えたが、**その事象の基準率は26%であり、右裾ではない**。基準率26%の
    事象で2倍のリフトを出すというのは「上位10%の52%が達成する」ことを要求する、
    ほとんど達成不可能な基準である。目標値だけを持ち越したのは読み替えの誤り
    だった。

    ここでは事象そのものを右裾へずらして測り直す。閾値は**各評価日の断面の
    リターン分位**で決めるので、強気相場でも弱気相場でも基準率は定義上
    `quantile` に固定される——レジームの影響を受けずに「モデルはその日の
    勝ち組上位◯%をどれだけ多く捕まえたか」だけを測れる。

    実測(28.11):v4 の上位10%のリフトは基準率とともに単調に上がる。
      上位25%の事象 → 1.32倍 / 上位10% → 1.49倍 / 上位5% → 1.60倍
    **モデルは裾を見つける道具であり、平均的な勝ちを見つける道具ではない。**
    """

    quantile: float  # 断面リターンの上位何割を「当たり」とするか
    median_threshold_return: float  # その分位に対応するリターン(評価日の中央値)
    top_decile_hit_rate: float
    lift: float
    worst_date_lift: float


@dataclass(frozen=True)
class CalibrationBin:
    """較正曲線の1点(28.8)。予測確率の階級ごとに、実測頻度と突き合わせる。"""

    lower: float
    upper: float
    count: int
    mean_predicted: float
    realized_rate: float


@dataclass(frozen=True)
class DecileStat:
    decile: int  # 1 = 最上位(確率が最も高い)
    count: int
    mean_probability: float
    median_return: float
    mean_return: float
    on_pace_rate: float
    loss_rate: float  # −50%以下になった割合(14.2の破綻回避率の代理)


@dataclass(frozen=True)
class BacktestMetrics:
    observation_count: int
    horizon_years: float
    on_pace_threshold: float
    universe_on_pace_rate: float
    universe_median_return: float

    deciles: list[DecileStat] = field(default_factory=list)

    # 14.2:スコア上位10%〜下位10%の各層の将来リターン中央値が単調か
    decile_monotonicity: float = 0.0  # デシル順位と中央値リターンの順位相関(+1が理想)
    strictly_monotonic: bool = False

    # 14.2:上位デシルのオンペース率 ÷ ユニバース全体のオンペース率
    lift_ratio: float = 0.0

    # 14.2:破綻回避率(上位デシルの大幅下落率 < ユニバース平均であること)
    universe_loss_rate: float = 0.0
    top_decile_loss_rate: float = 0.0

    # モデルが出す確率の水準そのものの検査(27.12)
    mean_predicted_on_pace_rate: float = 0.0
    calibration_error: float = 0.0

    # 27.11:上場廃止として決済された観測の割合。0%なら生存バイアスを疑う
    delisted_settlement_rate: float = 0.0

    # 28.9:平均だけでは分からないものを測る
    rank_ic: float = 0.0  # 評価日ごとの順位ICの平均(確率 vs 実現リターン)
    rank_ic_t_stat: float = 0.0  # 上の平均の t値(評価日を独立とみなした場合の上限値)
    lift_ratio_worst_date: float = 0.0  # 最悪の評価日のリフト倍率

    # S-8(2026-08-26、model_audit_v4_2026-08-26.md):価格ナウキャストが
    # `nowcast_cap`(または反転方向は `nowcast_cap_sign_flip`)の上限に
    # 張り付いている観測の割合。高いほど「補正のはずが実質モメンタム加点に
    # なっている」ことを示す診断指標。設計意図(28.3「これはモメンタム戦略
    # ではない」)と実挙動の乖離を継続的に監視するために追加した。
    nowcast_cap_hit_rate: float = 0.0

    # D-5(defect_and_edge_audit_2026-08-28.md):観測ごとの往復取引コスト(bps)の
    # 平均と、コストを差し引いた後の主要KPI。`after_cost` を**別立て**にするのは、
    # 片方だけを出すと次に読む人が必ず取り違えるため(D-5 修正案2)。
    mean_round_trip_cost_bps: float = 0.0
    after_cost: dict | None = None

    # D-4(defect_and_edge_audit_2026-08-28.md):ポートフォリオ・シミュレーション
    # (`backtest.portfolio_sim.PortfolioBacktest.as_dict()`)。上位N銘柄を等金額で
    # 建て、指数(IWC 等)超過CAGRと最大ドローダウンを1つの数字で出す。
    portfolio: dict | None = None

    # D-10(defect_and_edge_audit_2026-08-28.md):ライブ相当ゲート通過数 /
    # 旧「バックテスト専用」ゲート通過数。1.0 未満なら、ライブでだけ削られていた
    # 銘柄群(cash_runway_floor 等)がバックテストの母集団に混じっていた分。
    gate_parity: dict | None = None

    # D-8:単純ベースライン(momentum_12m / revenue_growth / cheapness /
    # gross_profit_scale / random)の lift / monotonicity / rank_ic。v4 がこれらに
    # ブートストラップCIを超える差で勝てなければ、v4 の複雑さは正当化されない。
    baselines: dict | None = None

    # C-5(I-4):KPIの層別(現状は市場規模5分位。浮動株比率が MoicInputs に
    # 入れば同じ関数で `by_public_float` を出せる)。edge がどの帯で立っているか。
    stratified_kpis: dict | None = None

    # D-2(defect_and_edge_audit_2026-08-28.md):観測数の少ない評価日を独立と
    # 扱わないための量。`effective_dates` は Kish の実効標本サイズ(評価日ごとの
    # 観測数で重み付けした「実質的な評価日数」)。CI は評価日単位のブロック・
    # ブートストラップ(同一日の銘柄は共通因子で相関しているので銘柄単位では
    # リサンプルしない)。`non_overlapping` はホライズン以上の間隔で走らせた
    # 重複ゼロの実行かどうか。
    effective_dates: float = 0.0
    rank_ic_ci: tuple[float, float] | None = None
    lift_ratio_ci: tuple[float, float] | None = None
    decile_monotonicity_ci: tuple[float, float] | None = None
    non_overlapping: bool = False

    # D-3(defect_and_edge_audit_2026-08-28.md):14.2 の成功指標に対する
    # PASS / FAIL / INSUFFICIENT_DATA 判定。`run-backtest` は FAIL があれば
    # 非ゼロ終了する。
    kpi_verdicts: dict = field(default_factory=dict)

    per_date: list[DateStat] = field(default_factory=list)
    calibration_curve: list[CalibrationBin] = field(default_factory=list)
    tail_lifts: list[TailLift] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "observation_count": self.observation_count,
            "horizon_years": self.horizon_years,
            "on_pace_threshold": self.on_pace_threshold,
            "universe_on_pace_rate": self.universe_on_pace_rate,
            "universe_median_return": self.universe_median_return,
            "decile_monotonicity": self.decile_monotonicity,
            "strictly_monotonic": self.strictly_monotonic,
            "lift_ratio": self.lift_ratio,
            "universe_loss_rate": self.universe_loss_rate,
            "top_decile_loss_rate": self.top_decile_loss_rate,
            "mean_predicted_on_pace_rate": self.mean_predicted_on_pace_rate,
            "calibration_error": self.calibration_error,
            "delisted_settlement_rate": self.delisted_settlement_rate,
            "rank_ic": self.rank_ic,
            "rank_ic_t_stat": self.rank_ic_t_stat,
            "lift_ratio_worst_date": self.lift_ratio_worst_date,
            "nowcast_cap_hit_rate": self.nowcast_cap_hit_rate,
            "mean_round_trip_cost_bps": self.mean_round_trip_cost_bps,
            "after_cost": self.after_cost,
            "portfolio": self.portfolio,
            "gate_parity": self.gate_parity,
            "baselines": self.baselines,
            "stratified_kpis": self.stratified_kpis,
            "kpi_verdicts": self.kpi_verdicts,
            "effective_dates": self.effective_dates,
            "rank_ic_ci": list(self.rank_ic_ci) if self.rank_ic_ci is not None else None,
            "lift_ratio_ci": list(self.lift_ratio_ci) if self.lift_ratio_ci is not None else None,
            "decile_monotonicity_ci": (
                list(self.decile_monotonicity_ci) if self.decile_monotonicity_ci is not None else None
            ),
            "non_overlapping": self.non_overlapping,
            "per_date": [
                {
                    "base_date": d.base_date,
                    "count": d.count,
                    "universe_on_pace_rate": d.universe_on_pace_rate,
                    "top_decile_on_pace_rate": d.top_decile_on_pace_rate,
                    "lift_ratio": d.lift_ratio,
                    "rank_ic": d.rank_ic,
                }
                for d in self.per_date
            ],
            "tail_lifts": [
                {
                    "quantile": t.quantile,
                    "median_threshold_return": t.median_threshold_return,
                    "top_decile_hit_rate": t.top_decile_hit_rate,
                    "lift": t.lift,
                    "worst_date_lift": t.worst_date_lift,
                }
                for t in self.tail_lifts
            ],
            "calibration_curve": [
                {
                    "lower": b.lower,
                    "upper": b.upper,
                    "count": b.count,
                    "mean_predicted": b.mean_predicted,
                    "realized_rate": b.realized_rate,
                }
                for b in self.calibration_curve
            ],
            "deciles": [
                {
                    "decile": d.decile,
                    "count": d.count,
                    "mean_probability": d.mean_probability,
                    "median_return": d.median_return,
                    "mean_return": d.mean_return,
                    "on_pace_rate": d.on_pace_rate,
                    "loss_rate": d.loss_rate,
                }
                for d in self.deciles
            ],
        }


def on_pace_threshold(target_moic: float, horizon_years: float, model_horizon_years: float) -> float:
    """ホライズン `horizon_years` における「10倍/7年ペース」の等価倍率。

    `target_moic ** (horizon_years / model_horizon_years)`。7年で10倍と同じ
    年率(38.9%)を、より短い期間に引き直しただけの値。
    """
    return target_moic ** (horizon_years / model_horizon_years)


def scale_probability_to_horizon(
    log_moic_mu: float,
    log_moic_sigma: float,
    target_moic: float,
    horizon_years: float,
    model_horizon_years: float,
) -> float:
    """モデルの7年予測を、バックテストのホライズンにおけるオンペース確率へ引き直す。

    log-MOIC をドリフト付きランダムウォークとみなし、割合 f = h/H に対して

        mu_f = mu * f,   sigma_f = sigma * sqrt(f)

    とする。閾値も同じ割合で縮む(`on_pace_threshold`)ので、

        P(オンペース) = 1 - Φ( (ln(target)*f - mu_f) / sigma_f )

    **近似であることの明記**:実際のモデルは成長減衰により初期のドリフトが
    大きい(前倒しになる)ため、この線形按分は短いホライズンでの上昇を
    やや過小評価する。較正誤差を読むときはこの向きのバイアスを考慮すること。
    """
    fraction = horizon_years / model_horizon_years
    if fraction <= 0 or log_moic_sigma <= 0:
        return 0.0
    scaled_mu = log_moic_mu * fraction
    scaled_sigma = log_moic_sigma * math.sqrt(fraction)
    z = (math.log(target_moic) * fraction - scaled_mu) / scaled_sigma
    return 1 - _NORMAL.cdf(z)


def spearman(xs: list[float], ys: list[float]) -> float:
    """順位相関。デシル番号と中央値リターンの単調性を測るのに使う。

    サンプルが3未満、または一方が定数なら0を返す(単調性を主張しない)。
    """
    if len(xs) < 3 or len(xs) != len(ys):
        return 0.0
    rank_x = _ranks(xs)
    rank_y = _ranks(ys)
    try:
        return statistics.correlation(rank_x, rank_y)
    except statistics.StatisticsError:
        return 0.0


def _ranks(values: list[float]) -> list[float]:
    """平均順位法。同値は順位を平均する。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        average_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = average_rank
        i = j + 1
    return ranks


def compute_metrics(
    observations: list[Observation],
    target_moic: float,
    horizon_years: float,
    model_horizon_years: float,
    decile_count: int = 10,
    nowcast_cap: float = 0.0,
    *,
    bootstrap_resamples: int = 0,
    non_overlapping: bool = False,
    kpi_acceptance: object | None = None,
) -> BacktestMetrics:
    """観測群から14.2のKPIを算出する。

    デシルは**評価日ごとに**確率の降順で切ってから束ねる(decile 1 = 各評価日で
    モデルが最も有望とした10%)。理由は `_cross_sectional_buckets` を参照。

    D-2(defect_and_edge_audit_2026-08-28.md):
    - `rank_ic` は評価日ごとの値を**観測数で加重平均**する(観測20件の日と
      629件の日を同じ重みで平均していたのを是正)。
    - `lift_ratio_worst_date` は `MIN_DATE_OBSERVATIONS` 未満の日を母集団から外す。
    - `bootstrap_resamples > 0` のとき、評価日単位のブロック・ブートストラップで
      主要KPIの95%CIを出す(同一日の銘柄は共通因子で相関しているので銘柄単位では
      リサンプルしない)。
    - `kpi_acceptance` を渡すと `kpi_verdicts`(PASS/FAIL/INSUFFICIENT_DATA)を出す(D-3)。
    """
    threshold = on_pace_threshold(target_moic, horizon_years, model_horizon_years)
    if not observations:
        return BacktestMetrics(
            observation_count=0,
            horizon_years=horizon_years,
            on_pace_threshold=threshold,
            universe_on_pace_rate=0.0,
            universe_median_return=0.0,
        )

    returns = [o.realized_return for o in observations]
    universe_on_pace = _rate(returns, lambda r: 1 + r >= threshold)
    universe_loss = _rate(returns, lambda r: r <= -0.5)

    buckets = _cross_sectional_buckets(observations, decile_count)

    deciles: list[DecileStat] = []
    for index, bucket in enumerate(buckets, start=1):
        if not bucket:
            continue
        bucket_returns = [o.realized_return for o in bucket]
        deciles.append(
            DecileStat(
                decile=index,
                count=len(bucket),
                mean_probability=statistics.fmean(o.probability for o in bucket),
                median_return=statistics.median(bucket_returns),
                mean_return=statistics.fmean(bucket_returns),
                on_pace_rate=_rate(bucket_returns, lambda r: 1 + r >= threshold),
                loss_rate=_rate(bucket_returns, lambda r: r <= -0.5),
            )
        )

    # デシル番号は小さいほど有望なので、単調性は「−デシル番号 と 中央値リターン」の
    # 順位相関で測る(+1 = 上位ほどリターンが高い、という理想の並び)。
    monotonicity = spearman([-d.decile for d in deciles], [d.median_return for d in deciles])
    strictly_monotonic = all(
        deciles[i].median_return >= deciles[i + 1].median_return for i in range(len(deciles) - 1)
    )

    top = deciles[0] if deciles else None
    lift = (top.on_pace_rate / universe_on_pace) if (top and universe_on_pace > 0) else 0.0

    predicted = [
        scale_probability_to_horizon(
            o.log_moic_mu, o.log_moic_sigma, target_moic, horizon_years, model_horizon_years
        )
        for o in observations
    ]
    mean_predicted = statistics.fmean(predicted)

    per_date = per_date_stats(observations, threshold)
    date_ics = [d.rank_ic for d in per_date]
    date_counts = [d.count for d in per_date]
    # D-2:観測数で加重した順位IC。n=20 の日を n=629 の日と同じ重みで平均しない。
    weighted_rank_ic = _weighted_mean(date_ics, date_counts)
    # D-2:最悪日リフトは観測数の十分ある日だけから取る(薄い日のノイズを除く)。
    dense_lifts = [
        d.lift_ratio for d in per_date if d.lift_ratio > 0 and d.count >= MIN_DATE_OBSERVATIONS
    ]
    date_lifts = dense_lifts or [d.lift_ratio for d in per_date if d.lift_ratio > 0]
    effective_dates = _kish_effective_size(date_counts)

    # S-8:上限の99.9%以上まで動いた観測を「張り付いた」とみなす(浮動小数の
    # 丸め誤差でちょうど一致しないケースを取りこぼさないための許容)。
    nowcast_hit_rate = (
        _rate(observations, lambda o: abs(o.growth_nowcast_adjustment) >= nowcast_cap * 0.999)
        if nowcast_cap > 0
        else 0.0
    )

    mean_cost_bps = (
        statistics.fmean(o.cost_bps for o in observations)
        if any(o.cost_bps for o in observations)
        else 0.0
    )
    top_decile_loss_ratio = (top.loss_rate / universe_loss) if (top and universe_loss > 0) else None

    rank_ic_ci = lift_ratio_ci = decile_monotonicity_ci = None
    if bootstrap_resamples > 0:
        rank_ic_ci = bootstrap_kpi_interval(
            observations,
            lambda obs: _weighted_mean(
                [d.rank_ic for d in per_date_stats(obs, threshold)],
                [d.count for d in per_date_stats(obs, threshold)],
            ),
            n_resamples=bootstrap_resamples,
        )
        lift_ratio_ci = bootstrap_kpi_interval(
            observations,
            lambda obs: compute_metrics(
                obs, target_moic, horizon_years, model_horizon_years, decile_count
            ).lift_ratio,
            n_resamples=bootstrap_resamples,
        )
        decile_monotonicity_ci = bootstrap_kpi_interval(
            observations,
            lambda obs: compute_metrics(
                obs, target_moic, horizon_years, model_horizon_years, decile_count
            ).decile_monotonicity,
            n_resamples=bootstrap_resamples,
        )

    verdicts = evaluate_kpi_verdicts(
        lift_ratio=lift,
        decile_monotonicity=monotonicity,
        rank_ic=weighted_rank_ic,
        calibration_error=mean_predicted - universe_on_pace,
        top_decile_loss_ratio=top_decile_loss_ratio,
        effective_dates=effective_dates,
        acceptance=kpi_acceptance,
    )

    return BacktestMetrics(
        observation_count=len(observations),
        horizon_years=horizon_years,
        on_pace_threshold=threshold,
        universe_on_pace_rate=universe_on_pace,
        universe_median_return=statistics.median(returns),
        deciles=deciles,
        decile_monotonicity=monotonicity,
        strictly_monotonic=strictly_monotonic,
        lift_ratio=lift,
        universe_loss_rate=universe_loss,
        top_decile_loss_rate=top.loss_rate if top else 0.0,
        mean_predicted_on_pace_rate=mean_predicted,
        calibration_error=mean_predicted - universe_on_pace,
        delisted_settlement_rate=_rate(
            [o.settlement for o in observations],
            lambda s: s in ("delisted", "delisted_unpriced"),
        ),
        rank_ic=weighted_rank_ic,
        rank_ic_t_stat=_t_stat(date_ics),
        lift_ratio_worst_date=min(date_lifts) if date_lifts else 0.0,
        nowcast_cap_hit_rate=nowcast_hit_rate,
        mean_round_trip_cost_bps=mean_cost_bps,
        effective_dates=effective_dates,
        rank_ic_ci=rank_ic_ci,
        lift_ratio_ci=lift_ratio_ci,
        decile_monotonicity_ci=decile_monotonicity_ci,
        non_overlapping=non_overlapping,
        kpi_verdicts=verdicts,
        per_date=per_date,
        calibration_curve=calibration_curve(observations, predicted, threshold),
        tail_lifts=tail_lifts(observations),
    )


# 28.11:右裾のリフトを測る分位。上位25%は「平均的な勝ち」、上位2%が
# 「10バガーの入口」に相当する層。
TAIL_QUANTILES = (0.25, 0.10, 0.05, 0.02)


def tail_lifts(
    observations: list[Observation], quantiles: tuple[float, ...] = TAIL_QUANTILES
) -> list[TailLift]:
    """各評価日の断面リターン分位を閾値にして、上位デシルのリフトを測る(28.11)。

    閾値を絶対値(1.389倍など)ではなく**その日の断面の分位**で決めるのが要点。
    絶対閾値だと、強気相場の評価日では基準率が45%、弱気相場では17%になり、
    リフトの比較がレジームの比較になってしまう。分位で切れば基準率は定義上
    一定なので、残るのは純粋に「モデルが勝ち組を引けたか」だけになる。
    """
    by_date: dict[str, list[Observation]] = {}
    for observation in observations:
        by_date.setdefault(observation.base_date, []).append(observation)

    results: list[TailLift] = []
    for quantile in quantiles:
        thresholds: list[float] = []
        hit_rates: list[float] = []
        lifts: list[float] = []
        for bucket in by_date.values():
            if len(bucket) < 50:
                continue
            returns = sorted((o.realized_return for o in bucket), reverse=True)
            cutoff_index = max(1, int(len(returns) * quantile)) - 1
            cutoff_val = returns[cutoff_index]
            # D-14(defect_and_edge_audit_2026-08-28.md):`r >= threshold` は
            # 低価格帯で 0.00% リターンが密集する日にタイを全部「当たり」に数え、
            # リフトを過大にする。閾値を「カットオフ値と、それより厳密に小さい
            # 直近の値」の中点へ補間し、`>` で比較する(タイの下側を除外できる)。
            lower_vals = [r for r in returns[cutoff_index + 1 :] if r < cutoff_val]
            if lower_vals:
                threshold = (cutoff_val + lower_vals[0]) / 2
                predicate = lambda r, t=threshold: r > t
            else:
                threshold = cutoff_val
                predicate = lambda r, t=threshold: r >= t
            thresholds.append(threshold)
            ordered = sorted(bucket, key=lambda o: o.probability, reverse=True)
            top_n = max(1, len(ordered) // 10)
            hit_rate = _rate([o.realized_return for o in ordered[:top_n]], predicate)
            hit_rates.append(hit_rate)
            lifts.append(hit_rate / quantile)
        if not lifts:
            continue
        results.append(
            TailLift(
                quantile=quantile,
                median_threshold_return=statistics.median(thresholds),
                top_decile_hit_rate=statistics.fmean(hit_rates),
                lift=statistics.fmean(lifts),
                worst_date_lift=min(lifts),
            )
        )
    return results


def per_date_stats(observations: list[Observation], threshold: float) -> list[DateStat]:
    """評価日ごとのKPI(28.9)。

    **なぜ評価日で切るのか。** 保有期間が重なっている観測をひとつの母集団として
    平均すると、実質的に独立な観測期間が3つ程度しかないことが見えなくなる
    (27.18)。評価日ごとに切っておけば、「9日中何日でリフトが1を超えたか」
    という、平均より遥かに正直な読み方ができる。
    """
    by_date: dict[str, list[Observation]] = {}
    for observation in observations:
        by_date.setdefault(observation.base_date, []).append(observation)

    stats: list[DateStat] = []
    for base_date in sorted(by_date):
        bucket = by_date[base_date]
        if len(bucket) < 10:
            continue
        bucket_returns = [o.realized_return for o in bucket]
        universe_rate = _rate(bucket_returns, lambda r: 1 + r >= threshold)
        ordered = sorted(bucket, key=lambda o: o.probability, reverse=True)
        top_n = max(1, len(ordered) // 10)
        top_rate = _rate([o.realized_return for o in ordered[:top_n]], lambda r: 1 + r >= threshold)
        stats.append(
            DateStat(
                base_date=base_date,
                count=len(bucket),
                universe_on_pace_rate=universe_rate,
                top_decile_on_pace_rate=top_rate,
                lift_ratio=(top_rate / universe_rate) if universe_rate > 0 else 0.0,
                rank_ic=spearman(
                    [o.probability for o in bucket], [o.realized_return for o in bucket]
                ),
            )
        )
    return stats


def calibration_curve(
    observations: list[Observation], predicted: list[float], threshold: float, bins: int = 10
) -> list[CalibrationBin]:
    """予測確率の階級ごとに、実測オンペース率と突き合わせる(28.8)。

    `calibration_error` は平均どうしの差しか見ないので、「全体としては合って
    いるが、高い確率を出した銘柄では外している」という形の誤りを見逃す。
    階級別に並べれば、**どの確率帯で外しているか**が分かる。

    階級は予測確率の分位で切る(等幅で切ると、分布が0近傍に集中するため
    ほとんどの階級が空になる)。
    """
    paired = sorted(
        ((p, o.realized_return) for p, o in zip(predicted, observations)), key=lambda x: x[0]
    )
    if len(paired) < bins * 10:
        return []

    out: list[CalibrationBin] = []
    base, remainder = divmod(len(paired), bins)
    start = 0
    for i in range(bins):
        size = base + (1 if i < remainder else 0)
        chunk = paired[start : start + size]
        start += size
        if not chunk:
            continue
        out.append(
            CalibrationBin(
                lower=chunk[0][0],
                upper=chunk[-1][0],
                count=len(chunk),
                mean_predicted=statistics.fmean(p for p, _ in chunk),
                realized_rate=_rate([r for _, r in chunk], lambda r: 1 + r >= threshold),
            )
        )
    return out


def _t_stat(values: list[float]) -> float:
    """平均の t値。評価日を独立とみなすので**上限値**であり、真の検出力より甘い。

    保有期間が重なっている以上、評価日どうしは独立ではない(27.18)。それでも
    載せるのは、「符号が安定しているか」を1つの数字で見るためであり、
    有意性の主張として読んではいけない。
    """
    if len(values) < 3:
        return 0.0
    spread = statistics.stdev(values)
    if spread == 0:
        return 0.0
    return statistics.fmean(values) / (spread / math.sqrt(len(values)))


def _rate(values: list, predicate) -> float:
    if not values:
        return 0.0
    return sum(1 for v in values if predicate(v)) / len(values)


def _weighted_mean(values: list[float], weights: list[float]) -> float:
    """観測数で加重した平均(D-2)。重みが全て0/空なら単純平均へフォールバック。"""
    if not values:
        return 0.0
    total_w = sum(weights) if weights else 0
    if total_w <= 0:
        return statistics.fmean(values)
    return sum(v * w for v, w in zip(values, weights)) / total_w


def _kish_effective_size(counts: list[int]) -> float:
    """Kish の実効標本サイズ (Σw)² / Σw²(D-2)。

    評価日ごとの観測数がばらついていると、「評価日8点」と言っても実質的な
    独立点はもっと少ない。n=20 と n=629 が混在する v4 の標本では 8 → 約 4.6。
    """
    usable = [c for c in counts if c > 0]
    if not usable:
        return 0.0
    return sum(usable) ** 2 / sum(c * c for c in usable)


def _group_by_date(observations: list[Observation]) -> dict[str, list[Observation]]:
    by_date: dict[str, list[Observation]] = {}
    for observation in observations:
        by_date.setdefault(observation.base_date, []).append(observation)
    return by_date


def bootstrap_kpi_interval(
    observations: list[Observation],
    kpi_fn,
    n_resamples: int = 1000,
    seed: int = 0,
    ci: float = 0.95,
) -> tuple[float, float] | None:
    """評価日単位のブロック・ブートストラップによるKPIの信頼区間(D-2 修正案2)。

    **銘柄単位ではなく評価日単位でリサンプルする。** 同一日の銘柄は共通因子
    (マクロ・金利・セクター循環)で相関しており、銘柄単位でリサンプルすると
    独立性を仮定した過小なCIになる(27.18)。

    評価日が3未満、または有効なリサンプルが半数に満たなければ None。
    """
    by_date = _group_by_date(observations)
    dates = list(by_date)
    if len(dates) < 3:
        return None
    rng = random.Random(seed)
    values: list[float] = []
    for _ in range(n_resamples):
        sample: list[Observation] = []
        for _ in range(len(dates)):
            sample.extend(by_date[rng.choice(dates)])
        try:
            v = kpi_fn(sample)
        except Exception:
            v = None
        if v is not None and math.isfinite(v):
            values.append(v)
    if len(values) < n_resamples // 2:
        return None
    values.sort()
    lower_q = (1 - ci) / 2
    upper_q = 1 - lower_q
    lo = values[min(len(values) - 1, int(lower_q * len(values)))]
    hi = values[min(len(values) - 1, int(upper_q * len(values)))]
    return (lo, hi)


# D-3(defect_and_edge_audit_2026-08-28.md):KPIの合否判定。
_INSUFFICIENT = "INSUFFICIENT_DATA"
_PASS = "PASS"
_FAIL = "FAIL"


def evaluate_kpi_verdicts(
    *,
    lift_ratio: float,
    decile_monotonicity: float,
    rank_ic: float,
    calibration_error: float,
    top_decile_loss_ratio: float | None,
    effective_dates: float,
    acceptance: object | None,
) -> dict[str, str]:
    """14.2 の成功指標に対する PASS / FAIL / INSUFFICIENT_DATA(D-3)。

    `acceptance` は `config.KpiAcceptanceConfig`。None なら空 dict。
    実効的な評価日数が `min_effective_dates` 未満なら全項目 INSUFFICIENT_DATA
    ——D-1/D-2 が直るまで多くのKPIはここに落ちる想定で、それが正しい状態表明。
    """
    if acceptance is None:
        return {}
    if effective_dates < getattr(acceptance, "min_effective_dates", 6):
        return {
            name: _INSUFFICIENT
            for name in (
                "lift_ratio",
                "decile_monotonicity",
                "rank_ic",
                "calibration_error",
                "top_decile_loss_ratio",
            )
        }

    def verdict(ok: bool) -> str:
        return _PASS if ok else _FAIL

    verdicts = {
        "lift_ratio": verdict(lift_ratio >= acceptance.min_lift_ratio),
        "decile_monotonicity": verdict(
            decile_monotonicity >= acceptance.min_decile_monotonicity
        ),
        "rank_ic": verdict(rank_ic >= acceptance.min_rank_ic),
        "calibration_error": verdict(
            abs(calibration_error) <= acceptance.max_abs_calibration_error
        ),
    }
    if top_decile_loss_ratio is None:
        verdicts["top_decile_loss_ratio"] = _INSUFFICIENT
    else:
        verdicts["top_decile_loss_ratio"] = verdict(
            top_decile_loss_ratio <= acceptance.max_top_decile_loss_ratio
        )
    return verdicts


def cost_adjusted_metrics(
    observations: list[Observation],
    target_moic: float,
    horizon_years: float,
    model_horizon_years: float,
    decile_count: int = 10,
) -> dict | None:
    """D-5:各観測の実現リターンから往復コスト(`cost_bps`)を引いた後の主要KPI。

    コスト前の `BacktestMetrics` とは**別立て**で返す(片方だけ出すと取り違える)。
    観測に `cost_bps` が1つも無ければ None。
    """
    if not any(o.cost_bps for o in observations):
        return None
    net = [
        replace(o, realized_return=o.realized_return - o.cost_bps / 10_000)
        for o in observations
    ]
    m = compute_metrics(net, target_moic, horizon_years, model_horizon_years, decile_count)
    return {
        "lift_ratio": m.lift_ratio,
        "lift_ratio_worst_date": m.lift_ratio_worst_date,
        "decile_monotonicity": m.decile_monotonicity,
        "rank_ic": m.rank_ic,
        "universe_on_pace_rate": m.universe_on_pace_rate,
        "universe_loss_rate": m.universe_loss_rate,
        "top_decile_loss_rate": m.top_decile_loss_rate,
        "universe_median_return": m.universe_median_return,
    }


def _cross_sectional_buckets(
    observations: list[Observation], bucket_count: int
) -> list[list[Observation]]:
    """**評価日ごとに**デシルへ切ってから、同じデシル番号どうしを束ねる。

    **なぜ日ごとに切るのか(2026-08-26修正)**。以前は全評価日の観測を1つの
    プールにして確率の降順に並べ、そのまま10等分していた。だがモデルが出す確率の
    **水準**は評価日によって動く(断面統計・σ の縮小中心・ナウキャストの基準線が
    その日のユニバースから作られるため)。したがってプールしたまま切ると、
    上位デシルが「モデルが有望と見た銘柄」ではなく「たまたま確率の水準が高かった
    評価日の銘柄」で占められる。実測データでは評価日ごとのユニバース・オンペース率
    が 16.0% 〜 35.1% と2倍以上ばらついており、この混入は無視できない
    (実際、プール方式の `lift_ratio` 1.32 に対し、評価日ごとのリフトの平均は
    1.48 で、日をまたいだ構成の偏りがKPIを押し下げていた)。

    14.2 のデシル単調性・リフト倍率は「**同じ日に並んだ銘柄の中で**上位ほど
    リターンが高いか」を測る指標である。断面ごとにポートフォリオを組んでから
    束ねるのは、断面資産価格の標準的な手順でもある。

    観測が `bucket_count` に満たない評価日は、その日だけ1観測=1バケットになる
    (`_split_into_buckets` の既存の挙動をそのまま使う)。
    """
    by_date: dict[str, list[Observation]] = {}
    for observation in observations:
        by_date.setdefault(observation.base_date, []).append(observation)

    buckets: list[list[Observation]] = [[] for _ in range(bucket_count)]
    for base_date in sorted(by_date):
        # E-8(2026-08-27、defect_audit_2026-08-27.md):確率が完全同値の観測は、
        # 安定ソートだと入力(=dictの挿入)順でデシル所属が決まり、実行間で
        # 非決定的になりうる。成長率上限・粗利率フロア等のクランプで入力が
        # 実質同一になる銘柄群は起こりうるため、ticker_id を決定的な第二キーに
        # 使ってタイを解消する(確率降順・同値は ticker_id 昇順)。
        ordered = sorted(by_date[base_date], key=lambda o: (-o.probability, o.ticker_id))
        for index, bucket in enumerate(_split_into_buckets(ordered, bucket_count)):
            buckets[min(index, bucket_count - 1)].extend(bucket)
    return buckets


def _split_into_buckets(ordered: list[Observation], bucket_count: int) -> list[list[Observation]]:
    """順序を保ったまま、できるだけ均等な `bucket_count` 個の塊に分ける。"""
    total = len(ordered)
    if total < bucket_count:
        return [[o] for o in ordered]
    base, remainder = divmod(total, bucket_count)
    buckets: list[list[Observation]] = []
    start = 0
    for i in range(bucket_count):
        size = base + (1 if i < remainder else 0)
        buckets.append(ordered[start : start + size])
        start += size
    return buckets
