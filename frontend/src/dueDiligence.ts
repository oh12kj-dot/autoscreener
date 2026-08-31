/**
 * デューデリ・チェックリストの判定ロジック(J-5、docs/investment_decision_gap_2026-08-29.md)。
 *
 * 元文書 30.9.2 の11工程を、既存の API(`/candidates/{ticker}` と
 * `/research/{ticker}`)だけで3状態に落とす**純関数**。フロントに vitest 基盤が
 * 無いため、テスト基盤の新設はこの計画の対象外——型と `npm run build` で担保する
 * (`warnings.ts` と同じ層)。
 *
 * 状態:
 *   - `auto`     … 機械が判定済み(API の値から確定できる)
 *   - `recorded` … 人間が投資ノートに記録済み
 *   - `todo`     … 未着手(自動判定もノート記入もされていない)
 */

import type { CandidateDetail, ResearchNoteResponse } from "./api/types";

export type ChecklistState = "auto" | "recorded" | "todo";

export interface ChecklistItem {
  step: number;
  title: string;
  state: ChecklistState;
  detail: string;
  /** BLOCKING レッドフラグなど、目立たせるべき項目 */
  warn?: boolean;
}

function noteHasValue(note: ResearchNoteResponse | null, key: string): boolean {
  if (!note || !note.exists) return false;
  const v = note.front_matter[key];
  if (v == null || v === "") return false;
  if (Array.isArray(v)) return v.length > 0;
  return true;
}

function premortemCount(note: ResearchNoteResponse | null): number {
  const v = note?.front_matter["premortem"];
  return Array.isArray(v) ? v.length : 0;
}

