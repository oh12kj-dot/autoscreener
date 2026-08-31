"""銘柄単位のオーケストレーション:取得 → 検証 → DB保存 → 隔離リスト管理(18.1)。"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.orm import Session

from autoscreener.collectors.errors import (
    CollectionError,
    EmptyResponseError,
    ParseFailure,
    PermanentFailure,
    TransientFailure,
)
from autoscreener.collectors.yfinance_client import (
    fetch_isin,
    fetch_latest_price,
    fetch_raw_financials,
)
from autoscreener.config import CollectionConfig
from autoscreener.db.models import CollectionLog, PriceSnapshot, RawSnapshot, Ticker, TickerAlias
from autoscreener.validation.rules import detect_day_over_day_spike, validate_info

logger = logging.getLogger(__name__)


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
) -> str:
    """1銘柄を収集しDBに反映する。戻り値は collection_logs.status に相当する文字列。"""
    ticker = get_or_create_ticker(session, symbol)

    if ticker.is_quarantined:
        days_since_attempt = (
            (datetime.now(UTC) - ticker.last_attempted_at).days if ticker.last_attempted_at else None
        )
        if days_since_attempt is not None and days_since_attempt < collection_config.quarantine.retry_interval_days:
            _log(session, run_id, ticker.id, snapshot_date, "quarantined", {"days_since_attempt": days_since_attempt})
            return "quarantined"

    ticker.last_attempted_at = datetime.now(UTC)

    try:
        payload = fetch_raw_financials(symbol, collection_config.retry)
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
        _log(session, run_id, ticker.id, snapshot_date, "transient_failure", {"error": str(exc)})
        return "transient_failure"
    except ParseFailure as exc:
        # スキーマ変更の疑い(11章・14.14)。個別銘柄は失敗させるが全体は継続する。
        logger.warning("parse failure for %s: %s", symbol, exc)
        _log(session, run_id, ticker.id, snapshot_date, "parse_failure", {"error": str(exc)})
        return "parse_failure"

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
        price = fetch_latest_price(symbol, collection_config.retry)
    except CollectionError as exc:
        price = None
        _log(session, run_id, ticker.id, snapshot_date, "price_fetch_failed", {"error": str(exc)})

    if price is not None:
        # PriceSnapshotの列ではないメタ情報を先に取り出す(13.4の遡及調整)。
        _reconcile_splits(
            session,
            ticker.id,
            price.pop("recent_splits", []),
            price.pop("recent_closes", {}),
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
