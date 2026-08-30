"""定性評価バッチ(K-9)。**Message Batches API を使う。**

**なぜ Batch API か。** この処理は「数十〜数百銘柄・即時性は不要・失敗しても
明日でよい」という形をしており、Batch API の条件(最大24時間・料金50%)に
そのまま当てはまる。逆に `generate_report`(1回1件)に Batch を使う理由は
無いので、あちらは単発のストリーミング呼び出しにしてある。**バッチにすると
半額になる**のは、対象銘柄数が増えるほど効く。

**出力はゲートにもスコアにも入らない。** `conviction` は low/medium/high の
順序尺度であって点数ではない(理由は `llm/qualitative.py` の docstring)。
保存先も `llm_analyses` に隔離してある。

**中断しても失われない。** `submit_batch` が返す `batch_id` はログに出す。
待ち時間が `llm.batch_timeout_seconds` を超えて `LlmTransientFailure` に
なっても、サーバ側の処理は続いているので、その ID を `--batch-id` に渡して
回収だけをやり直せる。

**Batch API を持たないプロバイダ(`llm.provider = openai_compat`)** では
逐次の構造化出力にフォールバックする(`_store_sequential`)。半額にはならず
`--batch-id` での回収もできないが、`generate-report` と同じく「選べるモデルの
幅」を優先する。Claude を使うなら既定の Batch 経路のままが安い。
"""

from __future__ import annotations

import datetime
import hashlib
import logging
from typing import Any

from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request
from sqlalchemy.orm import Session

from autoscreener.batch.collect_filings import select_tracked_tickers
from autoscreener.batch.summarize_filings import DEFAULT_SECTIONS, latest_sections
from autoscreener.config import LlmConfig
from autoscreener.runtime_settings import resolve_llm_config
from autoscreener.dates import utc_today
from autoscreener.db.models import LlmAnalysis, Ticker
from autoscreener.db.session import session_scope
from autoscreener.llm.client import (
    LlmProvider,
    LlmUsage,
    build_provider,
    check_stop_reason,
    guard_input_size,
    text_of,
)
from autoscreener.llm.errors import (
    LlmDisabled,
    LlmError,
    LlmParseFailure,
    LlmRefusal,
    LlmTruncated,
)
from autoscreener.llm.filing_summary import SECTION_LABELS
from autoscreener.llm.prompts import prompt_fingerprint
from autoscreener.llm.qualitative import (
    QualitativeAssessment,
    QualitativeInput,
    assessment_to_dict,
    build_qualitative_user_message,
    parse_qualitative_json,
    qualitative_json_schema,
    qualitative_system,
)

logger = logging.getLogger(__name__)

KIND = "qualitative"


def _source_key(accessions: list[str]) -> str:
    """読んだ提出書類の集合から決まる鍵。

    日付にしないのは、**同じ書類しか無い日に再実行しても課金しないため**。
    新しい 10-Q が入れば集合が変わり、鍵が変わり、自動的に評価し直される。
    実際の accession 一覧は `source_refs` に残すので、鍵が短くても追跡できる。
    """
    joined = "|".join(sorted(accessions))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


def _build_payload(
    session: Session, ticker: Ticker, sections: tuple[str, ...], cfg: LlmConfig, day: datetime.date
) -> tuple[QualitativeInput, list[str], list[dict[str, str]]] | None:
    """1銘柄ぶんの入力を組む。本文が無ければ None(呼び出し側が数える)。"""
    rows = latest_sections(session, ticker.id, sections)
    if not rows:
        return None
    excerpts = [(SECTION_LABELS.get(r.section, r.section), r.text) for r in rows]
    combined = "\n".join(text for _label, text in excerpts)
    guard_input_size(combined, cfg, label=f"{ticker.symbol} 定性評価")
    refs = [
        {
            "accession_number": r.accession_number,
            "form": r.form,
            "section": r.section,
            "filed_date": r.filed_date.isoformat(),
        }
        for r in rows
    ]
    return QualitativeInput(symbol=ticker.symbol, as_of=day, excerpts=excerpts), [
        r.accession_number for r in rows
    ], refs


