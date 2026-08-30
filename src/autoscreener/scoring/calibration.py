"""確率の較正層(28.8)。すべて純粋関数。

**解こうとしている問題。** `moic.compute_moic` が返す確率は、log-MOIC が正規分布
だと**仮定**したときの閾値超過確率である。仮定が外れていれば、その数字は
「モデルの内部で整合している値」ではあっても「実際に起きる頻度」ではない。
v3の擬似バックテストでは、モデルの予測平均と実測頻度が 4.8pt ずれていた。
利用者は「P(10倍) = 3%」という表示を頻度として読むので、これは誤情報になる。

**7年後の10倍そのものは較正できない。** 価格ヒストリーは3年しかなく、7年後の
実測は原理的に今日は存在しない。ここで較正できるのは、擬似バックテストが
実際に観測している事象——**「ホライズン h の期間で10倍/7年ペース(年率38.9%)に
乗ったか」**——だけである。

したがって本モジュールが作るのは「較正済みの P(7年で10倍)」ではなく、
**「較正済みの P(h年でオンペース)」という別の数字**である。これは:

- 実測に裏打ちされている(バックテストの観測頻度そのもの)
- ホライズンが短いので、利用者が自分で答え合わせできる
- 7年後の10倍という検証不能な数字と**混同されないよう別の量として提示する**

7年のP(10倍)のほうは、較正せず生の値のまま出す。較正誤差は `/validation` に
常時表示して「この数字はモデルの仮定に依存している」ことを明示する。
較正した数字とそうでない数字を、同じ見た目で並べてはいけない。

**なぜ等調(単調)回帰なのか。** 較正に求められるのは「予測が高い群ほど実測
頻度も高い」という単調性を保ったまま水準を合わせることである。多項式や
ロジスティック回帰は形を仮定するぶん外挿で暴れるが、Pool Adjacent Violators
(PAVA)は単調性以外に何も仮定せず、観測の外側では端点で頭打ちになる。
9評価日・3,000観測という標本サイズで、これ以上の仮定を置く根拠はない。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass


@dataclass(frozen=True)
class CalibrationPoint:
    predicted: float
    calibrated: float


@dataclass(frozen=True)
class CalibrationMap:
    """生の予測確率 → 実測頻度 の単調写像。

    `horizon_days` は較正の対象になった観測期間。**この写像は他のホライズンへ
    転用できない**ので、必ず一緒に持ち回る。
    """

    horizon_days: int
    observation_count: int
    points: list[CalibrationPoint]
    fitted_at: str | None = None

    def apply(self, predicted: float) -> float:
        """線形補間して較正値を返す。観測範囲の外は端点で頭打ちにする。

        外挿しないのは意図的である。バックテストが一度も観測していない確率帯
        (たとえば予測60%)について、実測頻度を主張できる根拠は無い。
        """
        if not self.points:
            return predicted
        if predicted <= self.points[0].predicted:
            return self.points[0].calibrated
        if predicted >= self.points[-1].predicted:
            return self.points[-1].calibrated
        for left, right in zip(self.points, self.points[1:]):
            if left.predicted <= predicted <= right.predicted:
                span = right.predicted - left.predicted
                if span <= 0:
                    return left.calibrated
                weight = (predicted - left.predicted) / span
                return left.calibrated + weight * (right.calibrated - left.calibrated)
        return self.points[-1].calibrated

    def to_dict(self) -> dict:
        return {
            "horizon_days": self.horizon_days,
            "observation_count": self.observation_count,
            "fitted_at": self.fitted_at,
            "points": [{"predicted": p.predicted, "calibrated": p.calibrated} for p in self.points],
        }

    @classmethod
    def from_dict(cls, payload: dict | None) -> CalibrationMap | None:
        if not payload or not payload.get("points"):
            return None
        return cls(
            horizon_days=payload.get("horizon_days", 0),
            observation_count=payload.get("observation_count", 0),
            points=[
                CalibrationPoint(predicted=p["predicted"], calibrated=p["calibrated"])
                for p in payload["points"]
            ],
            fitted_at=payload.get("fitted_at"),
        )


def isotonic(pairs: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Pool Adjacent Violators。x昇順のペアに、単調非減少の y を当てはめる。

    隣り合う区間で単調性が破れていたら、その2区間を併合して加重平均で置き換え、
    破れが無くなるまで繰り返す。実測頻度のノイズによる小さな逆転だけが均され、
    大きな構造は保たれる。
    """
    if not pairs:
        return []
    # (合計y, 重み, 代表x) のブロック列として持つ
    blocks: list[list[float]] = []
    for x, y in sorted(pairs):
        blocks.append([y, 1.0, x])
        while len(blocks) >= 2 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            last = blocks.pop()
            prev = blocks.pop()
            blocks.append([prev[0] + last[0], prev[1] + last[1], last[2]])
    return [(block[2], block[0] / block[1]) for block in blocks]


def fit_calibration(
    predicted: list[float],
    outcomes: list[bool],
    horizon_days: int,
    bins: int = 10,
    min_observations: int = 1000,
) -> CalibrationMap | None:
    """予測確率と実現結果から較正写像を学習する。

    観測が `min_observations` に満たなければ None を返す——**足りない標本で
    較正すると、較正そのものがノイズを固定してしまう**。較正しないほうが
    「モデルの仮定どおり」という既知の状態に留まれるぶんまだ安全である。

    手順:
    1. 予測確率の**分位**でビンに切る(等幅だと分布が0近傍に集中して大半が空になる)
    2. 各ビンの (平均予測, 実測頻度) を出す
    3. PAVAで単調に均す
    """
    if len(predicted) != len(outcomes) or len(predicted) < min_observations:
        return None

    paired = sorted(zip(predicted, outcomes), key=lambda pair: pair[0])
    base, remainder = divmod(len(paired), bins)
    if base == 0:
        return None

    raw: list[tuple[float, float]] = []
    start = 0
    for index in range(bins):
        size = base + (1 if index < remainder else 0)
        chunk = paired[start : start + size]
        start += size
        if not chunk:
            continue
        mean_predicted = sum(p for p, _ in chunk) / len(chunk)
        realized_rate = sum(1 for _, hit in chunk if hit) / len(chunk)
        raw.append((mean_predicted, realized_rate))

    smoothed = isotonic(raw)
    if len(smoothed) < 2:
        return None

    return CalibrationMap(
        horizon_days=horizon_days,
        observation_count=len(paired),
        points=[CalibrationPoint(predicted=x, calibrated=y) for x, y in smoothed],
        fitted_at=datetime.datetime.now(datetime.UTC).isoformat(),
    )
