"""四半期モニタリングとレッドフラグの状態遷移をアラートとして記録するバッチ
(30.7.4)。

レッドフラグ(30.4)や監視指標(30.7.3)は毎日**再評価すれば同じ結論**が出る。
それでも `alerts` に行として保存するのは、**「いつ初めて点灯したか」がそれ
自体で情報**だから——決算の翌日に点いたのか、3か月前から点いていたのかで、
対応は変わる。導出結果ではなく状態遷移を記録するのがこのバッチの役割。

**既に同じ `(ticker_id, code)` で未解消(`acknowledged_at IS NULL`)の
アラートがあれば新規行を作らない。** 毎日同じアラートが積み上がると、通知が
無意味になる(アラート疲れ。18.7・E-2で一度学んだ教訓)。
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from autoscreener.batch.collect_filings import select_tracked_tickers
from autoscreener.config import load_monitoring_config, load_positions_config
from autoscreener.dates import utc_today
from autoscreener.db.models import Alert, Filing, PriceSnapshot, RawSnapshot, Ticker
from autoscreener.db.session import session_scope
from autoscreener.research.notes import load_note
from autoscreener.screening.monitoring_metrics import MonitoringThresholds, evaluate_monitoring
from autoscreener.screening.red_flags import evaluate_red_flags, filing_to_view

logger = logging.getLogger(__name__)

# 30.3.4と同じ上限を流用する(追跡対象の規模がSEC収集と揃っていれば十分)。
_DEFAULT_TICKER_LIMIT = 300


def _target_tickers(session: Session) -> list[Ticker]:
    positions = load_positions_config()
    open_symbols = {p.ticker.upper() for p in positions.positions if p.closed_on is None}
    position_tickers = (
        session.query(Ticker).filter(Ticker.symbol.in_(open_symbols)).all() if open_symbols else []
    )
    tracked = select_tracked_tickers(session, limit=_DEFAULT_TICKER_LIMIT)
    by_id = {t.id: t for t in [*position_tickers, *tracked]}
    return list(by_id.values())


def _share_counts(session: Session, ticker_id: int) -> list[tuple]:
    rows = (
        session.query(PriceSnapshot.trade_date, PriceSnapshot.shares_outstanding)
        .filter(PriceSnapshot.ticker_id == ticker_id, PriceSnapshot.shares_outstanding.isnot(None))
        .order_by(PriceSnapshot.trade_date.asc())
        .all()
    )
    return [(d, s) for d, s in rows]


def _record_alert(session: Session, ticker_id: int, code: str, severity: str, source: str, today, detail: dict) -> bool:
    """未解消の同一 (ticker_id, code) アラートが無ければ新規行を作る。戻り値は新規作成したか。"""
    existing = (
        session.query(Alert)
        .filter(Alert.ticker_id == ticker_id, Alert.code == code, Alert.acknowledged_at.is_(None))
        .first()
    )
    if existing is not None:
        return False
    session.add(
        Alert(
            ticker_id=ticker_id,
            code=code,
            severity=severity,
            source=source,
            triggered_on=today,
            detail=detail,
        )
    )
    return True


def run_monitoring(as_of=None) -> dict[str, int]:
    """保有・追跡銘柄を評価し、**新規に点灯したものだけ** alerts に書く。

    戻り値は {"tickers": n, "new_alerts": n, "already_open": n}。
    """
    today = as_of or utc_today()
    thresholds_config = load_monitoring_config()
    thresholds = MonitoringThresholds(
        revenue_growth_deceleration_quarters=thresholds_config.revenue_growth_deceleration_quarters,
        gross_margin_decline_quarters=thresholds_config.gross_margin_decline_quarters,
        share_count_annual_growth_ceiling=thresholds_config.share_count_annual_growth_ceiling,
        cash_runway_floor_months=thresholds_config.cash_runway_floor_months,
    )

    counts = {"tickers": 0, "new_alerts": 0, "already_open": 0}

    # J-8(docs/investment_decision_gap_2026-08-29.md):保有銘柄の取得単価。達成倍率が
    # ノートの `exit_plan.trim_rule` の閾値を超えたら info アラートを1回だけ立てる。
    open_positions_by_symbol = {
        p.ticker.upper(): p for p in load_positions_config().positions if p.closed_on is None
    }

    with session_scope() as session:
        tickers = _target_tickers(session)
        for ticker in tickers:
            counts["tickers"] += 1
            raw = (
                session.query(RawSnapshot)
                .filter_by(ticker_id=ticker.id)
                .order_by(RawSnapshot.snapshot_date.desc())
                .first()
            )
            payload = raw.payload if raw else {}
            info = payload.get("info") or {}
            metrics = evaluate_monitoring(
                payload.get("quarterly_income_stmt") or {},
                payload.get("quarterly_cash_flow") or {},
                info.get("totalCash"),
                _share_counts(session, ticker.id),
                thresholds,
            )
            triggered_codes: set[str] = set()
            for metric in metrics:
                if not metric.triggered:
                    continue
                triggered_codes.add(metric.code)
                created = _record_alert(
                    session,
                    ticker.id,
                    metric.code,
                    "warning",
                    "metric",
                    today,
                    {"label": metric.label, "detail": metric.detail, "current_value": metric.current_value},
                )
                counts["new_alerts" if created else "already_open"] += 1

            filing_rows = session.query(Filing).filter_by(ticker_id=ticker.id).all()
            red_flags = evaluate_red_flags([filing_to_view(f) for f in filing_rows], as_of=today)
            for flag in red_flags:
                created = _record_alert(
                    session,
                    ticker.id,
                    flag.code,
                    flag.severity,
                    "red_flag",
                    today,
                    {"detail": flag.detail, "document_url": flag.document_url},
                )
                counts["new_alerts" if created else "already_open"] += 1

            # 30.7.4:プレモーテム指標との接続——ノートの premortem[].indicator が
            # monitoring_metrics のコードと一致する場合、その指標が点灯したら
            # source="premortem" として**別のアラート**を立てる。「自分が事前に
            # 決めた反証条件が点灯した」は、汎用の閾値超過より重い。
            try:
                note = load_note(ticker.symbol)
            except Exception:
                note = None
            if note is not None:
                for item in note.front_matter.get("premortem") or []:
                    indicator = item.get("indicator") if isinstance(item, dict) else None
                    if indicator in triggered_codes:
                        created = _record_alert(
                            session,
                            ticker.id,
                            f"premortem_{indicator}",
                            "warning",
                            "premortem",
                            today,
                            {"cause": item.get("cause"), "indicator": indicator, "detail": item.get("detail")},
                        )
                        counts["new_alerts" if created else "already_open"] += 1

            # J-8:利食い計画の閾値到達。**売却シグナルではない**——価格に関係なく
            # 判断をやり直す合図。閾値ごとに別コードにするので、達成倍率が上がれば
            # 次の段が別途1回だけ点く(同じ閾値では `_record_alert` が重複を抑止)。
            position = open_positions_by_symbol.get(ticker.symbol.upper())
            current_price = info.get("currentPrice") or info.get("regularMarketPrice")
            if position is not None and current_price is not None and position.cost_basis_usd > 0:
                achieved_moic = current_price / position.cost_basis_usd
                exit_plan = note.front_matter.get("exit_plan") if note is not None else None
                exit_plan = exit_plan if isinstance(exit_plan, dict) else {}
                for rule in exit_plan.get("trim_rule") or []:
                    if not isinstance(rule, dict) or rule.get("at_moic") is None:
                        continue
                    at_moic = float(rule["at_moic"])
                    if achieved_moic >= at_moic:
                        created = _record_alert(
                            session,
                            ticker.id,
                            f"trim_threshold_{at_moic:g}",
                            "info",
                            "exit_plan",
                            today,
                            {
                                "at_moic": at_moic,
                                "achieved_moic": round(achieved_moic, 3),
                                "action": rule.get("action"),
                            },
                        )
                        counts["new_alerts" if created else "already_open"] += 1

    return counts
