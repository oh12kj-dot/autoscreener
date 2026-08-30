"""除外ゲート判定ロジック(15.2 Step 1、15.6)。

すべて純粋関数として実装する(DBアクセスを持たない)。呼び出し元
(`batch/apply_gates.py`)が raw_snapshots・price_snapshots から値を集めて
`GateInput` を組み立て、ここに渡す。

**フェーズ2時点でのスコープ**:15.2にある7ゲートのうち、データカバレッジゲート
(重み70%未満で除外)は、実際のサブスコア構成(7章・15.2改訂表)がフェーズ3
(スコアリングエンジン)まで存在しないため、ここでは実装しない。「どのサブ
スコアが算出可能か」を判定できるのはスコアリングロジックそのものであり、
先取りして近似値を作ると誤った確信を与えるため、明示的にフェーズ3へ委譲する。

**Altman Z''-Scoreはハードゲートにしない(v1.3後半の実データ検証で判明)**:
Z''スコアの利益剰余金/総資産の項は、R&D投資を続ける赤字グロース株を
「破綻寸前」と誤判定する(製造業向けに設計された指標のバイアス)。実データで
Z''<1.1のハードゲートを適用したところ、収集済み370銘柄中126銘柄(34%)が
除外され、その大半は臨床段階バイオテック等の正常な赤字成長企業だった。
同様の理由で「EBIT/支払利息 < 1」のインタレストカバレッジ・ゲートも検討したが、
EBITが分子である以上同じバイアスを再現する(実データでAxsome Therapeutics
(AXSM)のような成長後に成功した銘柄を誤って弾くことを確認)ため採用しなかった。
Z''-Score自体はフェーズ3のスコアリング(財務健全性サブスコア、程度評価として)
で使う。破綻リスクの直接的なハードゲートは、R&Dバイアスを受けない
`negative_equity`(自己資本マイナス=債務超過)と`cash_runway_floor`
(資金枯渇)の2つが担う。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from autoscreener.config import UniverseConfig

# 15.2で定めた閾値のうち、config/universe.yaml化していないもの
# (時価総額・売上高・株価・流動性・セクターはUniverseConfig側で外出し済み)
DILUTION_3Y_CAGR_CEILING = 0.25
CASH_RUNWAY_FLOOR_QUARTERS = 6.0

# 希薄化CAGRの年率換算に十分な観測期間とみなす最低経過年数。これより短い窓での
# 年率換算は誤差が大きすぎるため判定不能として扱う(呼び出し元の
# apply_gates.py・scoring/engine.py で個別に定義していた同じ値をここへ集約した)。
MIN_DILUTION_WINDOW_YEARS = 2.0


def latest_period_value(field_series: dict[str, float | None] | None) -> float | None:
    """{"YYYY-MM-DD": value, ...} 形式の時系列から最新の非Noneの値を返す。

    キーの辞書順(=日付文字列の昇順)は日付の時系列順と一致するため、
    ソートして末尾から検索すれば良い。
    """
    if not field_series:
        return None
    for date_key in sorted(field_series.keys(), reverse=True):
        value = field_series[date_key]
        if value is not None:
            return value
    return None


def parse_period_series(field_series: dict[str, float | None] | None) -> list[tuple[datetime.date, float]]:
    """{"YYYY-MM-DD": value, ...} を、非None値のみの (日付, 値) 昇順リストにする。

    パースできないキーはスキップする(`collectors.yfinance_client._df_to_json` は
    列がTimestampでない場合に `str(col)` へフォールバックするため、想定外の
    キーが payload に混ざりうる。1銘柄の異常キーでバッチ全体を止めない)。"""
    if not field_series:
        return []
    points: list[tuple[datetime.date, float]] = []
    for key, value in field_series.items():
        if value is None:
            continue
        try:
            year, month, day = key.split("-")
            parsed = datetime.date(int(year), int(month), int(day))
        except (ValueError, AttributeError):
            continue
        points.append((parsed, value))
    return sorted(points, key=lambda p: p[0])


def normalize_financial_currency_value(value: float | None, info: dict) -> float | None:
    """`info`由来の決算数値(financialCurrency建て)を、株価・時価総額と同じ
    取引通貨(currency)建てに換算する(13.5)。

    実データ検証(2026-08-24、HMY等)で判明:yfinanceは`currency`(株価建て通貨)と
    `financialCurrency`(決算報告通貨)が異なるADR等で、totalRevenue等の決算数値を
    financialCurrency建てのまま返す。これをUSD建てのゲート閾値やmarketCap建ての
    EVと無変換で比較・合成すると、桁が数倍〜数十倍狂う(collectors.yfinance_client.
    fetch_raw_financialsが換算レートを`info["_fx_rate_financial_to_trading"]`に
    添付している前提)。

    両通貨が同じ、またはいずれか欠損の場合はvalueをそのまま返す(不一致を検知
    できないケースは従来通りの挙動を維持し、既存の大多数の銘柄に影響しない)。
    換算レート自体が取得できていない場合はNone(判定不能)を返し、既存の
    「欠損は除外しない」ポリシーに委ねる。
    """
    if value is None:
        return None
    currency = info.get("currency")
    financial_currency = info.get("financialCurrency")
    if not currency or not financial_currency or currency == financial_currency:
        return value
    fx_rate = info.get("_fx_rate_financial_to_trading")
    if fx_rate is None:
        return None
    return value * fx_rate


def compute_altman_z(balance_sheet: dict, income_stmt: dict) -> float | None:
    """Altman Z''-Score(非製造業向け版、15.4-5)。

    6.56*(WC/TA) + 3.26*(RE/TA) + 6.72*(EBIT/TA) + 1.05*(Equity/TotalLiabilities)

    いずれかの入力が欠損している場合は None を返す(=判定不能。除外しない)。

    **ハードゲートには使わない**(モジュールdocstring参照)。フェーズ3の
    スコアリングで財務健全性サブスコアの程度評価として使う想定で残している。
    """
    total_assets = latest_period_value(balance_sheet.get("Total Assets"))
    working_capital = latest_period_value(balance_sheet.get("Working Capital"))
    retained_earnings = latest_period_value(balance_sheet.get("Retained Earnings"))
    stockholders_equity = latest_period_value(balance_sheet.get("Stockholders Equity"))
    total_liabilities = latest_period_value(balance_sheet.get("Total Liabilities Net Minority Interest"))
    ebit = latest_period_value(income_stmt.get("EBIT"))

    if total_assets is None or total_assets == 0 or total_liabilities is None or total_liabilities == 0:
        return None
    if any(v is None for v in (working_capital, retained_earnings, ebit, stockholders_equity)):
        return None

    return (
        6.56 * (working_capital / total_assets)
        + 3.26 * (retained_earnings / total_assets)
        + 6.72 * (ebit / total_assets)
        + 1.05 * (stockholders_equity / total_liabilities)
    )


def compute_cash_runway_quarters(total_cash: float | None, quarterly_cash_flow: dict) -> float | None:
    """キャッシュランウェイ(15.2:直近4四半期の平均FCFバーン基準)。

    FCFが黒字(バーンなし)の場合は無限大のランウェイとして扱い、常にこの
    ゲートを通過させる(``float("inf")``)。データ欠損時は None(判定不能)を返す。
    """
    fcf_series = quarterly_cash_flow.get("Free Cash Flow")
    if total_cash is None or not fcf_series:
        return None

    # 直近4期を切り出してからNoneを捨てるのではなく、「非Noneの直近4期」を取る。
    # 前者だと最新4期がすべてNone(yfinance側の欠測)というだけで、その手前に
    # 使える四半期があってもランウェイ判定不能になってしまう。
    recent_values = [
        value
        for value in (fcf_series[date_key] for date_key in sorted(fcf_series.keys(), reverse=True))
        if value is not None
    ][:4]
    if not recent_values:
        return None

    avg_fcf = sum(recent_values) / len(recent_values)
    if avg_fcf >= 0:
        return float("inf")

    burn_per_quarter = -avg_fcf
    return total_cash / burn_per_quarter


def count_available_quarters(quarterly_income_stmt: dict) -> int:
    """`quarterly_income_stmt`の"Total Revenue"行のうち、非Noneの期数を数える。

    4章・6.2で「上場後最低4四半期分の決算データがあること」を要件としていたが、
    `min_listed_quarters`設定値も`Ticker.listed_date`列も実装のどこからも
    参照されておらず、この要件が未実装のまま残っていたことが実データレビューで
    判明した(24.6)。上場日そのものではなく実際に取得できた決算データの期数を
    見るのは、13.1で判明した「上場期間に関わらずyfinance側の開示期数がばらつく」
    という実態により忠実である。
    """
    revenue_series = quarterly_income_stmt.get("Total Revenue") or {}
    return sum(1 for v in revenue_series.values() if v is not None)


def compute_dilution_cagr(
    shares_outstanding_start: float | None,
    shares_outstanding_end: float | None,
    elapsed_years: float,
) -> float | None:
    """分割調整後の発行済株式数の年率CAGR(13.4・15.2)。

    **2026-08-24修正**:以前は指数を`1/3`に固定していたが、実際に渡される
    観測期間は「price_snapshotsに存在する最古〜最新」であり3年とは限らない
    (バックフィルは3年分だが、日次収集が続けば窓は4年・5年と伸びていく)。
    固定の3年で年率換算すると、窓が伸びるほど希薄化率を過大評価し、
    `DILUTION_3Y_CAGR_CEILING`ゲートで正常な銘柄を誤除外する。実測の経過年数で
    年率換算する。

    `elapsed_years` が `MIN_DILUTION_WINDOW_YEARS` 未満の場合は、短すぎる窓の
    年率換算が不正確になるため判定不能(None)とする。
    """
    if shares_outstanding_start is None or shares_outstanding_end is None:
        return None
    if shares_outstanding_start <= 0 or shares_outstanding_end <= 0:
        return None
    if elapsed_years < MIN_DILUTION_WINDOW_YEARS:
        return None
    return (shares_outstanding_end / shares_outstanding_start) ** (1 / elapsed_years) - 1


def dilution_cagr_with_window(
    share_observations: list[tuple[datetime.date, float | None]],
) -> tuple[float | None, float]:
    """(希薄化CAGR, 実測窓の年数)を返す。観測2点未満・窓が短すぎれば (None, 窓)。

    `get_shares_full` の観測は不定期であり、価格系列の先頭側は株式数がNoneに
    なることが多い(ffillしても最初の観測より前は埋まらない)。非Noneの観測点
    だけを使い、その実測区間で年率換算する。

    窓の長さを返すのは、呼び出し元(`dilution_cagr`)が複数のデータ源から
    **最も長い実測窓を持つものを選ぶ**ために必要だから(下記参照)。
    """
    observed = [(d, s) for d, s in share_observations if s is not None and s > 0]
    if len(observed) < 2:
        return None, 0.0
    (start_date, start_shares), (end_date, end_shares) = observed[0], observed[-1]
    elapsed_years = (end_date - start_date).days / 365.25
    if elapsed_years < MIN_DILUTION_WINDOW_YEARS:
        return None, elapsed_years
    return compute_dilution_cagr(start_shares, end_shares, elapsed_years), elapsed_years


# 年次(貸借対照表の報告値)と日次(分割調整済み)の株式数が「同じ単位で並んで
# いるか」を確かめるための突き合わせ窓。期末に**最も近い**日次観測を使う。
# 窓を狭くするほど、期末から観測日までに実際に発行された株式による drift が減る。
_ANNUAL_SHARE_MATCH_WINDOW_DAYS = 20
# 比が期ごとにこの倍率以上ばらついたら、単位が揃っていないと判定する。
#
# **実際の増資は両系列に同じように効くので比は動かない。** 比が動くのは
# 片方だけが調整されているとき(=株式分割・併合)に限られる。
#
# **2.0 という水準の根拠**:分割・併合の倍率は実務上どんなに小さくても2倍
# (1:2併合 / 2:1分割)であり、それより細かい比率は存在しない。一方、期末から
# 20日以内の観測との比が銘柄内で2倍動くほどの増資は、**同じ20日窓の中で
# 2回、しかも逆向きに**起きる必要があり現実的でない。
#
# **1.25 では狭すぎた(2026-08-26、実装中に実データで検出)**:急速に希薄化して
# いる銘柄(FLOC:2025-01の22.0M株から2026-08の44.0M株へ倍増)は、期末と観測日の
# あいだの実際の増資だけで比が1.38まで動く。1.25で切ると**この銘柄の年率+9.8%の
# 希薄化が「測れない」に落ち、A-1の中立補完を経て総合4位まで浮上した**——
# 検知装置が、まさに検知したかったもの(急速な希薄化)を消していた。
# 実データの分布(5,287銘柄、±20日窓)は 1.1超が547件、1.5超が210件、
# 2.0超が134件、3.0超が95件と連続的で、1.5以下の領域には実際の増資が混ざる。
ANNUAL_SHARE_RATIO_SPREAD_LIMIT = 2.0
# 突き合わせ点が1つしか無いときの緩い絶対判定。デュアルクラス銘柄では
# `Ordinary Shares Number`(1クラス)と `get_shares_full`(合算)が定常的に
# 1.5倍程度ずれるため、そこを誤検知しない幅に取る。
_ANNUAL_SHARE_RATIO_ABSOLUTE_LIMITS = (0.25, 4.0)


def annual_share_series_is_comparable(
    annual_observations: list[tuple[datetime.date, float]],
    daily_observations: list[tuple[datetime.date, float | None]],
) -> bool:
    """年次の株式数系列を、日次の系列と同じ単位として扱ってよいか(13.4)。

    **2026-08-26に発見した欠陥。** `price_snapshots.shares_outstanding` は
    13.4の分割調整を通って**現在の株式単位**に揃えてあるが、貸借対照表の
    `Ordinary Shares Number` は yfinance が返す**報告値そのまま**であり、
    分割・併合を挟むと単位が変わる。`dilution_cagr` と
    `point_in_time.build_moic_inputs` はこの2系列を無条件に混ぜていた。

    実データでは 5,287銘柄中 **134銘柄(2.5%)** で両系列の比が期ごとに2倍以上
    動いていた。株式併合を挟んだ銘柄では年次系列が「株式数が99%減った」ように
    見え、**希薄化CAGRが自社株買い扱い(下限 −5%)の無償加点**になる。逆方向の
    分割では「年率+700%の希薄化」となり、`dilution_ceiling` ゲートで
    **正常な銘柄が誤除外**される。

    **直すのではなく、使わない。** 分割倍率そのものは payload に無いため、
    ここで復元することはできない。単位が揃っていないと判定したら年次系列を
    捨て、日次系列だけを使う(足りなければ「希薄化は測れない」= A-1 の
    断面中央値による中立補完へ回す)。誤った希薄化率を使うより、測れないと
    認めるほうが正しい(27.1の欠損方針と同じ)。
    """
    if not annual_observations:
        return False
    daily = sorted((d, float(v)) for d, v in daily_observations if v is not None and v > 0)
    ratios: list[float] = []
    for period_end, reported in annual_observations:
        if reported is None or reported <= 0:
            continue
        # 期末に最も近い日次観測を使う(前後どちらでもよい)。
        candidates = [
            (abs((observed_on - period_end).days), value)
            for observed_on, value in daily
            if abs((observed_on - period_end).days) <= _ANNUAL_SHARE_MATCH_WINDOW_DAYS
        ]
        if candidates:
            ratios.append(min(candidates)[1] / reported)
    if not ratios:
        # 日次系列と重なる期が無い(価格ヒストリーより古い決算しか無い等)。
        # 検証できないので従来どおり使う——ここで捨てると、上場から日が浅い
        # 銘柄の希薄化が一律に測れなくなる副作用のほうが大きい。
        return True
    if len(ratios) == 1:
        low, high = _ANNUAL_SHARE_RATIO_ABSOLUTE_LIMITS
        return low <= ratios[0] <= high
    return max(ratios) / min(ratios) <= ANNUAL_SHARE_RATIO_SPREAD_LIMIT


def dilution_cagr(
    share_observations: list[tuple[datetime.date, float | None]],
    balance_sheet: dict | None = None,
) -> float | None:
    """希薄化CAGR。price_snapshots由来の日次観測と、balance_sheetの
    `Ordinary Shares Number`(年次)の**両方から算出し、実測窓が長いほうを採る**。

    **なぜ「長いほう」なのか**(2026-08-25、27.9で判明):以前は日次観測から
    算出できた時点でそれを採用し、年次系列へは「日次で算出できなかったときだけ」
    フォールバックしていた。ところが日次観測の窓は `price_snapshots` の
    バックフィル開始日(3年前)に縛られるのに対し、年次系列は最大5期=約4年分ある。

    実データで、この優先順位が総合1位の銘柄を誤評価していた。ACTG
    (Acacia Research)は2022年末の43.5M株から2023年末に99.9M株へ**1年で
    株式数が倍増**(増資で事業を買収)しているが、価格ヒストリーの開始が
    2023-08-22 であり、その時点では既に増資後だった。結果、日次観測から測った
    希薄化はほぼ0%となり、「1株価値の保全」が高評価のまま総合1位に付いていた。
    年次系列を使えば年率+30%の希薄化が正しく検出される。

    15.1④は希薄化を「単独で最大の改善余地」としており、その軸が最も重要な
    銘柄で丸ごと欠測していたことになる。長い窓を優先すれば、増資イベントが
    観測区間の外に落ちる確率が構造的に下がる。
    """
    from_prices, price_window = dilution_cagr_with_window(share_observations)

    annual_observations: list[tuple[datetime.date, float | None]] = []
    if balance_sheet:
        annual_observations = [(d, v) for d, v in parse_period_series(balance_sheet.get("Ordinary Shares Number"))]
    # 13.4:分割調整の単位が日次系列と揃っていない年次系列は使わない
    # (`annual_share_series_is_comparable` 参照)。
    if annual_observations and not annual_share_series_is_comparable(
        [(d, v) for d, v in annual_observations if v is not None], share_observations
    ):
        annual_observations = []
    from_annual, annual_window = dilution_cagr_with_window(annual_observations)

    if from_annual is not None and annual_window > price_window:
        return from_annual
    if from_prices is not None:
        return from_prices
    return from_annual


@dataclass(frozen=True)
class GateInput:
    market_cap: float | None
    total_revenue: float | None
    price: float | None
    sector: str | None
    median_daily_dollar_volume: float | None
    dilution_3y_cagr: float | None
    stockholders_equity: float | None
    cash_runway_quarters: float | None
    available_quarters: int


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)


def evaluate_gates(gi: GateInput, universe_config: UniverseConfig) -> GateResult:
    """15.2のゲートを評価する。

    値が None(算出不能)の項目は、基準超過が明確に確認できないという理由で
    除外しない(6.2の base filter は market_cap/sector/price の欠損のみを
    最低限の計算可能性ゲートとして扱う。希薄化・自己資本・ランウェイの欠損は
    このゲートでは減点材料にせず、7章スコアリング側のカバレッジ調整に委ねる)。
    """
    reasons: list[str] = []

    if gi.market_cap is None:
        reasons.append("missing_market_cap")
    elif gi.market_cap >= universe_config.market_cap_ceiling_usd:
        reasons.append("market_cap_ceiling")

    if gi.total_revenue is None:
        reasons.append("missing_revenue")
    elif gi.total_revenue >= universe_config.revenue_ceiling_usd:
        reasons.append("revenue_ceiling")

    if gi.price is None:
        reasons.append("missing_price")
    elif gi.price < universe_config.min_price_usd:
        reasons.append("price_floor")

    if gi.sector is None:
        reasons.append("missing_sector")
    elif gi.sector in universe_config.excluded_sectors:
        reasons.append("excluded_sector")

    if gi.median_daily_dollar_volume is not None and gi.median_daily_dollar_volume < universe_config.min_daily_dollar_volume_usd:
        reasons.append("liquidity_floor")

    if gi.dilution_3y_cagr is not None and gi.dilution_3y_cagr > DILUTION_3Y_CAGR_CEILING:
        reasons.append("dilution_ceiling")

    # Altman Z''ではなく自己資本(債務超過)を直接見る(モジュールdocstring参照)。
    # 実データで、これはR&Dバイアスを受けずに実質的なレバレッジ過多・
    # 資金枯渇寸前の銘柄(BTMD・BYSI・ESLA・LESL等)を捕捉することを確認済み。
    if gi.stockholders_equity is not None and gi.stockholders_equity < 0:
        reasons.append("negative_equity")

    if gi.cash_runway_quarters is not None and gi.cash_runway_quarters < CASH_RUNWAY_FLOOR_QUARTERS:
        reasons.append("cash_runway_floor")

    # 4章・6.2:上場後最低4四半期分の決算データがあること(24.6で実装漏れが判明)。
    # available_quartersは0でも必ず算出できる値のため、欠損扱いにはしない。
    if gi.available_quarters < universe_config.min_listed_quarters:
        reasons.append("insufficient_listing_history")

    return GateResult(passed=len(reasons) == 0, reasons=reasons)
