"""証券口座で発注できるかの判定(30.2.1)。

元文書の実務ワークフロー工程1。**デューデリの最初の工程**であり、ここで
落ちる銘柄に分析時間を使わないことがワークフロー全体の設計意図である。

リストに無いことを「取扱不可」と断定しないのは、リストが利用者の手動更新に
依存していて古くなりうるため。`unknown` と `not_listed` を分けておかないと、
更新を忘れた月に全銘柄が「買えない」と表示され、機能そのものが信用を失う。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path

from autoscreener.symbols import normalize_symbol, symbol_variants

TRADABLE = "tradable"
NOT_LISTED = "not_listed"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class BrokerCoverage:
    """1証券会社の取扱銘柄集合。"""

    broker: str
    symbols: frozenset[str]
    source_path: Path
    loaded_at: datetime.date  # ファイルの mtime。古さをUIに出すため


@dataclass(frozen=True)
class TradabilityResult:
    status: str  # TRADABLE / NOT_LISTED / UNKNOWN
    brokers: list[str]  # 取扱のある証券会社名(status=TRADABLE のときのみ非空)


def _parse_symbol_file(path: Path) -> frozenset[str]:
    """1行1ティッカーのプレーンテキストを読む。`#` 始まりはコメント、空行は無視。"""
    symbols: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # 表記ゆれ(BRK.B / BRK-B)を読み込み時点で両方登録しておく。
        # 判定側(evaluate_tradability)でも symbol_variants を通すため片方だけで
        # 足りるが、ここでも両方持たせておくのが安全側(将来ここを直接参照する
        # コードが増えても表記ゆれの罠を踏まない)。
        symbols |= symbol_variants(stripped)
    return frozenset(symbols)


def load_broker_coverage(directory: Path | None = None) -> list[BrokerCoverage]:
    """`config/tradability/*.txt` をすべて読む。ディレクトリが無ければ空リスト。

    **ディレクトリが存在しないことをエラーにしない。** 30.1.3のとおり手動更新の
    運用であり、未整備の状態が正常系である(evaluate_tradability が全銘柄
    UNKNOWN を返すことで表現される)。
    """
    from autoscreener.config import CONFIG_DIR

    target = directory or CONFIG_DIR / "tradability"
    if not target.is_dir():
        return []

    coverage: list[BrokerCoverage] = []
    for path in sorted(target.glob("*.txt")):
        broker = path.stem
        symbols = _parse_symbol_file(path)
        mtime = datetime.datetime.fromtimestamp(path.stat().st_mtime, tz=datetime.timezone.utc).date()
        coverage.append(BrokerCoverage(broker=broker, symbols=symbols, source_path=path, loaded_at=mtime))
    return coverage


def evaluate_tradability(symbol: str, coverage: list[BrokerCoverage]) -> TradabilityResult:
    """純粋関数。coverage が空なら必ず UNKNOWN を返す。

    リストが1つでもあれば、どのリストにも載っていない銘柄は `not_listed`
    (「調べたが無かった」)にできる。リストが1つも無ければ何も調べていないので
    `unknown` にとどめる——この分岐が30.2.1の設計方針そのもの。
    """
    if not coverage:
        return TradabilityResult(status=UNKNOWN, brokers=[])

    variants = symbol_variants(symbol)
    brokers = sorted(bc.broker for bc in coverage if variants & bc.symbols)
    if brokers:
        return TradabilityResult(status=TRADABLE, brokers=brokers)
    return TradabilityResult(status=NOT_LISTED, brokers=[])


class _CachedCoverage:
    """ディレクトリの mtime を見て、変わっていたら読み直す小さなキャッシュ。

    `functools.lru_cache` を使わないのは、開発中にリストファイルを差し替えた
    ときにAPIプロセスを再起動しなくても反映されるようにするため(30.2.1)。
    ディレクトリ自体のmtimeに加え、中のファイル群の最大mtimeも見る——
    多くのファイルシステムでは既存ファイルの中身を書き換えてもディレクトリの
    mtimeは変わらないため。
    """

    def __init__(self) -> None:
        self._directory: Path | None = None
        self._signature: tuple[float, ...] | None = None
        self._coverage: list[BrokerCoverage] = []

    def _current_signature(self, directory: Path) -> tuple[float, ...]:
        if not directory.is_dir():
            return ()
        return tuple(sorted(p.stat().st_mtime for p in directory.glob("*.txt")))

    def get(self, directory: Path) -> list[BrokerCoverage]:
        signature = self._current_signature(directory)
        if directory != self._directory or signature != self._signature:
            self._coverage = load_broker_coverage(directory)
            self._directory = directory
            self._signature = signature
        return self._coverage


_cache = _CachedCoverage()


def get_cached_broker_coverage(directory: Path | None = None) -> list[BrokerCoverage]:
    """API層から呼ぶ、mtimeベースでキャッシュされる取扱銘柄一覧。"""
    from autoscreener.config import CONFIG_DIR

    target = directory or CONFIG_DIR / "tradability"
    return _cache.get(target)


__all__ = [
    "TRADABLE",
    "NOT_LISTED",
    "UNKNOWN",
    "BrokerCoverage",
    "TradabilityResult",
    "load_broker_coverage",
    "evaluate_tradability",
    "get_cached_broker_coverage",
    "normalize_symbol",
]
