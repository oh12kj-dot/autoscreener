"""除外ゲート(15.2)を実データに適用し、当日の universe_snapshots を確定する。

`tickers`・最新の `raw_snapshots`・`price_snapshots` から `GateInput` を組み立て、
`screening.exclusion_gates.evaluate_gates` で判定する。14.3の生存バイアス対策
どおり、除外された銘柄も `included=False` として記録し、マスタから削除しない。

**A-3(2026-09-04、docs/racr_wp_a_operational_safety_2026-09-04.md、
監査§10.2/10.3):並行削除耐性。** 以前は全銘柄を1つの `session_scope()` 内で
ループし(数千件・数分〜十数分規模)、最後に1回だけcommitしていた。この間に
外部が `Ticker` を削除すると、その `ticker_id` への `UniverseSnapshot` insert
が外部キー違反になり、**PostgreSQLは違反したsession全体を中断状態にする**
——以後そのsessionで発行するどの文も `current transaction is aborted` で
失敗し、既に判定済みの残り数千件分もまとめて失われる。2026-09-03の実障害
(`ticker_id=24528` が消えていた)がこれで日次全体を止めた。対処は3つ:

1. 小さいバッチ(`_GATE_COMMIT_BATCH_SIZE`)ごとに独立した `session_scope()`
   でcommitし、1バッチの失敗が他バッチへ波及しないようにする。
2. insert直前に `session.get(Ticker, ...)` で存在を再確認する——全件を
   先読みしてから長いループを回す構造そのものは変えていないが、実際に
   その銘柄を処理する瞬間に生きているかを見る。
3. 2の再確認とinsertの間に残るごく狭いレース(その一瞬に消された場合)は
   `session.begin_nested()`(SAVEPOINT)で個別に囲み、`IntegrityError` を
   バッチ全体ではなく1件だけの失敗として吸収する。
4. 消えていた/消えたticker_idは黙って無視せず `skipped_missing_tickers`
   として件数を、`skipped_missing_ticker_ids_sample` として一部IDを
   stage結果へ残す。
"""

from __future__ import annotations

import logging
import statistics
from datetime import date

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from autoscreener.config import UniverseConfig, load_universe_config
from autoscreener.dates import utc_today
from autoscreener.db.models import PriceSnapshot, RawSnapshot, Ticker, UniverseSnapshot
from autoscreener.db.session import session_scope
from autoscreener.screening.exclusion_gates import (
    GateInput,
    compute_cash_runway_quarters,
    count_available_quarters,
    dilution_cagr,
    evaluate_gates,
    latest_period_value,
    normalize_financial_currency_value,
)
from autoscreener.validation.rules import sanitize_info

logger = logging.getLogger(__name__)

# 流動性中央値の算出に使う直近営業日数(15.2は「中央値」とのみ規定。
# 3〜4ヶ月相当の90営業日を採用し、四半期単位の実態に近づける)
_LIQUIDITY_WINDOW_TRADING_DAYS = 90

# A-3:1コミットあたりの銘柄数。現行ユニバース規模(約5,900銘柄)で
# 20〜30バッチ程度になる粒度——小さすぎるとcommit回数が増えて遅くなり、
# 大きすぎると「1件のFK違反で日次全体を落とさない」という目的に対して
# 依然として巻き込む範囲が大きすぎる、という両端を避けた値。
_GATE_COMMIT_BATCH_SIZE = 250


