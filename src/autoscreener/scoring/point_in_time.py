"""任意の時点における `MoicInputs` の再構成(27.8)。

**このモジュールが本アプリの検証可能性そのものを担う。**

14.3は「yfinanceからポイントインタイムの財務データは取得できない → バックテスト
不可能 → 前方検証を待つしかない」と結論していた。この結論は厳しすぎた:

- 年次の `income_stmt` / `balance_sheet` / `cash_flow` は**期末日をキーとする
  時系列**で返る。ある評価時点 T において「T までに開示済みだった期」だけを
  残せば、近似的なポイントインタイムの財務諸表が構成できる
- 価格・発行済株式数は `price_snapshots` に日次で入っており、T 時点の値を
  そのまま引ける

したがって「T 時点のデータだけでスコアを付け、T 以降の実現リターンで検証する」
という擬似バックテストは**今日実行できる**。7年待つ必要はない。

**この方式の限界(正直に記録する)**:

1. **リステートメント** — yfinanceが返すのは現在時点で修正済みの数値であり、
   T 時点に実際に開示されていた数値とは異なりうる。過去の決算が上方/下方修正
   されていた場合、その修正を先読みしてしまう
2. **開示ラグの近似** — 実際の提出日は取得できないため、期末から
   `REPORTING_LAG_DAYS` 後に開示されたとみなす。早期提出企業では保守的、
   提出遅延企業では楽観的になる
3. **期数の制約** — 年次は最大5期しか取れない(13.1)ため、遡れる評価時点は
   実質的に直近3〜4年に限られる
4. **`info` は使えない** — TTM値・アナリスト予想・機関保有率・インサイダー取引は
   現在時点のスナップショットしか無く、T 時点に遡れない。`moic.py` が
   年次財務諸表と価格だけで閉じているのはこの制約に合わせた設計判断であり、
   **ライブとバックテストで完全に同一のモデルが走る**ことを保証している

限界はあるが、「1件も検証データが無いまま推測値の重みで7年運用する」よりは
桁違いにましである。
"""

from __future__ import annotations

import datetime
import math
import statistics
from dataclasses import dataclass

from autoscreener.scoring.financial_metrics import piotroski_f_score
from autoscreener.scoring.moic import MoicInputs
from autoscreener.screening.exclusion_gates import (
    GateInput,
    annual_share_series_is_comparable,
    compute_cash_runway_quarters,
    dilution_cagr_with_window,
    parse_period_series,
)

# 年次決算が開示されたとみなすまでの期末からの日数。米国の10-Kは会計年度末から
# 60〜90日以内の提出が義務づけられている(区分により異なる)。最も遅い90日を
# 採用し、先読みバイアスを避ける側に倒す。
REPORTING_LAG_DAYS = 90

# 売上CAGRを測る目標年数。年次データが最大5期(13.1)しか無いため、3年を基本と
# しつつ、それに満たない場合は取得できた期間で年率換算する。
_TARGET_CAGR_YEARS = 3

# CAGRの年率換算に必要な最小の実測期間。これより短い窓での年率換算は誤差が
# 大きすぎるため算出不能として扱う。
_MIN_CAGR_YEARS = 1.5


