/**
 * 警告バッジ(C-4、docs/model_audit_v4_2026-08-26.md)。
 *
 * 実データ監査で判明した「ランキング上位はモデルの外挿限界(クランプ)に
 * 当たった銘柄に偏っている」という構造(S-6/S-7/S-8/S-9・A-1・S-5)を、
 * モデルのロジックを変えずに利用者へ見せるための表示層。コードは
 * `CandidateSummary.warnings` / `CandidateDetail.warnings`(APIが
 * `factors` から算出)にそのまま対応する。
 */

export interface WarningInfo {
  label: string;
  description: string;
}

export const WARNING_INFO: Record<string, WarningInfo> = {
  growth_rate_clamped: {
    label: "成長率が上限に張り付き",
    description:
      "初期成長率がモデルの外挿限界(上限60%など)に張り付いています。この銘柄固有の成長力を測れたのではなく、モデルの丸めが効いた値です。同じフラグが付いた銘柄どうしは成長率の差が実質消えています。",
  },
  dilution_data_missing: {
    label: "希薄化データ欠損",
    description:
      "株式数の希薄化(増資ペース)が測れず、断面の中央値で補完しています。実際の希薄化がそれより悪ければ期待倍率は過大評価です。",
  },
  net_debt_data_missing: {
    label: "ネットデット構成データ欠損",
    description:
      "有利子負債または現金の行が財務データに存在せず、ネットデット(有利子負債−現金)をゼロとして計算しています。データが取れなかっただけの銘柄が「無借金の優良企業」として上位に浮上している可能性があります(A-1と同型)。",
  },
  high_leverage: {
    label: "高レバレッジ",
    description:
      "事業価値(EV)の変動が株主価値に1.5倍以上に増幅される負債水準です。上振れも下振れも同じ倍率で拡大されます。",
  },
  lease_heavy: {
    label: "リース債務が主体",
    description:
      "ネットデットの半分以上がオペレーティングリース債務(店舗賃借など)です。金融負債と同列に「高レバレッジ」として扱われていますが、性質が異なる可能性があります。",
  },
  cyclical_margin_extrapolation: {
    label: "粗利率の外挿が循環由来の疑い",
    description:
      "粗利率の年次履歴が上下に振れている(構造的に改善しているのではない)のに、直近2期の差分を7年後まで引き伸ばして粗利率の改善を計上しています。市況が上向いた直後の資源・エネルギー・素材の銘柄で典型的に起こります。改善が一過性なら期待倍率は過大です。",
  },
  terminal_multiple_capped: {
    label: "終端バリュエーションが上限に到達",
    description:
      "入口のEV/粗利がユニバース断面の最上位帯にあり、7年後の終端倍率が上限で頭打ちになっています。モデルは「今日の高い倍率が7年後も続く」とは想定していません。",
  },
  large_margin_extrapolation: {
    label: "粗利率の外挿が大きい",
    description: "直近の粗利率トレンドを7年後まで引き伸ばした結果、粗利率が1.5倍以上に変わる想定になっています。",
  },
  nowcast_upward: {
    label: "株価トレンドで成長率を上方修正",
    description:
      "直近12ヶ月の株価トレンドにより、決算ベースの成長率が+10pt以上引き上げられています。決算自体が縮小を示している場合でも起こりえます。",
  },
  low_survival_probability: {
    label: "生存確率が低い",
    description:
      "目標年数まで上場を維持できる確率が50%を下回っています。順位はこのリスクをほとんど反映していません(σの縮小推定のため、順位は実質的にリスク未調整の期待倍率の順序に近くなっています)。",
  },
  // 30.5.4:突合(XBRL)由来の警告。
  sec_value_mismatch: {
    label: "SEC原本と数値が不一致",
    description:
      "モデルが使っている売上/株式数/現金/負債のいずれかが、SECに提出された原本(XBRL)と大きく食い違っています。モデルの入力は yfinance の二次加工データであり、単位の取り違えや分割未調整が実際に見つかっています。詳細画面で該当項目と原本へのリンクを確認してください。",
  },
  sec_magnitude_mismatch: {
    label: "SEC原本と桁違い",
    description:
      "モデルが使っている値がSEC原本(XBRL)の10倍以上、または1/10以下です。単位の取り違え(千ドル単位と実額の混同等)の可能性が高く、最優先で確認してください。",
  },
  // 30.6.2:将来の希薄化見通し由来の警告。
  heavy_reserved_dilution: {
    label: "予約済み希薄化が重い",
    description:
      "投資ノートに記入したシェルフ残枠・ATM残枠の合計が時価総額の20%を超えています。7年で10倍という想定に対して重い負担になりえます(実行されるとは限りませんが、経営陣がいつでも使える手段として残っています)。",
  },
  // 30.4.4:レッドフラグ(即死要因)。severityで色分けするが、説明はここに集約する。
  restatement: {
    label: "リステートメント(決算の訂正)",
    description: "過去の決算を訂正する8-K(Item 4.02)が提出されています。事実上「売り」に相当する重大事象です。",
  },
  auditor_change: {
    label: "監査人の交代",
    description: "監査法人の交代を報告する8-K(Item 4.01)が提出されています。特に契約解除が会社都合でない場合は要注意です。",
  },
  listing_deficiency: {
    label: "上場基準抵触",
    description: "取引所の上場維持基準に抵触したことを報告する8-K(Item 3.01)が提出されています。",
  },
  late_filing: {
    label: "決算報告の遅延(NT提出)",
    description: "決算報告が期限に間に合わず、NT 10-K/NT 10-Qが提出されています。原則として新規建てを停止すべき事象です。",
  },
  going_concern: {
    label: "継続企業の前提に関する重要な不確実性",
    description: "直近の10-K/10-Q本文に、継続企業の前提に関する重要な不確実性(going concern)の記載があります。",
  },
  material_weakness: {
    label: "内部統制の重要な不備",
    description: "直近の10-K本文に、内部統制の重要な不備(material weakness)の記載があります。誤検知の可能性があるためWARNING扱いです。",
  },
  officer_departure: {
    label: "役員の退任",
    description: "役員(CFO等)の退任を報告する8-K(Item 5.02)が提出されています。特にCFOの突然の退任は要注意です。",
  },
  sec_comment_letter: {
    label: "SECコメントレター",
    description: "SECとの往復書簡(UPLOAD/CORRESP)が提出されています。SECが会計処理のどこを疑ったかを示す一次情報です。",
  },
  shelf_registration: {
    label: "普通株式の棚上げ登録(シェルフ)",
    description: "S-3/S-3ASRが提出されています。将来の希薄化枠であり、それ自体は即座の悪材料ではありません。",
  },
  secondary_offering: {
    label: "公募増資の実施",
    description: "424B5が提出されています。シェルフからの実弾の発行であり、資本配分の癖を示します。",
  },
  material_agreement: {
    label: "重要な契約の締結",
    description: "重要な契約の締結を報告する8-K(Item 1.01)が提出されています。",
  },
  activist_stake: {
    label: "アクティビストによる大量保有",
    description: "SC 13Dが提出されています。アクティビストが5%以上を取得し、経営に関与する意図を示しています。",
  },
  delisting_form: {
    label: "上場廃止関連書類の提出",
    description: "Form 25-NSEまたは15-12Bが提出されています。上場廃止の手続きが進行中です。",
  },
};

export function warningLabel(code: string): string {
  return WARNING_INFO[code]?.label ?? code;
}

export function warningDescription(code: string): string {
  return WARNING_INFO[code]?.description ?? "";
}