export function deriveChecklist(
  detail: CandidateDetail,
  note: ResearchNoteResponse | null,
): ChecklistItem[] {
  const items: ChecklistItem[] = [];

  // 01 取扱可否
  items.push(
    detail.tradability === "tradable"
      ? {
          step: 1,
          title: "証券会社の取扱可否",
          state: "auto",
          detail: `取扱あり(${detail.tradable_brokers.join("・") || "ブローカー不明"})`,
        }
      : {
          step: 1,
          title: "証券会社の取扱可否",
          state: "todo",
          detail:
            detail.tradability === "not_listed"
              ? "取扱可否リストに載っているが対象外。手で確認してください"
              : "取扱可否リストが未整備です。使う証券会社で発注できるか手で確認してください",
        },
  );

  // 02 流動性
  items.push(
    detail.adv_usd != null && detail.max_position_usd != null
      ? {
          step: 2,
          title: "流動性(ADV・投入上限)",
          state: "auto",
          detail: `ADV $${(detail.adv_usd / 1e3).toFixed(0)}K ・ 投入上限 $${detail.max_position_usd.toLocaleString()}${
            detail.position_binding_constraint
              ? `(${detail.position_binding_constraint === "liquidity" ? "板が制約" : "規律が制約"})`
              : ""
          }`,
        }
      : {
          step: 2,
          title: "流動性(ADV・投入上限)",
          state: "todo",
          detail: "価格履歴が不足しており ADV を計算できません",
        },
  );

  // 03 即死要因(レッドフラグ)
  if (detail.filings_checked_on == null) {
    items.push({
      step: 3,
      title: "即死要因(SEC提出書類)",
      state: "todo",
      detail: "追跡対象外のため EDGAR を未確認です",
    });
  } else {
    const blocking = detail.red_flags.filter((f) => f.severity === "blocking").length;
    const warning = detail.red_flags.filter((f) => f.severity === "warning").length;
    items.push({
      step: 3,
      title: "即死要因(SEC提出書類)",
      state: "auto",
      warn: blocking > 0,
      detail:
        blocking > 0
          ? `BLOCKING ${blocking}件 ・ WARNING ${warning}件(最終確認 ${detail.filings_checked_on})`
          : warning > 0
            ? `WARNING ${warning}件(最終確認 ${detail.filings_checked_on})`
            : `該当なし(最終確認 ${detail.filings_checked_on})`,
    });
  }

  // 04 原本照合(SEC XBRL 突合)
  if (detail.sec_reconciliation.length === 0) {
    items.push({
      step: 4,
      title: "SEC原本との突合",
      state: "todo",
      detail: "XBRL データが未収集です(追跡対象外)",
    });
  } else {
    const counts = detail.sec_reconciliation.reduce<Record<string, number>>((acc, item) => {
      acc[item.status] = (acc[item.status] ?? 0) + 1;
      return acc;
    }, {});
    const mismatched = (counts.mismatch ?? 0) + (counts.magnitude_mismatch ?? 0);
    items.push({
      step: 4,
      title: "SEC原本との突合",
      state: "auto",
      warn: mismatched > 0,
      detail:
        mismatched > 0
          ? `不一致 ${mismatched}件 / 一致 ${counts.match ?? 0}件`
          : `一致 ${counts.match ?? 0}件・不一致なし`,
    });
  }

  // 05 希薄化
  const dil = detail.dilution_outlook;
  if (dil?.reserved_dilution_ratio != null || noteHasValue(note, "dilution")) {
    items.push({
      step: 5,
      title: "希薄化(将来枠・実績)",
      state: dil?.reserved_dilution_ratio != null ? "auto" : "recorded",
      detail:
        dil?.reserved_dilution_ratio != null
          ? `予約済み希薄化比率 ${(dil.reserved_dilution_ratio * 100).toFixed(1)}%`
          : "投資ノートに dilution ブロックを記入済み",
    });
  } else if (dil) {
    items.push({
      step: 5,
      title: "希薄化(将来枠・実績)",
      state: "auto",
      detail: `シェルフ ${dil.shelf_filings.length}件 ・ 公募増資(直近3年)${dil.offerings_last_3y}件。残枠はノートに手入力してください`,
    });
  } else {
    items.push({
      step: 5,
      title: "希薄化(将来枠・実績)",
      state: "todo",
      detail: "提出履歴が無く、ノートにも記入がありません",
    });
  }

  // 06 事業の理解
  items.push(
    note?.exists && (note.body ?? "").trim().length > 0
      ? {
          step: 6,
          title: "事業の理解",
          state: "recorded",
          detail: "投資ノート本文に記述あり。上の「この会社は何をしているか」も併せて確認",
        }
      : {
          step: 6,
          title: "事業の理解",
          state: "todo",
          detail: "投資ノート本文が空です。上の「この会社は何をしているか」を読み、要点を書いてください",
        },
  );

  // 07 経営陣の検証
  items.push(
    noteHasValue(note, "assumptions")
      ? { step: 7, title: "経営陣の検証", state: "recorded", detail: "ノートの assumptions に記入あり" }
      : { step: 7, title: "経営陣の検証", state: "todo", detail: "ノートの assumptions が未記入です" },
  );

  // 08 反証(プレモーテム)
  const pm = premortemCount(note);
  items.push(
    pm >= 3
      ? { step: 8, title: "反証(プレモーテム3件以上)", state: "recorded", detail: `premortem ${pm}件` }
      : { step: 8, title: "反証(プレモーテム3件以上)", state: "todo", detail: `premortem ${pm}件(3件以上必要)` },
  );

  // 09 サイジングと記録
  items.push(
    note?.is_complete
      ? { step: 9, title: "サイジングと記録", state: "recorded", detail: "投資ノートの必須項目はすべて埋まっています" }
      : {
          step: 9,
          title: "サイジングと記録",
          state: "todo",
          detail: note?.exists
            ? `未記入: ${note.missing_fields.join("、") || "なし"}`
            : "投資ノート(research/<TICKER>.md)がまだありません",
        },
  );

  // 10 執行(往復コスト)
  items.push(
    detail.estimated_round_trip_cost_bps != null
      ? {
          step: 10,
          title: "執行コストの確認",
          state: "auto",
          detail: `推定往復コスト ${detail.estimated_round_trip_cost_bps.toFixed(0)} bps`,
        }
      : { step: 10, title: "執行コストの確認", state: "todo", detail: "往復コストを推定できません(価格履歴不足)" },
  );

  // 11 検証日
  items.push(
    noteHasValue(note, "verification_date")
      ? {
          step: 11,
          title: "次回の検証日",
          state: "recorded",
          detail: `verification_date: ${String(note?.front_matter["verification_date"])}`,
        }
      : { step: 11, title: "次回の検証日", state: "todo", detail: "ノートに verification_date が未記入です" },
  );

  return items;
}

export interface ExternalLink {
  label: string;
  href: string;
}

/** 30.9.2 の外部リンク。CIK が無い銘柄では EDGAR リンクを出さない。 */
export function externalLinks(detail: CandidateDetail): {
  links: ExternalLink[];
  cikUnresolved: boolean;
} {
  const cik = detail.profile?.cik ?? null;
  const links: ExternalLink[] = [];
  if (cik) {
    links.push({
      label: "EDGAR(10-K一覧)",
      href: `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=${cik}&type=10-K`,
    });
  }
  if (detail.profile?.website) {
    links.push({ label: "会社IRサイト", href: detail.profile.website });
  }
  links.push({
    label: "ショートレポート検索",
    href: `https://www.google.com/search?q=${encodeURIComponent(`"${detail.ticker}" short report`)}`,
  });
  links.push({ label: "証券集団訴訟(Stanford SCAC)", href: "https://securities.stanford.edu" });
  return { links, cikUnresolved: !cik };
}
