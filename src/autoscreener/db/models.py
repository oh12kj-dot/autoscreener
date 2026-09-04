"""SQLAlchemy models (要件定義書 9章、および v1.1で追加した universe_snapshots /
ticker_aliases / forward_returns、v1.1 14.11の price_snapshots分離)。

`scores` と `forward_returns` はフェーズ3(スコアリングエンジン)まで書き込みが
発生しないが、スキーマは9章の設計を先に固定しておく(17章の実装順序どおり、
後からのマイグレーションで手戻りしないよう最初から定義する)。
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Ticker(Base):
    """銘柄マスタ。主キーは内部連番(14.5):ティッカー文字列は改名・再利用が
    起こりうるため同一性の主キーにしない。"""

    __tablename__ = "tickers"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    isin: Mapped[str | None] = mapped_column(String(12), nullable=True)
    market: Mapped[str] = mapped_column(String(10))  # "US" (13.2: 初期は米国株のみ)
    sector: Mapped[str | None] = mapped_column(String(100), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(150), nullable=True)
    listed_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)

    # 30.3.2:SECの企業識別子(10桁ゼロ埋め文字列)。EDGARのあらゆるAPIの鍵になる。
    # 文字列にするのは、ゼロ埋めの桁数がURLの仕様そのものであり、intにすると
    # 呼び出し側が毎回 f"{cik:010d}" を書くことになるため。
    cik: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)

    # D-4(docs/defect_and_edge_audit_2026-08-28.md):ベンチマーク(IWM/IWC/IJR/SPY)。
    # 価格は収集・バックフィルするが、ゲート判定・スコアリング・ランキングには
    # 一切混ぜない。ポートフォリオ・シミュレーションの超過CAGR算出に使う。
    is_benchmark: Mapped[bool] = mapped_column(Boolean, default=False)

    # 18.1: 失敗分類・隔離リストの状態
    delisted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(default=0)
    is_quarantined: Mapped[bool] = mapped_column(Boolean, default=False)
    last_attempted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    aliases: Mapped[list[TickerAlias]] = relationship(back_populates="ticker")


class TickerAlias(Base):
    """ティッカー来歴(14.5)。改名・シンボル再利用があっても内部IDで名寄せできる
    ようにするための履歴テーブル。"""

    __tablename__ = "ticker_aliases"
    __table_args__ = (UniqueConstraint("symbol", "effective_from", name="uq_alias_symbol_effective_from"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(20))
    effective_from: Mapped[datetime.date] = mapped_column(Date)
    effective_to: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    ticker: Mapped[Ticker] = relationship(back_populates="aliases")


class UniverseSnapshot(Base):
    """その日時点での対象ユニバース構成銘柄(14.3:生存バイアス対策)。
    廃止銘柄をマスタから削除せず、日次のスナップショットとして「その日に
    対象だったか」を残すことで、後年のバックテストがサバイバーシップバイアス
    フリーになる(できるのはあくまで擬似バックテストであり、14.3のとおり
    ポイントインタイムの財務データ自体は取得できない点に注意)。"""

    __tablename__ = "universe_snapshots"
    __table_args__ = (UniqueConstraint("snapshot_date", "ticker_id", name="uq_universe_snapshot_date_ticker"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_date: Mapped[datetime.date] = mapped_column(Date, index=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    included: Mapped[bool] = mapped_column(Boolean)
    exclusion_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RawSnapshot(Base):
    """yfinanceから取得した生データのスナップショット(9章、5.3)。

    14.11のとおり、内容が変化した場合のみ新規行を追加する想定(財務データは
    実質四半期に1回しか変わらないため)。`content_hash` で変化検知を行い、
    変化がなければ `last_seen_date` だけ更新してストレージを節約する。
    """

    __tablename__ = "raw_snapshots"
    __table_args__ = (Index("ix_raw_snapshots_ticker_date", "ticker_id", "snapshot_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    snapshot_date: Mapped[datetime.date] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(30), default="yfinance")
    payload: Mapped[dict] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    last_seen_date: Mapped[datetime.date] = mapped_column(Date)
    available_from: Mapped[datetime.date] = mapped_column(Date)  # 14.3: 先読みバイアス対策

    is_valid: Mapped[bool] = mapped_column(Boolean, default=True)  # 14.10
    validation_errors: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PriceSnapshot(Base):
    """価格・出来高(14.11:JSONに埋め込まず正規化したOHLCVテーブルに分離)。"""

    __tablename__ = "price_snapshots"
    __table_args__ = (UniqueConstraint("ticker_id", "trade_date", name="uq_price_ticker_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    trade_date: Mapped[datetime.date] = mapped_column(Date, index=True)
    open: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    high: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    low: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    close: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # 13.4: 分割調整後の発行済株式数。希薄化率計算の基礎データ。
    shares_outstanding: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # 発行済株式数は週次＋イベント時取得。値を持ち越した日を「当日観測」と
    # 誤認しないよう、実取得日と状態を価格日付から独立して保持する。
    shares_observed_at: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    shares_coverage_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # D-11(docs/defect_and_edge_audit_2026-08-28.md):その取引日の1株あたり配当(ex-date)。
    # `_realized_return` を価格リターンから**総リターン**へ変えるのに使う。配当が
    # 抜けているとユニバース基準率(リフトの分母)が系統的に低く出て、リフトが
    # 過大に見える。分割と同じ経路(yfinance actions 列)で拾う。
    dividend: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CollectionCursor(Base):
    """外部増分収集の完了カーソル。失敗時は進めず、次回に再取得する。"""

    __tablename__ = "collection_cursors"
    __table_args__ = (UniqueConstraint("source", "scope", name="uq_collection_cursor_source_scope"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(40))
    scope: Mapped[str] = mapped_column(String(80))
    cursor_date: Mapped[datetime.date] = mapped_column(Date)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SourceProcessingLedger(Base):
    """原文×抽出器バージョンの処理結果。no-findingも保存して再走査を防ぐ。"""

    __tablename__ = "source_processing_ledger"
    __table_args__ = (
        UniqueConstraint(
            "source_type", "source_key", "processor", "processor_version",
            name="uq_source_processing_ledger_key",
        ),
        Index("ix_source_processing_ledger_processor", "processor", "processor_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int | None] = mapped_column(
        ForeignKey("tickers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_type: Mapped[str] = mapped_column(String(30))
    source_key: Mapped[str] = mapped_column(String(100))
    processor: Mapped[str] = mapped_column(String(60))
    processor_version: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(30))
    attempted_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class CollectionLog(Base):
    """データ収集ジョブの実行ログ(9章、18.1、18.7)。

    `ticker_id` が NULL の行は、サーキットブレーカー発動などバッチ単位の
    イベントを表す。
    """

    __tablename__ = "collection_logs"
    __table_args__ = (Index("ix_collection_logs_run_status", "run_id", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(index=True)
    ticker_id: Mapped[int | None] = mapped_column(ForeignKey("tickers.id"), nullable=True, index=True)
    snapshot_date: Mapped[datetime.date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(30))
    # success / transient_failure / permanent_failure / empty_response /
    # empty_response_delisted(B-5) / parse_failure / sanitized(B-7、旧名
    # invalid_dataは「除外された」ように誤読されるため改称。一部フィールドを
    # Noneへ差し替えたが行自体は保存・採用している) / quarantined /
    # circuit_breaker_tripped / run_started(B-6)・run_finished(B-6、いずれも
    # ticker_id IS NULLのバッチ単位マーカー)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Score(Base):
    """算出済みスコア(27章:実現時価総額倍率モデル)。

    v3で列構成を入れ替えた。旧v2の `overall_score` / `subscores` /
    `subscore_fallback` / `subscore_metric_coverage` / `coverage_ratio` は、
    8軸パーセンタイル加重幾何平均という廃止済みの構造に固有の列であり、
    新モデルには対応する概念が無いため削除した(27.1)。

    新しいランキングキーは `probability` = P(MOIC >= target_moic)。
    `factors` には15.1の恒等式に対応する5因子分解と診断値を保持する。
    """

    __tablename__ = "scores"
    __table_args__ = (
        UniqueConstraint("ticker_id", "score_date", "scoring_version", name="uq_score_ticker_date_version"),
        Index("ix_scores_date_version_probability", "score_date", "scoring_version", "probability"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    score_date: Mapped[datetime.date] = mapped_column(Date, index=True)
    scoring_version: Mapped[str] = mapped_column(String(20))  # 14.6
    config_hash: Mapped[str] = mapped_column(String(64))  # 14.6

    # P(MOIC >= target_moic)。0.0〜1.0。14.2のとおり通常は0.0001〜0.05のオーダー。
    probability: Mapped[float | None] = mapped_column(Numeric(10, 8), nullable=True)
    # 1株あたり実現倍率の中央値(点推定)
    median_moic: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    log_moic_mu: Mapped[float | None] = mapped_column(Numeric(10, 5), nullable=True)
    log_moic_sigma: Mapped[float | None] = mapped_column(Numeric(10, 5), nullable=True)
    survival_probability: Mapped[float | None] = mapped_column(Numeric(6, 5), nullable=True)
    # 28.8:実測で較正済みの「バックテストのホライズンでオンペースに乗る確率」。
    # `probability`(7年で10倍)とは**別の量**である。7年後の実測は原理的に
    # 今日存在しないため較正できないが、こちらは擬似バックテストが実際に
    # 観測している事象なので較正できる。較正写像が無いときは NULL。
    calibrated_on_pace_probability: Mapped[float | None] = mapped_column(
        Numeric(10, 8), nullable=True
    )
    # 5因子分解(revenue_multiple / margin_multiple / multiple_change /
    # leverage_effect / dilution_drag)と診断値(初期成長率・終端粗利率・
    # 現在および終端のEV/GrossProfit・健全性指標・規模の事前分布)
    factors: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # 27.24:モデルの入力そのもの(`MoicInputs` + セクター中央値)。
    # これを保存しておくと、APIが**任意のホライズン・目標倍率で厳密に再計算**できる
    # (「3年で3倍」「5年で5倍」など)。対数正規を時間で引き伸ばす近似ではなく、
    # 成長の減衰・希薄化の複利・生存確率をその年数で計算し直せる。
    # 1銘柄あたり十数個の数値にすぎず、raw_snapshots を読み直すより桁違いに速い。
    inputs: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # A-1(docs/defect_and_edge_audit_2026-08-28.md D-12):このスコアが実際に読んだ
    # データの日付。`score_date` と比べて何営業日古いかをAPIが `data_age_days` として
    # 返す。日次収集が一斉隔離などで止まっても `run_scoring` は前日以前のデータで
    # 当日付のランキングを書けてしまうため、その事実を利用者に見せる。
    price_as_of: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    financials_as_of: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ModelRun(Base):
    """Append-only execution record for an independent model version."""

    __tablename__ = "model_runs"
    __table_args__ = (
        CheckConstraint("mode IN ('shadow', 'active', 'legacy')", name="ck_model_runs_mode"),
        CheckConstraint("status IN ('running', 'succeeded', 'failed')", name="ck_model_runs_status"),
        Index("ix_model_runs_version_as_of", "model_version", "as_of"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    model_version: Mapped[str] = mapped_column(String(20))
    config_hash: Mapped[str] = mapped_column(String(64))
    as_of: Mapped[datetime.date] = mapped_column(Date, index=True)
    mode: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), index=True)
    population_count: Mapped[int] = mapped_column(default=0)
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metrics: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    warnings: Mapped[list | None] = mapped_column(JSONB, nullable=True)


class ModelScore(Base):
    """Per-ticker state and distribution output belonging to one model run."""

    __tablename__ = "model_scores"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "ticker_id", "target_horizon_years", "target_moic",
            name="uq_model_scores_run_ticker_target",
        ),
        Index("ix_model_scores_run_confidence", "run_id", "confidence"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("model_runs.id", ondelete="CASCADE"), index=True
    )
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    target_horizon_years: Mapped[int] = mapped_column()
    target_moic: Mapped[float] = mapped_column(Numeric(12, 4))
    distribution: Mapped[dict] = mapped_column(JSONB)
    states: Mapped[dict] = mapped_column(JSONB)
    features: Mapped[dict] = mapped_column(JSONB)
    confidence: Mapped[float] = mapped_column(Numeric(6, 5))
    warnings: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ObjectiveScore(Base):
    """Re-rankable objective output derived from an immutable v5 distribution."""

    __tablename__ = "objective_scores"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "ticker_id", "objective",
            name="uq_objective_scores_run_ticker_objective",
        ),
        Index("ix_objective_scores_run_objective_rank", "run_id", "objective", "rank"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("model_runs.id", ondelete="CASCADE"), index=True
    )
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    objective: Mapped[str] = mapped_column(String(50))
    score_value: Mapped[float | None] = mapped_column(Numeric(24, 12), nullable=True)
    rank: Mapped[int | None] = mapped_column(nullable=True)
    explanation: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ModelFeatureValue(Base):
    """D-4 (docs/racr_wp_d_reliability_layer_2026-09-04.md; audit P2, its
    ``db/models.py`` file-level entry): the per-ticker/per-feature layer
    that makes a v5 run reproducible and auditable -- one row per
    (run, ticker, feature) carrying the value, source, availability
    (``coverage_status``), reliability, and missing reason.

    ``model_scores.features`` (the existing nested JSONB blob) is kept
    unchanged for backward compatibility; this table is a queryable,
    indexable projection of the same evidence written alongside it, not a
    replacement -- a SQL ``WHERE feature_key = ... AND missing_reason IS
    NOT NULL`` is not possible against a JSONB blob without unnesting it
    every time.
    """

    __tablename__ = "model_feature_values"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "ticker_id", "feature_key",
            name="uq_model_feature_values_run_ticker_feature",
        ),
        Index("ix_model_feature_values_run_feature", "run_id", "feature_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("model_runs.id", ondelete="CASCADE"), index=True
    )
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    feature_key: Mapped[str] = mapped_column(String(64))
    value: Mapped[float | None] = mapped_column(Numeric(24, 12), nullable=True)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    coverage_status: Mapped[str] = mapped_column(String(30))
    # Some signal status strings are long, descriptive reason codes (e.g.
    # "fred_vintage_unsupported_historical_backtest_prohibited", 57 chars) --
    # sized generously rather than truncating an explanatory reason.
    status: Mapped[str] = mapped_column(String(100))
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    reliability: Mapped[float | None] = mapped_column(Numeric(6, 5), nullable=True)
    missing_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    observed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evidence: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ForwardReturn(Base):
    """前方検証(14.3)。スコア確定日から各ホライズン後の実現リターンを記録する。

    **`settlement` 列の意義(27.11)**:以前は、上場廃止された銘柄の価格収集が
    止まると `_exit_price` が None になり、その行が**書かれないまま**になって
    いた。上場廃止は −90% 〜 −100% という最悪の結果と強く相関するため、
    デシル単調性・リフト倍率(14.2)が実態より良く出る生存バイアスが、
    検証資産そのものに入り込んでいた。14.3が「廃止銘柄をマスタから削除しない」
    と定めていた意図と正面から矛盾する実装漏れだった。

    現在は上場廃止銘柄も最終観測価格で決済して行を書き、`settlement` に
    どちらで確定したかを残す:

    - ``"market"``   : 目標日前後の市場価格で通常どおり評価した
    - ``"delisted"`` : 上場廃止により最終観測価格(取れなければ −100%)で確定した
    """

    __tablename__ = "forward_returns"
    __table_args__ = (UniqueConstraint("ticker_id", "base_date", "horizon", name="uq_forward_return"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    base_date: Mapped[datetime.date] = mapped_column(Date, index=True)
    horizon: Mapped[str] = mapped_column(String(10))  # "1M"/"3M"/"6M"/"1Y"/"3Y"/"5Y"/"7Y"
    realized_return: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    settlement: Mapped[str | None] = mapped_column(String(10), nullable=True)
    computed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ModelV5ForwardReturn(Base):
    """Model v5 Phase 7 forward validation (Issue #3 section 27): the v5
    analogue of ``ForwardReturn``, append-only, keyed by ``run_id`` (not
    ``ticker_id``/``base_date`` like the v4 table) because v5's
    ``model_runs`` already carries the model-version/config identity that a
    v5 score belongs to -- multiple v5 runs can legitimately score the same
    ticker on the same day under different configs, which the v4 table's
    ``(ticker_id, base_date, horizon)`` key does not need to support since
    v4 has exactly one ``scores`` row per ``(ticker, score_date,
    scoring_version)``.

    Reuses ``scoring/forward_validation.py``'s entry/exit-price and
    delisted-settlement logic exactly (``_entry_price``/``_exit_price``/
    ``_is_delisted``/``_settle_delisted``) via
    ``run_forward_validation_v5()`` in that same module -- this table only
    changes the source (``model_scores`` instead of ``scores``) and the key
    shape, not the settlement definition itself.
    """

    __tablename__ = "model_v5_forward_returns"
    __table_args__ = (
        UniqueConstraint("run_id", "ticker_id", "horizon", name="uq_model_v5_forward_return"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("model_runs.id"), index=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    base_date: Mapped[datetime.date] = mapped_column(Date, index=True)
    horizon: Mapped[str] = mapped_column(String(10))  # "1M"/"3M"/"6M"/"1Y"/"3Y"/"5Y"/"7Y"
    realized_return: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    settlement: Mapped[str | None] = mapped_column(String(10), nullable=True)
    computed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BacktestRun(Base):
    """擬似バックテストの実行結果(27.8・14.2)。

    前方検証(`forward_returns`)が実データで成熟するには年単位の時間がかかる。
    その間の唯一の較正手段が、`scoring/point_in_time.py` で再構成した過去時点の
    入力に対して同じモデルを走らせるこのジョブである。設定を変えるたびに1行
    追加され、`metrics` に14.2のKPI(デシル単調性・リフト倍率・較正誤差)が入る。

    `config_snapshot` に当時の `config/scoring.yaml` の全内容を保存するのは、
    後から「どのパラメータでこのKPIが出たのか」を再現できるようにするため
    (14.6のバージョニングと同じ考え方)。
    """

    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    scoring_version: Mapped[str] = mapped_column(String(20))
    config_hash: Mapped[str] = mapped_column(String(64), index=True)
    horizon_days: Mapped[int] = mapped_column()
    rebalance_dates: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    observation_count: Mapped[int] = mapped_column()
    metrics: Mapped[dict] = mapped_column(JSONB)
    config_snapshot: Mapped[dict] = mapped_column(JSONB)
    # 28.8:この実行の観測から学習した「生の予測確率 → 実測頻度」の単調写像。
    # スコアリングは同じ scoring_version の最新実行のものを使う。較正写像を
    # 実行と同じ行に置くのは、**その設定で得られた観測にしか妥当しない**ため
    # (設定を変えたら較正もやり直しになる、という関係を構造で表す)。
    calibration_map: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # 28.12:評価日ごとの的中率の散らばりから推定した資産相関。
    # ポートフォリオ確率(「上位N銘柄で少なくとも1つ当たる確率」)に使う。
    asset_correlation: Mapped[float | None] = mapped_column(Numeric(6, 5), nullable=True)

    # D-2(docs/defect_and_edge_audit_2026-08-28.md):評価日の間隔がホライズン未満で、
    # 保有期間が重なっている実行かどうか。False(非重複)の実行が「正直な検出力」で
    # あり、`run-backtest --non-overlapping` で併走させる。
    overlapping: Mapped[bool] = mapped_column(Boolean, default=True)


class Filing(Base):
    """SEC提出書類のメタデータ(30.3.5)。

    本文は保存しない。数十MBの文書を全銘柄ぶん貯めるとDBが破裂し、
    しかも再取得はいつでもできる(SECのアーカイブは消えない)。保存するのは
    **判定に使った結論と、その根拠を人間が確認しに行くためのURL**だけ。
    """

    __tablename__ = "filings"
    __table_args__ = (
        UniqueConstraint("ticker_id", "accession_number", name="uq_filing_ticker_accession"),
        Index("ix_filings_ticker_filed", "ticker_id", "filed_date"),
        Index("ix_filings_form_filed", "form", "filed_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    cik: Mapped[str] = mapped_column(String(10))
    accession_number: Mapped[str] = mapped_column(String(25))
    form: Mapped[str] = mapped_column(String(20))
    # 14.3:**提出日がポイントインタイムの基準**である。report_date(決算期末)
    # ではない——期末の数字は、提出されるまで市場も我々も知りようがない。
    filed_date: Mapped[datetime.date] = mapped_column(Date, index=True)
    report_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    items: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # 8-Kのアイテム番号
    primary_document: Mapped[str | None] = mapped_column(String(200), nullable=True)
    document_url: Mapped[str | None] = mapped_column(String(400), nullable=True)
    # 30.4:本文解析の結果(going_concern / material_weakness / 抜粋)。
    # 解析していない場合は NULL。「解析した結果なにも無かった」(空dict)と
    # 「まだ解析していない」(NULL)を区別できるようにする。
    analysis: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class XbrlFact(Base):
    """SEC XBRL の実績値(30.5)。

    companyfacts の全量ではなく、**モデルの入力と突き合わせる4概念だけ**を
    保存する。全量を入れると1銘柄あたり数MBのJSONになり、しかも使わない。

    `filed_date` を必ず持つのは 14.3(先読みバイアス)のため。決算期末
    (`period_end`)の数字は、提出されるまで知りようがない。過去日の再現に
    使うときは `filed_date <= 基準日` で絞る。
    """

    __tablename__ = "xbrl_facts"
    __table_args__ = (
        UniqueConstraint(
            "ticker_id", "tag", "period_end", "form", "accession_number",
            name="uq_xbrl_fact",
        ),
        Index("ix_xbrl_facts_ticker_tag_end", "ticker_id", "tag", "period_end"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    taxonomy: Mapped[str] = mapped_column(String(20))  # "us-gaap" / "dei"
    tag: Mapped[str] = mapped_column(String(100))
    unit: Mapped[str] = mapped_column(String(20))  # "USD" / "shares"
    period_start: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[datetime.date] = mapped_column(Date)
    value: Mapped[float] = mapped_column(Numeric(24, 4))
    form: Mapped[str] = mapped_column(String(20))
    accession_number: Mapped[str] = mapped_column(String(25))
    filed_date: Mapped[datetime.date] = mapped_column(Date, index=True)
    fiscal_year: Mapped[int | None] = mapped_column(nullable=True)
    fiscal_period: Mapped[str | None] = mapped_column(String(4), nullable=True)  # FY/Q1..Q4
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Alert(Base):
    """保有・追跡銘柄で新たに点灯した監視項目(30.7.4)。

    レッドフラグ(30.4)や監視指標(30.7.3)は毎日**再評価すれば同じ結論**が
    出る。それでも行として保存するのは、**「いつ初めて点灯したか」が
    それ自体で情報**だから——決算の翌日に点いたのか、3か月前から点いていたの
    かで、対応は変わる。導出結果ではなく状態遷移を記録するテーブルである。
    """

    __tablename__ = "alerts"
    __table_args__ = (
        UniqueConstraint("ticker_id", "code", "triggered_on", name="uq_alert_ticker_code_date"),
        Index("ix_alerts_triggered", "triggered_on"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    code: Mapped[str] = mapped_column(String(50))
    severity: Mapped[str] = mapped_column(String(10))
    source: Mapped[str] = mapped_column(String(20))  # "red_flag" / "metric" / "premortem"
    triggered_on: Mapped[datetime.date] = mapped_column(Date)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # 人間が見たことを記録する。CLI `tenx ack <id>` から書く(APIは読み取り専用)
    acknowledged_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InsiderTransaction(Base):
    """Form 4 由来のインサイダー取引(J-7、docs/investment_decision_gap_2026-08-29.md)。

    **原則3:ゲートにもスコアにも入れない。** 表示とアラートのみ。インサイダー
    売却は権利行使・納税・分散のいずれでも起きるため、UI は色で断定しない。
    """

    __tablename__ = "insider_transactions"
    __table_args__ = (
        UniqueConstraint(
            "accession_number", "insider_name", "transaction_date", "transaction_code", "shares",
            name="uq_insider_transaction",
        ),
        Index("ix_insider_tx_ticker_date", "ticker_id", "transaction_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    accession_number: Mapped[str] = mapped_column(String(25))
    filed_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    transaction_date: Mapped[datetime.date] = mapped_column(Date)
    insider_name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str | None] = mapped_column(String(100), nullable=True)
    transaction_code: Mapped[str] = mapped_column(String(4))
    shares: Mapped[float] = mapped_column(Numeric(24, 4))
    price_usd: Mapped[float | None] = mapped_column(Numeric(20, 4), nullable=True)
    value_usd: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    is_derivative: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ShortInterest(Base):
    """FINRA の空売り残(J-7)。月2回・数営業日遅れ。**遅延日数を必ず画面に出す**
    (30.1.3 が「対象外」とした理由がこれである以上、出すなら鮮度の明示は必須)。
    原則3:ゲート・スコアには入れない。
    """

    __tablename__ = "short_interest"
    __table_args__ = (
        UniqueConstraint("ticker_id", "settlement_date", name="uq_short_interest"),
        Index("ix_short_interest_settlement", "settlement_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    settlement_date: Mapped[datetime.date] = mapped_column(Date)
    short_interest_shares: Mapped[float] = mapped_column(Numeric(24, 4))
    avg_daily_volume: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    days_to_cover: Mapped[float | None] = mapped_column(Numeric(14, 4), nullable=True)
    published_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EventCalendar(Base):
    """これから起きるイベント(J-6、docs/investment_decision_gap_2026-08-29.md)。

    **`scores` / `raw_snapshots` から物理的に分離した専用テーブル。** 次回決算日は
    現在時点のスナップショットしか取れず過去に遡れないため、モデル(`scoring/`)と
    バックテスト(`backtest/`)からは一切参照しない(27.16 のポイントインタイム
    汚染を再発させない)。`collected_on` は「いつ知った予定か」の記録。
    """

    __tablename__ = "event_calendar"
    __table_args__ = (
        UniqueConstraint("ticker_id", "event_type", "event_date", name="uq_event_calendar"),
        Index("ix_event_calendar_date", "event_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(20))  # 'earnings' / 'verification' / 'manual'
    event_date: Mapped[datetime.date] = mapped_column(Date)
    is_estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(30))  # 'yfinance' / 'note'
    collected_on: Mapped[datetime.date] = mapped_column(Date)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PipelineRun(Base):
    """日次パイプライン1回分の実行記録(14.15の運用監視、
    docs/daily_job_status_screen_2026-08-30.md §3.1)。

    `collection_logs` は収集工程の**銘柄単位**のログであり、パイプライン全体の
    実行単位ではない。両者は別の粒度なので別テーブルにする(collection_logs の
    run_id とは無関係な独立した uuid を持つ)。
    """

    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(unique=True, index=True)
    run_date: Mapped[datetime.date] = mapped_column(Date, index=True)  # utc_today()
    is_weekly: Mapped[bool] = mapped_column(Boolean, default=False)  # 月曜=週次工程あり
    trigger: Mapped[str] = mapped_column(String(20))  # "scheduled" / "manual"
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    # NULL = 実行中、またはプロセスが強制終了した(§4.3の孤児判定。API層でのみ
    # 死亡と見なし、DBのこの列自体は書き換えない)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # A-2(2026-09-04、docs/racr_wp_a_operational_safety_2026-09-04.md):
    # `PipelineRecorder.heartbeat()` が工程境界ごとに更新する実際の生存確認。
    # `started_at` だけでは「動いているが遅い」と「プロセスが死んで
    # `running` のまま残った」を区別できない——2026-09-03のgate stage FK違反は
    # 後者だった。`sweep_orphan_runs()` がこの列を見て、一定時間(既定90分)
    # 更新の無い `running` runを `aborted` へ確定する。
    last_heartbeat_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # "running" / "succeeded" / "degraded" / "failed" / "aborted"(§3.3、A-2)
    status: Mapped[str] = mapped_column(String(20), index=True)
    # §3.4 の健全性所見。空リストなら所見なし
    health: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PipelineStageRun(Base):
    """パイプラインの工程1つ分の実行記録(§3.2)。"""

    __tablename__ = "pipeline_stage_runs"
    __table_args__ = (
        UniqueConstraint("run_id", "stage", name="uq_stage_run_stage"),
        Index("ix_pipeline_stage_runs_run_seq", "run_id", "sequence"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_runs.run_id", ondelete="CASCADE"), index=True
    )
    stage: Mapped[str] = mapped_column(String(40))  # "collection" / "gates" / ...(§3.5)
    sequence: Mapped[int] = mapped_column()  # 実行順(表示順をDB側で決める)
    # "running" / "succeeded" / "failed" / "skipped"(§3.3)
    status: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 工程の戻り値(件数のdict)。失敗時はNone
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # skippedの理由"not_weekly"等 / failedの例外クラス名
    reason: Mapped[str | None] = mapped_column(String(60), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)  # str(exc)先頭2000字
    error_traceback: Mapped[str | None] = mapped_column(Text, nullable=True)  # 先頭8000字


class MacroSeries(Base):
    """FREDマクロ系列の観測値(30.8.2)。

    元文書 第09節。モデルのマルチプル項は「今の金利環境が7年続く」前提を
    暗黙に置いている。**スコアには一切接続しない**(30.8.3)——表示と人間への
    示唆に留める独立したテーブル。
    """

    __tablename__ = "macro_series"
    __table_args__ = (UniqueConstraint("series_id", "observation_date", name="uq_macro_series_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    series_id: Mapped[str] = mapped_column(String(30), index=True)
    observation_date: Mapped[datetime.date] = mapped_column(Date, index=True)
    value: Mapped[float | None] = mapped_column(Numeric(14, 6), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --- K-1(自動化計画 2026-08-30):判断材料の自動抽出 ----------------------------
#
# 投資ノートで人間が手入力していた項目を機械で埋めるための保存先。
# **原則3:以下5テーブルは `evaluate_gates` にも `scoring/` にも入れない。**
# 表示・チェックリスト・ノート起草・アラートのみが読者である。


class FilingSection(Base):
    """10-K/10-Q/8-K の本文を Item 単位で切り出した原文(K-1)。

    顧客集中・訴訟・希薄化条項の抽出はすべてこの表を読む。SECへの再アクセスは
    レート制限を食うので、切り出し結果は必ず保存して再利用する。`section` は
    'item1'(事業) / 'item1a'(リスク) / 'item3'(訴訟) / 'item7'(MD&A) /
    'ex99'(8-Kのプレスリリース添付)。
    """

    __tablename__ = "filing_sections"
    __table_args__ = (
        UniqueConstraint("accession_number", "section", name="uq_filing_section"),
        Index("ix_filing_sections_ticker_section", "ticker_id", "section"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    accession_number: Mapped[str] = mapped_column(String(25))
    form: Mapped[str] = mapped_column(String(20))
    filed_date: Mapped[datetime.date] = mapped_column(Date)
    section: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)
    char_count: Mapped[int] = mapped_column()
    source_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    extracted_on: Mapped[datetime.date] = mapped_column(Date)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DilutionCapacity(Base):
    """シェルフ残枠・ATM残枠・変動転換条項(K-1)。

    `research/TEMPLATE.md` の `dilution:` ブロック——これまで人間が S-3 と 10-Q を
    読んで手入力していた4項目——の機械版。`evidence` には根拠となった原文抜粋と
    その出所を残す(数字だけ出して根拠を出さないと、人間は結局原本を読み直す)。
    """

    __tablename__ = "dilution_capacity"
    __table_args__ = (UniqueConstraint("ticker_id", "as_of_date", name="uq_dilution_capacity"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    as_of_date: Mapped[datetime.date] = mapped_column(Date)
    shelf_registered_usd: Mapped[float | None] = mapped_column(Numeric(24, 2), nullable=True)
    shelf_remaining_usd: Mapped[float | None] = mapped_column(Numeric(24, 2), nullable=True)
    atm_authorized_usd: Mapped[float | None] = mapped_column(Numeric(24, 2), nullable=True)
    atm_remaining_usd: Mapped[float | None] = mapped_column(Numeric(24, 2), nullable=True)
    has_variable_conversion: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    unexercised_options_shares: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    unexercised_options_ratio: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    source_form: Mapped[str | None] = mapped_column(String(20), nullable=True)
    source_accession: Mapped[str | None] = mapped_column(String(25), nullable=True)
    evidence: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    collected_on: Mapped[datetime.date] = mapped_column(Date)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CustomerConcentration(Base):
    """10%超顧客の開示(K-1)。

    `research/TEMPLATE.md` が先行指標 `customer_concentration_disclosed_drop` を
    要求しているのに、`screening/monitoring_metrics.py` に実装が無かった穴を
    埋めるための実測値。`source` は 'xbrl'(ConcentrationRiskPercentage)または
    'text'(10-K本文の正規表現)。
    """

    __tablename__ = "customer_concentration"
    __table_args__ = (
        UniqueConstraint("ticker_id", "period_end", "customer_label", name="uq_customer_concentration"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    period_end: Mapped[datetime.date] = mapped_column(Date)
    fiscal_year: Mapped[int | None] = mapped_column(nullable=True)
    customer_label: Mapped[str] = mapped_column(String(100))
    revenue_pct: Mapped[float] = mapped_column(Numeric(10, 6))
    source: Mapped[str] = mapped_column(String(10))
    source_accession: Mapped[str | None] = mapped_column(String(25), nullable=True)
    collected_on: Mapped[datetime.date] = mapped_column(Date)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Guidance(Base):
    """8-K EX-99.1(決算プレスリリース)から抽出した会社ガイダンス(K-1)。

    決算説明会トランスクリプトは安定した無料取得先が無いが、**ガイダンスの
    原文は EDGAR に無料で存在する**。経営陣が自分で置いた数値目標を機械で拾い、
    次の四半期に達成したかを機械で突き合わせられるようにする。
    """

    __tablename__ = "guidance"
    __table_args__ = (UniqueConstraint("accession_number", "metric", "period_label", name="uq_guidance"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    filed_date: Mapped[datetime.date] = mapped_column(Date)
    accession_number: Mapped[str] = mapped_column(String(25))
    period_label: Mapped[str] = mapped_column(String(40))
    metric: Mapped[str] = mapped_column(String(40))
    low_usd: Mapped[float | None] = mapped_column(Numeric(24, 2), nullable=True)
    high_usd: Mapped[float | None] = mapped_column(Numeric(24, 2), nullable=True)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    collected_on: Mapped[datetime.date] = mapped_column(Date)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LitigationEvent(Base):
    """証券集団訴訟・SEC調査・ショートレポート起因の開示(K-1)。

    デューデリ11工程のうち「訴訟・ショートレポート」だけが Google 検索リンク
    しか無く、完全に人間の手作業として残っていた工程の機械版。
    """

    __tablename__ = "litigation_events"
    __table_args__ = (
        UniqueConstraint("ticker_id", "kind", "event_date", "title", name="uq_litigation_event"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    event_date: Mapped[datetime.date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(500))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_accession: Mapped[str | None] = mapped_column(String(25), nullable=True)
    collected_on: Mapped[datetime.date] = mapped_column(Date)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LlmAnalysis(Base):
    """Claude API が生成した定性分析の保存先(K-9)。

    **原則3(K-1のマイグレーションと同じ):これはゲート(`evaluate_gates`)にも
    スコア(`scoring/`)にも入れない。** 表示・投資ノートの下書き・人間の下読み
    のみ。理由は `llm/__init__.py` に書いたとおりで、LLMの出力は同じ入力でも
    揺れるため、除外や順位づけの根拠にするとバックテストが再現できなくなる。
    表を分けているのは、その約束を人間の記憶ではなくスキーマで守るためである。

    `kind` は 'filing_summary'(提出書類1セクションの要約)/ 'qualitative'
    (構造化された定性評価)/ 'daily_report'(当日ランキングの読み物)。
    テキスト系は `content` に、構造化系は `data` に入り、もう一方は NULL に
    なる——`Filing.analysis` と同じで、NULL は「その種類の出力ではない」を
    意味する。

    `prompt_fingerprint` を一意キーに含めているのは `scores.config_hash`
    (14.6)と同じ理由。ルーブリックやモデルを変えたら別物として並んで保存され、
    古い出力は消えない(何がどう変わったかを後から比較するため)。

    **`ticker_id` は NULL になりうる**(`daily_report` は銘柄横断)。Postgres の
    UNIQUE 制約は NULL 同士を「異なる」と扱い重複を許してしまうので、一意性は
    `ticker_id IS NULL` / `IS NOT NULL` で分けた**部分ユニークインデックス2本**
    で担保する。ダミーの `ticker_id` を用意して回避するのは、存在しない銘柄を
    DBに作ることになるので採らない。
    """

    __tablename__ = "llm_analyses"
    __table_args__ = (
        Index(
            "uq_llm_analyses_ticker",
            "ticker_id",
            "kind",
            "source_key",
            "prompt_fingerprint",
            unique=True,
            postgresql_where=text("ticker_id IS NOT NULL"),
        ),
        Index(
            "uq_llm_analyses_global",
            "kind",
            "source_key",
            "prompt_fingerprint",
            unique=True,
            postgresql_where=text("ticker_id IS NULL"),
        ),
        Index("ix_llm_analyses_kind_as_of", "kind", "as_of"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # 銘柄横断の出力(daily_report)では NULL。
    ticker_id: Mapped[int | None] = mapped_column(
        ForeignKey("tickers.id"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(30))
    # 同じ kind の中で「何について書いたか」を一意に決める鍵。
    # filing_summary は "<accession>:<section>"、qualitative と daily_report は
    # 基準日の ISO 文字列。日付ではなく文字列にしてあるのは、将来 kind が増えた
    # ときに日付以外の鍵(例: イベントID)を取れるようにするため。
    source_key: Mapped[str] = mapped_column(String(60))
    as_of: Mapped[datetime.date] = mapped_column(Date)

    model: Mapped[str] = mapped_column(String(60))
    effort: Mapped[str] = mapped_column(String(10))
    prompt_fingerprint: Mapped[str] = mapped_column(String(64))

    # テキスト出力(Markdown)。構造化系では NULL。
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 構造化出力。テキスト系では NULL。`advisory: true` を値の中にも持たせている。
    data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # 何を読んで書いたか(accession / URL / 対象銘柄の並び)。本文は保存しない。
    source_refs: Mapped[dict | list | None] = mapped_column(JSONB, nullable=True)
    # トークン内訳。`cache_read_tokens` が常に0ならキャッシュが効いていない
    # (エラーにはならず課金だけが増える失敗なので、実測を残して検算できるようにする)。
    usage: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # 障害報告用。SDK が `_request_id` で公開する値。
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class LlmConnection(Base):
    """名前を付けて保存する LLM 接続プロファイル(docs/ui_llm_provider_selection_2026-08-30.md)。

    UIから「provider / base_url / model / APIキー」を名前付きで何件でも保存し、
    そのうち1件を **アクティブ** にする。アクティブな行が
    `config/collection.yaml` / `.env` の上に重なる(CLIも同じ解決を通る)。
    アクティブが無ければ完全に yaml / .env のまま。

    **`api_key` は平文で入る**(`.env` と同じ扱い)。API は本体を決して返さず、
    `api_key_set` のブール値だけを出す——`autoscreener/runtime_settings.py` と
    `api/routes.py` を参照。

    `model` / `effort` は空(None)なら yaml の既定にフォールバックする。
    `is_active` は**部分ユニークインデックスで最大1件**に制限する(Postgres の
    `WHERE is_active` 付き UNIQUE)。

    **この表はゲートにもスコアにも影響しない。** LLM の宛先を選ぶだけで、
    `llm_analyses` への隔離(K-9)はそのまま。
    """

    __tablename__ = "llm_connections"
    __table_args__ = (
        Index(
            "uq_llm_connections_active",
            "is_active",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    # anthropic | openai_compat
    provider: Mapped[str] = mapped_column(String(20))
    base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    effort: Mapped[str | None] = mapped_column(String(10), nullable=True)
    send_effort: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    # 平文。API は返さない。
    api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# TENX Investment Decision v2: every table below is an append-only/display layer.
# Nothing in scoring/engine.py reads these models.
class DelistingEvent(Base):
    __tablename__ = "delisting_events"
    __table_args__ = (UniqueConstraint("ticker_id", "event_date", "event_type", name="uq_delisting_event"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    event_date: Mapped[datetime.date] = mapped_column(Date, index=True)
    event_type: Mapped[str] = mapped_column(String(30))
    last_trade_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    last_trade_price: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    settlement_value_per_share: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    settlement_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(String(60))
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), index=True)
    confidence: Mapped[str] = mapped_column(String(20), default="unknown")


class AnalystConsensusSnapshot(Base):
    __tablename__ = "analyst_consensus_snapshots"
    __table_args__ = (UniqueConstraint("ticker_id", "observed_at", "period_end", "source", name="uq_consensus_snapshot"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    observed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), index=True)
    source: Mapped[str] = mapped_column(String(60))
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    period_type: Mapped[str] = mapped_column(String(4))
    period_end: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    revenue_mean: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    revenue_low: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    revenue_high: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    eps_mean: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    ebitda_mean: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    analyst_count: Mapped[int | None] = mapped_column(nullable=True)
    target_price_mean: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    coverage_status: Mapped[str] = mapped_column(String(30))
    confidence: Mapped[str] = mapped_column(String(20), default="unknown")
    content_hash: Mapped[str] = mapped_column(String(64), index=True)


class ManagementGuidanceSnapshot(Base):
    __tablename__ = "management_guidance_snapshots"
    __table_args__ = (UniqueConstraint("ticker_id", "announced_at", "period_end", "metric", "source", name="uq_management_guidance_snapshot"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    announced_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), index=True)
    observed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), index=True)
    period_end: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    metric: Mapped[str] = mapped_column(String(80))
    low: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    high: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(30), nullable=True)
    status: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(60))
    source_accession: Mapped[str | None] = mapped_column(String(25), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    coverage_status: Mapped[str] = mapped_column(String(30))
    confidence: Mapped[str] = mapped_column(String(20), default="unknown")


class MarketOpportunityEstimate(Base):
    __tablename__ = "market_opportunity_estimates"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    observed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), index=True)
    as_of: Mapped[datetime.date] = mapped_column(Date)
    method: Mapped[str] = mapped_column(String(30))
    tam_value: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    sam_value: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    current_revenue_addressable: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    penetration_rate: Mapped[float | None] = mapped_column(Numeric(12, 8), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    formula_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(60))
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[str] = mapped_column(String(20))
    created_by: Mapped[str] = mapped_column(String(20))
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    coverage_status: Mapped[str] = mapped_column(String(30))


class MarketOpportunityComponent(Base):
    __tablename__ = "market_opportunity_components"
    id: Mapped[int] = mapped_column(primary_key=True)
    estimate_id: Mapped[int] = mapped_column(ForeignKey("market_opportunity_estimates.id"), index=True)
    component_name: Mapped[str] = mapped_column(String(120))
    quantity: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    price_per_unit: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    penetration_assumption: Mapped[float | None] = mapped_column(Numeric(12, 8), nullable=True)
    result_value: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)


class OperatingKpiDefinition(Base):
    __tablename__ = "operating_kpi_definitions"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(80), unique=True)
    label: Mapped[str] = mapped_column(String(160))
    unit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    model_family: Mapped[str | None] = mapped_column(String(40), nullable=True)
    higher_is_better: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class OperatingKpiObservation(Base):
    __tablename__ = "operating_kpi_observations"
    __table_args__ = (UniqueConstraint("ticker_id", "kpi_definition_id", "period_end", "reported_at", name="uq_kpi_observation"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    kpi_definition_id: Mapped[int] = mapped_column(ForeignKey("operating_kpi_definitions.id"), index=True)
    period_end: Mapped[datetime.date] = mapped_column(Date)
    reported_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), index=True)
    observed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), index=True)
    value: Mapped[float | None] = mapped_column(Numeric(24, 6), nullable=True)
    company_definition: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(60))
    source_accession: Mapped[str | None] = mapped_column(String(25), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_method: Mapped[str] = mapped_column(String(20))
    confidence: Mapped[str] = mapped_column(String(20))
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    coverage_status: Mapped[str] = mapped_column(String(30))


class CapitalAllocationEvent(Base):
    __tablename__ = "capital_allocation_events"
    __table_args__ = (UniqueConstraint("ticker_id", "source_accession", "event_type", "content_hash", name="uq_capital_allocation_event_evidence"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    announced_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), index=True)
    observed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), index=True)
    event_type: Mapped[str] = mapped_column(String(30))
    amount: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    counterparty_or_asset: Mapped[str | None] = mapped_column(Text, nullable=True)
    shares_issued: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    price_per_share: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    source: Mapped[str] = mapped_column(String(60))
    source_accession: Mapped[str | None] = mapped_column(String(25), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    coverage_status: Mapped[str] = mapped_column(String(30))
    confidence: Mapped[str] = mapped_column(String(20))


class ManagementIncentiveSnapshot(Base):
    __tablename__ = "management_incentive_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    proxy_date: Mapped[datetime.date] = mapped_column(Date)
    observed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), index=True)
    executive_name: Mapped[str] = mapped_column(String(160))
    role: Mapped[str | None] = mapped_column(String(100), nullable=True)
    founder_flag: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    tenure_years: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    beneficial_ownership_pct: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    total_compensation: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    equity_compensation_pct: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    performance_metrics: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    source: Mapped[str] = mapped_column(String(60))
    source_accession: Mapped[str | None] = mapped_column(String(25), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    coverage_status: Mapped[str] = mapped_column(String(30))
    confidence: Mapped[str] = mapped_column(String(20))


class DebtInstrument(Base):
    __tablename__ = "debt_instruments"
    __table_args__ = (UniqueConstraint("ticker_id", "instrument_id", "as_of", name="uq_debt_instrument"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    instrument_id: Mapped[str] = mapped_column(String(120))
    as_of: Mapped[datetime.date] = mapped_column(Date)
    observed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), index=True)
    instrument_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    principal: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    coupon_rate: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    rate_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    benchmark_rate: Mapped[str | None] = mapped_column(String(30), nullable=True)
    maturity_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    secured_flag: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    convertible_flag: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    conversion_price: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    covenant_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(60))
    source_accession: Mapped[str | None] = mapped_column(String(25), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    coverage_status: Mapped[str] = mapped_column(String(30))
    confidence: Mapped[str] = mapped_column(String(20))


class LiquidityFacility(Base):
    __tablename__ = "liquidity_facilities"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    as_of: Mapped[datetime.date] = mapped_column(Date)
    observed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), index=True)
    revolver_total: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    revolver_drawn: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    revolver_available: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    atm_remaining: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    shelf_remaining: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    cash_balance: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    source: Mapped[str] = mapped_column(String(60))
    source_accession: Mapped[str | None] = mapped_column(String(25), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    coverage_status: Mapped[str] = mapped_column(String(30))
    confidence: Mapped[str] = mapped_column(String(20))


class ThesisMilestone(Base):
    __tablename__ = "thesis_milestones"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    observed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), index=True)
    due_date: Mapped[datetime.date] = mapped_column(Date)
    category: Mapped[str] = mapped_column(String(30))
    metric_code: Mapped[str] = mapped_column(String(80))
    bull_threshold: Mapped[float | None] = mapped_column(Numeric(24, 6), nullable=True)
    base_threshold: Mapped[float | None] = mapped_column(Numeric(24, 6), nullable=True)
    bear_threshold: Mapped[float | None] = mapped_column(Numeric(24, 6), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source: Mapped[str] = mapped_column(String(60))
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20))
    resolved_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_value: Mapped[float | None] = mapped_column(Numeric(24, 6), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    coverage_status: Mapped[str] = mapped_column(String(30))
    confidence: Mapped[str] = mapped_column(String(20), default="manual")


class MacroExposureSnapshot(Base):
    __tablename__ = "macro_exposure_snapshots"
    __table_args__ = (UniqueConstraint("ticker_id", "factor", "observed_at", name="uq_macro_exposure"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    observed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), index=True)
    observation_end: Mapped[datetime.date] = mapped_column(Date)
    factor: Mapped[str] = mapped_column(String(40))
    beta: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    downside_beta: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    sample_count: Mapped[int | None] = mapped_column(nullable=True)
    source: Mapped[str] = mapped_column(String(60))
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    coverage_status: Mapped[str] = mapped_column(String(30))
    confidence: Mapped[str] = mapped_column(String(20))


class LiveDatasetCoverage(Base):
    """Collection attempt state, including the important no-finding outcome."""
    __tablename__ = "live_dataset_coverage"
    __table_args__ = (
        UniqueConstraint("ticker_id", "dataset", "observed_at", "source", name="uq_live_dataset_coverage"),
        CheckConstraint(
            "coverage_status IN ('not_collected', 'collected_no_finding', "
            "'collected_with_data', 'collection_failed', 'not_applicable')",
            name="ck_live_dataset_coverage_status",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    dataset: Mapped[str] = mapped_column(String(60), index=True)
    observed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), index=True)
    source: Mapped[str] = mapped_column(String(60))
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    coverage_status: Mapped[str] = mapped_column(String(30))
    confidence: Mapped[str] = mapped_column(String(20))
    reason_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    reason_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    retryable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
