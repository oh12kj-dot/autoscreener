# RACR shadow run 診断 — リスク項が順位情報を持っていない

**日付:** 2026-09-04
**対象run:** `1d7a4fc2-760c-4f3b-b546-b973e3284566`(v5、as_of 2026-09-04、population 1,266、分布available 1,157)
**実行コード:** `14c1e54`(WP-B `risk_adjusted_compounding` 初回実行)
**位置づけ:** WP-B の B-3 受け入れ条件だった「RACR vs `expected_return` の Spearman と Top20 重複を run metrics へ保存する」が `engine.py` 未対応で先送りされたため、DBから手計算で代替検証したもの。

---

## 1. 結論

**RACRの順位は CE CAGR の順位と完全に一致する(Spearman = 1.0000000000)。**
リスク控除項は3つとも順位情報を持っていない。

\[
RACR = 1.25 \times ce\_cagr - 0.2303
\]

これが 1,155/1,155 銘柄で**厳密に**成立する(残り2件は `ce_cagr >= 0` の銘柄で符号分岐が異なるだけ)。

つまり現状のRACRは、監査 §5.2 が要求した「頻度×深度×経路×永久損失×epistemic uncertaintyの統合」になっていない。**CE CAGR のアフィン変換である。**

---

## 2. 測定値

| 指標 | 実測 | 監査baseline |
|---|---:|---:|
| Spearman `expected_return` vs `risk_adjusted_compounding` | 0.890773 | — |
| Spearman `expected_return` vs `risk_adjusted`(旧) | 0.992020 | 0.991965 |
| Top20重複 ER vs RACR | 13/20 | — |
| Top20重複 ER vs `risk_adjusted`(旧) | 19/20 | 18/20 |
| **Spearman RACR vs `ce_cagr`** | **1.0000000000** | — |

旧 `risk_adjusted` の再現値(0.992020 / 19-20件)が監査の実測(0.991965 / 18件)とほぼ一致するので、測定方法自体は妥当である。

RACR が `expected_return` と 0.891 まで乖離して見えるのは、**RACRのリスク項の効果ではなく、CE CAGR が Expected CAGR と違うから**にすぎない。

---

## 3. 3つの原因

### 3.1 `tail_loss_10` が全銘柄で定数

`expected_shortfall_10pct_log` の**相異なる値は1個だけ**である。

```
distinct values = 1
min = max = median = -0.6578814551
tail_loss_10 = 0.6578814551411558   (全1,157銘柄で同一)
```

これは \(-\ln(0.01)/7 = 0.65788\dots\)、すなわち failure atom の floor そのものである。

原因:**survival probability の最大値が 0.8802 しかない。** つまり全1,157銘柄が12%以上のfailure massを持ち、下位10%分位は例外なくfailure atomの内側に完全に収まる。よって \(E[g \mid g \le q_{10}]\) は常に \(\ln(floor)/H\) になる。

| survival_probability | median 0.7917 | min 0.3193 | max 0.8802 |
|---|---|---|---|
| failure mass >= 10% の銘柄数 | **1,157 / 1,157** | | |

**これは監査 §4.3 が旧ES10について指摘した欠陥と同じ構造である。**
> 「以前使った下位10% ESはfailure atomが10%以上なら全銘柄で0になり、単なる定数差となっていた。Phase 10はこの不具合を直した」

Phase 10 は terminal ES 側でこれを直したが、WP-B が log 版で**同じ欠陥を再導入した**。0ではなく定数になっただけで、順位情報が無い点は変わらない。

### 3.2 `model_confidence` が全銘柄 0.5

```
model_confidence distinct values = [0.5]
```

`model_uncertainty = (1 - confidence) * |ce_cagr|` なので、これは `0.5 * |ce_cagr|` に退化する。λ_U=0.5 と合わせて `0.25 * |ce_cagr|` ——**独立情報ではなく ce_cagr の定数倍**である。監査 §4.2-9「confidenceは最新run上位でも概ね50%で、情報差を十分に分解できていない」がそのまま効いている。

### 3.3 drawdown / permanent loss 項が 0

設計どおり(`omitted_terms` で明示済み、これは正しい挙動)。ただし結果として、**failure確率がRACRへ入る経路が `ce_cagr` と定数tailだけになっている。** 監査 §5.2 の設計では失敗確率は λ_P·P(PermanentLoss) という独立項で入るはずだった。

---

## 4. floor 0.01 の感度

`CE_CAGR_FAILURE_FLOOR_MOIC = 0.01` は暫定値だが、水準への影響が極めて大きい。同じ continuous part から floor だけ変えて再計算した:

| floor | median CE CAGR | max CE CAGR |
|---:|---:|---:|
| 0.01(現行) | -16.648% | 0.397% |
| 0.05 | -12.468% | 3.642% |
| 0.10 | -10.590% | 5.180% |
| 0.20 | -8.770% | 6.741% |
| 0.30 | -7.635% | 7.664% |
| 0.50 | -6.164% | 8.839% |

**中央値で10.5pt、最大値で8.4pt動く。** 参考: `expected_cagr` の median は -0.844%、`ce_cagr` の median は -16.648%。

順位への影響は小さい(全銘柄に (1-s)·ln(floor) が掛かり、s の分散ぶんだけ効く)が、**画面に「年率何%」として出す以上、この定数の選択が表示値を支配している事実は伏せられない。** floor は回収率分布(WP-F)が入るまでの仮置きであり、UI・docsに明示が要る。

---

## 5. 対応方針

**昇格判定(WP-H)以前に必須:**

1. **B-3の診断をengine.pyへ実装する(先送り分)。** 各runで objective 間の Spearman・Top20重複・**「値が定数の項」の検出**を run metrics に保存する。今回の欠陥は自動で検知できたはずのものである。
2. **tail の測り方を変える。** failure mass を超える分位で測るか、生存条件付きで測り、失敗確率は独立項として入れる。現状のように atom 内部で分位を取ると必ず定数になる。
3. **confidence を実際に分散させる。** WP-D(reliability層)の前提。それまで λ_U 項は情報を持たない。
4. **floor を UI に明示する。** 「破綻時の回収率を1%と仮置きした値」であることが画面から分かること。

**現時点の扱い:** RACRはshadowであり `default_objective` は `ten_bagger` のままなので、実害は出ていない。ただし**この状態のRACRを「リスク調整済み」として昇格させてはならない。**
