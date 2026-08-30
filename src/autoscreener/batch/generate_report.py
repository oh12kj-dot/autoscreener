"""日次スクリーニング結果の読み物化(K-9)。

`scores` の上位候補を読み、Claude に説明文を書かせて `llm_analyses`
(`kind='daily_report'`)に保存する。**銘柄横断の出力なので `ticker_id` は
NULL** になる(`llm_analyses` の部分ユニークインデックス2本のうち、
`uq_llm_analyses_global` が効く)。

**Batch API は使わない。** 1回の実行で1リクエストしか出ないので、半額に
なっても待ち時間が増えるだけで得が無い(数十〜数百件を投げる
`score_qualitative` とはそこが違う)。代わりに単発のストリーミングを使う——
レポートは出力が長くなりうるので、HTTPタイムアウトを避ける必要がある。

**数字はモデルに計算させない。** 渡すのは算出済みの値だけで、モデルの仕事は
言語化に限る(理由は `llm/report.py` の docstring)。
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy.orm import Session

from autoscreener.config import LlmConfig
from autoscreener.runtime_settings import resolve_llm_config
from autoscreener.dates import business_days_between, utc_today
from autoscreener.db.models import LlmAnalysis, Score, Ticker
from autoscreener.db.session import session_scope
from autoscreener.llm.client import LlmProvider, build_provider
from autoscreener.llm.errors import LlmDisabled, LlmError
from autoscreener.llm.prompts import prompt_fingerprint
from autoscreener.llm.report import (
    CandidateBrief,
    build_report_user_message,
    report_source_refs,
    report_system,
)

logger = logging.getLogger(__name__)

KIND = "daily_report"

_DEFAULT_TOP_N = 10


def _latest_score_date(session: Session) -> datetime.date | None:
    row = session.query(Score.score_date).order_by(Score.score_date.desc()).first()
    return row[0] if row is not None else None


def load_candidates(
    session: Session, score_date: datetime.date, *, top_n: int = _DEFAULT_TOP_N
) -> list[CandidateBrief]:
    """当日の上位候補を、レポートに渡す形にして返す。純粋な読み出し。

    `data_age_days` を必ず載せるのは A-1 と同じ理由——収集が止まっていても
    `run_scoring` は前日以前のデータで当日付のランキングを書けてしまうので、
    その事実をレポートにも運ぶ。
    """
    rows = (
        session.query(Score, Ticker)
        .join(Ticker, Score.ticker_id == Ticker.id)
        .filter(Score.score_date == score_date, Score.probability.isnot(None))
        .order_by(Score.probability.desc())
        .limit(top_n)
        .all()
    )
    briefs: list[CandidateBrief] = []
    for rank, (score, ticker) in enumerate(rows, start=1):
        age = (
            business_days_between(score.price_as_of, score.score_date)
            if score.price_as_of is not None
            else None
        )
        briefs.append(
            CandidateBrief(
                rank=rank,
                symbol=ticker.symbol,
                sector=ticker.sector,
                probability=float(score.probability) if score.probability is not None else None,
                median_moic=float(score.median_moic) if score.median_moic is not None else None,
                factors=score.factors or {},
                price_as_of=score.price_as_of.isoformat() if score.price_as_of else None,
                financials_as_of=(
                    score.financials_as_of.isoformat() if score.financials_as_of else None
                ),
                data_age_days=age,
            )
        )
    return briefs


def generate_report(
    *,
    score_date: datetime.date | None = None,
    top_n: int = _DEFAULT_TOP_N,
    config: LlmConfig | None = None,
    client: LlmProvider | None = None,
    today: datetime.date | None = None,
    raise_on_llm_error: bool = False,
) -> dict[str, int]:
    """当日ランキングの説明文を1件生成し、`llm_analyses` に保存する。

    戻り値: {"candidates", "new_rows", "existing", "failures"}。
    `score_date` 省略時は `scores` にある最新日を使う——当日分がまだ無い日に
    空のレポートを書いて課金するのを避けるため(「今日」ではなく
    「最後に算出された日」を対象にする)。

    `raise_on_llm_error=True` のときは LLM 呼び出しの失敗を握りつぶさず
    そのまま送出する——UIから1件だけ生成するときは「失敗した」だけでなく
    **なぜ失敗したか**(認証・モデル名・base_url など)を呼び出し側に返したい。
    日次パイプラインなど「1件こけても全体は続行」したい経路では既定の False。
    """
    cfg = config if config is not None else resolve_llm_config()
    counts = {"candidates": 0, "new_rows": 0, "existing": 0, "failures": 0}

    if client is None:
        try:
            client = build_provider(cfg)
        except LlmDisabled as exc:
            logger.info("LLM report generation disabled: %s", exc)
            return counts

    day = today or utc_today()
    system = report_system()
    fingerprint = prompt_fingerprint(system, cfg.model, cfg.effort, cfg.provider)

    with session_scope() as session:
        target_date = score_date or _latest_score_date(session)
        if target_date is None:
            logger.info("scores が空なのでレポートは生成しない")
            return counts

        candidates = load_candidates(session, target_date, top_n=top_n)
        counts["candidates"] = len(candidates)
        if not candidates:
            logger.info("%s の候補が0件なのでレポートは生成しない", target_date)
            return counts

        source_key = target_date.isoformat()
        exists = (
            session.query(LlmAnalysis.id)
            .filter(
                LlmAnalysis.ticker_id.is_(None),
                LlmAnalysis.kind == KIND,
                LlmAnalysis.source_key == source_key,
                LlmAnalysis.prompt_fingerprint == fingerprint,
            )
            .first()
        )
        if exists is not None:
            counts["existing"] = 1
            return counts

        scoring_version = (
            session.query(Score.scoring_version)
            .filter(Score.score_date == target_date)
            .limit(1)
            .scalar()
        )
        universe_size = (
            session.query(Score.id).filter(Score.score_date == target_date).count()
        )

        try:
            result = client.complete_text(
                system=system,
                user=build_report_user_message(
                    target_date,
                    candidates,
                    scoring_version=scoring_version,
                    universe_size=universe_size,
                ),
            )
        except LlmError:
            counts["failures"] = 1
            logger.exception("%s: レポート生成に失敗", target_date)
            if raise_on_llm_error:
                raise
            return counts

        session.add(
            LlmAnalysis(
                ticker_id=None,
                kind=KIND,
                source_key=source_key,
                as_of=day,
                model=result.model,
                effort=cfg.effort,
                prompt_fingerprint=fingerprint,
                content=result.text,
                source_refs=report_source_refs(target_date, candidates),
                usage=result.usage.as_dict(),
                request_id=result.request_id,
            )
        )
        counts["new_rows"] = 1

    return counts
