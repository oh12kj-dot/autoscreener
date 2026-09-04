"""銘柄単位のオーケストレーション:取得 → 検証 → DB保存 → 隔離リスト管理(18.1)。"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from autoscreener.collectors.errors import (
    CollectionError,
    EmptyResponseError,
    ParseFailure,
    PermanentFailure,
    TransientFailure,
    collection_error_detail,
)
from autoscreener.collectors.yfinance_client import (
    fetch_isin,
    fetch_latest_price,
    fetch_raw_financials,
)
from autoscreener.config import CollectionConfig
from autoscreener.dates import WEEKLY_REFRESH_WEEKDAY
from autoscreener.db.models import CollectionLog, EventCalendar, Filing, PriceSnapshot, RawSnapshot, Ticker, TickerAlias
from autoscreener.validation.rules import detect_day_over_day_spike, validate_info

logger = logging.getLogger(__name__)

# S-2(daily_pipeline_throughput_plan_2026-09-04):`fetch_raw_financials`が
# `include_statements=True`のときだけ返す財務諸表5キー。`apply_gates.py`
# (`balance_sheet`・`quarterly_cash_flow`・`quarterly_income_stmt`)・
# `api/routes.py`(同3キー)・`run_monitoring.py`(`quarterly_income_stmt`・
# `quarterly_cash_flow`)がいずれも「`raw.payload`の最新1件」からこれらを
# 読むため、取得しなかった日もキー集合を欠かさず、直近の値を持ち越す。
_STATEMENT_KEYS = (
    "quarterly_income_stmt",
    "income_stmt",
    "balance_sheet",
    "cash_flow",
    "quarterly_cash_flow",
)

# S-7(daily_pipeline_throughput_plan_2026-09-04.md、設計上の選択肢1):財務諸表を
# 「本当に取得した日」を記録するキー。DDL無しでこれを実現する方法として、
# `tickers`にカラムを足す案と、payloadに常設キーを1つ足す案を検討し後者を採る。
# 理由:
#   - payloadの形(キー集合)を変えないことがS-2で守った不変条件であり
#     (`_carry_forward_statements`のdocstring参照)、後続の消費者
#     (`apply_gates.py`・`api/routes.py`・`run_monitoring.py`)は全て
#     `payload.get(specific_key)`でアクセスするため、キーを1つ増やすだけなら
#     既存コードへの影響がゼロ(素通しされる)。
#   - マイグレーションが要らない。今まさに本番の`run-daily-pipeline`が
#     `collection`工程を実行中で、Alembicのマイグレーションは同じテーブル群に
#     ロックを取るため実行できない(運用制約)。カラム追加案だと、マイグレー
#     ションが適用されるまで本機能は一切効かない「書いたが使えない」状態が
#     残る。
# 値は`snapshot_date.isoformat()`(ISO文字列)。JSONBカラムへのバインドは
# 標準の`json.dumps`を通るため、`date`オブジェクトのまま入れるとシリアライズに
# 失敗する(他の日付列も`_df_to_json`で文字列化している理由と同じ)。
_STATEMENTS_AS_OF_KEY = "_statements_as_of"

_SHARE_REFRESH_FORMS = frozenset({"10-K", "10-Q", "20-F", "40-F", "S-3", "S-3ASR", "424B4", "424B5", "8-K"})


def _latest_shares_observation(
    session: Session, ticker_id: int
) -> tuple[int | None, date | None]:
    row = (
        session.query(PriceSnapshot.shares_outstanding, PriceSnapshot.shares_observed_at)
        .filter(
            PriceSnapshot.ticker_id == ticker_id,
            PriceSnapshot.shares_outstanding.isnot(None),
        )
        .order_by(PriceSnapshot.trade_date.desc())
        .first()
    )
    if row is None:
        return None, None
    return int(row[0]), row[1]


def _shares_refresh_due(
    session: Session,
    ticker_id: int,
    market_session_date: date,
    interval_days: int,
    *,
    recovering_from_quarantine: bool,
) -> tuple[bool, str]:
    """Return whether the sparse shares series needs a provider refresh."""
    _value, observed_at = _latest_shares_observation(session, ticker_id)
    if observed_at is None:
        return True, "missing_observation"
    if recovering_from_quarantine:
        return True, "quarantine_recovery"
    if market_session_date.weekday() == WEEKLY_REFRESH_WEEKDAY:
        return True, "weekly"
    if (market_session_date - observed_at).days >= interval_days:
        return True, "interval_elapsed"
    relevant_filing = (
        session.query(Filing.id)
        .filter(
            Filing.ticker_id == ticker_id,
            Filing.form.in_(_SHARE_REFRESH_FORMS),
            Filing.filed_date > observed_at,
            Filing.filed_date <= market_session_date,
        )
        .first()
    )
    if relevant_filing is not None:
        return True, "sec_filing"
    return False, "carry_forward"


def _carry_forward_statements(session: Session, ticker_id: int, payload: dict[str, Any]) -> None:
    """財務諸表を取得しなかった日、直近の`raw_snapshots.payload`から
    財務諸表5キーを持ち越して`payload`に差し込む(破壊的更新)。

    **`payload`の形(キー集合)を日によって変えないことが目的。**
    `apply_gates._gather_gate_input`は`available_from <= snapshot_date`の
    最新1件を取るだけで、財務諸表を取得した日かどうかは見ない。財務諸表を
    欠いた行が最新になるとゲートが静かに壊れる(欠損値=ゼロ扱いになる
    項目がある)。持ち越し元が無ければ空dict(既存の`payload.get(key, {})`
    フォールバックと同じ挙動)を入れる——真に何も無いよりは、少なくとも
    キー自体は揃っている状態にする。
    """
    missing = [key for key in _STATEMENT_KEYS if key not in payload]
    if not missing:
        return
    previous = (
        session.query(RawSnapshot)
        .filter(RawSnapshot.ticker_id == ticker_id)
        .order_by(RawSnapshot.snapshot_date.desc())
        .first()
    )
    previous_payload = previous.payload if previous is not None else {}
    for key in missing:
        payload[key] = previous_payload.get(key, {})
    # S-7:`_statements_as_of`(財務諸表を実際に取得した日)も一緒に持ち越す。
    # **ここを`snapshot_date`(=今日)で更新してはならない。** それをやると
    # 「持ち越しただけの日」が「今日取得した日」と区別できなくなり、
    # `_earnings_triggered_refetch`が「もう決算後の値に更新済み」と誤判定して
    # 二度と再取得しなくなる(自己収束どころか永久に古いまま固定される)。
    # 持ち越し元(前日以前の行)に無ければ None のまま——S-7導入前の行、または
    # 初回収集で持ち越し元自体が無いケースで、その場合は「未取得」として扱う。
    payload[_STATEMENTS_AS_OF_KEY] = previous_payload.get(_STATEMENTS_AS_OF_KEY)


def _last_statements_fetch_date(session: Session, ticker_id: int) -> date | None:
    """直近の`raw_snapshots.payload`から、財務諸表を実際に取得した日
    (`_statements_as_of`)を読む。

    無ければ None——S-7導入前に書かれた行(キー自体が無い)、`_statements_as_of`
    がNoneのまま持ち越されてきた行(初回収集より前に遡る持ち越し元が無いケース)、
    または`raw_snapshots`自体がまだ無い銘柄のいずれか。呼び出し側は「未取得」
    として扱い、決算日を過ぎていれば再取得の対象にする。
    """
    previous = (
        session.query(RawSnapshot)
        .filter(RawSnapshot.ticker_id == ticker_id)
        .order_by(RawSnapshot.snapshot_date.desc())
        .first()
    )
    if previous is None:
        return None
    raw = previous.payload.get(_STATEMENTS_AS_OF_KEY)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def _earnings_triggered_refetch(
    session: Session, ticker_id: int, snapshot_date: date, grace_days: int
) -> bool:
    """S-7(daily_pipeline_throughput_plan_2026-09-04.md):決算日を跨いだティッカーは
    週次(月曜)を待たず財務諸表を再取得する。

    **背景**:S-2で財務諸表を週次へ格下げした副作用として、決算が週の半ば
    (火〜日)に出た銘柄は`apply_gates`への反映が最大6日遅れうる。実測(計画書
    のS-2章・本節)では観測された統計諸表の全変化220件中75件(34%)が非月曜日に
    発生しており、遅延は平均4.8日・最大6日。ゲート対象銘柄でも13銘柄が2週間の
    観測期間中にこれで影響を受けた。決算発表の翌営業日こそ財務諸表が最も
    変わりやすい日そのものであり、そこを取りこぼしているのが一番痛い。

    **`event_calendar`は「次回決算日」であり、過ぎればそのまま過去日として
    残り続ける点に注意**(週次`events`工程が次の日付に更新するまで)。これを
    単純に「event_date <= snapshot_date なら毎日再取得」にすると、決算通過後
    次の月曜が来るまで**毎日**再取得し続けてしまい、週次化(S-2)の効果を
    自ら打ち消す。したがって「決算日から`grace_days`日以内」かつ「まだ猶予窓の
    終わりまでの財務諸表を取得していない」の両方を要求する。

    **なぜ決算日ちょうど1回だけの再取得では足りないか(`grace_days`既定3)**:
    yfinanceのfundamentals-timeseriesは決算発表の反映に通常1〜2日ラグがある。
    `event_date`当日だけを狙うと、発表直後の未更新値を掴んでしまい、その
    未更新値のまま「取得済み」と判定されて次の月曜まで二度と試さない——
    決算通過をトリガーにするという目的そのものが骨抜きになる。

    **2026-09-04監査で発見した欠陥(修正済み)**:以前の実装は
    `last_fetch < event_date`(=決算日そのものより前かどうか)で判定していた。
    これだと`event_date`当日に1回fetchした瞬間`last_fetch == event_date`と
    なって条件が偽になり、`grace_days`が何日でも**猶予窓は事実上1回で終わる**
    ——ちょうど「決算日当日だけ狙って外す」失敗モードをそのまま再現していた。
    正しくは、猶予窓の**終わり**(`event_date + grace_days`)より前かどうかで
    判定する。これにより同じ決算に対して猶予窓の日数ぶん(既定4回:
    E, E+1, E+2, E+3)fetchを試み続け、いつ反映されても取りこぼさない。

    **それでも無限には続かない(自己収束、上のクエリのwhere句が担保する)**:
    下のクエリは`event_date >= snapshot_date - grace_days`も要求しているため、
    `snapshot_date > event_date + grace_days`になった時点で対象決算が
    ヒットしなくなり(`latest_relevant_event_date is None`)、`grace_days`日を
    超えて毎日再取得し続けることはない——次の月曜、または次に`events`工程が
    新しい決算日を見つけるまで静かに黙る。

    **コスト試算**:1日あたり決算日を迎えるティッカーは293銘柄中およそ5件
    (293銘柄 ÷ 四半期91日換算)。それぞれ猶予窓の間(既定4日)毎日fetchする
    ため、ある1日に「猶予窓の中にいる」ティッカーは重複込みで高々20件程度、
    1銘柄あたり5リクエスト(財務諸表5本)なので日次+100本程度——S-2後の基準
    17,397本/日の1%に満たない。

    **「毎回同じ内容なら省略できないか」について**:猶予窓の間、決算が
    未反映で前日と同じ財務諸表が返り続けるティッカーがいる。これを検知して
    早期に諦めることは意図的にしない——フェッチしてみるまで「変わって
    いないこと」自体が分からない(それを知るための手段がフェッチしかない)
    うえ、最大4回/決算のコストは上のとおり無視できる水準であり、複雑さに
    見合わない。
    """
    # 猶予窓 [snapshot_date - grace_days, snapshot_date] に入る決算日のうち
    # 最新のものを見る。`event_calendar`は過去日も削除せず積み上がっていく
    # ため(`collect_events._upsert_event`)、複数行がヒットしうる——その中で
    # 最も新しい決算日を基準にしないと、古い決算日をとうに消化済みなのに
    # 別の古い行に引っ張られて判定を誤る。このwhere句自体が「猶予窓を過ぎたら
    # 対象決算がヒットしなくなる」という自己収束の担保でもある。
    latest_relevant_event_date = (
        session.query(func.max(EventCalendar.event_date))
        .filter(
            EventCalendar.ticker_id == ticker_id,
            EventCalendar.event_type == "earnings",
            EventCalendar.event_date <= snapshot_date,
            EventCalendar.event_date >= snapshot_date - timedelta(days=grace_days),
        )
        .scalar()
    )
    if latest_relevant_event_date is None:
        return False
    last_fetch = _last_statements_fetch_date(session, ticker_id)
    # **猶予窓の終わりと比べる(決算日そのものと比べない)。** 決算日当日に
    # 1回fetchしただけで`last_fetch == event_date`になり、それを`event_date`
    # と比べると窓の残り日数を無視して即座に「取得済み」扱いになってしまう
    # (2026-09-04監査で発見・上のdocstring参照)。猶予窓の終わり
    # (`event_date + grace_days`)より前なら、まだ窓の中でfetchを続ける。
    return last_fetch is None or last_fetch < latest_relevant_event_date + timedelta(days=grace_days)


def _content_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_or_create_ticker(session: Session, symbol: str, market: str = "US") -> Ticker:
    ticker = session.query(Ticker).filter_by(symbol=symbol).one_or_none()
    if ticker is None:
        ticker = Ticker(symbol=symbol, market=market)
        session.add(ticker)
        session.flush()
    return ticker


def _reassign_ticker_for_symbol_reuse(
    session: Session, old_ticker: Ticker, symbol: str, snapshot_date: date
) -> Ticker:
    """14.5で懸念していたティッカー再利用ケース:ISIN不一致により、廃止された
    銘柄と同じシンボルを別会社が使い始めたと判明した場合の処理。

    旧 `ticker_id` に別会社のデータを混入させると raw_snapshots の履歴が汚染され
    後続のスコアリング・前方検証(14.3)が意味を成さなくなるため、旧レコードは
    シンボルを退避して固定し(symbolのUNIQUE制約を空ける)、新しい内部IDを
    発行して以後のデータはそちらに紐づける。旧レコードの来歴は ticker_aliases
    (9章で定義済みだが今まで書き込み先がなかった)に残す。
    """
    session.add(
        TickerAlias(
            ticker_id=old_ticker.id,
            symbol=symbol,
            effective_from=old_ticker.listed_date or old_ticker.created_at.date(),
            effective_to=snapshot_date,
        )
    )
    # tickers.symbol は String(20)。id自体が一意なのでsuffixだけで衝突は起きないが、
    # 元シンボルが長い場合に列長を超えないよう先頭側を切り詰める。
    suffix = f"~D{old_ticker.id}"
    old_ticker.symbol = f"{symbol[: 20 - len(suffix)]}{suffix}"
    session.flush()

    new_ticker = Ticker(symbol=symbol, market=old_ticker.market)
    session.add(new_ticker)
    session.flush()
    return new_ticker


def _apply_split_to_price_history(
    session: Session, ticker_id: int, split_date: date, ratio: float
) -> int:
    """13.4:分割が発生したとき、`price_snapshots` に蓄積済みの分割前の行を
    分割後の単位に遡って調整する。

    バックフィル(`fetch_price_and_shares_history`)は取得時点で分割調整済みの
    系列を保存するが、日次収集で1日ずつ積み上げた行は**その日時点の単位**の
    ままDBに残る。分割が起きた瞬間、過去行だけが分割前の単位で取り残され、

    - 12-1モメンタム:3:1分割で「−67%の暴落」を誤検知する
    - 希薄化CAGR:株式数が3倍になったように見え、`dilution_ceiling`で誤除外する
    - 流動性中央値:過去の売買代金がずれる

    という形で評価ロジックを直撃する(13.4がバックフィルについて指摘した罠と
    同じものが、日次経路にだけ残っていた)。価格は`ratio`で割り、出来高・
    発行済株式数は`ratio`倍する。

    **べき等性(18.3)**:呼び出し元は「分割日以降の行がまだ1件も無い」ことを
    確認してから呼ぶ。同日中にパイプラインを再実行しても二重適用されない。
    """
    if ratio <= 0 or ratio == 1.0:
        return 0

    rows = (
        session.query(PriceSnapshot)
        .filter(PriceSnapshot.ticker_id == ticker_id, PriceSnapshot.trade_date < split_date)
        .all()
    )
    for row in rows:
        for field in ("open", "high", "low", "close"):
            value = getattr(row, field)
            if value is not None:
                setattr(row, field, float(value) / ratio)
        if row.volume is not None:
            row.volume = int(row.volume * ratio)
        if row.shares_outstanding is not None:
            row.shares_outstanding = int(row.shares_outstanding * ratio)
    return len(rows)


# 保存済みの終値と、取得しなおした(常に分割調整済みの)終値が「同じ値」と
# みなせる許容幅。日中の再計算や配当調整の差を吸収する。
_SPLIT_RECONCILE_TOLERANCE = 0.02


def _stored_rows_are_pre_split(
    session: Session,
    ticker_id: int,
    split_date: date,
    ratio: float,
    recent_closes: dict[date, float],
) -> bool | None:
    """保存済みの分割前の行が、まだ分割前の単位かどうか。判定できなければ None。

    **2026-08-26に発見した欠陥**:以前は「分割日以降の行がまだ1件も無いこと」を
    条件にしていた。これは「分割当日の収集が、Yahooが分割を反映する**前**に
    走って分割前価格の行を書いた」場合に破綻する——翌日以降は
    `already_have_post_split_rows` が真になり、**分割前の行は永久に調整されない**。

    実データで FLOC がこの状態だった:2026年5月の株式併合(約1:2.5)が
    `price_snapshots` に反映されておらず、発行済株式数の系列が
    22M(2025-01) → 93M(2025-12) → 42M(2026-05) と単位の混ざった形で残り、
    希薄化CAGRが測定不能になっていた。

    保存値と取得値(常に現在単位)を同じ取引日で比べれば、行の有無ではなく
    **値そのもの**で判定できる。
    """
    if not recent_closes:
        return None
    candidates = (
        session.query(PriceSnapshot)
        .filter(
            PriceSnapshot.ticker_id == ticker_id,
            PriceSnapshot.trade_date < split_date,
            PriceSnapshot.close.isnot(None),
        )
        .order_by(PriceSnapshot.trade_date.desc())
        .limit(30)
        .all()
    )
    for row in candidates:
        fetched = recent_closes.get(row.trade_date)
        if fetched is None or fetched <= 0:
            continue
        observed_ratio = float(row.close) / fetched
        if abs(observed_ratio - 1.0) <= _SPLIT_RECONCILE_TOLERANCE:
            return False  # 保存側は既に調整済み
        if abs(observed_ratio / ratio - 1.0) <= _SPLIT_RECONCILE_TOLERANCE:
            return True  # 保存側は分割前の単位のまま
    return None


def _reconcile_splits(
    session: Session,
    ticker_id: int,
    recent_splits: list[tuple[date, float]],
    recent_closes: dict[date, float] | None = None,
) -> None:
    """取得窓の中で観測した分割を、未適用のものだけ蓄積済み履歴に反映する。"""
    recent_closes = recent_closes or {}
    for split_date, ratio in sorted(recent_splits):
        verdict = _stored_rows_are_pre_split(session, ticker_id, split_date, ratio, recent_closes)
        if verdict is False:
            continue
        if verdict is None:
            # 値で判定できない(取得窓と保存済み行が重ならない)。従来どおり
            # 「分割日以降の行の有無」で判断する。
            already_have_post_split_rows = (
                session.query(PriceSnapshot)
                .filter(PriceSnapshot.ticker_id == ticker_id, PriceSnapshot.trade_date >= split_date)
                .count()
                > 0
            )
            if already_have_post_split_rows:
                continue
        adjusted = _apply_split_to_price_history(session, ticker_id, split_date, ratio)
        logger.info(
            "ticker_id=%s: applied %s-for-1 split of %s retroactively to %d stored price rows (13.4)",
            ticker_id,
            ratio,
            split_date,
            adjusted,
        )


# 14.10の前日比急変検知が見る `info` のフィールド。派生値(marketCap)ではなく
# 一次的な観測値を優先し、単位の取り違えが出やすい金額系に絞る。
_SPIKE_FIELDS = ("totalRevenue", "totalCash", "sharesOutstanding", "marketCap")


def _detect_spikes(session: Session, ticker_id: int, info: dict[str, Any]) -> list[str]:
    """直前のスナップショットと比べた急変(14.10)を検知する。

    **2026-08-26に発見した欠陥**:`validation.rules.detect_day_over_day_spike` は
    実装もテストもされていたが、**どこからも呼ばれていなかった**。14.10が
    「単位変更・yfinance側の不具合の早期検知」として定めた仕組みが、丸ごと
    存在しないのと同じ状態だった(`min_listed_quarters`・`is_valid`・
    `available_from` に続く4例目)。

    検知結果は `is_valid` を動かさない。急変は「異常の疑い」であって
    「値が間違っている証明」ではなく、決算期をまたいだ正常な急変(赤字転換・
    大型買収)もあるためである。ゲートやスコアを止めるのではなく、
    ログと `collection_logs.detail` に残して運用者の目に触れさせる。
    """
    previous = (
        session.query(RawSnapshot)
        .filter(RawSnapshot.ticker_id == ticker_id)
        .order_by(RawSnapshot.snapshot_date.desc())
        .first()
    )
    if previous is None:
        return []
    previous_info = previous.payload.get("info") or {}
    spikes: list[str] = []
    for field in _SPIKE_FIELDS:
        before, after = previous_info.get(field), info.get(field)
        if not isinstance(before, (int, float)) or not isinstance(after, (int, float)):
            continue
        code = detect_day_over_day_spike(field, float(before), float(after))
        if code is not None:
            spikes.append(code)
    return spikes


def _register_failure(ticker: Ticker, collection_config: CollectionConfig) -> None:
    ticker.consecutive_failures += 1
    if ticker.consecutive_failures >= collection_config.quarantine.consecutive_failure_threshold:
        ticker.is_quarantined = True


def _log(
    session: Session,
    run_id: uuid.UUID,
    ticker_id: int | None,
    snapshot_date: date,
    status: str,
    detail: dict[str, Any] | None,
) -> None:
    session.add(
        CollectionLog(run_id=run_id, ticker_id=ticker_id, snapshot_date=snapshot_date, status=status, detail=detail)
    )


def collect_one(
    session: Session,
    run_id: uuid.UUID,
    symbol: str,
    collection_config: CollectionConfig,
    snapshot_date: date,
    market_session_date: date | None = None,
    force_statement_refresh: bool = False,
) -> str:
    """1銘柄を収集しDBに反映する。戻り値は collection_logs.status に相当する文字列。"""
    ticker = get_or_create_ticker(session, symbol)
    market_session_date = market_session_date or snapshot_date
    recovering_from_quarantine = bool(ticker.is_quarantined)

    if ticker.is_quarantined:
        days_since_attempt = (
            (datetime.now(UTC) - ticker.last_attempted_at).days if ticker.last_attempted_at else None
        )
        if days_since_attempt is not None and days_since_attempt < collection_config.quarantine.retry_interval_days:
            _log(session, run_id, ticker.id, snapshot_date, "quarantined", {"days_since_attempt": days_since_attempt})
            return "quarantined"

    ticker.last_attempted_at = datetime.now(UTC)

    # S-2(daily_pipeline_throughput_plan_2026-09-04):財務諸表(5本の
    # fundamentals-timeseries)は四半期に1度しか変わらないので、週次日
    # (`WEEKLY_REFRESH_WEEKDAY`、`xbrl_facts`工程と同じ曜日)だけ取得する。
    # 例外は2つ:
    #   1. 持ち越し元(直近のraw_snapshot)が無い銘柄(新規上場・初回収集)は、
    #      その日に取得しないと財務諸表が永久に空のままになるので週次日で
    #      なくても取得する。
    #   2. `delisted_at`が設定されている銘柄は、ISIN確認により別会社への
    #      再割当て(14.5)が起こりうる。再割当て後の新しいticker_idには
    #      持ち越し元が無いが、その判定はfetch後(info取得後)にしか
    #      できないため、このケースは単純に「常に取得」にして持ち越しの
    #      対象から外す(発生頻度が低く、復活検知自体が例外的なイベントの
    #      ため、週次最適化の対象にしない)。
    #
    # **監査で指摘された副作用(2026-09-04、意図的に許容):**
    # 財務諸表は週次(月曜)でしか更新されないため、決算が週の半ば(火〜日)に
    # 出ても`apply_gates`に反映されるまで最大6日かかりうる。これは
    # `xbrl_facts`工程(週次)で既に受け入れられている方針と同じで、計画が
    # 明示的に許容したトレードオフである。
    #
    # 加えてS-2固有の副作用:`is_quarantined`(18.1)から週の半ばに復帰した
    # 銘柄は、上の条件(delisted_atではなくquarantineは対象外)に該当しない
    # ため`include_statements=False`になり、**隔離される前の(=quarantine期間
    # ぶん古い)財務諸表をそのまま持ち越す**。S-2導入前は日次収集が毎回
    # 財務諸表を丸ごと取り直していたため、復帰した日には必ず最新の値が
    # 入っていた——ここは挙動が変わった箇所である。次の月曜まで最新化
    # されない点も含め、後日この日のゲート判定を「古いデータで壊れている」
    # と誤診しないよう、ここに明記しておく。
    #
    # S-7(2026-09-04、同計画書):上の副作用のうち「決算が週の半ばに出ると
    # 最大6日遅れる」を実測した結果(観測220件中75件=34%が非月曜変化、平均
    # 4.8日遅延・ゲート対象銘柄でも13銘柄が影響)、これを許容し続けるのは
    # 危険と判断し追加の例外を導入した。
    #   3. `event_calendar`の次回決算日を過ぎ、かつ猶予日数
    #      (`statement_refresh_grace_days`)以内のティッカーは、週次日で
    #      なくても財務諸表を取得する(`_earnings_triggered_refetch`。
    #      自己収束の仕組み・猶予日数が必要な理由はそちらのdocstring参照)。
    #      `event_calendar`に行が無いティッカー(追跡対象外)はこれまでどおり
    #      週次のみで、それは意図した挙動(タスク仕様どおりカバレッジを
    #      広げない)。
    has_prior_snapshot = (
        session.query(RawSnapshot.id).filter_by(ticker_id=ticker.id).first() is not None
    )
    earnings_refresh = _earnings_triggered_refetch(
        session, ticker.id, snapshot_date, collection_config.statement_refresh_grace_days
    )
    include_statements = (
        # A weekly refresh is scheduled from the *completed US market week*,
        # not from the local batch date.  In particular, a Tuesday JST run may
        # be the first successful run after Monday's US close.
        force_statement_refresh
        or market_session_date.weekday() == WEEKLY_REFRESH_WEEKDAY
        or ticker.delisted_at is not None
        or not has_prior_snapshot
        or earnings_refresh
    )

    try:
        payload = fetch_raw_financials(symbol, collection_config.retry, include_statements=include_statements)
    except PermanentFailure as exc:
        ticker.delisted_at = datetime.now(UTC)
        _log(session, run_id, ticker.id, snapshot_date, "permanent_failure", {"error": str(exc)})
        return "permanent_failure"
    except EmptyResponseError as exc:
        _register_failure(ticker, collection_config)
        # B-5(2026-08-26、docs/model_audit_v4_2026-08-26.md):yfinanceはHTTP 404を
        # 例外として送出せず、空の`info`として返すことがある。この経路では
        # `PermanentFailure`に届かないため`delisted_at`が一度も設定されず、
        # 実質的に消えた銘柄が`tickers`に残り続けバックテストの生存バイアスを
        # 恒久化していた。「resolves within days」な一時的欠落と区別するため、
        # `retry_interval_days`で数週間分の連続失敗を要求してから確定する。
        if ticker.consecutive_failures >= collection_config.quarantine.empty_response_delisted_threshold:
            ticker.delisted_at = datetime.now(UTC)
            _log(
                session,
                run_id,
                ticker.id,
                snapshot_date,
                "empty_response_delisted",
                {"error": str(exc), "consecutive_failures": ticker.consecutive_failures},
            )
            return "empty_response_delisted"
        _log(session, run_id, ticker.id, snapshot_date, "empty_response", {"error": str(exc)})
        return "empty_response"
    except TransientFailure as exc:
        _register_failure(ticker, collection_config)
        _log(
            session,
            run_id,
            ticker.id,
            snapshot_date,
            "transient_failure",
            collection_error_detail(exc),
        )
        return "transient_failure"
    except ParseFailure as exc:
        # スキーマ変更の疑い(11章・14.14)。個別銘柄は失敗させるが全体は継続する。
        logger.warning("parse failure for %s: %s", symbol, exc)
        _log(
            session,
            run_id,
            ticker.id,
            snapshot_date,
            "parse_failure",
            collection_error_detail(exc),
        )
        return "parse_failure"

    if not include_statements:
        # `ticker.delisted_at is not None` の分岐では常に`include_statements`が
        # Trueになる(上の判定条件)ため、ここに来る時点で`ticker`は再割当て
        # されていない(=このticker_idの持ち越し元を安全に参照できる)。
        _carry_forward_statements(session, ticker.id, payload)
    else:
        # S-7:財務諸表を実際に取得した日を刻む。ISO文字列にするのは
        # `_STATEMENTS_AS_OF_KEY`の定義コメント参照(JSONBバインドが`date`を
        # 直接扱えないため)。これが無いと`_earnings_triggered_refetch`は
        # 「いつ最後に取得したか」を知る手段が無く、決算通過後も毎日
        # 再取得し続けるか、逆に一度も再取得しないかのどちらかに壊れる。
        payload[_STATEMENTS_AS_OF_KEY] = snapshot_date.isoformat()

    info = payload["info"]
    if ticker.delisted_at is not None:
        # 24.7で発見:delisted_atは設定される一方でどこからもクリアされていなかった。
        # 単に解除するだけでは、14.5で懸念していたティッカー再利用(廃止銘柄と同じ
        # シンボルを別会社が使い始めるケース)で他社のデータが同じticker_idの履歴に
        # 混入してしまう。ISINは会社に紐づき同一性を跨いで変わらないため、これを
        # 使って「本当に同じ会社が復活したのか」を確認してから解除する。
        fresh_isin = fetch_isin(symbol)
        if fresh_isin and ticker.isin and fresh_isin != ticker.isin:
            logger.warning(
                "%s: ISIN mismatch (was %s, now %s) while recovering from delisted_at=%s — "
                "symbol appears to have been reused by a different company (14.5). "
                "Archiving ticker_id=%s and issuing a new internal id instead of merging.",
                symbol,
                ticker.isin,
                fresh_isin,
                ticker.delisted_at,
                ticker.id,
            )
            ticker = _reassign_ticker_for_symbol_reuse(session, ticker, symbol, snapshot_date)
            ticker.last_attempted_at = datetime.now(UTC)
        else:
            logger.warning(
                "%s: was marked delisted_at=%s but is now returning valid data; clearing (%s)",
                symbol,
                ticker.delisted_at,
                "ISIN confirmed same company" if fresh_isin else "no ISIN on record to verify against",
            )
            ticker.delisted_at = None
        if ticker.isin is None:
            ticker.isin = fresh_isin
    elif ticker.isin is None:
        # 銘柄ごとに一度だけ取得して固定する(日次収集の通常経路には追加APIコールを
        # 発生させない。ISINは会社の生涯を通じて不変なので取得は初回のみで十分)。
        ticker.isin = fetch_isin(symbol)
    ticker.sector = info.get("sector") or ticker.sector
    ticker.industry = info.get("industry") or ticker.industry
    ticker.consecutive_failures = 0
    ticker.is_quarantined = False

    validation = validate_info(info)
    spikes = _detect_spikes(session, ticker.id, info)
    if spikes:
        # 14.10:単位変更・yfinance側の不具合の早期検知。**ランキングには一切
        # 影響させない**——閾値を跨いだ1銘柄を機械的に落とすのは、Altman Z''を
        # ハードゲートにしなかった教訓(exclusion_gates.py)と同じ失敗になる。
        # 運用者が気づけるようログと `collection_logs.detail` に残すだけ。
        logger.warning("%s: day-over-day spike detected: %s (14.10)", symbol, ", ".join(spikes))
    content_hash = _content_hash(payload)

    existing = (
        session.query(RawSnapshot)
        .filter_by(ticker_id=ticker.id, content_hash=content_hash)
        .order_by(RawSnapshot.snapshot_date.desc())
        .first()
    )
    if existing is not None:
        # 14.11: 内容に変化がなければ新規行を作らず鮮度だけ更新する。
        # ただし検証結果は毎回上書きする(検証ロジックを直しても、内容が
        # 変わらない限り古い判定が残り続けてしまうため。14.10)。
        existing.last_seen_date = snapshot_date
        existing.is_valid = validation.is_valid
        existing.validation_errors = validation.errors or None
    else:
        session.add(
            RawSnapshot(
                ticker_id=ticker.id,
                snapshot_date=snapshot_date,
                source="yfinance",
                payload=payload,
                content_hash=content_hash,
                last_seen_date=snapshot_date,
                available_from=snapshot_date,
                is_valid=validation.is_valid,
                validation_errors=validation.errors or None,
            )
        )

    try:
        fetch_shares, shares_refresh_reason = _shares_refresh_due(
            session,
            ticker.id,
            market_session_date,
            collection_config.shares_refresh_interval_days,
            recovering_from_quarantine=recovering_from_quarantine,
        )
        if earnings_refresh and not fetch_shares:
            # 決算トリガーで財務諸表を取り直す日は希薄化入力も同期する。
            fetch_shares = True
            shares_refresh_reason = "earnings_refresh"
        price = fetch_latest_price(
            symbol, collection_config.retry, include_shares=fetch_shares
        )
    except CollectionError as exc:
        price = None
        _log(
            session,
            run_id,
            ticker.id,
            snapshot_date,
            "price_fetch_failed",
            collection_error_detail(exc),
        )

    if price is not None and price["trade_date"] < market_session_date:
        # Some provider responses return a stale last row for halted or
        # otherwise unavailable symbols.  Never overwrite that historical row
        # with shares learned today; doing so would create point-in-time
        # leakage even though shares_observed_at exposes the mismatch.
        _log(
            session,
            run_id,
            ticker.id,
            snapshot_date,
            "price_stale",
            {
                "trade_date": price["trade_date"].isoformat(),
                "expected_session": market_session_date.isoformat(),
            },
        )
        price = None

    if price is not None:
        shares_requested = bool(price.pop("_shares_requested", True))
        # PriceSnapshotの列ではないメタ情報を先に取り出す(13.4の遡及調整)。
        _reconcile_splits(
            session,
            ticker.id,
            price.pop("recent_splits", []),
            price.pop("recent_closes", {}),
        )

        if shares_requested and price.get("shares_outstanding") is not None:
            price["shares_observed_at"] = market_session_date
            price["shares_coverage_status"] = "collected_with_data"
        else:
            carried_shares, carried_observed_at = _latest_shares_observation(session, ticker.id)
            if carried_shares is not None:
                price["shares_outstanding"] = carried_shares
                price["shares_observed_at"] = carried_observed_at
                price["shares_coverage_status"] = (
                    "carried_forward_no_finding" if shares_requested else "carried_forward"
                )
            else:
                price["shares_observed_at"] = None
                price["shares_coverage_status"] = (
                    "collected_no_finding" if shares_requested else "not_collected"
                )
        logger.debug(
            "%s: shares refresh=%s reason=%s observed_at=%s status=%s",
            symbol,
            shares_requested,
            shares_refresh_reason,
            price.get("shares_observed_at"),
            price.get("shares_coverage_status"),
        )

        existing_price = (
            session.query(PriceSnapshot).filter_by(ticker_id=ticker.id, trade_date=price["trade_date"]).one_or_none()
        )
        if existing_price is None:
            session.add(PriceSnapshot(ticker_id=ticker.id, **price))
        else:
            # 同日内の再実行で値が更新されるケースに対応(18.3:べき等性は
            # 「重複を作らない」だけでなく「再実行すれば正しい値に収束する」
            # ことも含む)
            for field, value in price.items():
                setattr(existing_price, field, value)

    # B-7(2026-08-26、docs/model_audit_v4_2026-08-26.md):この行は`success`と対比
    # される旧名`invalid_data`だと「除外された」ように読めるが、`sanitize_info`
    # は問題のあるフィールドだけをNoneへ差し替えて**行自体は保存し使う**設計
    # である(is_validはスコアリングを止めない)。実データでは18.7%がこちらに
    # 該当し、それが「除外された」ように誤読されていた。
    status = "success" if validation.is_valid else "sanitized"
    detail: dict[str, Any] = {}
    if validation.errors:
        detail["validation_errors"] = validation.errors
    if spikes:
        detail["day_over_day_spikes"] = spikes
    _log(session, run_id, ticker.id, snapshot_date, status, detail or None)
    return status
