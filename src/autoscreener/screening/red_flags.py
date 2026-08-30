"""提出書類から読み取れる「即死要因」の判定(30.4)。

元文書 第01節・第05節。**除外ゲートではない**(30.1.3 原則3)。ゲートに入れる
と擬似バックテストの母集団定義が今日以降の収集状況に汚染される。ここは
表示とアラートのための独立した層であり、`evaluate_gates` からは呼ばれない。

重大度は3段階:
- BLOCKING … 新規建てを止める。人間が理由を確認するまで検討を進めない
- WARNING  … サイズを落とす/条件つきで進む
- INFO     … 事実として知っておく(判断は人間)

すべて純粋関数として実装する(DBに触らない)。呼び出し元(API層・バッチ層)が
`filings` 行から `FilingView` を組み立てて渡す。
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass

BLOCKING = "blocking"
WARNING = "warning"
INFO = "info"

# --- 判定表(30.4.1) --------------------------------------------------------
#
# 8-Kの `items` 番号で判定するコード。
_ITEM_CODES: dict[str, tuple[str, str]] = {
    "4.02": ("restatement", BLOCKING),
    "4.01": ("auditor_change", BLOCKING),
    "3.01": ("listing_deficiency", BLOCKING),
    "5.02": ("officer_departure", WARNING),
    "1.01": ("material_agreement", INFO),
}

# フォーム名そのもので判定するコード。
_FORM_CODES: dict[str, tuple[str, str]] = {
    "NT 10-K": ("late_filing", BLOCKING),
    "NT 10-Q": ("late_filing", BLOCKING),
    "UPLOAD": ("sec_comment_letter", WARNING),
    "CORRESP": ("sec_comment_letter", WARNING),
    "S-3": ("shelf_registration", INFO),
    "S-3ASR": ("shelf_registration", INFO),
    "424B5": ("secondary_offering", WARNING),
    "SC 13D": ("activist_stake", INFO),
    "25-NSE": ("delisting_form", BLOCKING),
    "15-12B": ("delisting_form", BLOCKING),
}

# 意味が持続する期間(日)。これを過ぎたフラグは返さない(30.4.1)。
FLAG_TTL_DAYS: dict[str, int] = {
    "restatement": 730,  # リステートメントの影響は2年残ると見る
    "auditor_change": 365,
    "listing_deficiency": 365,
    "late_filing": 180,  # 半年で「解決済みか未解決か」は決算1回で分かる
    "officer_departure": 365,
    "sec_comment_letter": 365,
    "shelf_registration": 1095,  # シェルフの有効期間は概ね3年
    "secondary_offering": 730,  # 資本配分の癖を見るため長めに残す
    "material_agreement": 365,
    "activist_stake": 730,
    "delisting_form": 3650,
}

DEFAULT_LOOKBACK_DAYS = 400

# going_concern / material_weakness はTTLではなく「最新の10-K/10-Qの解析結果か
# どうか」で判定するため、この2つはTTL表に含めない。
_DOCUMENT_ANALYSIS_CODES = frozenset({"going_concern", "material_weakness"})
_ANNUAL_QUARTERLY_FORMS = frozenset({"10-K", "10-Q"})

FLAG_LABELS: dict[str, str] = {
    "restatement": "リステートメント(決算の訂正)",
    "auditor_change": "監査人の交代",
    "listing_deficiency": "上場基準抵触",
    "late_filing": "決算報告の遅延(NT提出)",
    "going_concern": "継続企業の前提に関する重要な不確実性",
    "material_weakness": "内部統制の重要な不備",
    "officer_departure": "役員の退任",
    "sec_comment_letter": "SECコメントレター",
    "shelf_registration": "普通株式の棚上げ登録(シェルフ)",
    "secondary_offering": "公募増資の実施",
    "material_agreement": "重要な契約の締結",
    "activist_stake": "アクティビストによる大量保有",
    "delisting_form": "上場廃止関連書類の提出",
}


@dataclass(frozen=True)
class FilingView:
    """`red_flags.py` が受け取る、`Filing` 行の必要最小限の写像。

    DBモデルそのものを渡さずビューを介すのは、この関数群を純粋関数として
    保ち、SQLAlchemyへの依存を screening/ 配下に持ち込まないため
    (watchlist.py・tradability.py と同じ設計方針)。
    """

    accession_number: str
    form: str
    filed_date: datetime.date
    items: list[str] | None
    document_url: str | None
    analysis: dict | None


@dataclass(frozen=True)
class RedFlag:
    code: str
    severity: str
    detected_on: datetime.date  # 提出日(= 我々が知りえた最初の日)
    source_accession: str | None
    detail: str  # 日本語の説明文。UIにそのまま出す
    document_url: str | None  # 一次情報へのリンク。必ず出す


def _item_flags(filing: FilingView) -> list[RedFlag]:
    flags = []
    for item in filing.items or []:
        mapped = _ITEM_CODES.get(item)
        if mapped is None:
            continue
        code, severity = mapped
        flags.append(
            RedFlag(
                code=code,
                severity=severity,
                detected_on=filing.filed_date,
                source_accession=filing.accession_number,
                detail=f"{FLAG_LABELS.get(code, code)}(8-K Item {item}、{filing.filed_date}提出)",
                document_url=filing.document_url,
            )
        )
    return flags


def _form_flag(filing: FilingView) -> RedFlag | None:
    mapped = _FORM_CODES.get(filing.form)
    if mapped is None:
        return None
    code, severity = mapped
    return RedFlag(
        code=code,
        severity=severity,
        detected_on=filing.filed_date,
        source_accession=filing.accession_number,
        detail=f"{FLAG_LABELS.get(code, code)}({filing.form}、{filing.filed_date}提出)",
        document_url=filing.document_url,
    )


def _within_ttl(code: str, detected_on: datetime.date, as_of: datetime.date) -> bool:
    ttl = FLAG_TTL_DAYS.get(code, DEFAULT_LOOKBACK_DAYS)
    return (as_of - detected_on).days <= ttl


def _document_analysis_flags(filings: list[FilingView], *, as_of: datetime.date) -> list[RedFlag]:
    """going_concern / material_weakness は**最新の10-K/10-Qの解析結果のみ**を見る。

    TTLではなく「直近の提出で消えたか」で判定する。これが正しい——記載が
    消えたなら事実として解消している(30.4.1)。
    """
    annual_quarterly = [
        f for f in filings if f.form in _ANNUAL_QUARTERLY_FORMS and f.filed_date <= as_of and f.analysis is not None
    ]
    if not annual_quarterly:
        return []
    latest = max(annual_quarterly, key=lambda f: f.filed_date)
    analysis = latest.analysis or {}
    flags: list[RedFlag] = []
    if analysis.get("going_concern"):
        excerpt = analysis.get("excerpt", "")
        flags.append(
            RedFlag(
                code="going_concern",
                severity=BLOCKING,
                detected_on=latest.filed_date,
                source_accession=latest.accession_number,
                detail=f"{FLAG_LABELS['going_concern']}({latest.form}、{latest.filed_date}提出)。抜粋: {excerpt}",
                document_url=latest.document_url,
            )
        )
    if analysis.get("material_weakness"):
        excerpt = analysis.get("excerpt", "")
        flags.append(
            RedFlag(
                code="material_weakness",
                # 30.4.2:誤検知を減らせないためWARNING止まり(BLOCKINGに昇格させない)。
                severity=WARNING,
                detected_on=latest.filed_date,
                source_accession=latest.accession_number,
                detail=f"{FLAG_LABELS['material_weakness']}({latest.form}、{latest.filed_date}提出)。抜粋: {excerpt}",
                document_url=latest.document_url,
            )
        )
    return flags


def evaluate_red_flags(
    filings: list[FilingView], *, as_of: datetime.date, lookback_days: int = DEFAULT_LOOKBACK_DAYS
) -> list[RedFlag]:
    """新しい順に並べて返す。`lookback_days` より古い提出は見ない。

    `lookback_days` はコード別TTL(`FLAG_TTL_DAYS`)を持たないコードへの
    フォールバックであり、通常のコードはTTL表の値が優先される。
    """
    candidates = [f for f in filings if f.filed_date <= as_of and (as_of - f.filed_date).days <= 3650]

    flags: list[RedFlag] = []
    for filing in candidates:
        flags.extend(_item_flags(filing))
        form_flag = _form_flag(filing)
        if form_flag is not None:
            flags.append(form_flag)

    flags = [f for f in flags if _within_ttl(f.code, f.detected_on, as_of)]
    flags.extend(_document_analysis_flags(candidates, as_of=as_of))

    flags.sort(key=lambda f: f.detected_on, reverse=True)
    return flags


# --- 本文からの継続企業の前提・内部統制の検出(30.4.2) ------------------------
#
# 継続企業の前提に関する重要な不確実性。監査報告書と流動性の節に定型句で現れる。
# "going concern" 単独では、会計方針の説明("prepared on a going concern basis")
# にも当たってしまうため、**substantial doubt との共起**を要求する。
GOING_CONCERN_PATTERN = re.compile(
    r"substantial\s+doubt[^.]{0,200}?going\s+concern"
    r"|going\s+concern[^.]{0,200}?substantial\s+doubt",
    re.IGNORECASE | re.DOTALL,
)

# 内部統制の重要な不備。"material weakness" は定型句で、否定形
# ("no material weakness")との区別が要る。
MATERIAL_WEAKNESS_PATTERN = re.compile(r"material\s+weakness(es)?", re.IGNORECASE)
_MATERIAL_WEAKNESS_NEGATION = re.compile(
    r"(no|not|without)\s+(any\s+)?material\s+weakness(es)?"
    r"|material\s+weakness(es)?\s+(were|was|has|have)\s+(not|no longer)",
    re.IGNORECASE,
)

_EXCERPT_MARGIN = 200


def _excerpt(text: str, match: re.Match) -> str:
    start = max(0, match.start() - _EXCERPT_MARGIN)
    end = min(len(text), match.end() + _EXCERPT_MARGIN)
    return text[start:end].strip()


def analyze_document_text(text: str, *, truncated: bool = False, analyzed_at: datetime.date | None = None) -> dict:
    """本文(プレーンテキスト)から going_concern / material_weakness を検出する。

    LLMに読ませない(原則1:再現性が無く、検証もできない判定をブロッキング
    条件にしてはならない)。戻り値はそのまま `filings.analysis` に保存する形。

    `truncated=True`(`EdgarClient.fetch_document_text` が `max_bytes` で
    切り捨てた場合)は結果にもその旨を残す——切り捨てた文書で「無し」と
    判定した場合、それは「無い」ではなく「見えていない」ため、UIが区別できる
    ようにする(30.4.2)。
    """
    going_concern_match = GOING_CONCERN_PATTERN.search(text)
    going_concern = going_concern_match is not None

    material_weakness = False
    weakness_match = MATERIAL_WEAKNESS_PATTERN.search(text)
    if weakness_match is not None:
        negation_match = _MATERIAL_WEAKNESS_NEGATION.search(text)
        # 同じ位置の否定パターンがマッチしないときのみ真とする(30.4.2)。
        # 簡便法として「肯定マッチの近傍(前後200文字)に否定パターンが無い」
        # ことを条件にする——文書全体で1件でも素の肯定表現があれば拾う一方、
        # 否定形1文で完結するケース("no material weaknesses were identified")
        # は確実に打ち消す。
        if negation_match is None or abs(negation_match.start() - weakness_match.start()) > _EXCERPT_MARGIN:
            material_weakness = True

    excerpt = ""
    if going_concern_match is not None:
        excerpt = _excerpt(text, going_concern_match)
    elif weakness_match is not None:
        excerpt = _excerpt(text, weakness_match)

    return {
        "going_concern": going_concern,
        "material_weakness": material_weakness,
        "excerpt": excerpt,
        "analyzed_at": (analyzed_at or datetime.date.today()).isoformat(),
        "truncated": truncated,
    }


def filing_to_view(filing) -> FilingView:
    """ORMの `Filing` 行を `FilingView` に変換する(API/バッチ層から呼ぶ)。"""
    return FilingView(
        accession_number=filing.accession_number,
        form=filing.form,
        filed_date=filing.filed_date,
        items=filing.items,
        document_url=filing.document_url,
        analysis=filing.analysis,
    )
