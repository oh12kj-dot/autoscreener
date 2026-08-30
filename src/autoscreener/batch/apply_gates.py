"""除外ゲート(15.2)を実データに適用し、当日の universe_snapshots を確定する。

`tickers`・最新の `raw_snapshots`・`price_snapshots` から `GateInput` を組み立て、
`screening.exclusion_gates.evaluate_gates` で判定する。14.3の生存バイアス対策
どおり、除外された銘柄も `included=False` として記録し、マスタから削除しない。
"""

from __future__ import annotations

import statistics
from datetime import date

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

# 流動性中央値の算出に使う直近営業日数(15.2は「中央値」とのみ規定。
# 3〜4ヶ月相当の90営業日を採用し、四半期単位の実態に近づける)
_LIQUIDITY_WINDOW_TRADING_DAYS = 90


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


def apply_gates(
    snapshot_date: date | None = None,
    universe_config: UniverseConfig | None = None,
) -> dict[str, int]:
    snapshot_date = snapshot_date or utc_today()
    universe_config = universe_config or load_universe_config()

    counts = {"included": 0, "excluded": 0, "no_data": 0, "delisted": 0, "benchmark": 0}

    with session_scope() as session:
        # 廃止済み(delisted_at)の銘柄も対象に含めて明示的に included=False を
        # 書き込む。以前はクエリ自体から除外していたため、週次の
        # `refresh_universe` がシンボルディレクトリ由来で書いたスタブ行
        # (included=True)が上書きされずに残り、廃止銘柄がそのまま
        # `run_scoring` の対象になる抜け穴があった。14.3(生存バイアス対策)
        # の観点でも「その日に廃止扱いだった」ことを記録するほうが正しい。
        tickers = session.query(Ticker).filter(Ticker.market == universe_config.market).all()

        for ticker in tickers:
            # D-4:ベンチマークETFは価格を収集するがランキングには混ぜない。
            if ticker.is_benchmark:
                counts["benchmark"] += 1
                _upsert_snapshot(session, snapshot_date, ticker.id, False, "benchmark")
                continue

            if ticker.delisted_at is not None:
                passed, reason = False, "delisted"
                counts["delisted"] += 1
                _upsert_snapshot(session, snapshot_date, ticker.id, passed, reason)
                continue

            gate_input = _gather_gate_input(session, ticker, snapshot_date)
            if gate_input is None:
                # raw_snapshotが無い銘柄は「判定不能」であって「合格」ではない
                # (実データ検証で発見。ACGL等)。universe_refresh.py が書く
                # スタブ行を必ずここで上書きし、未収集銘柄を対象外に落とす。
                passed, reason = False, "no_raw_data"
                counts["no_data"] += 1
            else:
                result = evaluate_gates(gate_input, universe_config)
                passed, reason = result.passed, (",".join(result.reasons) if result.reasons else None)
                counts["included" if passed else "excluded"] += 1

            _upsert_snapshot(session, snapshot_date, ticker.id, passed, reason)

    return counts
