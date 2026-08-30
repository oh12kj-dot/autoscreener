"""FINRA 空売り残高(defect_and_edge_audit_2026-08-28.md I-5)。

半月ごとの全銘柄空売り残高が無料・機械可読で公開されている(consolidated short
interest ファイル)。`days_to_cover = short_interest / avg_daily_volume`。
マイクロキャップでは (a) バリュートラップの検知、(b) スクイーズの上振れ、
両方の情報を持つ。**優先度は I-1〜I-4 の後。単独では edge にならない。**

このモジュールは FINRA の固定区切りテキストのパースと `days_to_cover` の算出まで
(純粋関数)。ダウンロードは `fred_client` と同型の薄いHTTPで、ネットワークが要る。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

# FINRA consolidated short interest はパイプ区切り。列名はヘッダ行に入る。
# 代表的な列:
#   settlementDate | symbolCode | issueName | marketClassCode |
#   currentShortPositionQuantity | previousShortPositionQuantity |
#   changePercent | daysToCoverQuantity | ...
_DELIMITER = "|"


@dataclass(frozen=True)
class ShortInterestRecord:
    settlement_date: datetime.date
    symbol: str
    current_short_shares: float
    previous_short_shares: float | None
    avg_daily_volume: float | None
    reported_days_to_cover: float | None

    def days_to_cover(self, fallback_adv_shares: float | None = None) -> float | None:
        """FINRA が出す daysToCover が無ければ、ADV(株数)から計算する。"""
        if self.reported_days_to_cover is not None:
            return self.reported_days_to_cover
        adv = self.avg_daily_volume or fallback_adv_shares
        if not adv or adv <= 0:
            return None
        return self.current_short_shares / adv

    def short_interest_change(self) -> float | None:
        if self.previous_short_shares is None or self.previous_short_shares <= 0:
            return None
        return self.current_short_shares / self.previous_short_shares - 1.0


def _to_float(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return None


def _to_date(value: str | None) -> datetime.date | None:
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%m/%d/%Y"):
        try:
            return datetime.datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def parse_short_interest(text: str) -> list[ShortInterestRecord]:
    """FINRA consolidated short interest ファイル(パイプ区切り)をパースする。

    ヘッダ行から列位置を決めるので、列順の変更に強い。認識できない行は捨てる。
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    header = [h.strip() for h in lines[0].split(_DELIMITER)]
    idx = {name.lower(): i for i, name in enumerate(header)}

    def col(row: list[str], *names: str) -> str | None:
        for name in names:
            i = idx.get(name.lower())
            if i is not None and i < len(row):
                return row[i]
        return None

    records: list[ShortInterestRecord] = []
    for line in lines[1:]:
        row = [c.strip() for c in line.split(_DELIMITER)]
        symbol = col(row, "symbolCode", "Symbol", "symbol")
        settlement = _to_date(col(row, "settlementDate", "Settlement Date"))
        current = _to_float(col(row, "currentShortPositionQuantity", "Current Short Position", "ShortInterest"))
        if not symbol or settlement is None or current is None:
            continue
        records.append(
            ShortInterestRecord(
                settlement_date=settlement,
                symbol=symbol.upper(),
                current_short_shares=current,
                previous_short_shares=_to_float(
                    col(row, "previousShortPositionQuantity", "Previous Short Position")
                ),
                avg_daily_volume=_to_float(col(row, "averageDailyVolumeQuantity", "Average Daily Volume", "avgDailyVolume")),
                reported_days_to_cover=_to_float(col(row, "daysToCoverQuantity", "Days To Cover")),
            )
        )
    return records
