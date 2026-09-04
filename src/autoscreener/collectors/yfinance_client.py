"""yfinanceを呼び出す薄いラッパー(8.3・14.9・18.1)。

**実データ検証で確認した重要な挙動**:yfinance 1.4.1 は無効・上場廃止ティッカーに
対して例外を送出しない。内部でHTTPエラーをログ出力するだけで、`.info` は
`{"trailingPegRatio": None}` のようなほぼ空のdictを、`.history()`/`.quarterly_income_stmt`
は空のDataFrameを返す。したがって「銘柄が存在しない」の一次検出手段は例外分類
ではなく `EmptyResponseError`(必須フィールド欠如の検知)であり、18.1で設計した
「3日連続で空なら恒久的失敗に格上げ」のロジックがこのケースを正しく吸収する。
`classify_exception` によるHTTP例外・yfinance例外の分類は、実際に例外が送出される
別経路(レート制限、接続エラー、将来のyfinanceバージョン変更等)に対する多層防御
として維持する。
"""

from __future__ import annotations

import logging
import threading
import traceback
from datetime import timedelta
from typing import Any

import pandas as pd
import yfinance as yf
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from autoscreener.collectors.errors import (
    CollectionError,
    EmptyResponseError,
    TransientFailure,
    YFinanceSessionFailure,
    classify_exception,
    is_known_yfinance_session_typeerror,
)
from autoscreener.collectors.rate_limit import get_shared_limiter
from autoscreener.config import RetryConfig

logger = logging.getLogger(__name__)

# 13.1/13.5: これらが全てNone/欠如していれば「観測できたが空」として扱う
_REQUIRED_INFO_FIELDS = ("marketCap", "sector", "currency")


def _retrying(retry_config: RetryConfig):
    return retry(
        retry=retry_if_exception_type(TransientFailure),
        stop=stop_after_attempt(retry_config.max_attempts),
        wait=wait_exponential_jitter(initial=retry_config.backoff_base_seconds, max=retry_config.backoff_max_seconds),
        reraise=True,
    )


def _reset_yfinance_session_state() -> None:
    """Discard only yfinance's cached cookie/crumb after its known transient bug.

    The next retry still passes through ``YfData._make_request`` and therefore
    the process-wide HTTP limiter.  Attribute checks make this compatible with
    yfinance versions that do not expose one of these private caches.
    """
    data = yf.data.YfData()
    lock = getattr(data, "_cookie_lock", None)
    if lock is None:
        for attr in ("_cookie", "_crumb"):
            if hasattr(data, attr):
                setattr(data, attr, None)
        return
    # YfData is a process-wide singleton shared by collection workers.  Use
    # yfinance's own cookie lock so a reset cannot interleave with another
    # worker's cookie/crumb acquisition.
    with lock:
        for attr in ("_cookie", "_crumb"):
            if hasattr(data, attr):
                setattr(data, attr, None)