def _gather_gate_input(session: Session, ticker: Ticker, snapshot_date: date) -> GateInput | None:
    """`snapshot_date` 時点で入手できていたデータだけでゲート入力を組み立てる。

    **2026-08-26修正**:以前は日付に関係なく「最新の raw_snapshot」と
    「price_snapshots の全行の末尾90行」を読んでいた。`apply_gates --date` は
    過去日を指定できる(CLIがそう案内している)ので、これは**過去日の
    universe_snapshots を今日のデータで書き直す**ことを意味していた。
    スコアリング側(`point_in_time.build_moic_inputs`)は as_of で厳密に
    切っているため、同じ日付の「ゲート判定」と「スコア」が別の時点の情報で
    作られるという不整合にもなっていた。

    `raw_snapshots.available_from` は 14.3(先読みバイアス対策)のために用意
    されていた列だが、**どのクエリからも参照されていなかった**。ここで使う。
    """
    raw = (
        session.query(RawSnapshot)
        .filter(RawSnapshot.ticker_id == ticker.id, RawSnapshot.available_from <= snapshot_date)
        .order_by(RawSnapshot.snapshot_date.desc())
        .first()
    )
    if raw is None:
        return None

    info = sanitize_info(raw.payload.get("info", {}))
    balance_sheet = raw.payload.get("balance_sheet", {})
    quarterly_cash_flow = raw.payload.get("quarterly_cash_flow")
    quarterly_income_stmt = raw.payload.get("quarterly_income_stmt", {})

    price_rows = (
        session.query(PriceSnapshot)
        .filter(PriceSnapshot.ticker_id == ticker.id, PriceSnapshot.trade_date <= snapshot_date)
        .order_by(PriceSnapshot.trade_date.asc())
        .all()
    )

    median_dollar_volume: float | None = None
    if price_rows:
        recent = price_rows[-_LIQUIDITY_WINDOW_TRADING_DAYS:]
        dollar_volumes = [float(r.close) * r.volume for r in recent if r.close is not None and r.volume is not None]
        if dollar_volumes:
            median_dollar_volume = statistics.median(dollar_volumes)


    dilution = dilution_cagr([(r.trade_date, r.shares_outstanding) for r in price_rows], balance_sheet)
    stockholders_equity = latest_period_value(balance_sheet.get("Stockholders Equity"))
    cash_runway = (
        compute_cash_runway_quarters(info.get("totalCash"), quarterly_cash_flow) if quarterly_cash_flow else None
    )

    return GateInput(
        market_cap=info.get("marketCap"),
        total_revenue=normalize_financial_currency_value(info.get("totalRevenue"), info),
        price=info.get("currentPrice") or info.get("regularMarketPrice"),
        sector=info.get("sector"),
        median_daily_dollar_volume=median_dollar_volume,
        dilution_3y_cagr=dilution,
        stockholders_equity=stockholders_equity,
        cash_runway_quarters=cash_runway,
        available_quarters=count_available_quarters(quarterly_income_stmt),
    )


def _upsert_snapshot(
    session: Session, snapshot_date: date, ticker_id: int, passed: bool, reason: str | None
) -> None:
    """その日の判定結果を universe_snapshots に書き込む(同日再実行はべき等)。"""
    existing = (
        session.query(UniverseSnapshot).filter_by(snapshot_date=snapshot_date, ticker_id=ticker_id).one_or_none()
    )
    if existing is None:
        session.add(
            UniverseSnapshot(
                snapshot_date=snapshot_date, ticker_id=ticker_id, included=passed, exclusion_reason=reason
            )
        )
    else:
        existing.included = passed
        existing.exclusion_reason = reason