def score_qualitative(
    symbols: list[str] | None = None,
    *,
    sections: tuple[str, ...] = DEFAULT_SECTIONS,
    limit: int | None = None,
    config: LlmConfig | None = None,
    client: LlmProvider | None = None,
    today: datetime.date | None = None,
    batch_id: str | None = None,
) -> dict[str, int]:
    """追跡対象銘柄の定性評価を Batch API で作り、`llm_analyses` に保存する。

    `batch_id` を渡すと**投げ直さずに回収だけ**を行う(前回の実行が待ち時間
    超過で落ちた場合の再開経路)。

    戻り値: {"tickers", "submitted", "new_rows", "existing", "no_sections",
    "too_large", "errored", "refused", "truncated", "parse_failures"}。
    """
    cfg = config if config is not None else resolve_llm_config()
    counts = {
        "tickers": 0,
        "submitted": 0,
        "new_rows": 0,
        "existing": 0,
        "no_sections": 0,
        "too_large": 0,
        "errored": 0,
        "refused": 0,
        "truncated": 0,
        "parse_failures": 0,
    }

    if client is None:
        try:
            client = build_provider(cfg)
        except LlmDisabled as exc:
            logger.info("LLM qualitative scoring disabled: %s", exc)
            return counts

    day = today or utc_today()
    system = qualitative_system()
    fingerprint = prompt_fingerprint(system, cfg.model, cfg.effort, cfg.provider)
    ticker_limit = limit if limit is not None else cfg.max_tickers_per_run

    if batch_id is not None and not getattr(client, "supports_batch", True):
        raise LlmError(
            "--batch-id での回収は Batch API 対応プロバイダ(anthropic)専用。"
            "openai_compat では逐次呼び出しなので回収する batch は存在しない。"
        )

    # custom_id → (ticker_id, source_key, source_refs, payload)。バッチ結果は
    # **順不同**で返るので位置ではなく custom_id で引き当てる。`payload` は
    # Batch 非対応プロバイダの逐次経路で使う。
    pending: dict[str, tuple[int, str, list[dict[str, str]], QualitativeInput]] = {}
    requests: list[Request] = []

    with session_scope() as session:
        if symbols:
            tickers = (
                session.query(Ticker).filter(Ticker.symbol.in_([s.upper() for s in symbols])).all()
            )
        else:
            tickers = select_tracked_tickers(session, limit=ticker_limit)
        tickers = tickers[:ticker_limit]

        for ticker in tickers:
            counts["tickers"] += 1
            try:
                built = _build_payload(session, ticker, sections, cfg, day)
            except LlmError as exc:
                counts["too_large"] += 1
                logger.warning("%s: %s", ticker.symbol, exc)
                continue
            if built is None:
                counts["no_sections"] += 1
                continue
            payload, accessions, refs = built
            key = _source_key(accessions)

            exists = (
                session.query(LlmAnalysis.id)
                .filter_by(
                    ticker_id=ticker.id, kind=KIND, source_key=key, prompt_fingerprint=fingerprint
                )
                .first()
            )
            if exists is not None:
                counts["existing"] += 1
                continue

            pending[ticker.symbol] = (ticker.id, key, refs, payload)
            requests.append(
                Request(
                    custom_id=ticker.symbol,
                    params=MessageCreateParamsNonStreaming(
                        model=cfg.model,
                        max_tokens=cfg.max_output_tokens,
                        thinking={"type": "adaptive"},
                        output_config={
                            "effort": cfg.effort,
                            "format": {"type": "json_schema", "schema": qualitative_json_schema()},
                        },
                        system=system,
                        messages=[
                            {"role": "user", "content": build_qualitative_user_message(payload)}
                        ],
                    ),
                )
            )

    # Batch API を持たないプロバイダ(openai_compat)は逐次に落とす。半額には
    # ならないが、`generate-report` 同様「使えるモデルの幅」を優先する。
    if not getattr(client, "supports_batch", True):
        if not pending:
            return counts
        logger.info(
            "provider %s は Batch 非対応。%d 銘柄を逐次で評価する(料金は50%%にならない)",
            getattr(client, "provider_name", "?"),
            len(pending),
        )
        counts["submitted"] = len(pending)
        with session_scope() as session:
            for symbol, (ticker_id, key, refs, payload) in pending.items():
                if _store_sequential(
                    session, client, symbol, ticker_id, key, refs, payload,
                    system, day, cfg, fingerprint, counts,
                ):
                    counts["new_rows"] += 1
        return counts

    if batch_id is None:
        if not requests:
            return counts
        batch_id = client.submit_batch(requests)
        counts["submitted"] = len(requests)
        # **必ずログに出す。** 待ち時間超過で落ちても、このIDがあれば回収できる。
        logger.info("submitted batch %s (%d requests)", batch_id, len(requests))

    client.wait_for_batch(batch_id)

    with session_scope() as session:
        for result in client.collect_batch(batch_id):
            symbol = result.custom_id
            entry = pending.get(symbol)
            if entry is None:
                # `--batch-id` で回収だけをやり直した場合、pending は空になる。
                # その場合はシンボルからDBを引き直す。
                ticker = session.query(Ticker).filter_by(symbol=symbol).one_or_none()
                if ticker is None:
                    logger.warning("batch result for unknown symbol %s", symbol)
                    continue
                entry = (ticker.id, "", [], None)
            ticker_id, key, refs, _payload = entry

            stored = _store_result(session, result, ticker_id, key, refs, day, cfg, fingerprint, counts)
            if stored:
                counts["new_rows"] += 1

    return counts