def _raise_classified_yfinance_exception(exc: Exception, *, operation: str) -> None:
    """Classify an error without broadening retry eligibility for TypeError."""
    if is_known_yfinance_session_typeerror(exc):
        _reset_yfinance_session_state()
        raise YFinanceSessionFailure(
            operation=operation,
            original_error=f"{type(exc).__name__}: {exc}",
            traceback_text="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        ) from exc
    raise classify_exception(exc) from exc



_HTTP_THROTTLE_MARKER = "_autoscreener_http_throttle_installed"


def _install_http_throttle() -> None:
    """yfinanceの実HTTP境界(`YfData._make_request`)を1箇所でラップし、
    ここでレート制御する(S-1、docs/daily_pipeline_throughput_plan_2026-09-04.md)。

    **旧実装(`_throttle()`)の問題**:以前は`fetch_raw_financials`等の
    **呼び出し単位**でacquireしていたが、1回の呼び出しは内部で最大6本の
    HTTPリクエストを出す(quoteSummary×1 + fundamentals-timeseries×5)。
    実測(2026-09-02、`YfData._make_request`を計数):設定値
    `yfinance_requests_per_second: 2.0`に対し、実効HTTPレートは
    **6.34 req/秒**(名目上限の約3.2倍)——上限は何も表していなかった。
    consensus工程(`YfinanceConsensusProvider.fetch`)に至っては呼び出し単位の
    acquireすら通っておらず、2本のHTTPが完全に素通りしていた。

    `YfData`はyfinance側のプロセス内シングルトンで(`data.py`の
    `SingletonMeta`)、`get()`/`post()`はどちらも最終的に`_make_request()`を
    通る(`yfinance/data.py`で確認済み:`get`は`_make_request(..., request_method=
    self._session.get, ...)`、`post`も同様)。ここを1箇所だけモンキーパッチ
    すれば、`ticker.info`・`.quarterly_income_stmt`・`.history()`・
    `.get_shares_full()`・`.revenue_estimate`等、経路によらず全てのHTTPが
    等しく間隔制御を受ける。これに伴い、各関数が個別に呼んでいた旧
    `_throttle()`(呼び出し単位)は撤去した——残すと二重にacquireするだけで、
    実HTTP本数あたりの待ち時間が不必要に増える(6本のHTTPに対し呼び出し単位
    +HTTP単位の両方で待つと7回待つことになり、S-2/S-4で得た削減分を
    無駄食いする)。

    プロセスで一度だけ適用する。モジュールは通常一度しかimportされない
    (Pythonのimportロックが並行importでも一度しか実行させない)が、HTTP単位で
    本当にthrottleされるかを検証するテストが`YfData._make_request`を差し替えて
    この関数を再度呼ぶ場面があるため、既にラップ済みかをマーカー属性で判定し、
    二重ラップ(=待ち時間が倍になる)を防ぐ。
    """
    target = yf.data.YfData
    if getattr(target._make_request, _HTTP_THROTTLE_MARKER, False):
        return  # 既にラップ済み

    original_make_request = target._make_request

    def _throttled_make_request(self: Any, *args: Any, **kwargs: Any) -> Any:
        get_shared_limiter("yfinance").acquire()
        return original_make_request(self, *args, **kwargs)

    setattr(_throttled_make_request, _HTTP_THROTTLE_MARKER, True)
    target._make_request = _throttled_make_request


_install_http_throttle()


def _df_to_json(df: pd.DataFrame | None) -> dict[str, dict[str, Any]]:
    """財務諸表DataFrame(行=科目、列=決算期)をJSON化可能な形に変換する。
    NaNはNoneに変換し、列(Timestamp)はISO日付文字列にする。"""
    if df is None or df.empty:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row_label in df.index:
        row = df.loc[row_label]
        result[str(row_label)] = {
            (col.strftime("%Y-%m-%d") if hasattr(col, "strftime") else str(col)): (
                None if pd.isna(val) else float(val)
            )
            for col, val in row.items()
        }
    return result


def fetch_raw_financials(
    symbol: str, retry_config: RetryConfig, *, include_statements: bool = True
) -> dict[str, Any]:
    """1銘柄分の生データ(info + 財務諸表)を取得する。

    戻り値は raw_snapshots.payload にそのまま保存できるJSON化可能な辞書。
    失敗時は CollectionError の各サブクラス(18.1)を送出する。

    S-2(daily_pipeline_throughput_plan_2026-09-04):`include_statements=False`
    のときは財務諸表5本(fundamentals-timeseries)を取得しない——四半期に
    1度しか変わらない値を毎日取り直していたのが元々の無駄で、`xbrl_facts`
    工程(EDGAR側の実績値)は既に同じ理由で週次に格下げ済み
    (`daily_pipeline.py`のコメント参照)。yfinance側の財務諸表だけが日次の
    ままになっていた。銘柄あたりのHTTPは6本(info+財務諸表5本)→1本(info)に
    減る。呼び出し元(`collectors/snapshot_collector.py`の`collect_one`)が、
    取得しなかった日は直近の`raw_snapshots.payload`から財務諸表5キーを
    持ち越して補い、payloadの形(キー集合)を日によって変えない。
    """

    @_retrying(retry_config)
    def _call() -> dict[str, Any]:
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info or {}

            if not info or all(info.get(f) is None for f in _REQUIRED_INFO_FIELDS):
                raise EmptyResponseError(f"{symbol}: info missing all required fields {_REQUIRED_INFO_FIELDS}")

            # 13.5:報告通貨(financialCurrency)と株価通貨(currency)が異なる銘柄
            # (ADR等)向けに換算レートを添付する。決算数値(totalRevenue等)は
            # financialCurrency建てだが、時価総額・株価はcurrency建てのまま
            # (13.5・実データ検証で確認)なので、両者を混在させる計算箇所
            # (apply_gates.py・financial_metrics.py)で使う。
            currency = info.get("currency")
            financial_currency = info.get("financialCurrency")
            if currency and financial_currency and currency != financial_currency:
                info["_fx_rate_financial_to_trading"] = fetch_fx_rate(financial_currency, currency)

            payload: dict[str, Any] = {"info": info}
            if include_statements:
                payload.update(
                    {
                        "quarterly_income_stmt": _df_to_json(ticker.quarterly_income_stmt),
                        "income_stmt": _df_to_json(ticker.income_stmt),
                        "balance_sheet": _df_to_json(ticker.balance_sheet),
                        "cash_flow": _df_to_json(ticker.cash_flow),
                        # 15.2のキャッシュランウェイゲート(直近4四半期の平均FCFバーン)
                        # には四半期粒度が必要。年次cash_flowだけでは代替できない。
                        "quarterly_cash_flow": _df_to_json(ticker.quarterly_cash_flow),
                        # 27.16:`eps_revisions`・`earnings_dates`・`insider_transactions`
                        # の収集はやめた。これらは旧v2の「予想修正モメンタム」
                        # 「発掘度」サブスコア専用のデータであり、実現倍率モデルは
                        # どれも使わない。いずれも**現在時点のスナップショットしか
                        # 取れず過去に遡れない**ため、使えばモデルが検証不能になる
                        # (27.8)。銘柄あたり3回の追加APIコールを削れるので、
                        # レート制限(8.3・14.9)にも効く。
                    }
                )
            return payload
        except CollectionError:
            raise
        except Exception as exc:
            _raise_classified_yfinance_exception(exc, operation="fetch_raw_financials")

    return _call()


def fetch_isin(symbol: str) -> str | None:
    """ISIN(国際証券識別番号)を取得する(14.5:ティッカーシンボル再利用の検知に
    使う同一性確認)。会社の同一性を跨いで変わらない識別子である一方、日次収集の
    通常経路では呼ばない(銘柄ごとに一度取得すれば十分なため、呼び出し元が
    `tickers.isin`未設定の銘柄に限って呼ぶ)。取得失敗は収集全体を失敗させない
    best-effortとし、リトライもしない。"""
    try:
        isin = yf.Ticker(symbol).get_isin()
    except Exception as exc:  # noqa: BLE001
        logger.debug("%s: failed to fetch ISIN: %s", symbol, exc)
        return None
    if not isin or not isinstance(isin, str) or isin == "-":
        return None
    return isin


# 13.5・実データ検証(2026-08-24):currency(株価建て通貨)とfinancialCurrency
# (決算報告通貨)が異なる銘柄(HMY等のADR)が210銘柄(4%)存在し、totalRevenue等の
# 決算数値をUSD建てゲート閾値と無変換で比較していたバグが判明。通貨ペアごとに
# レートを1回だけ取得して使い回す(同じfinancialCurrencyの銘柄が複数あるため、
# 銘柄数ではなく通貨ペア数だけAPIコールが発生する)。
_fx_rate_cache: dict[tuple[str, str], float | None] = {}
_fx_rate_cache_lock = threading.Lock()


def fetch_fx_rate(from_currency: str, to_currency: str) -> float | None:
    """`from_currency`建ての金額に掛けると`to_currency`建てに換算できるレートを返す。
    取得失敗時はbest-effortでNoneを返す(呼び出し元は「換算不能=不明」として
    既存のNone欠損ポリシーに委ねる)。"""
    if from_currency == to_currency:
        return 1.0

    cache_key = (from_currency, to_currency)
    with _fx_rate_cache_lock:
        if cache_key in _fx_rate_cache:
            return _fx_rate_cache[cache_key]

    try:
        pair = yf.Ticker(f"{from_currency}{to_currency}=X")
        rate = pair.info.get("regularMarketPrice")
    except Exception as exc:  # noqa: BLE001
        logger.debug("failed to fetch FX rate %s->%s: %s", from_currency, to_currency, exc)
        rate = None

    if not isinstance(rate, (int, float)) or rate <= 0:
        rate = None

    with _fx_rate_cache_lock:
        _fx_rate_cache[cache_key] = rate
    return rate


def _recent_splits(hist: pd.DataFrame) -> list[tuple[Any, float]]:
    """`history()`が返す窓の中で発生した株式分割を (取引日, 倍率) の昇順で返す。

    yfinanceは`actions=True`(既定)のとき"Stock Splits"列を付け、分割が無い日は
    0.0を入れる。13.4のとおり `price_snapshots` に蓄積済みの過去行は分割前の
    単位のままなので、呼び出し元(snapshot_collector)がこの情報で遡って調整する。
    """
    if "Stock Splits" not in hist.columns:
        return []
    splits: list[tuple[Any, float]] = []
    for idx, value in hist["Stock Splits"].items():
        if value is None or pd.isna(value):
            continue
        ratio = float(value)
        if ratio <= 0 or ratio == 1.0:
            continue
        splits.append((idx.date() if hasattr(idx, "date") else idx, ratio))
    return sorted(splits, key=lambda p: p[0])


def fetch_latest_price(
    symbol: str,
    retry_config: RetryConfig,
    *,
    include_shares: bool = True,
) -> dict[str, Any] | None:
    """直近の株価・出来高・発行済株式数を取得する(price_snapshots用)。

    価格が取得できないこと自体は財務情報の欠如ほど致命的ではないため、例外では
    なく None を返す。呼び出し元(snapshot_collector)がこれを price 側の
    欠損として記録する。

    戻り値の `recent_splits` と `recent_closes` は PriceSnapshot の列ではなく、
    13.4の遡及調整に使うメタ情報(呼び出し元が取り出してから行を組み立てる)。

    **窓を1ヶ月に広げた理由(2026-08-26)**:`_reconcile_splits` は「分割前の
    保存済み行が、まだ分割前の単位かどうか」を判定するために、**同じ取引日の
    保存値と取得値(常に分割調整済み)を突き合わせる**。5日窓では分割から
    数日以内に収集が走らないと突き合わせる日が無くなる。1ヶ月に広げても
    APIコール数は変わらない。
    """

    @_retrying(retry_config)
    def _call() -> dict[str, Any] | None:
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="1mo", auto_adjust=False)
            if hist.empty:
                return None
            last = hist.iloc[-1]
            trade_date = hist.index[-1].date()
            dividend = None
            if "Dividends" in hist.columns and not pd.isna(last["Dividends"]) and last["Dividends"] != 0:
                dividend = float(last["Dividends"])
            recent_splits = _recent_splits(hist)

            # A split changes the share-count unit immediately.  Even on a
            # normal carry-forward day, force one provider observation so the
            # newly written row is checked against the adjusted history.
            shares_requested = include_shares or bool(recent_splits)

            # 発行済株式数は不定期更新のため、直近日付だけを窓にすると
            # 観測点が1件もなくNoneが返る(実データ検証で確認)。過去に
            # 遡って直近の観測値を拾えるよう十分広い窓を指定する。
            shares_outstanding: int | None = None
            if shares_requested:
                lookback_start = trade_date - timedelta(days=400)
                shares = ticker.get_shares_full(start=lookback_start.isoformat())
                if shares is not None and not shares.empty:
                    shares_outstanding = int(shares.iloc[-1])

            return {
                "trade_date": trade_date,
                "open": float(last["Open"]),
                "high": float(last["High"]),
                "low": float(last["Low"]),
                "close": float(last["Close"]),
                "volume": int(last["Volume"]),
                "shares_outstanding": shares_outstanding,
                "_shares_requested": shares_requested,
                "dividend": dividend,  # D-11:総リターン算出用
                "recent_splits": recent_splits,
                # 13.4の遡及調整の判定材料。`history()` が返す終値は常に
                # **現在の単位に分割調整済み**なので、保存済み行と同じ取引日を
                # 比べれば、保存側が調整済みかどうかが分かる。
                "recent_closes": {
                    (idx.date() if hasattr(idx, "date") else idx): float(value)
                    for idx, value in hist["Close"].items()
                    if not pd.isna(value)
                },
            }
        except CollectionError:
            raise
        except Exception as exc:
            _raise_classified_yfinance_exception(exc, operation="fetch_latest_price")

    return _call()


