"""Provider-neutral analyst consensus snapshots."""

from __future__ import annotations

from dataclasses import dataclass
import datetime
from typing import Callable, Protocol

# S-4監査(2026-09-04、daily_pipeline_throughput_plan_2026-09-04.md)で発見:
# この`import`は見た目上未使用に見えるが、副作用が目的である。
#
# `_install_http_throttle()`(S-1)は`yfinance.data.YfData._make_request`を
# モンキーパッチする関数で、`collectors/yfinance_client`が**importされた
# 時点で**モジュール直下の呼び出しとして実行される。ところが本モジュール
# (`collectors/consensus.py`)は従来これを一切importしておらず、
# `YfinanceConsensusProvider.__init__`の中で独立に`import yfinance as yf`
# していただけだった。S-4で`batch/collect_consensus.py`を並列化(最大
# `max_workers`並列)したとき、その並列化の安全性は「`YfData._make_request`
# が共有`yfinance`リミッターでスロットルされている」という前提の上に
# 成り立っているが、**その前提はimportグラフのどこか別の場所
# (たまたま`cli.py`が`snapshot_collector`経由で`yfinance_client`を
# importしていたこと)に依存する偶然でしかなかった**。`collect_consensus()`
# を直接呼ぶ別の呼び出し経路(スクリプト・将来のCLI配線・テスト)からは
# スロットルが一切入らないまま`max_workers`並列でYahooに投げる状態が
# 起こりえた——まさに計画書6章が禁じている「レート上限が構造的に保証
# されない状態での並列化」。ここでモジュール直下からimportすることで、
# 「`collectors.consensus`がimportされた時点でスロットルが入っている」
# ことを、呼び出し元のimport順序に依存しない構造的な保証にする
# (`_install_http_throttle()`はマーカー属性で二重適用を防いでいるので、
# 他経路が先にimportしていても安全に冪等)。
from autoscreener.collectors import yfinance_client as _yfinance_client


@dataclass(frozen=True)
class ConsensusSnapshot:
    observed_at: datetime.datetime
    source: str
    period_type: str
    period_end: datetime.date | None
    revenue_mean: float | None = None
    revenue_low: float | None = None
    revenue_high: float | None = None
    eps_mean: float | None = None
    ebitda_mean: float | None = None
    analyst_count: int | None = None
    target_price_mean: float | None = None
    raw_payload: dict | None = None
    source_url: str | None = None


class ConsensusProvider(Protocol):
    name: str
    def fetch(
        self, ticker: str, as_of: datetime.datetime, target_mean_price: float | None = None
    ) -> list[ConsensusSnapshot]: ...


def _number(value) -> float | None:
    try:
        if value is None or value != value:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


class YfinanceConsensusProvider:
    """Initial provider. The stored schema does not depend on yfinance."""
    name = "yfinance"

    def __init__(self, ticker_factory: Callable | None = None):
        if ticker_factory is None:
            # `_yfinance_client.yf`は`collectors/yfinance_client`が既に
            # `import yfinance as yf`した参照の再利用(モジュール先頭の
            # コメント参照)。ここで独立に`import yfinance as yf`しないのは、
            # そちらの経路だと「モジュールをimportしただけではスロットルが
            # 入る保証がない」という元の欠陥に戻ってしまうため。
            ticker_factory = _yfinance_client.yf.Ticker
        self._ticker_factory = ticker_factory

    def fetch(
        self, ticker: str, as_of: datetime.datetime, target_mean_price: float | None = None
    ) -> list[ConsensusSnapshot]:
        """S-3(daily_pipeline_throughput_plan_2026-09-04):以前はここで`obj.info`を
        別途取得し、`targetMeanPrice`1項目のためだけにHTTPを1本(quoteSummary)
        投げていた。同じ`info`は数時間前のcollection工程が既に取得して
        `raw_snapshots.payload["info"]`へ保存済みである。呼び出し元
        (`batch/collect_consensus.py`)がそこから読んで`target_mean_price`として
        渡す——1銘柄あたりのconsensus用HTTPが2本→1本(`.revenue_estimate`/
        `.earnings_estimate`のみ)になる。
        """
        obj = self._ticker_factory(ticker)
        revenue = getattr(obj, "revenue_estimate", None)
        earnings = getattr(obj, "earnings_estimate", None)
        rows: list[ConsensusSnapshot] = []
        if revenue is None or getattr(revenue, "empty", True):
            return rows
        for label, row in revenue.iterrows():
            label_text = str(label)
            # revenue_estimate contains quarterly (0q/+1q) and annual
            # (0y/+1y) rows.  This collector stores annual consensus for the
            # reverse-valuation horizon; treating 0q as FY created the same
            # (source, period_end) key as 0y and aborted the whole batch.
            if not label_text.lower().endswith("y"):
                continue
            # Persist the provider label while using a deterministic
            # approximate calendar-year end.
            years = 2 if "+2" in label_text else 1 if "+1" in label_text else 0
            period_end = datetime.date(as_of.year + years, 12, 31)
            eps_row = earnings.loc[label] if earnings is not None and label in earnings.index else {}
            rows.append(ConsensusSnapshot(
                observed_at=as_of, source=self.name, period_type="FY", period_end=period_end,
                revenue_mean=_number(row.get("avg")), revenue_low=_number(row.get("low")),
                revenue_high=_number(row.get("high")), eps_mean=_number(getattr(eps_row, "get", lambda *_: None)("avg")),
                analyst_count=int(row.get("numberOfAnalysts")) if _number(row.get("numberOfAnalysts")) is not None else None,
                target_price_mean=_number(target_mean_price),
                raw_payload={"provider_period": label_text},
            ))
        return rows