def financial_to_trading_rate(payload: dict) -> float | None:
    """決算数値(`financialCurrency`建て)を株価と同じ取引通貨(`currency`)建てに
    換算するレート。換算不要なら1.0、**必要なのに取れない**なら None。

    **なぜモデル側でも必要か(2026-08-26に判明した欠陥)**:13.5のとおり
    yfinanceはADR等で「株価・時価総額は取引通貨、決算数値は報告通貨」という
    混在した単位で値を返す。除外ゲート(`apply_gates`)は
    `normalize_financial_currency_value` でこれを換算していたが、**MOICモデルの
    入力を組み立てるこのモジュールには同じ処理が無かった**。その結果、

        enterprise_value = market_cap(取引通貨) + net_debt(報告通貨)
        current_multiple = enterprise_value / gross_profit(報告通貨)

    という、単位の違う数を足し引きする式が5,287銘柄中262銘柄(5.0%)で走って
    いた。BRL建てで報告するAFYA(fx=0.194)ではネットデットが実質5.2倍に
    膨らみ、レバレッジ倍率・EV倍率・終端株主価値のすべてが歪んだまま
    **総合17位**に載っていた(2026-08-26のランキングで確認)。

    換算レートが取れない場合に None を返して呼び出し元に「測れない」と
    扱わせるのは、`normalize_financial_currency_value` の「換算不能は判定不能」
    という既存方針と揃えるためである。誤った単位で計算した順位を出すより、
    Tier 2(監視リスト)へ回すほうが正しい。

    **限界**:`_fx_rate_financial_to_trading` は収集時点のスポットレートであり、
    過去時点に遡れない。バックテストでは全評価日に現在のレートを当てることに
    なる(リステートメントの先読みと同種の近似。モジュールdocstring参照)。
    """
    info = payload.get("info") or {}
    currency = info.get("currency")
    financial_currency = info.get("financialCurrency")
    if not currency or not financial_currency or currency == financial_currency:
        return 1.0
    rate = info.get("_fx_rate_financial_to_trading")
    if rate is None or not isinstance(rate, (int, float)) or rate <= 0:
        return None
    return float(rate)


# 現金残高として採用する貸借対照表の行。上にあるものを優先する。
_CASH_ROWS = (
    "Cash Cash Equivalents And Short Term Investments",
    "Cash And Cash Equivalents",
    "Cash Equivalents",
    "Cash Financial",
)


@dataclass(frozen=True)
class PointInTimeStatements:
    """評価時点 `as_of` までに開示済みだった年次財務諸表(期末日の昇順)。"""

    as_of: datetime.date
    income_stmt: dict[str, dict[str, float]]
    balance_sheet: dict[str, dict[str, float]]
    cash_flow: dict[str, dict[str, float]]
    visible_period_ends: list[datetime.date]


def _visible_period_ends(payload: dict, as_of: datetime.date) -> list[datetime.date]:
    """`as_of` 時点で開示済みとみなせる年次期末日の一覧(昇順)。

    売上高(Total Revenue)の期末日を基準にする。売上が無い期は財務諸表として
    実質的に使えないため、他の行だけが存在する期を可視期に数える意味がない。
    """
    cutoff = as_of - datetime.timedelta(days=REPORTING_LAG_DAYS)
    revenue_points = parse_period_series((payload.get("income_stmt") or {}).get("Total Revenue"))
    return [period_end for period_end, _ in revenue_points if period_end <= cutoff]


def _filter_statement(statement: dict | None, visible: set[datetime.date]) -> dict[str, dict[str, float]]:
    """財務諸表の各行から、可視期のみを残す。"""
    if not statement:
        return {}
    filtered: dict[str, dict[str, float]] = {}
    for row_name, series in statement.items():
        if not isinstance(series, dict):
            continue
        kept = {
            date_key: value
            for date_key, value in series.items()
            if value is not None and _parse_key(date_key) in visible
        }
        if kept:
            filtered[row_name] = kept
    return filtered


def _parse_key(date_key: str) -> datetime.date | None:
    try:
        year, month, day = date_key.split("-")
        return datetime.date(int(year), int(month), int(day))
    except (ValueError, AttributeError):
        return None


def build_point_in_time_statements(payload: dict, as_of: datetime.date) -> PointInTimeStatements:
    """`as_of` 時点で開示済みだった年次財務諸表だけを残したビューを作る。"""
    visible = _visible_period_ends(payload, as_of)
    visible_set = set(visible)
    return PointInTimeStatements(
        as_of=as_of,
        income_stmt=_filter_statement(payload.get("income_stmt"), visible_set),
        balance_sheet=_filter_statement(payload.get("balance_sheet"), visible_set),
        cash_flow=_filter_statement(payload.get("cash_flow"), visible_set),
        visible_period_ends=visible,
    )


def _series(statement: dict[str, dict[str, float]], row: str) -> list[tuple[datetime.date, float]]:
    return parse_period_series(statement.get(row))


def _latest(statement: dict[str, dict[str, float]], row: str) -> float | None:
    points = _series(statement, row)
    return points[-1][1] if points else None