def _apply_split_adjustment(shares: pd.Series, splits: pd.Series | None) -> pd.Series:
    """`shares`(分割未調整の実測発行済株式数)を、その後に発生した分割の
    倍率で遡って調整し、現在時点の株式単位に揃える(13.4)。

    分割が発生していない銘柄・観測日以降に分割が無かった観測点は無変換のまま
    (空のSeriesのprod()は1.0になるため、分岐を特別扱いする必要はない)。
    """
    if splits is None or splits.empty:
        return shares
    splits_sorted = splits.sort_index()
    # `splits.index` には寄り付き時刻(例:09:30:00)が付与されているが、
    # `shares.index`(get_shares_full)は日付のみ(00:00:00)。時刻粒度の
    # 不一致をそのまま比較すると「分割当日の観測値」まで誤って再調整して
    # しまう(実データで確認:分割当日のraw値は既に分割後の値だった)。
    # 日付単位に正規化してから比較する。
    split_dates_normalized = splits_sorted.index.normalize()
    # `shares`はint64(株数は整数)だが、分割倍率を掛けると端数が出ることがある
    # (例:1:3分割ではない変則的な比率)。int64のままだと代入時にpandasが
    # LossySetitemErrorを送出し、当該銘柄の価格・株式数履歴が丸ごと取得できなく
    # なるバグがあった(実データ検証、2026-08-24:680銘柄中相当数がこれで失敗)。
    # 端数は呼び出し元(fetch_price_and_shares_history)がint()で切り捨てて
    # 保存する前提であり、途中経過をfloatで持つこと自体に問題はない。
    adjusted = shares.astype("float64").copy()
    for ts in adjusted.index:
        factor = float(splits_sorted[split_dates_normalized > ts.normalize()].prod())
        if factor and factor != 1.0:
            adjusted.loc[ts] = adjusted.loc[ts] * factor
    return adjusted


