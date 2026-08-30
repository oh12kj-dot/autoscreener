"""前方検証ジョブ(14.3)。

`scores` の各行に対し、各ホライズン後の実現リターンを後追いで `forward_returns`
に記録する。擬似バックテスト(`backtest/`)がモデルの較正を担うのに対し、
こちらは**実際に運用したスコアの実績**を積み上げる。両者は補完関係にある
(前者は過去の再構成、後者は先読みの余地が原理的に無い記録)。

**基準価格の定義**(14.1):スコア確定日の翌営業日始値。当日終値ではなく
翌営業日始値を使うのは、スコアが確定してから実際に売買できるタイミングを
反映するため。

**上場廃止の決済(27.11)**:このジョブの最大の欠陥だった生存バイアスを修正した。
詳細は `_settle_delisted` のdocstringを参照。
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy.orm import Session

from autoscreener.dates import utc_today
from autoscreener.db.models import ForwardReturn, PriceSnapshot, Score, Ticker
from autoscreener.db.session import session_scope

logger = logging.getLogger(__name__)

# (ホライズンラベル, 日数)。厳密な暦月/暦年ではなく概算日数を使う
# (14.1で定義したホライズンの近似。取引可能な直近営業日に合わせて丸める)。
# 7Yは14.1の「10バガーの評価ホライズン=7年」そのものであり、これが最終的な
# 判定になる。それ以前のホライズンは中間指標(14.1の「部分達成の扱い」)。
HORIZONS: list[tuple[str, int]] = [
    ("1M", 30),
    ("3M", 91),
    ("6M", 182),
    ("1Y", 365),
    ("3Y", 1095),
    ("5Y", 1825),
    ("7Y", 2557),
]

# これより短いホライズンは存在しないため、スコア確定からこの日数未満の
# 行は毎回の実行で無駄にチェックしない
_MIN_HORIZON_DAYS = min(days for _, days in HORIZONS)

# 基準価格・目標日価格を探すときに許容する最大の後ろ倒し日数。
# 無制限に「最初に見つかった行」を採ると、収集が数ヶ月止まっていた銘柄で
# まったく別の時期の価格を採用してしまい、検証資産(14.3)そのものが汚染される。
_MAX_ENTRY_LOOKAHEAD_DAYS = 7
_MAX_EXIT_LOOKAHEAD_DAYS = 10

SETTLEMENT_MARKET = "market"
SETTLEMENT_DELISTED = "delisted"


def _entry_price(session: Session, ticker_id: int, score_date: datetime.date) -> float | None:
    """スコア確定日の翌営業日始値(14.1)。

    `_MAX_ENTRY_LOOKAHEAD_DAYS` を超えて先の行しか無い場合は「翌営業日の
    価格が観測できていない」とみなしてNoneを返す。"""
    row = (
        session.query(PriceSnapshot)
        .filter(
            PriceSnapshot.ticker_id == ticker_id,
            PriceSnapshot.trade_date > score_date,
            PriceSnapshot.trade_date <= score_date + datetime.timedelta(days=_MAX_ENTRY_LOOKAHEAD_DAYS),
        )
        .order_by(PriceSnapshot.trade_date.asc())
        .first()
    )
    return float(row.open) if row is not None and row.open is not None else None


def _exit_price(session: Session, ticker_id: int, target_date: datetime.date) -> float | None:
    """目標日以降で最初に観測できる終値(`_MAX_EXIT_LOOKAHEAD_DAYS` 以内)。"""
    row = (
        session.query(PriceSnapshot)
        .filter(
            PriceSnapshot.ticker_id == ticker_id,
            PriceSnapshot.trade_date >= target_date,
            PriceSnapshot.trade_date <= target_date + datetime.timedelta(days=_MAX_EXIT_LOOKAHEAD_DAYS),
        )
        .order_by(PriceSnapshot.trade_date.asc())
        .first()
    )
    return float(row.close) if row is not None and row.close is not None else None


def _last_observed_close(
    session: Session, ticker_id: int, after: datetime.date, before: datetime.date
) -> float | None:
    """`after` より後・`before` 以前で最後に観測された終値。"""
    row = (
        session.query(PriceSnapshot)
        .filter(
            PriceSnapshot.ticker_id == ticker_id,
            PriceSnapshot.trade_date > after,
            PriceSnapshot.trade_date <= before,
            PriceSnapshot.close.isnot(None),
        )
        .order_by(PriceSnapshot.trade_date.desc())
        .first()
    )
    return float(row.close) if row is not None else None


def _is_delisted(ticker: Ticker, last_price_date: datetime.date | None, target_date: datetime.date) -> bool:
    """この銘柄が目標日時点で既に取引を終えていたとみなせるか。

    `delisted_at` は `collect_one` が `PermanentFailure`(yfinanceが恒久的に
    その銘柄を返さなくなった)を捕まえたときに立つ、最も直接的な証拠。

    それに加えて「隔離済み(`is_quarantined`)かつ目標日より十分前で価格観測が
    途切れている」場合も廃止扱いにする。隔離は一過性の失敗の連続でも起こるため
    単独では根拠にならないが、**価格が数週間以上更新されていない**という事実が
    伴えば、実質的に取引が終わっていると判断してよい。
    """
    if ticker.delisted_at is not None:
        return True
    if not ticker.is_quarantined:
        return False
    if last_price_date is None:
        return True
    return last_price_date < target_date - datetime.timedelta(days=_MAX_EXIT_LOOKAHEAD_DAYS)


def _settle_delisted(
    session: Session,
    ticker_id: int,
    entry_price: float,
    score_date: datetime.date,
    target_date: datetime.date,
) -> float:
    """上場廃止銘柄の実現リターンを最終観測価格で確定する。

    **なぜ必要か(27.11)**:以前は上場廃止銘柄の `_exit_price` が None になり、
    `missing_price` として数えるだけで**行を1件も書かなかった**。上場廃止は
    −90%〜−100% という最悪の結果と強く相関するため、検証データから
    「負けの極端値だけ」が系統的に消えていた。14.2のKPI(デシル単調性・
    リフト倍率)はその汚染されたサンプル上で計算されるので、実態より必ず良く出る。
    14.3が「生存バイアス対策のため廃止銘柄をマスタから削除しない」と定めた意図に、
    実装が追いついていなかった。

    **最終観測価格を使い、−100%を決め打ちしない理由**:上場廃止の原因は破綻
    だけではなく、買収(TOB)による廃止も同数以上ある。前者では最終価格が
    ほぼゼロに落ちており、後者では買収価格まで上がっている。最終観測価格は
    どちらの結末も正しく反映する。価格が1件も残っていない場合に限り、
    全損(−100%)として扱う。
    """
    last_close = _last_observed_close(session, ticker_id, after=score_date, before=target_date)
    if last_close is None:
        return -1.0
    return last_close / entry_price - 1


def run_forward_validation(as_of_date: datetime.date | None = None) -> dict[str, int]:
    as_of_date = as_of_date or utc_today()
    computed = 0
    settled_delisted = 0
    not_matured = 0
    missing_price = 0

    with session_scope() as session:
        # スコア確定から最短ホライズン(1M)にすら達していない行は対象外
        cutoff = as_of_date - datetime.timedelta(days=_MIN_HORIZON_DAYS)
        scores = session.query(Score).filter(Score.score_date <= cutoff).all()

        tickers: dict[int, Ticker] = {}
        last_price_dates: dict[int, datetime.date | None] = {}

        for score in scores:
            ticker_id = score.ticker_id
            if ticker_id not in tickers:
                tickers[ticker_id] = session.get(Ticker, ticker_id)
                last_price_dates[ticker_id] = (
                    session.query(PriceSnapshot.trade_date)
                    .filter(PriceSnapshot.ticker_id == ticker_id)
                    .order_by(PriceSnapshot.trade_date.desc())
                    .limit(1)
                    .scalar()
                )
            ticker = tickers[ticker_id]
            if ticker is None:
                continue

            entry_price = _entry_price(session, ticker_id, score.score_date)
            if entry_price is None or entry_price == 0:
                # 翌営業日の価格が観測できていない。エントリーできていない以上
                # この銘柄・この基準日は検証対象にならない(上場廃止の決済とは
                # 別問題:そもそも建玉が無い)。
                missing_price += 1
                continue

            for horizon_label, days in HORIZONS:
                target_date = score.score_date + datetime.timedelta(days=days)
                if as_of_date < target_date:
                    not_matured += 1
                    continue

                existing = (
                    session.query(ForwardReturn)
                    .filter_by(ticker_id=ticker_id, base_date=score.score_date, horizon=horizon_label)
                    .one_or_none()
                )
                if existing is not None and existing.realized_return is not None:
                    continue  # 算出済み

                exit_price = _exit_price(session, ticker_id, target_date)
                if exit_price is not None:
                    realized_return = exit_price / entry_price - 1
                    settlement = SETTLEMENT_MARKET
                elif _is_delisted(ticker, last_price_dates[ticker_id], target_date):
                    realized_return = _settle_delisted(
                        session, ticker_id, entry_price, score.score_date, target_date
                    )
                    settlement = SETTLEMENT_DELISTED
                    settled_delisted += 1
                else:
                    # 上場は続いているが価格が欠測している(収集失敗など)。
                    # 廃止として決済すると生きている銘柄を誤って損失計上するため、
                    # 未確定のまま次回の実行に委ねる。
                    missing_price += 1
                    continue

                now = datetime.datetime.now(datetime.UTC)
                if existing is None:
                    session.add(
                        ForwardReturn(
                            ticker_id=ticker_id,
                            base_date=score.score_date,
                            horizon=horizon_label,
                            realized_return=realized_return,
                            settlement=settlement,
                            computed_at=now,
                        )
                    )
                else:
                    existing.realized_return = realized_return
                    existing.settlement = settlement
                    existing.computed_at = now
                computed += 1

    return {
        "computed": computed,
        "settled_delisted": settled_delisted,
        "not_matured": not_matured,
        "missing_price": missing_price,
    }