def _latest_two(statement: dict[str, dict[str, float]], row: str) -> tuple[float, float] | None:
    points = _series(statement, row)
    if len(points) < 2:
        return None
    return points[-1][1], points[-2][1]


def revenue_cagr(revenue_points: list[tuple[datetime.date, float]], target_years: int) -> float | None:
    """年次売上のCAGR。`target_years` 年前に最も近い期を基準にする。

    実測の経過年数で年率換算する(期末日の間隔は必ずしも整数年ではない)。
    基準期の売上が0以下なら比率が定義できないため None。
    """
    if len(revenue_points) < 2:
        return None
    latest_date, latest_value = revenue_points[-1]
    if latest_value <= 0:
        return None
    target_date = latest_date - datetime.timedelta(days=int(365.25 * target_years))
    candidates = [p for p in revenue_points if p[0] <= target_date] or [revenue_points[0]]
    base_date, base_value = candidates[-1]
    if base_value <= 0:
        return None
    elapsed_years = (latest_date - base_date).days / 365.25
    if elapsed_years < _MIN_CAGR_YEARS:
        return None
    return (latest_value / base_value) ** (1 / elapsed_years) - 1


def revenue_yoy(revenue_points: list[tuple[datetime.date, float]]) -> float | None:
    """直近年次 vs 前年次の売上成長率。

    `info.revenueGrowth`(TTM)は使わない。TTMは過去時点に遡れず、使うと
    ライブとバックテストでモデルが別物になるため(モジュールdocstring)。
    基数効果(前年がほぼゼロ)の守りは `growth.max_initial_rate` の丸めが担う。
    """
    if len(revenue_points) < 2:
        return None
    latest, prior = revenue_points[-1][1], revenue_points[-2][1]
    if prior <= 0:
        return None
    return latest / prior - 1


def revenue_growth_volatility(revenue_points: list[tuple[datetime.date, float]]) -> float | None:
    """過去の年次売上成長率の標準偏差。3期以上の成長率が取れなければ None。

    旧v2の「成長安定性CV」(平均で割った変動係数)と異なり、平均で割らない。
    CVは平均成長率が0近傍の銘柄で発散し、`inf` の特別扱いが必要だった。
    ここでは成長率そのもののばらつきを `moic.log_moic_sigma` へ渡すだけなので、
    平均で正規化する必要がない。
    """
    if len(revenue_points) < 4:
        return None
    rates = [
        current / prior - 1
        for (_, prior), (_, current) in zip(revenue_points, revenue_points[1:])
        if prior > 0
    ]
    if len(rates) < 3:
        return None
    return statistics.pstdev(rates)


def series_trend_consistency(values: list[float]) -> float | None:
    """系列の変化が**一方向に積み上がっているか**を 0〜1 で測る(30.1)。

        consistency = |Σ Δ| / Σ |Δ|

    1 に近いほど単調(構造的な変化)、0 に近いほど上下に振れているだけ
    (循環的な変化)。Kaufman の efficiency ratio と同じ形。

    **なぜこれが投資理論上必要なのか。** モデルは粗利率トレンドと初期成長率を
    「直近の観測をそのまま7年へ引き伸ばす」形で使っている。この推定量は、
    対象が**単調に改善している事業**なら不偏に近いが、**循環している事業**では
    観測時点が循環のどこにあるかで符号ごと変わる。市況が上向いた直後の
    資源会社は「粗利率が急改善し、売上も急増している企業」に見えるが、
    その改善は7年間続く性質のものではない。

    重要なのは、これは**不確実性(σ)ではなく点推定のバイアス**だという点である。
    v4 は循環性を `revenue_growth_volatility` → σ の経路でしか扱っておらず、
    しかも σ は 85% 縮小されているため断面差はほぼ消える(28.4)。つまり
    **現行モデルには循環と構造を区別する経路が実質的に存在しない**。

    3点未満(=変化が2つ未満)では測れないので None を返す。変化が全く無い
    (Σ|Δ| = 0)場合も判定できないため None。
    """
    if len(values) < 3:
        return None
    deltas = [b - a for a, b in zip(values, values[1:])]
    total_abs = sum(abs(d) for d in deltas)
    if total_abs <= 0:
        return None
    return min(1.0, abs(sum(deltas)) / total_abs)


