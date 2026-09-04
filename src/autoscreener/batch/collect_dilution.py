"""希薄化キャパシティの収集バッチ(K-4)。

S-3/S-3ASR/424B5 の本文からシェルフ・ATM残枠を、10-Q(`filing_sections` の
`item7` があれば優先、無ければ `filings.document_url` から)からATM消化実績を、
XBRLから未行使オプション比率を集め、`dilution_capacity` へ upsert する。

**`evidence` に「どのaccessionのどの文からこの数字を取ったか」を必ず残す。**
数字だけ出して根拠を出さないと、人間は結局S-3/10-Qの原本を読み直すことになり、
自動化した意味が消える(`dilution_outlook.py` と同じ設計思想)。

原則3(2026-08 策定、K-4):このバッチが書く `dilution_capacity` は v4 の
`evaluate_gates` にも v4 の `scoring/`(`scoring/engine.py` 系の除外ゲート・
実現時価総額倍率モデル本体)にも一切読まれない。表示・チェックリストのみが読者。
**v4 に対しては今もこの原則のまま変えていない。**

2026-09-03 追記(model_v5_phase6、Issue #3 §12、ユーザー判断で確定):
上記の「一切読まれない」を、以降は **v4 の evaluate_gates / scoring/ に限定して
解釈する**。v5 (`scoring/v5/`) は v4 とは別の独立した shadow challenger であり、
Issue #3 の主旨(「v4 を聖域にしない」)に沿って新しい情報源を取り込む対象として
設計されている。`scoring/v5/balance_sheet.py` の `future_dilution_capacity`
signal は `dilution_capacity` を読む——ただし v4 の `dilution_drag` や v5
Phase 4 の `per_share_economics` と三重計上しないよう、接続先は
「将来の希薄化株式数 → 1株価値の平均倍率」のみに限定し、上限で抑える
(docs/model_v5_phase6_tail_macro_competing_risk_2026-09-03.md 参照)。
原則3自体を黙って書き換えたのではなく、v4 側の適用は変えず、v5 という新しい
読者を追加したという経緯を、ここに残す。

例外は銘柄単位で握ってログに残し、次の銘柄へ進む(1銘柄のS-3が読めなくても
バッチ全体を止めない)。
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy.orm import Session

from autoscreener.batch.collect_filings import select_tracked_tickers
from autoscreener.batch.parallel_runner import run_parallel_tickers
from autoscreener.collectors.edgar_client import EdgarClient
from autoscreener.collectors.errors import CollectionError
from autoscreener.config import get_settings, load_edgar_config
from autoscreener.dates import utc_today
from autoscreener.db.models import DilutionCapacity, Filing, FilingSection, PriceSnapshot, Ticker
from autoscreener.db.session import session_scope
from autoscreener.screening.dilution_capacity import (
    detect_variable_conversion,
    options_ratio,
    parse_atm_capacity,
    parse_shelf_capacity,
)

logger = logging.getLogger(__name__)

_DEFAULT_LIMIT = 300
_LOOKBACK_YEARS = 3
_SHELF_FORMS = ("S-3", "S-3ASR", "424B5")
# 30.5:us-gaap タクソノミの未行使オプション数タグ。
_OPTIONS_TAG = "ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsOutstandingNumber"


def _recent_filings(
    session: Session, ticker_id: int, forms: tuple[str, ...], *, as_of: datetime.date
) -> list[Filing]:
    cutoff = as_of - datetime.timedelta(days=365 * _LOOKBACK_YEARS)
    return (
        session.query(Filing)
        .filter(
            Filing.ticker_id == ticker_id,
            Filing.form.in_(forms),
            Filing.filed_date >= cutoff,
            Filing.filed_date <= as_of,
        )
        .order_by(Filing.filed_date.desc())
        .all()
    )


def _latest_shares_outstanding(session: Session, ticker_id: int) -> float | None:
    row = (
        session.query(PriceSnapshot.shares_outstanding)
        .filter(PriceSnapshot.ticker_id == ticker_id, PriceSnapshot.shares_outstanding.isnot(None))
        .order_by(PriceSnapshot.trade_date.desc())
        .first()
    )
    return float(row[0]) if row is not None and row[0] is not None else None


def _options_from_xbrl(
    client: EdgarClient, cik: str, shares_outstanding: float | None
) -> tuple[float | None, float | None, dict | None]:
    """XBRLの未行使オプション数を取り比率を計算する。取れなければ (None, None, None)。

    `companyconcept` の `units.shares` は事実の配列(instant値、`end` に基準日)。
    最新(`end` が最大)のものを採用する——文字列比較でよいのはISO8601形式のため。
    """
    if shares_outstanding is None:
        return None, None, None
    try:
        payload = client.fetch_company_concept(cik, "us-gaap", _OPTIONS_TAG)
    except CollectionError:
        return None, None, None

    units = ((payload.get("units") or {}).get("shares")) or []
    if not units:
        return None, None, None
    latest = max(units, key=lambda u: u.get("end") or "")
    shares = latest.get("val")
    if shares is None:
        return None, None, None

    ratio = options_ratio(float(shares), shares_outstanding)
    evidence = {
        "tag": _OPTIONS_TAG,
        "end": latest.get("end"),
        "accn": latest.get("accn"),
        "val": shares,
    }
    return float(shares), ratio, evidence


def _collect_one(session: Session, client: EdgarClient, ticker: Ticker, *, as_of: datetime.date) -> bool:
    """1銘柄ぶんの `dilution_capacity` を組み立てて upsert する。書き込んだら True。"""
    evidence: dict = {}
    shelf_registered: float | None = None
    shelf_remaining: float | None = None
    atm_authorized: float | None = None
    atm_remaining: float | None = None
    has_variable_conversion: bool | None = None
    source_form: str | None = None
    source_accession: str | None = None

    for filing in _recent_filings(session, ticker.id, _SHELF_FORMS, as_of=as_of):
        if not filing.document_url:
            continue
        try:
            text, _truncated = client.fetch_document_text(filing.document_url)
        except CollectionError:
            logger.warning(
                "%s: failed to fetch %s (%s)", ticker.symbol, filing.accession_number, filing.form,
                exc_info=True,
            )
            continue

        if shelf_registered is None:
            shelf = parse_shelf_capacity(text)
            if shelf is not None:
                shelf_registered = shelf.amount_usd
                # 表紙の総登録額を起点とする。ATM側で消化実績が取れればそちらを
                # 差し引いた値に更新する(下のATM消化ブロック参照)。
                shelf_remaining = shelf.amount_usd
                source_form = filing.form
                source_accession = filing.accession_number
                evidence["shelf"] = {
                    "accession_number": filing.accession_number,
                    "form": filing.form,
                    "text": shelf.evidence,
                }

        if atm_authorized is None:
            atm = parse_atm_capacity(text)
            if atm is not None and atm.authorized_usd is not None:
                atm_authorized = atm.authorized_usd
                if atm.remaining_usd is not None:
                    atm_remaining = atm.remaining_usd
                evidence["atm_capacity"] = {
                    "accession_number": filing.accession_number,
                    "form": filing.form,
                    "text": atm.evidence.get("authorized", ""),
                }

        if has_variable_conversion is None:
            finding = detect_variable_conversion(text)
            if finding is not None:
                has_variable_conversion = True
                evidence["variable_conversion"] = {
                    "accession_number": filing.accession_number,
                    "form": filing.form,
                    "pattern": finding.matched_pattern,
                    "text": finding.evidence,
                }

        if shelf_registered is not None and atm_authorized is not None and has_variable_conversion is not None:
            break

    # 10-Q:item7(MD&A)があれば優先。無ければ主文書から取る(document_url経由)。
    quarterly_text: str | None = None
    quarterly_accession: str | None = None
    quarterly_form: str | None = None
    quarterly_source_kind: str | None = None

    item7 = (
        session.query(FilingSection)
        .filter(FilingSection.ticker_id == ticker.id, FilingSection.section == "item7")
        .order_by(FilingSection.filed_date.desc())
        .first()
    )
    if item7 is not None:
        quarterly_text = item7.text
        quarterly_accession = item7.accession_number
        quarterly_form = item7.form
        quarterly_source_kind = "filing_section:item7"
    else:
        cutoff = as_of - datetime.timedelta(days=365 * _LOOKBACK_YEARS)
        latest_10q = (
            session.query(Filing)
            .filter(
                Filing.ticker_id == ticker.id,
                Filing.form == "10-Q",
                Filing.filed_date >= cutoff,
                Filing.filed_date <= as_of,
            )
            .order_by(Filing.filed_date.desc())
            .first()
        )
        if latest_10q is not None and latest_10q.document_url:
            try:
                text, _truncated = client.fetch_document_text(latest_10q.document_url)
                quarterly_text = text
                quarterly_accession = latest_10q.accession_number
                quarterly_form = latest_10q.form
                quarterly_source_kind = "filing_document"
            except CollectionError:
                logger.warning("%s: failed to fetch latest 10-Q text", ticker.symbol, exc_info=True)

    if quarterly_text is not None:
        atm = parse_atm_capacity(quarterly_text)
        if atm is not None:
            if atm.sold_usd is not None and atm_authorized is not None:
                atm_remaining = atm_authorized - atm.sold_usd
            if "sold" in atm.evidence:
                evidence["atm_consumed"] = {
                    "accession_number": quarterly_accession,
                    "form": quarterly_form,
                    "source": quarterly_source_kind,
                    "text": atm.evidence["sold"],
                }
        if has_variable_conversion is None:
            finding = detect_variable_conversion(quarterly_text)
            if finding is not None:
                has_variable_conversion = True
                evidence["variable_conversion"] = {
                    "accession_number": quarterly_accession,
                    "form": quarterly_form,
                    "pattern": finding.matched_pattern,
                    "text": finding.evidence,
                }

    shares_outstanding = _latest_shares_outstanding(session, ticker.id)
    unexercised_shares: float | None = None
    unexercised_ratio: float | None = None
    if ticker.cik:
        unexercised_shares, unexercised_ratio, options_evidence = _options_from_xbrl(
            client, ticker.cik, shares_outstanding
        )
        if options_evidence is not None:
            evidence["options"] = options_evidence

    if not evidence:
        # S-3/424B5/10-Qが期間内に無い、または全て解析失敗。空行を書いても
        # 意味が無いので upsert しない(「未入力」と「該当なし」を混同しない —
        # dilution_outlook.py 30.6.1 と同じ配慮:行が無いことが「未入力」を表す)。
        return False

    row = (
        session.query(DilutionCapacity)
        .filter_by(ticker_id=ticker.id, as_of_date=as_of)
        .one_or_none()
    )
    if row is None:
        row = DilutionCapacity(ticker_id=ticker.id, as_of_date=as_of, collected_on=as_of)
        session.add(row)

    row.shelf_registered_usd = shelf_registered
    row.shelf_remaining_usd = shelf_remaining
    row.atm_authorized_usd = atm_authorized
    row.atm_remaining_usd = atm_remaining
    row.has_variable_conversion = has_variable_conversion
    row.unexercised_options_shares = unexercised_shares
    row.unexercised_options_ratio = unexercised_ratio
    row.source_form = source_form
    row.source_accession = source_accession
    row.evidence = evidence
    row.collected_on = as_of
    return True


def _process_ticker(session: Session, ticker_id: int, client: EdgarClient, as_of: datetime.date) -> dict[str, int]:
    """1銘柄ぶんの希薄化キャパシティ収集を専用セッションで行う(S-5:並列化の
    ため銘柄ごとに独立したセッションを使う)。"""
    local = {"tickers": 0, "written": 0, "skipped_no_cik": 0, "failures": 0}
    ticker = session.get(Ticker, ticker_id)
    if ticker is None:
        return local
    if not ticker.cik:
        local["skipped_no_cik"] += 1
        return local
    local["tickers"] += 1
    try:
        written = _collect_one(session, client, ticker, as_of=as_of)
    except Exception:
        logger.exception("%s: dilution capacity collection failed", ticker.symbol)
        local["failures"] += 1
        return local
    if written:
        local["written"] += 1
    return local


def collect_dilution(
    symbols: list[str] | None = None, *, limit: int = _DEFAULT_LIMIT, max_workers: int | None = None
) -> dict[str, int]:
    """追跡対象銘柄の希薄化キャパシティを収集し `dilution_capacity` へ upsert する。

    S-5(daily_pipeline_throughput_plan_2026-09-04):以前は`for ticker in
    tickers:`の完全な逐次ループだった。銘柄ごとに独立したセッションを開いて
    共有`sec`リミッター配下で並列化する——上限自体は動かさない。

    戻り値は {"tickers": n, "written": n, "skipped_no_cik": n, "failures": n}。
    1銘柄の失敗で全体を止めない(collect_filings.collect_filings と同じ方針)。
    """
    settings = get_settings()
    config = load_edgar_config()
    zeros = {"tickers": 0, "written": 0, "skipped_no_cik": 0, "failures": 0}
    if not config.enabled:
        logger.info("edgar collection disabled by config")
        return zeros

    client = EdgarClient(config, settings.edgar_user_agent or "")
    today = utc_today()

    with session_scope() as session:
        if symbols:
            ticker_ids = [
                row[0]
                for row in session.query(Ticker.id).filter(Ticker.symbol.in_([s.upper() for s in symbols])).all()
            ]
        else:
            ticker_ids = [t.id for t in select_tracked_tickers(session, limit=limit)]

    counts = run_parallel_tickers(
        ticker_ids,
        lambda session, ticker_id: _process_ticker(session, ticker_id, client, today),
        max_workers=max_workers or config.max_workers,
    )
    return {**zeros, **counts}