def _store_sequential(
    session: Session,
    client: LlmProvider,
    symbol: str,
    ticker_id: int,
    source_key: str,
    refs: list[dict[str, str]],
    payload: QualitativeInput,
    system: list[dict[str, Any]],
    day: datetime.date,
    cfg: LlmConfig,
    fingerprint: str,
    counts: dict[str, int],
) -> bool:
    """Batch 非対応プロバイダで1銘柄を単発の構造化出力として評価・保存する。

    失敗の数え分けは Batch 経路(`_store_result`)と揃える——呼び出し側が
    プロバイダの違いで分岐しなくて済むように。
    """
    try:
        parsed, usage, request_id = client.parse_structured(
            system=system,
            user=build_qualitative_user_message(payload),
            output_model=QualitativeAssessment,
        )
    except LlmRefusal as exc:
        counts["refused"] += 1
        logger.warning("%s: %s", symbol, exc)
        return False
    except LlmTruncated as exc:
        counts["truncated"] += 1
        logger.warning("%s: %s", symbol, exc)
        return False
    except LlmParseFailure as exc:
        counts["parse_failures"] += 1
        logger.warning("%s: %s", symbol, exc)
        return False
    except LlmError as exc:
        counts["errored"] += 1
        logger.warning("%s: %s", symbol, exc)
        return False

    session.add(
        LlmAnalysis(
            ticker_id=ticker_id,
            kind=KIND,
            source_key=source_key or day.isoformat(),
            as_of=day,
            model=cfg.model,
            effort=cfg.effort,
            prompt_fingerprint=fingerprint,
            data=assessment_to_dict(parsed),
            source_refs=refs,
            usage=usage.as_dict(),
            request_id=request_id,
        )
    )
    return True


def _store_result(
    session: Session,
    result: Any,
    ticker_id: int,
    source_key: str,
    refs: list[dict[str, str]],
    day: datetime.date,
    cfg: LlmConfig,
    fingerprint: str,
    counts: dict[str, int],
) -> bool:
    """バッチ結果1件を検証して保存する。保存したら True。

    `result.result.type` は succeeded / errored / canceled / expired の4種。
    成功しても `stop_reason` の検査は要る——拒否と打ち切りは HTTP 200 で
    返るので、ここを飛ばすと空文字列や途中で切れた出力を保存してしまう。
    """
    outcome = result.result.type
    if outcome != "succeeded":
        counts["errored"] += 1
        logger.warning("batch result %s: %s", result.custom_id, outcome)
        return False

    message = result.result.message
    try:
        check_stop_reason(message)
        assessment = parse_qualitative_json(text_of(message))
    except LlmRefusal as exc:
        counts["refused"] += 1
        logger.warning("%s: %s", result.custom_id, exc)
        return False
    except LlmTruncated as exc:
        counts["truncated"] += 1
        logger.warning("%s: %s", result.custom_id, exc)
        return False
    except LlmParseFailure as exc:
        counts["parse_failures"] += 1
        logger.warning("%s: %s", result.custom_id, exc)
        return False

    # `--batch-id` で回収だけをやり直した経路では `source_key` が空になる。
    # その場合は refs から作り直し、refs も無ければ基準日で代用する
    # (一意キーが空文字列で衝突するのを避けるため)。
    key = source_key
    if not key and refs:
        key = _source_key([r["accession_number"] for r in refs])
    if not key:
        key = day.isoformat()
    session.add(
        LlmAnalysis(
            ticker_id=ticker_id,
            kind=KIND,
            source_key=key,
            as_of=day,
            model=getattr(message, "model", cfg.model),
            effort=cfg.effort,
            prompt_fingerprint=fingerprint,
            data=assessment_to_dict(assessment),
            source_refs=refs,
            usage=LlmUsage.from_response(message.usage).as_dict(),
            request_id=None,  # バッチ結果は個別のリクエストIDを持たない
        )
    )
    return True