def gross_margin_series(income_stmt: dict[str, dict[str, float]]) -> list[float]:
    """年次の粗利率系列(期末日の昇順)。売上が正の期のみ。"""
    revenue = dict(_series(income_stmt, "Total Revenue"))
    gross_profit = dict(_series(income_stmt, "Gross Profit"))
    out: list[float] = []
    for period in sorted(revenue):
        rev = revenue[period]
        gp = gross_profit.get(period)
        if gp is None or rev is None or rev <= 0:
            continue
        out.append(gp / rev)
    return out


# 28.3:価格ナウキャストが見る窓(年)。1年より短いと決算の陳腐化を埋めきれず、
# 長いとモメンタムの反転(3〜5年)に踏み込む。
_MOMENTUM_WINDOW_YEARS = 1.0
# 実測窓がこの範囲から外れたら算出しない。上場直後(短すぎる)と、価格系列が
# 途切れている銘柄(長すぎる)を弾く。
_MIN_MOMENTUM_YEARS = 0.5
_MAX_MOMENTUM_YEARS = 2.0


def annualized_log_momentum(
    price_observations: list[tuple[datetime.date, float]], as_of: datetime.date
) -> float | None:
    """`as_of` 時点から見た直近12ヶ月の**年率対数リターン**(28.3)。

    `moic.nowcast_initial_growth` の入力。年率にするのは、これを成長率の
    修正量へ換算するときに次元を合わせるためである(マルチプルの成長弾力性 κ は
    「年率成長率が1pt高いとマルチプルが何%高いか」を表す)。

    **実測の経過年数で割る**ので、価格観測が日次でも月次でも結果はほぼ変わらない。
    これは意図的な設計で、ライブ(日次の `price_snapshots` 全行)とバックテスト
    (メモリのために月次サンプリング)で同じ関数を通しても値がぶれないようにする
    ためである。窓が `_MIN_MOMENTUM_YEARS` 〜 `_MAX_MOMENTUM_YEARS` から外れる
    場合は None を返し、ナウキャストは無効化される(補正0)。
    """
    observed = [(d, p) for d, p in price_observations if p is not None and p > 0 and d <= as_of]
    if len(observed) < 2:
        return None
    observed.sort(key=lambda point: point[0])
    latest_date, latest_price = observed[-1]

    target = as_of - datetime.timedelta(days=int(365.25 * _MOMENTUM_WINDOW_YEARS))
    earlier = [point for point in observed if point[0] <= target]
    base_date, base_price = earlier[-1] if earlier else observed[0]

    elapsed_years = (latest_date - base_date).days / 365.25
    if not (_MIN_MOMENTUM_YEARS <= elapsed_years <= _MAX_MOMENTUM_YEARS):
        return None
    return math.log(latest_price / base_price) / elapsed_years


def shares_outstanding_at(
    share_observations: list[tuple[datetime.date, float | None]], as_of: datetime.date
) -> float | None:
    """`as_of` 以前で最後に観測された発行済株式数。"""
    observed = [(d, s) for d, s in share_observations if s is not None and d <= as_of]
    return observed[-1][1] if observed else None


def _cash_balance(balance_sheet: dict[str, dict[str, float]]) -> float | None:
    for row in _CASH_ROWS:
        value = _latest(balance_sheet, row)
        if value is not None:
            return value
    return None


def cash_runway_quarters_annual(
    balance_sheet: dict[str, dict[str, float]], cash_flow: dict[str, dict[str, float]]
) -> float | None:
    """年次FCFベースのキャッシュランウェイ(四半期数)。

    除外ゲート(`exclusion_gates.compute_cash_runway_quarters`)は直近4四半期の
    FCFを使うが、四半期データは最大5期しか無く(13.1)過去時点に遡れない。
    モデル側は年次FCFを4で割ってバーンレートとすることで、ライブと
    バックテストで同じ値が出るようにする。FCFが黒字なら無限大。
    """
    cash = _cash_balance(balance_sheet)
    annual_fcf = _latest(cash_flow, "Free Cash Flow")
    if cash is None or annual_fcf is None:
        return None
    if annual_fcf >= 0:
        return math.inf
    return cash / (-annual_fcf / 4)


