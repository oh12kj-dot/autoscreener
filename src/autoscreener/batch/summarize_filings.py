"""提出書類セクションの要約バッチ(K-9)。

`filing_sections`(K-2が保存した原文)を読み、Claude に要約させて
`llm_analyses` に保存する。**EDGAR には触れない**——本文はすでにDBにあり、
SECへの再アクセスはレート制限を食うだけだから(`collect_concentration` と
同じ方針)。

**日次パイプラインからは呼ばない。** `research/draft.py` と同じ理由に、課金が
加わる。全銘柄ぶん毎日要約しても誰も読まないうえ、1回あたり実費が出る。
人間が `summarize-filings` を明示的に叩いたときだけ動く。

**ストリーミングを使う**理由は表示ではなく、10-Kのリスク要因のような長文で
HTTPタイムアウトに当たらないため(`LlmClient.complete_text` を参照)。
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy.orm import Session

from autoscreener.batch.collect_filings import select_tracked_tickers
from autoscreener.config import LlmConfig
from autoscreener.runtime_settings import resolve_llm_config
from autoscreener.dates import utc_today
from autoscreener.db.models import FilingSection, LlmAnalysis, Ticker
from autoscreener.db.session import session_scope
from autoscreener.llm.client import LlmProvider, build_provider, guard_input_size
from autoscreener.llm.errors import (
    LlmDisabled,
    LlmError,
    LlmInputTooLarge,
    LlmRefusal,
    LlmTruncated,
)
from autoscreener.llm.filing_summary import (
    SECTION_LABELS,
    SectionInput,
    build_summary_user_message,
    source_refs,
    summary_system,
)
from autoscreener.llm.prompts import prompt_fingerprint

logger = logging.getLogger(__name__)

KIND = "filing_summary"

# 既定で読むセクション。Item 1A(リスク要因)と Item 7(MD&A)に絞るのは、
# 「事業の理解」に最も効き、かつ定型文の比率が低いため。Item 1(事業の説明)は
# 有用だが 1A と重複が多く、課金が2倍になる割に増える情報が少ない。
DEFAULT_SECTIONS: tuple[str, ...] = ("item1a", "item7")


def latest_sections(
    session: Session, ticker_id: int, sections: tuple[str, ...]
) -> list[FilingSection]:
    """セクション種別ごとに**最新の提出1件だけ**を返す。

    過去分まで要約すると課金が線形に増える一方、下読みに要るのは直近の開示
    である。過去分が要るときは `--section` と銘柄を指定して明示的に回す。
    """
    rows: list[FilingSection] = []
    for section in sections:
        row = (
            session.query(FilingSection)
            .filter(FilingSection.ticker_id == ticker_id, FilingSection.section == section)
            .order_by(FilingSection.filed_date.desc(), FilingSection.id.desc())
            .first()
        )
        if row is not None:
            rows.append(row)
    return rows


def _source_key(section: FilingSection) -> str:
    """`llm_analyses.source_key`。同じ提出書類の別セクションを別行にする。"""
    return f"{section.accession_number}:{section.section}"


def summarize_filings(
    symbols: list[str] | None = None,
    *,
    sections: tuple[str, ...] = DEFAULT_SECTIONS,
    limit: int | None = None,
    config: LlmConfig | None = None,
    client: LlmProvider | None = None,
    today: datetime.date | None = None,
) -> dict[str, int]:
    """追跡対象銘柄の直近提出書類を要約し、`llm_analyses` に保存する。

    戻り値: {"tickers", "sections", "new_rows", "existing", "too_large",
    "refused", "truncated", "failures"}。

    銘柄単位の失敗(`LlmError` 系)は握って次へ進む。**課金が発生する処理なので、
    1件の失敗で残り全部を捨てるのは高くつく**——一方で失敗の種類は数え分けて
    返し、運用者が「設定を直すべき失敗」と「待てば直る失敗」を区別できるようにする。
    """
    cfg = config if config is not None else resolve_llm_config()
    counts = {
        "tickers": 0,
        "sections": 0,
        "new_rows": 0,
        "existing": 0,
        "too_large": 0,
        "refused": 0,
        "truncated": 0,
        "failures": 0,
    }

    if client is None:
        try:
            client = build_provider(cfg)
        except LlmDisabled as exc:
            # FRED未設定時と同じ扱い:失敗ではなく「使わない構成」。
            logger.info("LLM summarization disabled: %s", exc)
            return counts

    day = today or utc_today()
    system = summary_system()
    fingerprint = prompt_fingerprint(system, cfg.model, cfg.effort, cfg.provider)
    ticker_limit = limit if limit is not None else cfg.max_tickers_per_run

    with session_scope() as session:
        if symbols:
            tickers = (
                session.query(Ticker)
                .filter(Ticker.symbol.in_([s.upper() for s in symbols]))
                .all()
            )
        else:
            tickers = select_tracked_tickers(session, limit=ticker_limit)
        # `max_tickers_per_run` は**課金の上限**なので、銘柄を明示された場合も含めて効かせる。
        tickers = tickers[:ticker_limit]

        for ticker in tickers:
            counts["tickers"] += 1
            for row in latest_sections(session, ticker.id, sections):
                counts["sections"] += 1
                key = _source_key(row)
                exists = (
                    session.query(LlmAnalysis.id)
                    .filter_by(
                        ticker_id=ticker.id,
                        kind=KIND,
                        source_key=key,
                        prompt_fingerprint=fingerprint,
                    )
                    .first()
                )
                if exists is not None:
                    # 同じ提出書類・同じ指示文なら作り直さない(課金の無駄)。
                    # ルーブリックを変えれば指紋が変わり、自動的に作り直される。
                    counts["existing"] += 1
                    continue

                payload = SectionInput(
                    symbol=ticker.symbol,
                    form=row.form,
                    filed_date=row.filed_date,
                    section=row.section,
                    text=row.text,
                    accession_number=row.accession_number,
                    source_url=row.source_url,
                )
                label = f"{ticker.symbol} {row.form} {SECTION_LABELS.get(row.section, row.section)}"
                try:
                    guard_input_size(row.text, cfg, label=label)
                    result = client.complete_text(
                        system=system,
                        user=build_summary_user_message(payload),
                    )
                except LlmError as exc:
                    _tally_failure(exc, counts, label)
                    continue

                session.add(
                    LlmAnalysis(
                        ticker_id=ticker.id,
                        kind=KIND,
                        source_key=key,
                        as_of=day,
                        model=result.model,
                        effort=cfg.effort,
                        prompt_fingerprint=fingerprint,
                        content=result.text,
                        source_refs=source_refs([payload]),
                        usage=result.usage.as_dict(),
                        request_id=result.request_id,
                    )
                )
                counts["new_rows"] += 1

    return counts


def _tally_failure(exc: LlmError, counts: dict[str, int], label: str) -> None:
    """失敗を種類別に数える。

    ひとつの `failures` に丸めないのは、対処が違うため——`too_large` は設定か
    分割の問題、`truncated` は `max_output_tokens` の問題、`refused` は入力の
    内容の問題であり、残りは接続かAPIキーの問題である。
    """
    if isinstance(exc, LlmInputTooLarge):
        counts["too_large"] += 1
        logger.warning("%s: %s", label, exc)
    elif isinstance(exc, LlmRefusal):
        counts["refused"] += 1
        logger.warning("%s: %s", label, exc)
    elif isinstance(exc, LlmTruncated):
        counts["truncated"] += 1
        logger.warning("%s: %s", label, exc)
    else:
        counts["failures"] += 1
        logger.exception("%s: LLM summarization failed", label)
