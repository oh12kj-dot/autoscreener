"""A-3の再現テスト(docs/racr_wp_a_operational_safety_2026-09-04.md、監査§10.2)。

2026-09-03の実障害:`apply_gates` が全銘柄を1つの `session_scope()` 内で
ループしている間に `ticker_id=24528` が外部から削除され、`UniverseSnapshot`
insertがFK違反になった。PostgreSQLはFK違反したsessionを中断状態にするため、
**その1件の削除が、既に判定済み・未判定を問わず残り数千件ぶんの結果を
巻き込んで日次全体を落とした**。

`docker compose up -d` で起動済みのローカル開発用Postgres(専用テストDB。
`tests/conftest.py` のA-1ガードにより `TEST_DATABASE_URL` 必須)に対して
実行する(test_apply_gates_point_in_time.py と同じ方針)。

タイミングに依存せず決定的に再現するため、`_gather_gate_input` を差し替え、
特定の1銘柄を処理する「その瞬間」に**別のセッション**からTickerを削除・
コミットする——これは「テストが別プロセスの並行削除を模す」ものであり、
本番のFK違反(ticker削除→FK違反)と同じ因果関係を作る。
"""

from __future__ import annotations

import datetime

import pytest

from autoscreener.batch import apply_gates as apply_gates_module
from autoscreener.batch.apply_gates import apply_gates
from autoscreener.config import load_universe_config
from autoscreener.db.models import Ticker, UniverseSnapshot
from autoscreener.db.session import session_scope

_PREFIX = "ZZGATEDEL"
_SNAPSHOT_DATE = datetime.date(2099, 3, 1)


def _cleanup() -> None:
    with session_scope() as session:
        tickers = session.query(Ticker).filter(Ticker.symbol.like(f"{_PREFIX}%")).all()
        ticker_ids = [t.id for t in tickers]
        if ticker_ids:
            session.query(UniverseSnapshot).filter(UniverseSnapshot.ticker_id.in_(ticker_ids)).delete(
                synchronize_session=False
            )
        for ticker in tickers:
            session.delete(ticker)


@pytest.fixture
def three_tickers():
    _cleanup()
    universe_config = load_universe_config()
    ids: list[int] = []
    with session_scope() as session:
        for i in range(3):
            ticker = Ticker(symbol=f"{_PREFIX}{i}", market=universe_config.market, sector="Technology")
            session.add(ticker)
            session.flush()
            ids.append(ticker.id)
    yield ids
    _cleanup()


def test_ticker_deleted_mid_loop_does_not_fail_the_whole_gate_stage(three_tickers, monkeypatch):
    """監査10.2の障害を再現し、A-3の修正後にそれが緑になることを確認する。"""
    victim_id = three_tickers[1]  # 3件の真ん中を狙い、前後が処理を継続できるかを見る
    real_gather = apply_gates_module._gather_gate_input

    def _gather_and_delete_victim(session, ticker, snapshot_date):
        if ticker.id == victim_id:
            # 監査10.2の再現:このtickerを処理している最中に、別セッション
            # (=別プロセスを模す)が同じ行を削除してコミットする。
            with session_scope() as other_session:
                other_session.query(Ticker).filter_by(id=victim_id).delete()
        return real_gather(session, ticker, snapshot_date)

    monkeypatch.setattr(apply_gates_module, "_gather_gate_input", _gather_and_delete_victim)

    universe_config = load_universe_config()
    result = apply_gates(_SNAPSHOT_DATE, universe_config=universe_config)

    # 削除された銘柄は「黙って無視」ではなく件数として見える
    # (A-3受け入れ条件、監査§10.4修正案6)。
    assert result["skipped_missing_tickers"] >= 1
    assert victim_id in result.get("skipped_missing_ticker_ids_sample", [])

    # 日次全体は落ちない(=例外が apply_gates() から送出されずここまで来た事
    # 自体が最初の証拠)。削除された銘柄の前後も正常にupsertされていることを
    # 確認する——1件のFK違反が他銘柄を巻き込んでいないことの直接証拠。
    survivor_ids = [tid for tid in three_tickers if tid != victim_id]
    with session_scope() as session:
        survivor_snapshots = (
            session.query(UniverseSnapshot)
            .filter(UniverseSnapshot.ticker_id.in_(survivor_ids))
            .filter_by(snapshot_date=_SNAPSHOT_DATE)
            .all()
        )
        victim_snapshot = (
            session.query(UniverseSnapshot).filter_by(ticker_id=victim_id, snapshot_date=_SNAPSHOT_DATE).one_or_none()
        )

    assert len(survivor_snapshots) == 2
    assert {s.ticker_id for s in survivor_snapshots} == set(survivor_ids)
    assert all(s.included is False and s.exclusion_reason == "no_raw_data" for s in survivor_snapshots)
    # 削除されたticker_idへは(参照先が無いので)universe_snapshot自体を
    # 書かない——存在しないtickerの判定を捏造しないことも正しい挙動である。
    assert victim_snapshot is None


def test_apply_gates_commits_in_small_batches(monkeypatch):
    """A-3受け入れ条件の一部:全件を1つの `session_scope()` にまとめず、
    小さいバッチごとに独立してcommitしていること。

    バッチサイズを2に差し替え、5銘柄(3バッチ)を処理させる。`session_scope`
    の呼び出し回数を数え、「idの先読み1回 + バッチ3回 = 4回」であることを
    確認する——以前の実装は全体で1回しか `session_scope` を開かなかった
    ため、この検証は「最後に1回だけcommitする設計」との違いを直接示す。
    """
    monkeypatch.setattr(apply_gates_module, "_GATE_COMMIT_BATCH_SIZE", 2)
    universe_config = load_universe_config()
    _cleanup()
    ids: list[int] = []
    try:
        with session_scope() as session:
            for i in range(5):
                ticker = Ticker(symbol=f"{_PREFIX}B{i}", market=universe_config.market, sector="Technology")
                session.add(ticker)
                session.flush()
                ids.append(ticker.id)

        real_session_scope = apply_gates_module.session_scope
        call_count = 0

        from contextlib import contextmanager

        @contextmanager
        def _counting_session_scope():
            nonlocal call_count
            call_count += 1
            with real_session_scope() as session:
                yield session

        monkeypatch.setattr(apply_gates_module, "session_scope", _counting_session_scope)

        result = apply_gates(_SNAPSHOT_DATE, universe_config=universe_config)
        assert result["no_data"] == 5
        # 先読み1回(ticker_idsを集める) + ceil(5/2)=3バッチ = 4回。
        assert call_count == 4

        with real_session_scope() as session:
            snapshots = (
                session.query(UniverseSnapshot)
                .filter(UniverseSnapshot.ticker_id.in_(ids))
                .filter_by(snapshot_date=_SNAPSHOT_DATE)
                .all()
            )
        assert len(snapshots) == 5
    finally:
        _cleanup()
