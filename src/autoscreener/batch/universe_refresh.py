"""週次ユニバース更新(5.2、4章)。

NASDAQ/NYSEの公開シンボルディレクトリから候補銘柄を再取得し、`tickers` を
更新した上で当日の `universe_snapshots` を記録する(14.3:生存バイアス対策の
ため、廃止銘柄をマスタから削除せず「その日のスナップショット」として残す)。

ここで記録するのは「ETF・テスト銘柄・非普通株式のみを除いた広い候補プール」で
あって、ゲート(15.2・15.6)を通過した銘柄ではない。したがってスタブ行は
`included=False` / `exclusion_reason="pending_gate_evaluation"` として書き、
同日中に走る `apply_gates` が実際の判定結果で上書きする。

**2026-08-24修正**:以前はスタブを `included=True` で書いていた。日次パイプラインは
必ず直後に `apply_gates` を走らせる前提だったが、`apply_gates` が失敗した日・
`collect-universe` を単独実行した場合には、ゲート未評価の全候補(5,000銘柄超)が
「合格」として残り、`run_scoring` がそれを対象にしてしまう。既定値は安全側
(未評価=対象外)にしておく。
"""

from __future__ import annotations

import logging
from datetime import date

from autoscreener.collectors.snapshot_collector import get_or_create_ticker
from autoscreener.collectors.universe_source import fetch_universe_candidates
from autoscreener.dates import utc_today
from autoscreener.db.models import Ticker, UniverseSnapshot
from autoscreener.db.session import session_scope

logger = logging.getLogger(__name__)

# B-8:掃引が一度に隔離してよい上限(追跡中の銘柄数に対する比率)。これを
# 超えるなら「本当にそれだけ上場廃止された」よりも「候補リストの取得・パースが
# 部分的に失敗した」ほうが圧倒的にありそうなので、掃引を見送る。
# 実勢では週あたりの上場廃止は5,300銘柄中せいぜい数十(<1%)なので25%は十分に緩い。
_MAX_STALE_RATIO = 0.25
# 上の比率ガードを適用する最小の追跡銘柄数。数銘柄しか無いDB(テスト等)では
# 比率そのものが意味を持たないため、本番規模でのみ効かせる。
_RATIO_GUARD_MIN_TRACKED = 100


def refresh_universe(snapshot_date: date | None = None) -> int:
    snapshot_date = snapshot_date or utc_today()
    candidates = fetch_universe_candidates()
    logger.info("fetched %d universe candidates for %s", len(candidates), snapshot_date)
    candidate_symbols = {c.symbol for c in candidates}

    with session_scope() as session:
        for candidate in candidates:
            ticker = get_or_create_ticker(session, candidate.symbol, market="US")
            existing_snapshot = (
                session.query(UniverseSnapshot)
                .filter_by(snapshot_date=snapshot_date, ticker_id=ticker.id)
                .one_or_none()
            )
            if existing_snapshot is None:
                session.add(
                    UniverseSnapshot(
                        snapshot_date=snapshot_date,
                        ticker_id=ticker.id,
                        included=False,
                        exclusion_reason="pending_gate_evaluation",
                    )
                )

        # B-8(2026-08-26、model_audit_v4_2026-08-26.md):`tickers` には候補フィルタ
        # (`universe_source.filter_candidates`)が実装される**前**に登録された
        # 残骸が残っていた——実データで優先株等24銘柄("AHL$D"等)。これらは
        # 毎日の収集対象であり続け、存在しないシンボルへの404を吐き続けていた。
        # 今回の候補リストに存在しない US銘柄を隔離する。**削除はしない**
        # (`forward_returns`・`backtest_runs` が参照している可能性があるため)。
        # `is_quarantined` は既存の週次リトライ機構(18.1)が対象にしているので、
        # 候補リストに復帰すれば自動的に収集が再開する。
        #
        # **部分取得への防御**:この掃引の基準は外部HTTP(NASDAQ Trader)の
        # パース結果である。レスポンスが途中で切れた・先方のフォーマットが
        # 変わってパースが減った、という失敗は例外を投げずに「候補が減った」
        # 形で現れるため、そのまま信じるとユニバースを大量に誤隔離する
        # (そして次の週次更新まで誰も気づけない)。隔離しようとしている件数が
        # 追跡中の銘柄に対して大きすぎる場合は、掃引ごと見送ってERRORを出す。
        if not candidate_symbols:
            logger.warning("universe candidate list is empty — skipping the stale-ticker sweep")
            return len(candidates)

        tracked_count = (
            session.query(Ticker).filter(Ticker.market == "US", Ticker.is_quarantined.is_(False)).count()
        )
        stale = (
            session.query(Ticker)
            .filter(
                Ticker.market == "US",
                Ticker.is_quarantined.is_(False),
                ~Ticker.symbol.in_(candidate_symbols),
            )
            .all()
        )
        if (
            tracked_count >= _RATIO_GUARD_MIN_TRACKED
            and len(stale) > tracked_count * _MAX_STALE_RATIO
        ):
            logger.error(
                "stale-ticker sweep would quarantine %d of %d tracked tickers (>%.0f%%) — "
                "the candidate list (%d symbols) is more likely truncated than the universe "
                "having shrunk that much; skipping the sweep",
                len(stale),
                tracked_count,
                _MAX_STALE_RATIO * 100,
                len(candidate_symbols),
            )
            return len(candidates)

        for ticker in stale:
            ticker.is_quarantined = True
        if stale:
            logger.info(
                "quarantined %d tickers no longer present in the universe candidate list", len(stale)
            )

    return len(candidates)
