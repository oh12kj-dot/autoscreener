"""日次スクリーニング結果を読み物にするリクエストの組み立て(K-9)。純関数のみ。

**モデルに数字を計算させない。** 渡すのはアプリが定量モデルで既に算出した値
(`scores.probability` / `median_moic` / `factors` / 鮮度)であり、モデルの
仕事はその並べ替えでも再計算でもなく、**言語化**だけである。再計算させると、
DBに残る数字とレポートの数字が食い違いうる——そして食い違ったとき、どちらが
正しいかを読者は判断できない。

**鮮度(`data_age_days`)を必ず渡す**理由は A-1(`price_as_of` /
`financials_as_of` を `scores` に持たせた件)と同じ。収集が止まっていても
`run_scoring` は前日以前のデータで当日付のランキングを書けてしまうので、
レポートにもその事実を運ばせる。
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from autoscreener.llm.prompts import DAILY_REPORT_RUBRIC, cached_system


@dataclass(frozen=True)
class CandidateBrief:
    """レポートに渡す1銘柄ぶんの、既に算出済みの数字。

    ここに無い値はレポートにも現れない(モデルは補完しない、と指示してある)。
    増やすときは「人間が読んで意味が取れる量か」で判断すること——JSONを
    大きくすればするほど、要点を選ぶ仕事がモデル任せになる。
    """

    rank: int
    symbol: str
    sector: str | None = None
    probability: float | None = None
    median_moic: float | None = None
    # 15.1の恒等式に対応する5因子(revenue_multiple / margin_multiple /
    # multiple_change / leverage_effect / dilution_drag)。無ければ空dict。
    factors: dict[str, Any] = field(default_factory=dict)
    price_as_of: str | None = None
    financials_as_of: str | None = None
    data_age_days: int | None = None


def report_system() -> list[dict[str, Any]]:
    return cached_system(DAILY_REPORT_RUBRIC)


def build_report_user_message(
    as_of: datetime.date,
    candidates: Sequence[CandidateBrief],
    *,
    scoring_version: str | None = None,
    universe_size: int | None = None,
) -> str:
    """候補一覧をJSONで渡す。

    JSONにするのは、**数字を文章に埋め込むと転記の余地が生まれる**ため。
    キー名つきで渡せば、モデルは値を写すだけで済む。
    `sort_keys=True` にしてあるのは、同じ内容なら同じ文字列になるようにして、
    プロンプトキャッシュと指紋が揺れないようにするため。
    """
    payload: dict[str, Any] = {
        "as_of": as_of.isoformat(),
        "scoring_version": scoring_version,
        "universe_size": universe_size,
        "candidates": [asdict(c) for c in candidates],
    }
    return (
        "以下は当日のスクリーニング結果である。数値はすべてこのJSONの値をそのまま使うこと。\n\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    )


def report_source_refs(as_of: datetime.date, candidates: Sequence[CandidateBrief]) -> dict[str, Any]:
    """`llm_analyses.source_refs` に入れる、レポートが読んだ範囲。

    銘柄シンボルと順位だけを残す(数字そのものは `scores` にある)。後から
    「このレポートはどのランキングを見て書かれたか」を復元できれば足りる。
    """
    return {
        "as_of": as_of.isoformat(),
        "ranked_symbols": [{"rank": c.rank, "symbol": c.symbol} for c in candidates],
    }