def _process_one_ticker(
    session: Session,
    ticker_id: int,
    snapshot_date: date,
    universe_config: UniverseConfig,
    counts: dict[str, int],
) -> bool:
    """1銘柄ぶんのゲート判定と upsert。

    呼び出し直前(このバッチの `session` を開いた後、この銘柄を処理する
    瞬間)に既に存在しなかった場合、または存在確認と実際のinsertの間の
    ごく狭いレースで消された場合は `False` を返す——どちらも
    `apply_gates` 側で `skipped_missing_tickers` として数える。
    """
    # A-3:insert直前の再確認。全件を先読みした後の長いループの間に外部が
    # 削除した銘柄(2026-09-03の実障害)をここで検出し、FK違反そのものを
    # 未然に防ぐ。
    ticker = session.get(Ticker, ticker_id)
    if ticker is None:
        return False

    try:
        # A-3:再確認とinsertの間に残る狭いレースへの防御。SAVEPOINTだけを
        # ロールバックすることで、万一ここで `IntegrityError` になっても
        # このバッチの他銘柄・後続バッチの既commit分には影響させない。
        with session.begin_nested():
            # D-4:ベンチマークETFは価格を収集するがランキングには混ぜない。
            if ticker.is_benchmark:
                counts["benchmark"] += 1
                _upsert_snapshot(session, snapshot_date, ticker.id, False, "benchmark")
                return True

            if ticker.delisted_at is not None:
                counts["delisted"] += 1
                _upsert_snapshot(session, snapshot_date, ticker.id, False, "delisted")
                return True

            gate_input = _gather_gate_input(session, ticker, snapshot_date)
            if gate_input is None:
                # raw_snapshotが無い銘柄は「判定不能」であって「合格」ではない
                # (実データ検証で発見。ACGL等)。universe_refresh.py が書く
                # スタブ行を必ずここで上書きし、未収集銘柄を対象外に落とす。
                counts["no_data"] += 1
                _upsert_snapshot(session, snapshot_date, ticker.id, False, "no_raw_data")
            else:
                result = evaluate_gates(gate_input, universe_config)
                passed = result.passed
                reason = ",".join(result.reasons) if result.reasons else None
                counts["included" if passed else "excluded"] += 1
                _upsert_snapshot(session, snapshot_date, ticker.id, passed, reason)
        return True
    except IntegrityError:
        logger.warning(
            "apply_gates: ticker_id=%s disappeared between the existence check and "
            "the insert (FK violation) -- skipping this ticker only",
            ticker_id,
        )
        return False


def apply_gates(
    snapshot_date: date | None = None,
    universe_config: UniverseConfig | None = None,
) -> dict[str, int | list[int]]:
    snapshot_date = snapshot_date or utc_today()
    universe_config = universe_config or load_universe_config()

    counts: dict[str, int] = {
        "included": 0,
        "excluded": 0,
        "no_data": 0,
        "delisted": 0,
        "benchmark": 0,
        # A-3:黙って無視せず件数を明示する(監査§10.4修正案6)。
        "skipped_missing_tickers": 0,
    }
    missing_ticker_ids: list[int] = []

    # 廃止済み(delisted_at)の銘柄も対象に含めて明示的に included=False を
    # 書き込む。以前はクエリ自体から除外していたため、週次の
    # `refresh_universe` がシンボルディレクトリ由来で書いたスタブ行
    # (included=True)が上書きされずに残り、廃止銘柄がそのまま
    # `run_scoring` の対象になる抜け穴があった。14.3(生存バイアス対策)
    # の観点でも「その日に廃止扱いだった」ことを記録するほうが正しい。
    #
    # A-3:ここではidだけを読む(Tickerオブジェクトを長時間セッションに
    # 保持しない)。実際に処理する瞬間の生存確認は `_process_one_ticker` が
    # バッチごとの新しいsessionで行う。
    with session_scope() as session:
        ticker_ids = [
            row.id for row in session.query(Ticker.id).filter(Ticker.market == universe_config.market).all()
        ]

    for batch_start in range(0, len(ticker_ids), _GATE_COMMIT_BATCH_SIZE):
        batch = ticker_ids[batch_start : batch_start + _GATE_COMMIT_BATCH_SIZE]
        with session_scope() as session:
            for ticker_id in batch:
                if not _process_one_ticker(session, ticker_id, snapshot_date, universe_config, counts):
                    counts["skipped_missing_tickers"] += 1
                    missing_ticker_ids.append(ticker_id)

    if missing_ticker_ids:
        logger.warning(
            "apply_gates: %d ticker(s) disappeared mid-run and were skipped (sample: %s)",
            len(missing_ticker_ids),
            missing_ticker_ids[:20],
        )

    result: dict[str, int | list[int]] = dict(counts)
    if missing_ticker_ids:
        # 一部IDだけを残す(監査§10.4修正案6「件数と一部IDを記録する」)。
        # 全件を残さないのは、大量欠落時にstage結果(JSON列)を肥大させない
        # ため——傾向を掴むには先頭20件で十分で、全量はログ(上のwarning)にある。
        result["skipped_missing_ticker_ids_sample"] = missing_ticker_ids[:20]
    return result