def _pit_quarterly_series(
    series: dict | None, as_of: datetime.date
) -> dict[str, float]:
    """四半期系列から、`as_of` 時点で開示済みとみなせる期だけを残す(D-10)。

    実際の提出日は取れないので、期末 + `REPORTING_LAG_DAYS`(年次と同じ90日)を
    可視化の閾値にする——I-1(XBRL の `filed` 日付)が入るまでの近似。
    """
    if not series:
        return {}
    cutoff = as_of - datetime.timedelta(days=REPORTING_LAG_DAYS)
    kept: dict[str, float] = {}
    for key, value in series.items():
        if value is None:
            continue
        period_end = _parse_key(key)
        if period_end is not None and period_end <= cutoff:
            kept[key] = value
    return kept


def build_gate_input(
    payload: dict,
    as_of: datetime.date,
    inputs: MoicInputs,
    price: float,
    median_dollar_volume: float | None,
    min_annual_periods: int,
) -> GateInput:
    """D-10(docs/defect_and_edge_audit_2026-08-28.md):ライブの `evaluate_gates` を
    バックテストでも**そのまま**呼べるように、ポイントインタイム値で `GateInput`
    を組み立てる。

    以前の `runner._passes_point_in_time_gate` はライブと別物のゲートを実装して
    おり、`cash_runway_floor`(6四半期未満で除外)がライブでだけ効いていた——
    σ と health_index が最も効く脆弱・高ボラ銘柄群を、KPIを測った母集団からは
    削らずライブでだけ削っていた。ここで同じゲートを通す。

    **四半期データの限界**:四半期は最大5期しか無く(13.1)PIT で切ると先頭
    付近の評価日で `available_quarters` が 0 になる。年次期数が
    `min_annual_periods` 以上あれば「上場後の期数は足りている」とみなし、
    `available_quarters` を四半期換算(年次×4)で底上げする——「再構成が原理的に
    無理な項目はライブ側でも無効化」という D-10 修正案2の方針。
    """
    statements = build_point_in_time_statements(payload, as_of)
    stockholders_equity = _latest(statements.balance_sheet, "Stockholders Equity")

    quarterly_fcf = _pit_quarterly_series(
        (payload.get("quarterly_cash_flow") or {}).get("Free Cash Flow"), as_of
    )
    cash = _cash_balance(statements.balance_sheet)
    cash_runway = (
        compute_cash_runway_quarters(cash, {"Free Cash Flow": quarterly_fcf})
        if quarterly_fcf
        else cash_runway_quarters_annual(statements.balance_sheet, statements.cash_flow)
    )

    pit_quarters = len(
        _pit_quarterly_series((payload.get("quarterly_income_stmt") or {}).get("Total Revenue"), as_of)
    )
    annual_periods = len(statements.visible_period_ends)
    available_quarters = pit_quarters
    if annual_periods >= min_annual_periods:
        available_quarters = max(available_quarters, annual_periods * 4)

    return GateInput(
        market_cap=inputs.market_cap,
        total_revenue=inputs.revenue_latest,
        price=price,
        sector=inputs.sector,
        median_daily_dollar_volume=median_dollar_volume,
        dilution_3y_cagr=inputs.dilution_cagr,
        stockholders_equity=stockholders_equity,
        cash_runway_quarters=cash_runway,
        available_quarters=available_quarters,
    )


