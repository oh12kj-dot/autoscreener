"""Piotroski F-Score(15.4-4)。

**27.16でこのモジュールを大幅に縮小した。** 旧v2の8サブスコアが必要としていた
指標群(Rule of 40・ROIC・Sloanアクルーアル・EV/Sales÷成長率・52週高値近接度・
アナリストカバレッジ・予想修正モメンタム・決算サプライズ連続性・インサイダー
買い越し・12-1モメンタム・成長安定性CV)は、実現倍率モデル(`moic.py`)が
1つも使わないため削除した。理由は2つある。

1. **モデルの構造が変わった** — 旧v2は指標をパーセンタイル化して加点する
   構造だったため、相関の低い指標を並べるほど「情報が増える」建て付けだった。
   実現倍率モデルは15.1の恒等式に現れる4因子(売上・利益率・マルチプル・株式数)
   と生存確率しか必要としない。恒等式に現れない量は、掛け算のどこにも入らない
2. **過去に遡れない指標を使うとモデルが検証不能になる** — アナリスト予想・
   インサイダー取引・機関保有率・TTM値はいずれも現在時点のスナップショットしか
   取得できない。それらに依存した瞬間、擬似バックテスト(27.8)が成立しなくなり、
   14.3が指摘した「7年待たないと何も分からない」状態に逆戻りする

残した Piotroski F-Score は、年次財務諸表だけで計算でき過去に遡れる。加点軸
としてではなく `moic.health_index` の入力=**生存確率**の推定に使う(27.4)。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from autoscreener.screening.exclusion_gates import latest_period_value, parse_period_series


def _two_most_recent(field_series: dict[str, float | None] | None) -> tuple[float, float] | None:
    """直近2期の値を (latest, prior) で返す。2期分そろわなければ None。"""
    points = parse_period_series(field_series)
    if len(points) < 2:
        return None
    return points[-1][1], points[-2][1]


# Piotroski 9基準の識別子とラベル。`piotroski_f_score` が criteria を積む順と一致。
# J-2(investment_decision_gap_2026-08-29.md):UI で「成長の質の根拠」を9項目に
# 分解して見せるために、合成値だけでなく内訳を露出する。
PIOTROSKI_CRITERIA: tuple[tuple[str, str], ...] = (
    ("roa_positive", "ROA > 0(黒字)"),
    ("roa_improved", "ROA が前年より改善"),
    ("cfo_positive", "営業CF > 0"),
    ("accruals_ok", "営業CF > 純利益(利益の質)"),
    ("leverage_reduced", "負債比率が低下"),
    ("current_ratio_improved", "流動比率が改善"),
    ("no_new_shares", "増資なし(株式数が非増加)"),
    ("gross_margin_improved", "粗利率が改善"),
    ("asset_turnover_improved", "資産回転率が改善"),
)


@dataclass(frozen=True)
class PiotroskiResult:
    score_ratio: float | None  # 0.0〜1.0(算出できた基準に対する充足率)
    criteria_computable: int
    criteria_met: int
    # J-2:9基準それぞれの判定。True=満たす / False=満たさない / None=判定不能。
    # `score_ratio` は None を除いた computable のうち True の割合で、この dict の
    # 値の集計と厳密に一致する(表示専用。加点には使わない)。
    criteria: dict[str, bool | None] = field(default_factory=dict)


def piotroski_f_score(balance_sheet: dict, income_stmt: dict, cash_flow: dict) -> PiotroskiResult:
    """Piotroski F-Score(15.4-4)の9基準を評価する。

    各基準は直近2期が両方そろわなければ判定不能(Noneとしてスキップ)。
    9基準中6基準未満しか判定できない場合は全体を算出不能とする。
    スコアは0〜9ではなく「判定できた基準のうち満たした割合」(0.0〜1.0)で
    返す。異なるデータ欠損状況の銘柄間で比較可能にするため。
    """
    criteria: list[bool | None] = []

    # 1. ROA > 0 / 3. ΔROA > 0
    net_income_pts = _two_most_recent(income_stmt.get("Net Income"))
    total_assets_pts = _two_most_recent(balance_sheet.get("Total Assets"))
    if net_income_pts and total_assets_pts and total_assets_pts[0] != 0 and total_assets_pts[1] != 0:
        roa_latest = net_income_pts[0] / total_assets_pts[0]
        roa_prior = net_income_pts[1] / total_assets_pts[1]
        criteria.append(roa_latest > 0)
        criteria.append(roa_latest > roa_prior)
    else:
        criteria.extend([None, None])

    # 2. CFO > 0
    cfo_latest = latest_period_value(cash_flow.get("Operating Cash Flow"))
    criteria.append(cfo_latest > 0 if cfo_latest is not None else None)

    # 4. CFO > NI
    ni_latest = latest_period_value(income_stmt.get("Net Income"))
    if cfo_latest is not None and ni_latest is not None:
        criteria.append(cfo_latest > ni_latest)
    else:
        criteria.append(None)

    # 5. Δ負債比率 < 0(Total Debt / Total Assetsが低下)
    debt_pts = _two_most_recent(balance_sheet.get("Total Debt"))
    if debt_pts and total_assets_pts and total_assets_pts[0] != 0 and total_assets_pts[1] != 0:
        leverage_latest = debt_pts[0] / total_assets_pts[0]
        leverage_prior = debt_pts[1] / total_assets_pts[1]
        criteria.append(leverage_latest < leverage_prior)
    else:
        criteria.append(None)

    # 6. Δ流動比率 > 0
    ca_pts = _two_most_recent(balance_sheet.get("Current Assets"))
    cl_pts = _two_most_recent(balance_sheet.get("Current Liabilities"))
    if ca_pts and cl_pts and cl_pts[0] != 0 and cl_pts[1] != 0:
        cr_latest = ca_pts[0] / cl_pts[0]
        cr_prior = ca_pts[1] / cl_pts[1]
        criteria.append(cr_latest > cr_prior)
    else:
        criteria.append(None)

    # 7. 増資なし(発行済株式数が増えていない)
    shares_pts = _two_most_recent(balance_sheet.get("Ordinary Shares Number"))
    if shares_pts:
        criteria.append(shares_pts[0] <= shares_pts[1])
    else:
        criteria.append(None)

    # 8. Δ粗利率 > 0
    revenue_pts = _two_most_recent(income_stmt.get("Total Revenue"))
    gp_pts = _two_most_recent(income_stmt.get("Gross Profit"))
    if gp_pts and revenue_pts and revenue_pts[0] != 0 and revenue_pts[1] != 0:
        gm_latest = gp_pts[0] / revenue_pts[0]
        gm_prior = gp_pts[1] / revenue_pts[1]
        criteria.append(gm_latest > gm_prior)
    else:
        criteria.append(None)

    # 9. Δ資産回転率 > 0
    if revenue_pts and total_assets_pts and total_assets_pts[0] != 0 and total_assets_pts[1] != 0:
        turnover_latest = revenue_pts[0] / total_assets_pts[0]
        turnover_prior = revenue_pts[1] / total_assets_pts[1]
        criteria.append(turnover_latest > turnover_prior)
    else:
        criteria.append(None)

    detail = {name: value for (name, _label), value in zip(PIOTROSKI_CRITERIA, criteria)}
    computable = [c for c in criteria if c is not None]
    if len(computable) < 6:
        return PiotroskiResult(
            score_ratio=None,
            criteria_computable=len(computable),
            criteria_met=0,
            criteria=detail,
        )

    met = sum(1 for c in computable if c)
    return PiotroskiResult(
        score_ratio=met / len(computable),
        criteria_computable=len(computable),
        criteria_met=met,
        criteria=detail,
    )
