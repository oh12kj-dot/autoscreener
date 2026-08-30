"""投資ノートの読み込みと検証(30.7.2)。

元文書 第13節。**アプリは書かない、読むだけ**(30.1.1 原則2)。
「建てる前に書くこと」「後から書き換えないこと」という要件は、gitの
コミット履歴が担保する——DBのUPDATEでは担保できない。

**記入漏れの検出がこのモジュールの主目的**である。元文書の実務ワークフロー
工程9は「埋められない項目があるうちは建てない」と定めており、それを人間の
自制心ではなくアプリの表示で支える。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

REQUIRED_FIELDS = ("thesis", "assumptions", "premortem", "sizing", "verification_date")
MIN_PREMORTEM_ITEMS = 3  # 元文書 第08節「失敗要因を3つ書き出す」
# J-8(investment_decision_gap_2026-08-29.md):買う前に降り方を決める(元文書 第11節)。
MIN_THESIS_BREAK_ITEMS = 3

_FRONT_MATTER_DELIMITER = "---"


class NoteParseError(RuntimeError):
    """フロントマターのYAMLが壊れている(パースできない)場合のみ送出する。

    書きかけのノート(必須項目が空)は**正常な状態**であり例外にしない
    (`missing_fields` で表現する)。壊れたYAML(構文エラー)だけをここで
    落とす——ファイルパスを含めるのは、`research/` に複数ファイルがある
    運用でどれが壊れているかをすぐ特定できるようにするため。
    """

    def __init__(self, path: Path, error: Exception) -> None:
        self.path = path
        self.error = error
        super().__init__(f"{path} のフロントマターYAMLが壊れています: {error}")


@dataclass(frozen=True)
class ResearchNote:
    ticker: str
    path: Path
    front_matter: dict
    body: str
    missing_fields: list[str]  # 記入漏れ。空なら「建ててよい」状態
    is_complete: bool


def _split_front_matter(raw: str) -> tuple[str, str]:
    """`---\\nYAML\\n---\\n本文` を (YAML文字列, 本文) に分ける。

    フロントマターが無い(`---` で始まらない)場合は空YAML・全体を本文として扱う。
    """
    stripped = raw.lstrip("﻿")
    if not stripped.startswith(_FRONT_MATTER_DELIMITER):
        return "", raw
    rest = stripped[len(_FRONT_MATTER_DELIMITER):]
    marker = "\n" + _FRONT_MATTER_DELIMITER
    idx = rest.find(marker)
    if idx == -1:
        return "", raw
    front = rest[:idx].lstrip("\n")
    body = rest[idx + len(marker):].lstrip("\n")
    return front, body


def _missing_fields(front_matter: dict) -> list[str]:
    missing: list[str] = []
    for field in REQUIRED_FIELDS:
        value = front_matter.get(field)
        if value is None or value == "" or value == []:
            missing.append(field)
    premortem = front_matter.get("premortem")
    if isinstance(premortem, list) and len(premortem) < MIN_PREMORTEM_ITEMS and "premortem" not in missing:
        missing.append("premortem")

    # J-8:売却規律の器。`exit_plan.thesis_break`(3件以上)と `exit_plan.trim_rule`。
    # 既存ノートはここで `missing_fields` に出るだけで、API は落ちない。
    exit_plan = front_matter.get("exit_plan")
    exit_plan = exit_plan if isinstance(exit_plan, dict) else {}
    thesis_break = exit_plan.get("thesis_break")
    if not isinstance(thesis_break, list) or len(thesis_break) < MIN_THESIS_BREAK_ITEMS:
        missing.append("exit_plan.thesis_break")
    trim_rule = exit_plan.get("trim_rule")
    if not isinstance(trim_rule, list) or not trim_rule:
        missing.append("exit_plan.trim_rule")

    return missing


def load_note(ticker: str, directory: Path | None = None) -> ResearchNote | None:
    """`research/<TICKER>.md` を読む。無ければ None。"""
    from autoscreener.config import PROJECT_ROOT

    target_dir = directory or PROJECT_ROOT / "research"
    path = target_dir / f"{ticker.upper()}.md"
    if not path.is_file():
        return None

    raw = path.read_text(encoding="utf-8")
    front_raw, body = _split_front_matter(raw)
    try:
        front_matter = yaml.safe_load(front_raw) or {} if front_raw else {}
    except yaml.YAMLError as exc:
        raise NoteParseError(path, exc) from exc
    if not isinstance(front_matter, dict):
        raise NoteParseError(path, ValueError("front matter is not a mapping"))

    missing = _missing_fields(front_matter)
    return ResearchNote(
        ticker=ticker.upper(),
        path=path,
        front_matter=front_matter,
        body=body,
        missing_fields=missing,
        is_complete=not missing,
    )


def load_all_notes(directory: Path | None = None) -> dict[str, ResearchNote]:
    """検討中の銘柄一覧(30.3.4 の追跡対象選定に使う)。

    `TEMPLATE.md` は雛形であり銘柄ではないので除外する。壊れたノートが1件
    あっても他のノートの読み込みは止めない——`load_note` が送出する
    `NoteParseError` はここでは伝播させ、呼び出し元(バッチ)にログさせる形を
    採るのではなく、**壊れたノート1件をスキップし警告を出す**ほうが実務上
    有用(追跡対象選定のような集計処理を1ファイルの構文ミスで止めたくない)。
    """
    import logging

    from autoscreener.config import PROJECT_ROOT

    logger = logging.getLogger(__name__)
    target_dir = directory or PROJECT_ROOT / "research"
    if not target_dir.is_dir():
        return {}

    notes: dict[str, ResearchNote] = {}
    for path in sorted(target_dir.glob("*.md")):
        if path.stem.upper() == "TEMPLATE":
            continue
        try:
            note = load_note(path.stem, target_dir)
        except NoteParseError:
            logger.warning("%s: failed to parse front matter, skipping", path, exc_info=True)
            continue
        if note is not None:
            notes[note.ticker] = note
    return notes
