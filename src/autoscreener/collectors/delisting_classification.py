"""上場廃止イベントの原因分類(docs/delisting_label_backfill_2026-09-04.md)。

**背景**:`delisting_events` の94件は全件 `event_type='unknown'`・決済額欠落
(2026-09-04調査で実測)。RACR統合再設計の WP-F(competing-risk・permanent loss
モデル)は原因区分と回収額が無いと学習できない。本モジュールはその原因分類を
**既にDBにある証拠だけから**試みる——新規のEDGAR一括取得はしない(調査時点の
制約:テストは外部通信を遮断し、dev DBへの書き込みも禁止されている)。

**taxonomyは新規に作らない。** `backtest/runner.py:472-489`(実現リターン計算)
と `api/routes.py` の M&A履歴エンドポイントが既に
`unknown` / `cash_acquisition` / `stock_acquisition` / `bankruptcy` /
`liquidation` / `exchange_transfer` という6値を消費している。ここへ独自の
値(例えば `acquisition` や `going_private`)を書き込むと、そのコード達は
知らない値を無視するか誤って `unknown` 相当の分岐に落ちる。`EVENT_TYPES` は
その6値と完全一致させる。

**`cash_acquisition` / `stock_acquisition` を自動では絶対に付けない理由**
(`backtest/runner.py:479-484` を読んで確認した実際の挙動):
```
if event_type == "cash_acquisition" and settlement is not None:
    return (settlement + dividends) / entry - 1, "cash_acquisition"
if event_type in {"cash_acquisition", "stock_acquisition"}:
    return -1.0, "unknown_delisting"          # settlement が無いと "-100%" 扱い!
```
つまり `settlement_value_per_share` を伴わずに `cash_acquisition`/
`stock_acquisition` を書き込むと、実際には利益で終わったはずの買収イグジットが
**丸ごと-100%の損失として実現リターンに算入される**。8-K Item 2.01(取得完了)
だけでは現金/株式のどちらの対価かも、金額もわからない——決済額を推測で埋める
くらいなら`unknown`のままのほうが安全、という監査 §5.3 の原則そのものが、既存
コードのこの挙動によって具体的に裏付けられる。したがって本モジュールは
Item 2.01/Schedule 13E-3/DEFM14Aの証拠を見つけても、決済額が無い限り
`event_type` は `unknown` のまま返す(証拠自体はrationaleに残す——人間が
決済額を追加で調べれば手動で確定できるように)。

**`exchange_transfer` も自動では付けない理由**:同じ `runner.py` で
`exchange_transfer` は `unknown` と全く同じ分岐(`-1.0, "unknown_delisting"`)
に落ちる。8-K Item 3.01(上場基準抵触通知)は「通知が出た」事実は語るが、
その後カービングされたのか本当に強制上場廃止まで進んだのかは別問題であり、
`exchange_transfer` を付けても`unknown`と数値上の扱いは変わらない一方、
「原因が分かった」という誤った印象だけを残す。付ける実益が無い分類は
避ける。

**`bankruptcy` だけは自動分類する。** 8-K Item 1.03(Bankruptcy or
Receivership)はSEC規則上、破産法上の手続き開始そのものを指す一次的で
明確な事象であり、`runner.py` 側も決済額欠落時は "recovery=0" という既存の
(本モジュールが新たに導入したのではない)保守的な扱いを既にしている。

**中心的な発見(2026-09-04調査、詳細は docs/delisting_label_backfill_2026-09-04.md)**:
1. 94件は全て `source='ticker_master_backfill'`。SECフルインデックスのForm 25/15
   走査(`register_delisting_events`, `source='sec_full_index'`)が実際に書いた
   行は0件——EDGARの一次証拠(提出フォーム・accession番号)を一度も持ったこと
   がない。`event_date` の実体は `tickers.delisted_at`(yfinanceの404等の失敗
   シグナル)であり、SECの提出日ではない。
2. `batch/collect_filings.py` と `batch/run_daily_collection.py` は
   `delisted_at IS NOT NULL` を明示的に日次収集から除外する。この結果、原因を
   語るフォーム(Form 25/15そのもの、8-K Item 1.03/2.01/3.01、DEFM14A、
   SC 13E3)は廃止フラグが立った瞬間から収集が止まるため、後から見に行っても
   ほぼ存在しない。94件のうち `filings.ticker_id` に1行でもあるのは1件
   (LXU)だけ(`cik` 経由のJOINでは2件ヒットするが、うち1件は次の理由で誤帰属)。
3. **CIK共有の罠**:`filings` は `cik` 列も持つが、`ticker_id` で厳密に
   紐付いた行だけが「そのティッカー自身の提出書類」である。倒産再編後の
   ワラント等(実例:CIK 0000098222 は現役銘柄 `TDW` と廃止済み `TDGMW` の
   両方が共有)では、`cik` だけでJOINすると無関係な現役銘柄の書類を廃止銘柄の
   証拠として誤帰属する。本モジュールは **`ticker_id` でのみ** 証拠を引き、
   同じCIKを持つ他の現役銘柄が存在する場合は `ambiguous_shared_cik=True` で
   警告する。
4. 唯一 `filings.ticker_id` に自身の行を持つLXUも、保存されているフォームは
   廃止前の通常8-K群(Item 7.01/8.01/5.02等)のみで、原因を語るItem
   (1.03/2.01/3.01)もForm 25/15も無い。
5. ネットワーク越しに取得できたとしても `EdgarClient.fetch_filings` は
   `submissions` APIの `filings.recent`(直近1000件・目安1年分)しか読まない
   (`edgar_client.py:216-223`)。提出頻度の高い発行体では、廃止から時間が
   経つほど当該Form 25/15自体が `recent` ウィンドウから外れて取得不能になる。

**結論**:現在DBにある証拠だけでは94件中 **0件** が分類可能(確認済み)。
本モジュールは将来 (a) `filings` が対象銘柄について収集され次第、自動的に
分類が効くようにする受け皿、(b) 分類ロジック自体を単体テストできる実装、を
提供する。**証拠が無ければ、または安全に確定できなければ `unknown` のまま。**
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
from collections.abc import Sequence

from sqlalchemy.orm import Session

from autoscreener.db.models import DelistingEvent, Filing, Ticker

logger = logging.getLogger(__name__)

# delisting_events.event_type に許される値。`backtest/runner.py` と
# `api/routes.py` の M&A履歴エンドポイントが既に消費している6値と完全一致
# させること(migration の CHECK 制約もこれと一致させる)。新しい値を足すには
# その2箇所の消費ロジックも同時に更新する必要がある。
EVENT_TYPES: tuple[str, ...] = (
    "unknown",
    "cash_acquisition",
    "stock_acquisition",
    "bankruptcy",
    "liquidation",
    "exchange_transfer",
)

# 分類の根拠として意味を持つフォーム(SEC公表のフォーム定義に基づく——特定企業の
# 事実を推測するものではない)。
#   25 / 25-NSE          … 取引所発の上場廃止届
#   15-12B / 15-12G       … 登録抹消(報告義務終了)。原因は単独では語らない。
#   15F-12B / 15F-12G     … 外国民間発行体の登録抹消
_DEREGISTRATION_FORMS: frozenset[str] = frozenset(
    {"25", "25-NSE", "15-12B", "15-12G", "15F-12B", "15F-12G"}
)
# 8-K Item番号(Form 8-K instructionsで定義される項目)。
#   1.03 … Bankruptcy or Receivership
#   2.01 … Completion of Acquisition or Disposition of Assets
#   3.01 … Notice of Delisting or Failure to Satisfy a Continued Listing Rule
_ITEM_BANKRUPTCY = "1.03"
_ITEM_ACQUISITION_COMPLETION = "2.01"
_ITEM_DELISTING_NOTICE = "3.01"
# Going-private取引の届出書(SEC Rule 13e-3)。
_GOING_PRIVATE_FORMS: frozenset[str] = frozenset({"SC 13E3", "SC 13E-3"})
# 合併/買収の委任状勧誘書類(現行 `collect_filings.TRACKED_FORMS` には
# 'DEFM14A'/'SC 13E3' 自体が入っていない点に注意——将来これらを追跡対象に
# 加えないと、廃止前に収集していても証拠として残らない)。
_MERGER_PROXY_FORMS: frozenset[str] = frozenset({"DEFM14A", "DEF 14A"})

# 証拠探索の既定ウィンドウ。`event_date` は `delisted_at`(データベンダーの
# 障害シグナル)由来で誤差を持ちうる一方、8-K(破綻協議・合併合意)はそれより
# 何か月も前に出ることがあるため、片側を広めに取る。
DEFAULT_LOOKBACK_DAYS = 730
DEFAULT_LOOKAHEAD_DAYS = 60


@dataclasses.dataclass(frozen=True)
class FilingEvidence:
    """分類ロジックへ渡す1件分の提出書類証拠。"""

    form: str
    filed_date: datetime.date
    items: tuple[str, ...]
    document_url: str | None
    accession_number: str | None


@dataclasses.dataclass(frozen=True)
class Classification:
    """分類結果。`event_type='unknown'` のときも証拠(`evidence_*`)は残ることが
    ある——「安全に確定できないだけで手がかりはある」ことを人間に伝えるため。
    決済額を要する `cash_acquisition`/`stock_acquisition` は自動では出さない
    (モジュールdocstring参照)。"""

    event_type: str
    confidence: str  # "high" | "medium" | "unknown"
    rationale: str
    evidence_form: str | None = None
    evidence_url: str | None = None
    evidence_filed_date: datetime.date | None = None


_UNKNOWN_NO_EVIDENCE = Classification(
    event_type="unknown",
    confidence="unknown",
    rationale="no delisting-relevant filings found for this ticker_id in the evidence window",
)


def classify_from_filings(evidence: Sequence[FilingEvidence]) -> Classification:
    """既知の証拠だけから原因を分類する純粋関数。

    `bankruptcy` 以外は自動では確定させない——理由はモジュールdocstring
    (`cash_acquisition`/`stock_acquisition` は決済額を伴わないと downstream の
    `backtest/runner.py` に "-100%" として誤って学習される。`exchange_transfer`
    は同コードで `unknown` と数値上の扱いが同じで、確定させる実益が無い)。
    証拠自体は見つかった中で最も強いものを rationale/evidence_* に残す。
    """
    if not evidence:
        return _UNKNOWN_NO_EVIDENCE

    forms = {e.form for e in evidence}
    has_deregistration = bool(forms & _DEREGISTRATION_FORMS)

    def _find(item: str) -> FilingEvidence | None:
        for e in evidence:
            if item in e.items:
                return e
        return None

    def _find_form(candidates: frozenset[str]) -> FilingEvidence | None:
        for e in evidence:
            if e.form in candidates:
                return e
        return None

    bankruptcy_filing = _find(_ITEM_BANKRUPTCY)
    if bankruptcy_filing is not None:
        return Classification(
            event_type="bankruptcy",
            confidence="high" if has_deregistration else "medium",
            rationale=(
                "8-K Item 1.03 (Bankruptcy or Receivership) filed"
                + (
                    "; corroborated by a deregistration filing (Form 25/15) in the same window"
                    if has_deregistration
                    else "; no deregistration filing found in window, cause inferred from the 8-K alone"
                )
            ),
            evidence_form=bankruptcy_filing.form,
            evidence_url=bankruptcy_filing.document_url,
            evidence_filed_date=bankruptcy_filing.filed_date,
        )

    going_private_filing = _find_form(_GOING_PRIVATE_FORMS)
    if going_private_filing is not None:
        return Classification(
            event_type="unknown",
            confidence="unknown",
            rationale=(
                "Schedule 13E-3 (going-private transaction) on file, but the cash/stock "
                "consideration and per-share settlement value are not in this database — "
                "asserting cash_acquisition/stock_acquisition without a settlement value would "
                "be read by backtest/runner.py as a -100% loss, which a going-private buyout "
                "usually is not. Needs a manual settlement lookup before this can be classified."
            ),
            evidence_form=going_private_filing.form,
            evidence_url=going_private_filing.document_url,
            evidence_filed_date=going_private_filing.filed_date,
        )

    acquisition_item = _find(_ITEM_ACQUISITION_COMPLETION)
    if acquisition_item is not None and has_deregistration:
        merger_proxy = _find_form(_MERGER_PROXY_FORMS)
        rationale = (
            "8-K Item 2.01 (Completion of Acquisition) corroborated by a deregistration filing "
            "(Form 25/15), but neither states whether consideration was cash or stock nor the "
            "per-share amount — leaving event_type=unknown rather than guessing "
            "cash_acquisition/stock_acquisition (see module docstring on why an unsettled "
            "acquisition label is read as a full loss downstream)"
        )
        if merger_proxy is not None:
            rationale += f"; a merger proxy ({merger_proxy.form}) is also on file and may contain the settlement value"
        return Classification(
            event_type="unknown",
            confidence="unknown",
            rationale=rationale,
            evidence_form=acquisition_item.form,
            evidence_url=acquisition_item.document_url,
            evidence_filed_date=acquisition_item.filed_date,
        )

    delisting_notice = _find(_ITEM_DELISTING_NOTICE)
    if delisting_notice is not None and has_deregistration:
        return Classification(
            event_type="unknown",
            confidence="unknown",
            rationale=(
                "8-K Item 3.01 (Notice of Delisting or Failure to Satisfy a Continued Listing "
                "Rule) corroborated by a deregistration filing (Form 25/15); not auto-classified "
                "as exchange_transfer because backtest/runner.py treats exchange_transfer "
                "identically to unknown (-100%), so asserting it carries no benefit and implies "
                "false certainty about the outcome"
            ),
            evidence_form=delisting_notice.form,
            evidence_url=delisting_notice.document_url,
            evidence_filed_date=delisting_notice.filed_date,
        )

    if has_deregistration:
        dereg_filing = _find_form(_DEREGISTRATION_FORMS)
        assert dereg_filing is not None  # has_deregistration guarantees this
        return Classification(
            event_type="unknown",
            confidence="unknown",
            rationale=(
                f"{dereg_filing.form} confirms deregistration but does not by itself state a "
                "cause (voluntary deregistration, a completed going-private merger, and a prior "
                "bankruptcy can all end in the same filing); no corroborating 8-K item found"
            ),
            evidence_form=dereg_filing.form,
            evidence_url=dereg_filing.document_url,
            evidence_filed_date=dereg_filing.filed_date,
        )

    return _UNKNOWN_NO_EVIDENCE


@dataclasses.dataclass(frozen=True)
class EventEvidenceBundle:
    """1つの `delisting_events` 行に対して集めた証拠と、その信頼性に関わる文脈。"""

    event_id: int
    ticker_id: int
    symbol: str
    cik: str | None
    evidence: tuple[FilingEvidence, ...]
    ambiguous_shared_cik: bool
    shared_cik_active_symbols: tuple[str, ...]


def gather_evidence_for_event(
    session: Session,
    event: DelistingEvent,
    ticker: Ticker,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    lookahead_days: int = DEFAULT_LOOKAHEAD_DAYS,
) -> EventEvidenceBundle:
    """DBに既にある `filings` 行から証拠を集める(ネットワークアクセス無し)。

    **`ticker_id` でのみJOINする**(`cik` では引かない)。理由はモジュール
    docstring 3.:同一CIKを複数ティッカーが共有するケース(倒産再編後のワラント
    等)で、無関係な現役銘柄の書類を誤って証拠にしてしまうため。同じCIKを持つ
    他の現役銘柄(`delisted_at IS NULL`)が存在する場合は
    `ambiguous_shared_cik=True` で呼び出し側に警告する——分類自体は行うが、
    レポート上で目立たせて人間の確認を促し、`apply_classifications` は自動では
    書き込まない。
    """
    window_start = event.event_date - datetime.timedelta(days=lookback_days)
    window_end = event.event_date + datetime.timedelta(days=lookahead_days)
    rows = (
        session.query(Filing)
        .filter(
            Filing.ticker_id == ticker.id,
            Filing.filed_date >= window_start,
            Filing.filed_date <= window_end,
        )
        .order_by(Filing.filed_date.desc())
        .all()
    )
    evidence = tuple(
        FilingEvidence(
            form=row.form,
            filed_date=row.filed_date,
            items=tuple(row.items or []),
            document_url=row.document_url,
            accession_number=row.accession_number,
        )
        for row in rows
    )

    shared_active: tuple[str, ...] = ()
    if ticker.cik:
        other_symbols = (
            session.query(Ticker.symbol)
            .filter(
                Ticker.cik == ticker.cik,
                Ticker.id != ticker.id,
                Ticker.delisted_at.is_(None),
            )
            .all()
        )
        shared_active = tuple(sorted(sym for (sym,) in other_symbols))

    return EventEvidenceBundle(
        event_id=event.id,
        ticker_id=ticker.id,
        symbol=ticker.symbol,
        cik=ticker.cik,
        evidence=evidence,
        ambiguous_shared_cik=bool(shared_active),
        shared_cik_active_symbols=shared_active,
    )


@dataclasses.dataclass(frozen=True)
class EventClassificationOutcome:
    bundle: EventEvidenceBundle
    classification: Classification


def classify_stored_delisting_events(
    session: Session,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    lookahead_days: int = DEFAULT_LOOKAHEAD_DAYS,
) -> list[EventClassificationOutcome]:
    """`event_type='unknown'` の全行について、既存の `filings` から分類を試みる。

    書き込みは行わない(呼び出し側が `apply_classifications` で反映する)。
    """
    outcomes: list[EventClassificationOutcome] = []
    rows = (
        session.query(DelistingEvent, Ticker)
        .join(Ticker, Ticker.id == DelistingEvent.ticker_id)
        .filter(DelistingEvent.event_type == "unknown")
        .order_by(DelistingEvent.event_date.desc())
        .all()
    )
    for event, ticker in rows:
        bundle = gather_evidence_for_event(
            session, event, ticker, lookback_days=lookback_days, lookahead_days=lookahead_days
        )
        classification = classify_from_filings(bundle.evidence)
        outcomes.append(EventClassificationOutcome(bundle=bundle, classification=classification))
    return outcomes


def apply_classifications(
    session: Session, outcomes: Sequence[EventClassificationOutcome]
) -> dict[str, int]:
    """分類が確定した(`event_type != 'unknown'`)行だけをDBへ反映する。

    `classify_from_filings` は決済額を要する分類は常に `unknown` を返す設計
    なので、ここで実際に書き込まれるのは現状 `bankruptcy` のみになる。
    """
    counts = {"classified": 0, "left_unknown": 0, "ambiguous_shared_cik_skipped": 0}
    ids = [o.bundle.event_id for o in outcomes]
    events_by_id = {
        e.id: e for e in session.query(DelistingEvent).filter(DelistingEvent.id.in_(ids)).all()
    } if ids else {}

    for outcome in outcomes:
        classification = outcome.classification
        if classification.event_type == "unknown":
            counts["left_unknown"] += 1
            continue
        if outcome.bundle.ambiguous_shared_cik:
            # CIKを共有する現役銘柄がある場合、証拠が本当にこの銘柄自身のものか
            # 確認が要る(モジュールdocstring3.)。自動では書き込まず、人間の確認
            # 待ちとして別カウントにする。
            counts["ambiguous_shared_cik_skipped"] += 1
            continue
        row = events_by_id.get(outcome.bundle.event_id)
        if row is None:
            continue
        row.event_type = classification.event_type
        row.confidence = classification.confidence
        if classification.evidence_url:
            row.source_url = classification.evidence_url
        counts["classified"] += 1
    return counts