def build_moic_inputs(
    payload: dict,
    share_observations: list[tuple[datetime.date, float | None]],
    price_observations: list[tuple[datetime.date, float]],
    as_of: datetime.date,
    sector: str | None,
) -> MoicInputs | None:
    """`as_of` 時点のデータだけから `MoicInputs` を組み立てる。

    `share_observations` は (日付, 分割調整後の発行済株式数) の昇順リスト。
    `price_snapshots` 由来の日次観測と、貸借対照表の `Ordinary Shares Number`
    (年次)の両方を渡してよい——呼び出し元が結合する。`as_of` より後の観測は
    ここで捨てるので、呼び出し元が事前に切る必要はない。

    `price_observations` は (日付, 分割調整後の終値) の系列。時価総額に使う
    「`as_of` 以前で最後の終値」と、ナウキャスト用の12ヶ月モメンタム(28.3)の
    両方をここから作る。**呼び出し元が終値を1つだけ渡す形にはしない**——
    そうするとライブとバックテストでモメンタムの算出経路が分かれ、
    「同一の関数が走る」という27.8の保証が崩れるため。

    必須入力(売上・粗利・株式数・株価)が欠ければ None を返す。
    """
    # 13.5:決算数値(報告通貨)を株価・時価総額と同じ取引通貨へ揃える。
    # 換算できないなら「測れない」として扱う(`financial_to_trading_rate` 参照)。
    fx = financial_to_trading_rate(payload)
    if fx is None:
        return None

    prices = sorted(
        ((d, float(p)) for d, p in price_observations if p is not None and float(p) > 0),
        key=lambda point: point[0],
    )
    visible_prices = [point for point in prices if point[0] <= as_of]
    price_at_as_of = visible_prices[-1][1] if visible_prices else None
    statements = build_point_in_time_statements(payload, as_of)
    income_stmt = statements.income_stmt
    balance_sheet = statements.balance_sheet
    cash_flow = statements.cash_flow

    revenue_points = _series(income_stmt, "Total Revenue")
    if not revenue_points:
        return None
    revenue_latest = revenue_points[-1][1]
    gross_profit_latest = _latest(income_stmt, "Gross Profit")
    if revenue_latest <= 0 or gross_profit_latest is None:
        return None

    # 貸借対照表由来の年次株式数も観測点に混ぜる。`price_snapshots` の
    # `get_shares_full` 観測は不定期で窓が短くなりがちであり、年次系列のほうが
    # 長い実測窓を持つことが多い(27.9)。
    #
    # ただし混ぜてよいのは**単位が揃っているときだけ**(13.4、2026-08-26に発見)。
    # 日次系列は分割調整済み・年次系列は報告値そのままなので、分割や併合を
    # 挟むと単位が食い違う。実データで5,287銘柄中219銘柄が該当した。
    # 揃っていない銘柄では年次系列を捨て、日次系列だけで測る。
    annual_shares = [(d, v) for d, v in _series(balance_sheet, "Ordinary Shares Number")]
    visible_daily_shares = [(d, s) for d, s in share_observations if s is not None and d <= as_of]
    if annual_shares and not annual_share_series_is_comparable(annual_shares, visible_daily_shares):
        annual_shares = []
    combined_shares = sorted(visible_daily_shares + annual_shares, key=lambda p: p[0])
    shares = shares_outstanding_at(combined_shares, as_of)
    if shares is None or shares <= 0 or price_at_as_of is None or price_at_as_of <= 0:
        return None

    market_cap = price_at_as_of * shares

    # E-1(2026-08-27、docs/defect_audit_2026-08-27.md):`Total Debt` / 現金の行が
    # 貸借対照表ペイロードに**存在しない**場合、`_latest` / `_cash_balance` は
    # None を返す。`or 0.0` はこの「測れない」を無条件に「有利子負債ゼロ」へ
    # 読み替えており、A-1(希薄化の欠損を「希薄化ゼロ」に読み替えていた欠陥)と
    # 全く同型のバグである。まず挙動は変えず(実データでの発生頻度調査と
    # `run-backtest` でのKPI確認が済むまで)、欠損の事実を診断フラグとして
    # `MoicInputs` → `MoicResult` → API の警告バッジ経路まで通す(S-5段階1と同じ手順)。
    total_debt_raw = _latest(balance_sheet, "Total Debt")
    cash_raw = _cash_balance(balance_sheet)
    net_debt_data_missing = total_debt_raw is None or cash_raw is None
    total_debt = total_debt_raw or 0.0
    cash = cash_raw or 0.0
    net_debt = (total_debt - cash) * fx
    # S-5診断用(2026-08-26、docs/model_audit_v4_2026-08-26.md):`Total Debt` は
    # ASC842以降のオペレーティングリース債務(`Capital Lease Obligations`)を
    # 含む。店舗網を持つ企業(DBI等)ではこれが net_debt の大半を占め、
    # leverage_effect を金融負債と同列に扱ってしまう。ランキングの計算式は
    # 変えず、UIの警告表示のための比率のみをここで持ち回る。
    lease_liability = _latest(balance_sheet, "Capital Lease Obligations")
    if lease_liability is not None:
        lease_liability *= fx

    gross_margin_latest = gross_profit_latest / revenue_latest
    gross_margin_prior: float | None = None
    revenue_two = _latest_two(income_stmt, "Total Revenue")
    gross_profit_two = _latest_two(income_stmt, "Gross Profit")
    if revenue_two and gross_profit_two and revenue_two[1] > 0:
        gross_margin_prior = gross_profit_two[1] / revenue_two[1]

    # 希薄化は、日次観測と年次系列のうち**実測窓が長いほうを採用する**(27.9)。
    # 日次観測を無条件に優先していた旧実装では、価格ヒストリーの開始が
    # 大規模増資の後だった銘柄(ACTG:2023年に株式数が43.5M→99.9Mへ倍増)で
    # 希薄化ゼロと誤測定され、総合1位に押し上げられていた。
    daily_dilution, daily_years = dilution_cagr_with_window(
        [(d, s) for d, s in share_observations if d <= as_of]
    )
    # `annual_shares` は上で単位検証済み(揃っていなければ空になっている)。
    annual_dilution, annual_years = dilution_cagr_with_window(annual_shares)
    if annual_dilution is not None and annual_years > daily_years:
        dilution = annual_dilution
    elif daily_dilution is not None:
        dilution = daily_dilution
    else:
        dilution = annual_dilution

    piotroski = piotroski_f_score(balance_sheet, income_stmt, cash_flow)

    total_assets = _latest(balance_sheet, "Total Assets")
    stockholders_equity = _latest(balance_sheet, "Stockholders Equity")
    equity_to_assets = (
        stockholders_equity / total_assets if total_assets and total_assets > 0 and stockholders_equity is not None
        else None
    )

    annual_fcf = _latest(cash_flow, "Free Cash Flow")
    fcf_margin = annual_fcf / revenue_latest if annual_fcf is not None else None

    # 30.1:循環と構造の区別。粗利率と売上の年次系列が一方向に積み上がって
    # いるのか、上下に振れているだけなのかを測る(`series_trend_consistency`)。
    # 点推定(終端粗利率・初期成長率)の外挿量を、この一致度で割り引く。
    margins = gross_margin_series(income_stmt)
    gross_margin_consistency = series_trend_consistency(margins)
    revenue_consistency = series_trend_consistency([v for _, v in revenue_points])

    return MoicInputs(
        market_cap=market_cap,
        net_debt=net_debt,
        # 金額そのものを持つ4項目だけが換算対象。ほかの入力(成長率・粗利率・
        # 希薄化・FCFマージン・自己資本比率・ランウェイ)はすべて同一通貨どうしの
        # 比なので、通貨を掛けても値が変わらない。
        revenue_latest=revenue_latest * fx,
        gross_profit_latest=gross_profit_latest * fx,
        revenue_cagr=revenue_cagr(revenue_points, _TARGET_CAGR_YEARS),
        revenue_yoy=revenue_yoy(revenue_points),
        revenue_growth_volatility=revenue_growth_volatility(revenue_points),
        gross_margin_latest=gross_margin_latest,
        gross_margin_prior=gross_margin_prior,
        gross_margin_consistency=gross_margin_consistency,
        revenue_trend_consistency=revenue_consistency,
        dilution_cagr=dilution,
        piotroski_ratio=piotroski.score_ratio,
        cash_runway_quarters=cash_runway_quarters_annual(balance_sheet, cash_flow),
        equity_to_assets=equity_to_assets,
        fcf_margin=fcf_margin,
        sector=sector,
        log_momentum_12m=annualized_log_momentum(visible_prices, as_of),
        lease_liability=lease_liability,
        net_debt_data_missing=net_debt_data_missing,
    )