def fetch_price_and_shares_history(
    symbol: str, retry_config: RetryConfig, period: str = "3y"
) -> list[dict[str, Any]]:
    """価格・出来高・発行済株式数の履歴を一括取得する(1回限りのバックフィル用)。

    15.2の流動性ゲート(日次売買代金の中央値)には数十営業日分、希薄化ゲート
    (3年CAGR)には約3年分の時系列が必要であり、日次収集を数ヶ月待つのは
    非効率(19.3で暫定案としていたが、yfinanceは過去分を1回のリクエストで
    遡って取得できるため待つ必要はない)。

    株式数は不定期更新の観測点(`get_shares_full`)を、`ffill` で各取引日に
    引き当てる。get_shares_full の観測は同一日に重複行を持つことがあるため
    reindex前に重複排除する(実データで確認)。

    **13.4の分割調整**:`get_shares_full` は分割前後で単位が変わる実測値を
    返す(例:CELHの2023-11-15 3:1分割の前後で、分割前の観測値は分割後の
    1/3の単位)。分割日より前の観測値には、その後発生した分割の倍率を
    遡って乗じ、現在時点の株式単位に揃えてから保存する。この調整をせずに
    複数年の窓でCAGRを取ると、実際の希薄化ではなく分割そのものを
    「希薄化」として誤検知する(要件定義書13.4で指摘した罠)。
    """

    @_retrying(retry_config)
    def _call() -> list[dict[str, Any]]:
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period=period, auto_adjust=False)
            if hist.empty:
                return []

            shares_start = hist.index[0] - pd.Timedelta(days=400)
            shares = ticker.get_shares_full(start=shares_start.date().isoformat())
            if shares is not None and not shares.empty:
                shares = shares[~shares.index.duplicated(keep="last")].sort_index()
                shares = _apply_split_adjustment(shares, ticker.splits)
                aligned_shares = shares.reindex(hist.index, method="ffill")
            else:
                aligned_shares = None

            rows: list[dict[str, Any]] = []
            for idx, row in hist.iterrows():
                if pd.isna(row["Close"]):
                    continue
                shares_value = None
                if aligned_shares is not None:
                    raw = aligned_shares.loc[idx]
                    shares_value = None if pd.isna(raw) else int(raw)
                dividend = None
                if "Dividends" in hist.columns and not pd.isna(row["Dividends"]) and row["Dividends"] != 0:
                    dividend = float(row["Dividends"])
                rows.append(
                    {
                        "trade_date": idx.date(),
                        "open": float(row["Open"]) if not pd.isna(row["Open"]) else None,
                        "high": float(row["High"]) if not pd.isna(row["High"]) else None,
                        "low": float(row["Low"]) if not pd.isna(row["Low"]) else None,
                        "close": float(row["Close"]),
                        "volume": int(row["Volume"]) if not pd.isna(row["Volume"]) else None,
                        "shares_outstanding": shares_value,
                        # D-11:総リターン算出用。分割と同じ actions 列から拾う。
                        "dividend": dividend,
                    }
                )
            return rows
        except CollectionError:
            raise
        except Exception as exc:
            _raise_classified_yfinance_exception(exc, operation="fetch_price_and_shares_history")

    return _call()
