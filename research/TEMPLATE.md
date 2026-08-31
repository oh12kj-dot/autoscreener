---
ticker: ABCD
created_on: 2026-08-28
thesis: |
  3文以内。なぜこの会社が7年で10倍になり得るのか。
assumptions:
  # TENXの値と自分の値の両方を書く。差の出どころを後から集計できるようにするため
  revenue_growth: { model: 0.42, mine: 0.30 }
  terminal_margin: { model: 0.55, mine: 0.50 }
  terminal_multiple: { model: 12.0, mine: 9.0 }
  dilution_rate: { model: 0.06, mine: 0.10 }
model_divergence: |
  TENXより弱気。理由:成長率が上限クランプに当たっており実力が測れていない。
premortem:
  # 元文書 第08節:失敗要因3つと、それぞれの「事前に観測可能な先行指標」
  - cause: 主要顧客の契約が更新されない
    indicator: customer_concentration_disclosed_drop
    detail: 10-Kの10%超顧客の記載が消える、または売上が2四半期連続で減速
  - cause: 価格競争の開始
    indicator: gross_margin_decline
    detail: 粗利率が2四半期連続で低下
  - cause: 資金繰りに追われた増資
    indicator: share_count_growth
    detail: 株式数が年率15%を超えて増加
sizing:
  amount_usd: 4000
  rationale: ADV制約(上限$3,200)ではなく規律側が効いた。二値イベント無し
verification_date: 2026-11-05   # 次にテーゼが試される日(次回決算)
milestones:
  # bull/base/bear は「売却シグナル」ではなく、仮説を再検証するための事前基準。
  - due_date: 2026-11-05
    category: financial
    metric: revenue_yoy
    bull: 0.35
    base: 0.25
    bear: 0.15
    unit: ratio
  - due_date: 2027-03-31
    category: customer
    metric: customer_count
    base: 5000
    unit: count
exit_plan:
  # J-8 / 元文書 第11節:買う前に降り方を決める。閾値は売却条件ではない——
  # 点灯は「価格に関係なく判断をやり直す」合図であって、機械的な売りシグナルとして
  # 使ってはならない。
  thesis_break:            # テーゼが壊れたと判断する条件(3件以上)
    - condition: 粗利率が3四半期連続で低下
      indicator: gross_margin_decline
    - condition: 主要顧客の10%超開示が消える/売上が2四半期連続で減速
      indicator: customer_concentration_disclosed_drop
    - condition: 資金繰りに追われた増資(株式数が年率15%超で増加)
      indicator: share_count_growth
  trim_rule:               # 利食い計画。機械実行はしない
    - at_moic: 3.0
      action: 1/3 を売却して原資を回収
    - at_moic: 6.0
      action: さらに 1/3
  max_hold_review_months: 24   # 何もなくても再検討する期限
dilution:
  remaining_shelf_capacity_usd: 150000000
  atm_remaining_usd: 40000000
  unexercised_options_ratio: 0.11
  has_variable_conversion_price: false
review: null   # 事後レビュー。結果が出てから追記する
---

ここから下は自由記述。事業の理解、モートの型、TAMのボトムアップ計算、
決算説明会Q&Aで気になった点、ショートレポートの検証結果など。
