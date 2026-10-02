#!/usr/bin/env python3
"""ToPARZ IT News ビルドスクリプト。

content/YYYY-MM-DD.md（原稿）を読み、templates/ のテンプレートに流し込んで
articles/YYYY-MM-DD.html・index.html・latest.html・feed.xml を生成する。
標準ライブラリだけで動く。

使い方:
    python3 scripts/build.py           # すべて生成
    python3 scripts/build.py --check   # 原稿の解析結果と [注意] だけ表示する（ファイルは書かない）
    python3 scripts/build.py --check YYYY-MM-DD   # その号だけを検査する
    python3 scripts/build.py --url YYYY-MM-DD   # その号の公開URLを表示する

原稿の書き方は README.md を参照。
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import hashlib
import html
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
CONTENT_DIR = ROOT / "content"
ARTICLES_DIR = ROOT / "articles"
TEMPLATES_DIR = ROOT / "templates"
SITE_FILE = ROOT / "site.json"

WEEKDAYS = "月火水木金土日"
CHARS_PER_MINUTE = 500  # 日本語の読了時間の目安
JST = dt.timezone(dt.timedelta(hours=9))

# 小見出し（### ）の種類。決まった書き出しのものだけを囲みにし、それ以外は本文の小見出し（plain）として扱う。
# 先に一致したものを採用する。旧形式の「〜（分析）」「ただし」も囲みのまま表示できるよう残している。
SUMMARY_LABELS = ("3行で", "３行で", "要点")
INSIGHT_LABELS = ("どう見るか", "編集部の見方", "視点")
CAUTION_LABELS = ("まだ分かっていないこと", "ただし", "注意点")
SPEC_LABELS = ("メモ",)  # 名前・使える場所・料金・上限など、使う人が確かめたい事実をまとめる欄
SUMMARY_ITEMS = 3  # 要約の行数の目安
# 引用（> ）の最終行がこの記号で始まれば、発言者として表示する
CITE_MARKS = ("—", "―", "─", "–")
# 出典行：「出典」「参考」などの語そのもの、またはその語＋コロンで始まる行
SOURCE_LABEL_RE = re.compile(r"^(出典|参考資料|参考リンク|参考|参照|一次資料)\s*(?:[:：]|$)")

FRONT_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n", re.S)
# URL内の括弧は1段までなら許す（例: Wikipediaの記事名）
LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://(?:[^\s()]|\([^\s()]*\))+)\)")
TOP_ITEM_RE = re.compile(r"^- (.*)$")
SUB_ITEM_RE = re.compile(r"^(?: {2,}|\t+)- (.*)$")
TABLE_ROW_RE = re.compile(r"^\|(.*)\|\s*$")
TABLE_RULE_RE = re.compile(r"^:?-{2,}:?$")  # 表の2行目（区切り行）のセル
NAME_NOBREAK_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.]*(?:-[A-Za-z0-9.]+)+")  # GLM-5.3、GPT-6.1 など
NAME_NOBREAK_MAX = 24  # これより長いものは、狭い画面からはみ出すので折り返しを許す
TABLE_NUM_RE = re.compile(r"\d")
TABLE_NUM_MAX = 14  # これより短く数字を含むセルは、数字のセルとして折り返さない
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_]\w*)\s*\}\}")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 原稿の検査（[注意]）で使う目安。超えても生成は止めない。
TITLE_MAX = 56  # 見出しの字数（目安は45字前後）
SUMMARY_ITEM_MAX = 100  # 要約1行の字数
MAIN_STORY_MAX = 2000  # 主記事（号の中で最も長い記事）の字数。数え方は Story.text_length
SIDE_STORY_MAX = 1000  # 2本目以降の字数（主記事の半分以下が目安）
STORIES_MAX = 3  # 1号の本数
TABLE_COLS_MAX = 4  # スマホの幅に収まる表の列数
HEDGE_MAX = 2  # 推測で締める文の数（1記事あたり）
HEDGE_RE = re.compile(r"(?:そうです|かもしれません|可能性があります)。")
PROCEDURE_RE = re.compile(r"複数の発信者|今回読んだ投稿|今回確認した(?:投稿|タイムライン|範囲)|言及を確認|独立した(?:投稿|言及)")
SPEC_ITEM_RE = re.compile(r"^\*\*[^*]+\*\*[:：]")  # メモ欄の1行「**項目**：内容」
# 公開リポジトリに載せないリンク（Xの投稿）。見つけたら生成を止める。
X_LINK_RE = re.compile(r"https?://(?:[A-Za-z0-9-]+\.)*(?:x\.com|twitter\.com|t\.co)(?![A-Za-z0-9.-])")


# ---------------------------------------------------------------------------
# データ構造
# ---------------------------------------------------------------------------


@dataclass
class Paragraph:
    text: str
    note: bool = False  # 「※」で始まる注記（小さく表示する）


@dataclass
class Heading:
    text: str
    kind: str  # summary / insight / caution / spec / plain


@dataclass
class Quote:
    text: str
    cite: str = ""  # 発言者。あれば発言の引用、なければ締めの一文として表示する


@dataclass
class ListBlock:
    items: list[tuple[str, list[str]]] = field(default_factory=list)  # (項目, 入れ子の項目)


@dataclass
class TableBlock:
    header: list[str]
    aligns: list[str]  # 列ごとの "left" / "right" / "center"
    rows: list[list[str]] = field(default_factory=list)


Block = Union[Paragraph, Heading, Quote, ListBlock, TableBlock]


@dataclass
class Story:
    title: str
    lead: str = ""  # 書き出しの段落（少し大きく表示する）
    lead_after_summary: bool = False  # 要約の囲みを先に置き、その直後を書き出しにした場合
    body: list[Block] = field(default_factory=list)  # 出現順
    sources: list[str] = field(default_factory=list)  # 出典の文（リンクを含む）

    def text_length(self) -> int:
        """読了時間の目安に使う文字数。出典と表は数えない（読み飛ばせるため）。"""
        total = len(self.title) + len(self.lead)
        in_spec = False  # メモ欄の箇条書きは数えない（確かめたい人だけが読む欄のため）
        for block in self.body:
            if isinstance(block, Heading):
                in_spec = block.kind == "spec"
                if in_spec:
                    continue
            if isinstance(block, ListBlock):
                if in_spec:
                    in_spec = False
                    continue
                total += sum(len(t) + sum(len(s) for s in subs) for t, subs in block.items)
            elif isinstance(block, Quote):
                total += len(block.text) + len(block.cite)
            elif not isinstance(block, TableBlock):
                total += len(block.text)
        return total

    def summary_items(self) -> int | None:
        """要約（3行で言うと）の行数。要約がなければ None。"""
        for i, block in enumerate(self.body):
            if isinstance(block, Heading) and block.kind == "summary":
                nxt = self.body[i + 1] if i + 1 < len(self.body) else None
                return len(nxt.items) if isinstance(nxt, ListBlock) else 0
        return None


@dataclass
class Issue:
    date: dt.date
    meta: dict[str, str]
    intro: list[str]
    stories: list[Story]
    source_text: str = ""  # 原稿ファイルの全文（build-id の計算に使う）
    number: int = 0

    @property
    def slug(self) -> str:
        return self.date.isoformat()

    @property
    def href(self) -> str:
        return f"articles/{self.slug}.html"

    @property
    def title_ja(self) -> str:
        d = self.date
        return f"{d.year}年{d.month}月{d.day}日（{WEEKDAYS[d.weekday()]}）"

    @property
    def reading_minutes(self) -> int:
        chars = sum(s.text_length() for s in self.stories) + sum(len(p) for p in self.intro)
        return max(1, math.ceil(chars / CHARS_PER_MINUTE))


# ---------------------------------------------------------------------------
# 原稿の解析
# ---------------------------------------------------------------------------


def classify(label: str) -> str:
    if label.startswith(SUMMARY_LABELS):
        return "summary"
    if label.startswith(INSIGHT_LABELS) or label.endswith(("（分析）", "(分析)")):
        return "insight"
    if label.startswith(CAUTION_LABELS):
        return "caution"
    if label.startswith(SPEC_LABELS):
        return "spec"
    return "plain"


def split_table_row(line: str) -> list[str]:
    return [cell.strip() for cell in TABLE_ROW_RE.match(line).group(1).split("|")]


def strip_source_prefix(text: str) -> str:
    return SOURCE_LABEL_RE.sub("", text).strip()


def split_sources(text: str) -> list[str]:
    """「出典：[a](URL)、[b](URL)」のように読点で並べた出典を1件ずつに分ける。"""
    parts = re.split(r"\s*[、,]\s*(?=\[)", text)
    return [p.strip() for p in parts if p.strip()]


def join_lines(lines: list[str]) -> str:
    """連続する行を1段落にする。日本語どうしは詰め、英数字・記号（ASCII）どうしは空白で継ぐ。"""
    out = ""
    for line in lines:
        if out and out[-1].isascii() and not out[-1].isspace() and line[:1].isascii() and not line[:1].isspace():
            out += " "
        out += line
    return out


def parse_front_matter(raw: str) -> tuple[dict[str, str], str]:
    m = FRONT_RE.match(raw)
    if not m:
        return {}, raw
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    return meta, raw[m.end():]


class _StoryBuilder:
    """1本の記事を行単位で組み立てる。"""

    def __init__(self, title: str):
        self.story = Story(title=title)
        self.para: list[str] = []
        self.quote: list[str] = []
        self.table: list[list[str]] = []
        self.list: ListBlock | None = None

    def flush(self) -> None:
        if self.para:
            text = join_lines(self.para)
            self.para = []
            if SOURCE_LABEL_RE.match(text):
                self.story.sources.extend(split_sources(strip_source_prefix(text)))
            elif not self.story.lead and not self.story.body:
                self.story.lead = text
            elif not self.story.lead and self._only_summary_so_far() and not text.startswith("※"):
                self.story.lead = text
                self.story.lead_after_summary = True
            else:
                self.story.body.append(Paragraph(text, note=text.startswith("※")))
        if self.quote:
            lines = [q for q in self.quote if q]
            cite = ""
            if len(lines) > 1 and lines[-1].startswith(CITE_MARKS):
                cite = lines.pop().lstrip("".join(CITE_MARKS) + " ").strip()
            self.story.body.append(Quote(join_lines(lines), cite))
            self.quote = []
        if self.table:
            self.story.body.append(self._table())
            self.table = []
        self.list = None

    def _only_summary_so_far(self) -> bool:
        """ここまでの本文が「要約の小見出し＋箇条書き」だけか。"""
        body = self.story.body
        return (
            len(body) == 2
            and isinstance(body[0], Heading)
            and body[0].kind == "summary"
            and isinstance(body[1], ListBlock)
        )

    def _table(self) -> TableBlock:
        """「| 見出し | … |」「| --- | ---: |」「| 値 | … |」の3行以上を表にする。区切り行がなければ1行目を見出しとする。"""
        header, rest = self.table[0], self.table[1:]
        aligns = ["left"] * len(header)
        if rest and all(TABLE_RULE_RE.match(c) for c in rest[0]):
            for i, cell in enumerate(rest[0][: len(header)]):
                if cell.startswith(":") and cell.endswith(":"):
                    aligns[i] = "center"
                elif cell.endswith(":"):
                    aligns[i] = "right"
            rest = rest[1:]
        rows = [(row + [""] * len(header))[: len(header)] for row in rest]
        return TableBlock(header, aligns, rows)

    def line(self, line: str) -> None:
        if not line.strip():
            self.flush()
            return
        if SOURCE_LABEL_RE.match(line.strip()):
            # 出典行は前後に空行がなくても単独で扱う
            self.flush()
            self.story.sources.extend(split_sources(strip_source_prefix(line.strip())))
            return
        if line.startswith("### "):
            self.flush()
            text = line[4:].strip()
            self.story.body.append(Heading(text, classify(text)))
            return
        if TABLE_ROW_RE.match(line.strip()):
            if self.para or self.quote or self.list:
                self.flush()
            self.table.append(split_table_row(line.strip()))
            return
        if self.table:
            self.flush()
        if line.startswith(">"):
            if self.para or self.list:
                self.flush()
            self.quote.append(line[1:].strip())
            return
        sub = SUB_ITEM_RE.match(line)
        if sub and self.list and self.list.items:
            self.list.items[-1][1].append(sub.group(1).strip())
            return
        top = TOP_ITEM_RE.match(line) or sub
        if top:
            text = top.group(1).strip()
            if SOURCE_LABEL_RE.match(text):
                self.story.sources.extend(split_sources(strip_source_prefix(text)))
                return
            if self.list is None:
                if self.para or self.quote:
                    self.flush()
                self.list = ListBlock()
                self.story.body.append(self.list)
            self.list.items.append((text, []))
            return
        if self.quote or self.list:
            self.flush()
        self.para.append(line.strip())

    def done(self) -> Story:
        self.flush()
        return self.story


def parse_issue(path: Path) -> Issue:
    raw = path.read_text(encoding="utf-8")
    x_link = X_LINK_RE.search(raw)
    if x_link:
        raise SystemExit(f"{path.name}: Xへのリンクは載せられません（{x_link.group(0)}…）。本文で内容を説明し、リンクは外してください")
    meta, body = parse_front_matter(raw)
    date_text = meta.get("date") or path.stem
    if not DATE_RE.match(date_text):
        raise SystemExit(f"{path.name}: date は YYYY-MM-DD 形式で書いてください（現在: {date_text}）")
    try:
        date = dt.date.fromisoformat(date_text)
    except ValueError as exc:
        raise SystemExit(f"{path.name}: date が正しい日付ではありません ({exc})")
    if path.stem != date.isoformat():
        raise SystemExit(
            f"{path.name}: front matter の date（{date.isoformat()}）とファイル名が一致しません。"
            "ファイル名を YYYY-MM-DD.md にし、date と揃えてください"
        )

    intro: list[str] = []
    stories: list[Story] = []
    builder: _StoryBuilder | None = None

    for raw_line in body.splitlines():
        line = raw_line.rstrip()
        if line.startswith("## "):
            if builder:
                stories.append(builder.done())
            builder = _StoryBuilder(line[3:].strip())
            continue
        if builder is None:
            if line.startswith("# "):
                meta.setdefault("title", line[2:].strip())
            elif line.strip():
                intro.append(line.strip())
            continue
        builder.line(line)
    if builder:
        stories.append(builder.done())

    if not stories:
        raise SystemExit(f"{path.name}: 「## 見出し」で始まる記事が1本もありません")
    return Issue(date=date, meta=meta, intro=intro, stories=stories, source_text=raw)


def story_sections(st: Story) -> list[tuple[str, Block]]:
    """本文の各ブロックに、それが属する節の種類（小見出しより前は空文字）を添えて返す。"""
    kind, out = "", []
    for block in st.body:
        if isinstance(block, Heading):
            kind = block.kind
        out.append((kind, block))
    return out


def is_brief(st: Story) -> bool:
    """短信（要約・書き出し・メモだけで、本文の節も意見欄もない記事）か。"""
    return not any(isinstance(b, Heading) and b.kind in ("plain", "insight", "caution") for b in st.body)


def warn_story(st: Story, is_main: bool) -> list[str]:
    """1本の記事について、書き方の目安から外れている点を返す。"""
    out: list[str] = []
    sections = story_sections(st)
    if not st.lead:
        out.append("書き出しの段落（見出しの直後か、要約の直後）がありません")
    if not st.body:
        out.append("本文がありません")
    if not st.sources:
        out.append("出典がありません")
    if len(st.title) > TITLE_MAX:
        out.append(f"見出しが長すぎます（{len(st.title)}字。目安は45字前後、上限{TITLE_MAX}字）")

    items = st.summary_items()
    if items is None:
        out.append("要約（### 3行で言うと）がありません")
    elif items != SUMMARY_ITEMS:
        out.append(f"要約は箇条書き{SUMMARY_ITEMS}行にしてください（現在 {items}行）")
    for kind, block in sections:
        if kind == "summary" and isinstance(block, ListBlock):
            for n, (text, _) in enumerate(block.items, 1):
                if len(text) > SUMMARY_ITEM_MAX:
                    out.append(f"要約の{n}行目が長すぎます（{len(text)}字。上限{SUMMARY_ITEM_MAX}字）")

    brief = is_brief(st)
    if not brief and not any(isinstance(b, Heading) and b.kind == "insight" for b in st.body):
        out.append("意見欄（### どう見るか）がありません。短信にするなら本文の節をなくし、要約・書き出し・メモだけにします")
    length = st.text_length()
    limit = MAIN_STORY_MAX if is_main else SIDE_STORY_MAX
    if length > limit:
        role = "主記事" if is_main else "2本目以降の記事"
        out.append(f"{role}が長すぎます（{length}字。上限{limit}字）。同じ事実の言い直しと、優先度の低い数字を削ります")

    tables = [b for b in st.body if isinstance(b, TableBlock)]
    if len(tables) > 1:
        out.append(f"表は1記事に1つまでにします（現在 {len(tables)}つ）")
    for table in tables:
        if len(table.header) > TABLE_COLS_MAX:
            out.append(f"表の列が多すぎます（{len(table.header)}列。スマホで収まるのは{TABLE_COLS_MAX}列まで）")
    for kind, block in sections:
        if kind == "spec" and isinstance(block, ListBlock):
            if any(not SPEC_ITEM_RE.match(text) for text, _ in block.items):
                out.append("メモ欄の各行は「**項目**：内容」の形にします")

    for block in st.body:
        if isinstance(block, Heading) and (block.text.endswith(("（分析）", "(分析)")) or block.text.startswith("ただし")):
            out.append(f"旧形式の小見出しです（{block.text}）。意見は「どう見るか」に、留保は本文の該当箇所に書きます")
        if isinstance(block, Quote) and not block.cite:
            out.append("締めの一文（> ）は使いません。結論は「どう見るか」に書きます")
    prose = [st.lead] + [b.text for kind, b in sections if isinstance(b, Paragraph) and kind != "spec"]
    hedges = sum(len(HEDGE_RE.findall(text)) for text in prose)
    if hedges > HEDGE_MAX:
        out.append(f"推測で締める文が多すぎます（「〜そうです」「〜かもしれません」「〜可能性があります」が{hedges}回。上限{HEDGE_MAX}回）")
    for text in prose:
        m = PROCEDURE_RE.search(text)
        if m:
            out.append(f"取材の手続きは本文に書きません（「{m.group(0)}」）。確認した範囲は front matter の scope に書きます")
            break
    return out


def warn_issue(issue: Issue, name: str) -> None:
    """原稿の検査結果を [注意] として表示する。目安から外れても生成は止めない。"""
    lengths = [st.text_length() for st in issue.stories]
    main = lengths.index(max(lengths))  # 号の中で最も長い記事を主記事とみなす
    for i, st in enumerate(issue.stories):
        for message in warn_story(st, is_main=(i == main)):
            print(f"[注意] {name} 記事{i + 1}: {message}", file=sys.stderr)
    if len(issue.stories) > STORIES_MAX:
        print(f"[注意] {name}: 記事が多すぎます（{len(issue.stories)}本。主記事1本と、短い記事を{STORIES_MAX - 1}本まで）", file=sys.stderr)


# ---------------------------------------------------------------------------
# HTML 生成
# ---------------------------------------------------------------------------


def _marks(text: str) -> str:
    """太字とコードだけを変換する（入力はエスケープ済み）。"""
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return text


def inline(text: str, links: str = "html") -> str:
    """HTMLエスケープした上で、リンク・太字・コードだけをMarkdownから変換する。

    links="html"   通常のリンクにする
    links="source" リンクの後ろにドメインを添える（出典欄用）
    links="plain"  リンクを外して表示文字だけ残す（<a> の中に入れる見出し用）
    """
    text = html.escape(text)
    stash: list[str] = []

    def link(m: re.Match[str]) -> str:
        label, url = _marks(m.group(1)), m.group(2)
        if links == "plain":
            rendered = label
        else:
            rendered = f'<a href="{url}" target="_blank" rel="noopener">{label}</a>'
            if links == "source":
                domain = html.escape(urlsplit(html.unescape(url)).netloc.removeprefix("www."))
                rendered += f'<span class="domain">{domain}</span>'
        stash.append(rendered)
        return f"\x00{len(stash) - 1}\x00"

    text = LINK_RE.sub(link, text)
    text = _marks(text)
    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)


def render_template(name: str, ctx: dict[str, object]) -> str:
    tpl = (TEMPLATES_DIR / name).read_text(encoding="utf-8")

    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in ctx:
            raise SystemExit(f"templates/{name}: テンプレート変数 {{{{{key}}}}} に対応する値がありません")
        return str(ctx[key])

    return PLACEHOLDER_RE.sub(sub, tpl)


def render_list(block: ListBlock) -> str:
    out = ['<ul class="story-list">']
    for text, subs in block.items:
        out.append(f"<li>{inline(text)}")
        if subs:
            out.append("<ul>" + "".join(f"<li>{inline(s)}</li>" for s in subs) + "</ul>")
        out.append("</li>")
    out.append("</ul>")
    return "".join(out)


def render_table(block: TableBlock) -> str:
    def cells(tag: str, values: list[str]) -> str:
        scope = ' scope="col"' if tag == "th" else ""
        out = []
        for i, v in enumerate(values):
            # 数字のセル（「2ドル／10ドル」など）は、狭い画面でも途中で折り返さない
            num = " num" if tag == "td" and TABLE_NUM_RE.search(v) and len(v) <= TABLE_NUM_MAX else ""
            out.append(f'<{tag} class="{block.aligns[i]}{num}"{scope}>{inline(v)}</{tag}>')
        return "".join(out)

    body = "".join(f"<tr>{cells('td', row)}</tr>" for row in block.rows)
    return (
        '<div class="table-wrap"><table class="story-table">'
        f"<thead><tr>{cells('th', block.header)}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def render_quote(block: Quote) -> str:
    """発言者があれば発言の引用、なければ締めの一文（旧形式の「ひとこと」）。"""
    if block.cite:
        return (
            f'<blockquote class="voice"><p>{inline(block.text)}</p>'
            f"<footer>{inline(block.cite)}</footer></blockquote>"
        )
    return f'<blockquote class="takeaway"><p>{inline(block.text)}</p></blockquote>'


def render_sources(sources: list[str]) -> str:
    items = "".join(f'<span class="source-item">{inline(s, links="source")}</span>' for s in sources)
    return f'<footer class="story-source"><span class="label">出典</span>{items}</footer>'


def keep_names_together(fragment: str) -> str:
    """「GPT-6.1」「GLM-5.3」のような名前が、ハイフンの位置で行をまたがないようにする。

    タグの中（URLなど）とコード表記の中は触らない。
    """

    def wrap(m: re.Match[str]) -> str:
        name = m.group(0)
        return f'<span class="nobr">{name}</span>' if len(name) <= NAME_NOBREAK_MAX else name

    out, in_code = [], False
    for piece in re.split(r"(<[^>]+>)", fragment):
        if piece.startswith("<"):
            if piece.startswith("<code"):
                in_code = True
            elif piece.startswith("</code"):
                in_code = False
        elif not in_code:
            piece = NAME_NOBREAK_RE.sub(wrap, piece)
        out.append(piece)
    return "".join(out)


def render_story(story: Story, index: int) -> str:
    return keep_names_together(_render_story(story, index))


def _render_story(story: Story, index: int) -> str:
    out = [
        f'<article class="story" id="story-{index}">',
        f'<p class="story-num">{index:02d}</p>',
        f'<h2 class="story-title">{inline(story.title)}</h2>',
    ]
    lead = f'<p class="story-lead">{inline(story.lead)}</p>' if story.lead else ""
    if lead and not story.lead_after_summary:
        out.append(lead)
        lead = ""
    open_kind = ""  # 開いている節の種類（開いていなければ空）
    for block in story.body:
        if isinstance(block, Heading):
            if open_kind:
                out.append("</section>")
            out.append(f'<section class="part part-{block.kind}">')
            out.append(f'<h3 class="part-title">{inline(block.text)}</h3>')
            open_kind = block.kind
        elif isinstance(block, Quote):
            # 締めの一文は節の外に出す。発言の引用は節の中に置く
            if open_kind and not block.cite:
                out.append("</section>")
                open_kind = ""
            out.append(render_quote(block))
        elif isinstance(block, ListBlock):
            out.append(render_list(block))
            if open_kind in ("summary", "spec"):
                # 要約とメモの囲みは箇条書きまで。続く段落は本文に戻す
                out.append("</section>")
                open_kind = ""
                if lead:
                    out.append(lead)
                    lead = ""
        elif isinstance(block, TableBlock):
            out.append(render_table(block))
        elif block.note:
            out.append(f'<p class="note">{inline(block.text)}</p>')
        else:
            out.append(f"<p>{inline(block.text)}</p>")
    if open_kind:
        out.append("</section>")
    if story.sources:
        out.append(render_sources(story.sources))
    out.append("</article>")
    return "\n".join(out)


def render_headline_list(issue: Issue, href_prefix: str = "") -> str:
    """目次・トップ用の見出しリスト。<a> の中に入るのでリンクは外す。"""
    return "".join(
        f'<li><a href="{href_prefix}#story-{i}"><span class="n">{i:02d}</span>'
        f'<span>{inline(st.title, links="plain")}</span></a></li>'
        for i, st in enumerate(issue.stories, 1)
    )


DIGEST_MIN_STORIES = 3  # 目次を出す本数。これより少ない号は、見出しの繰り返しになるので出さない


def render_digest(issue: Issue) -> str:
    if len(issue.stories) < DIGEST_MIN_STORIES:
        return ""
    return (
        '<nav class="digest" aria-labelledby="digest-title">'
        f'<h2 id="digest-title" class="digest-title">今日の{len(issue.stories)}本</h2>'
        f"<ol>{render_headline_list(issue)}</ol></nav>"
    )


def issue_description(issue: Issue) -> str:
    text = "今日の" + str(len(issue.stories)) + "本：" + "／".join(st.title for st in issue.stories)
    return text if len(text) <= 200 else text[:199] + "…"


def issue_meta_parts(issue: Issue) -> list[str]:
    parts = [f"{len(issue.stories)}本", f"読了 約{issue.reading_minutes}分"]
    updated = issue.meta.get("updated", "")
    if updated:
        try:
            d = dt.date.fromisoformat(updated)
        except ValueError:
            raise SystemExit(f"{issue.slug}.md: updated は YYYY-MM-DD 形式で書いてください（現在: {updated}）")
        parts.append(f"{d.month}月{d.day}日 再編集")
    return parts


def render_issue_scope(issue: Issue) -> str:
    """号の末尾に小さく添える、話題を確認した範囲。なければ何も出さない。"""
    scope = issue.meta.get("scope", "")
    return f'<p class="issue-scope">この号について：{html.escape(scope)}</p>' if scope else ""


def nav_link(issue: Issue | None, kind: str) -> str:
    if issue is None:
        return ""
    small = "前の号" if kind == "prev" else "次の号"
    arrow = "← " if kind == "prev" else ""
    tail = " →" if kind == "next" else ""
    return (
        f'<a class="{kind}" href="{issue.slug}.html"><small>{small}</small>'
        f"{arrow}{issue.title_ja}{tail}</a>"
    )


def render_issue_nav(prev: Issue | None, nxt: Issue | None) -> str:
    """前後の号へのリンク。どちらもなければ（創刊号だけのとき）何も出さない。"""
    links = nav_link(prev, "prev") + nav_link(nxt, "next")
    return f'<nav class="issue-nav" aria-label="前後の号">{links}</nav>' if links else ""


def site_context(site: dict[str, str]) -> dict[str, object]:
    return {
        "site_name": html.escape(site["name"]),
        "tagline": html.escape(site["tagline"]),
        "publisher": html.escape(site["publisher"]),
        "policy": html.escape(site["policy"]),
    }


def build_id(issue: Issue) -> str:
    """原稿・テンプレート・CSS・サイト設定から決まる識別子。公開後のページが今回の内容かを照合する。"""
    parts = [
        issue.source_text,
        (TEMPLATES_DIR / "article.html").read_text(encoding="utf-8"),
        (ROOT / "assets" / "style.css").read_text(encoding="utf-8"),
        SITE_FILE.read_text(encoding="utf-8"),
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def build_article(site: dict[str, str], issue: Issue, prev: Issue | None, nxt: Issue | None) -> str:
    page_title = issue.meta.get("title") or f"{issue.title_ja}のIT・AIニュース"
    ctx: dict[str, object] = {
        **site_context(site),
        "build_id": build_id(issue),
        "page_title": html.escape(f"{page_title}｜{site['name']}"),
        "og_title": html.escape(f"{page_title}｜{site['name']}"),
        "description": html.escape(issue_description(issue)),
        "canonical_url": html.escape(site["base_url"] + issue.href),
        "date_iso": issue.date.isoformat(),
        "assets_path": "../assets/",
        "index_path": "../",
        "rss_path": "../feed.xml",
        "kicker": f"Daily IT News ・ No.{issue.number}",
        "issue_title": html.escape(issue.title_ja),
        "issue_meta": "".join(f"<span>{p}</span>" for p in issue_meta_parts(issue)),
        "issue_intro": "".join(f'<p class="issue-intro">{inline(p)}</p>' for p in issue.intro),
        "digest": render_digest(issue),
        "stories": "\n\n".join(render_story(st, i) for i, st in enumerate(issue.stories, 1)),
        "issue_scope": render_issue_scope(issue),
        "issue_nav": render_issue_nav(prev, nxt),
        "year": str(issue.date.year),
    }
    return render_template("article.html", ctx)


def build_index(site: dict[str, str], issues: list[Issue]) -> str:
    if issues:
        latest = issues[-1]
        feature = (
            '<article class="feature">'
            f'<h2 class="feature-date"><a href="{latest.href}">{html.escape(latest.title_ja)}</a>'
            f'<span class="no">No.{latest.number}</span></h2>'
            f'<p class="feature-meta">{"・".join(issue_meta_parts(latest))}</p>'
            f'<ol class="feature-list">{render_headline_list(latest, latest.href)}</ol>'
            f'<a class="button" href="{latest.href}">この号を読む</a>'
            "</article>"
        )
        rows = []
        for iss in reversed(issues):
            titles = "".join(f'<span>{inline(st.title, links="plain")}</span>' for st in iss.stories)
            rows.append(
                f'<li><a href="{iss.href}"><span class="issue-list-date">{html.escape(iss.title_ja)}</span>'
                f'<span class="issue-list-titles">{titles}</span></a></li>'
            )
        issues_html = f'<ul class="issue-list">{"".join(rows)}</ul>'
        year = max(dt.date.today().year, latest.date.year)
    else:
        feature = '<p class="empty">まだ記事がありません。最初の号を公開すると、ここに表示されます。</p>'
        issues_html = '<p class="empty">バックナンバーはまだありません。</p>'
        year = dt.date.today().year

    ctx: dict[str, object] = {
        **site_context(site),
        "page_title": html.escape(site["name"]),
        "og_title": html.escape(site["name"]),
        "description": html.escape(site["tagline"]),
        "canonical_url": html.escape(site["base_url"]),
        "assets_path": "assets/",
        "index_path": "./",
        "rss_path": "feed.xml",
        "latest": feature,
        "issues": issues_html,
        "year": str(year),
    }
    return render_template("index.html", ctx)


def build_latest_redirect(issue: Issue | None) -> str:
    href = issue.href if issue else "./"
    label = f"最新号（{html.escape(issue.title_ja)}）" if issue else "トップページ"
    return (
        "<!DOCTYPE html>\n<html lang=\"ja\">\n<head>\n<meta charset=\"utf-8\">\n"
        f'<meta http-equiv="refresh" content="0; url={href}">\n'
        f'<link rel="canonical" href="{href}">\n<title>最新号へ移動</title>\n</head>\n'
        f'<body><a href="{href}">{label}へ移動します</a></body>\n</html>\n'
    )


def build_feed(site: dict[str, str], issues: list[Issue]) -> str:
    base = site["base_url"]
    items = []
    for iss in reversed(issues):
        link = base + iss.href
        published = dt.datetime.combine(iss.date, dt.time(9, 0), tzinfo=JST)  # 公開時刻は毎号9:00固定
        titles = "".join(f"<li>{html.escape(st.title)}</li>" for st in iss.stories)
        description = html.escape(f"<ol>{titles}</ol>")
        items.append(
            "    <item>\n"
            f"      <title>{html.escape(iss.title_ja)}のIT・AIニュース（{len(iss.stories)}本）</title>\n"
            f"      <link>{html.escape(link)}</link>\n"
            f'      <guid isPermaLink="true">{html.escape(link)}</guid>\n'
            f"      <pubDate>{email.utils.format_datetime(published)}</pubDate>\n"
            f"      <description>{description}</description>\n"
            "    </item>"
        )
    newest_date = issues[-1].date if issues else dt.date.today()
    newest = dt.datetime.combine(newest_date, dt.time(9, 0), tzinfo=JST)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "  <channel>\n"
        f"    <title>{html.escape(site['name'])}</title>\n"
        f"    <link>{html.escape(base)}</link>\n"
        f"    <description>{html.escape(site['tagline'])}</description>\n"
        "    <language>ja</language>\n"
        f"    <lastBuildDate>{email.utils.format_datetime(newest)}</lastBuildDate>\n"
        f'    <atom:link href="{html.escape(base + "feed.xml")}" rel="self" type="application/rss+xml"/>\n'
        + ("\n".join(items) + "\n" if items else "")
        + "  </channel>\n</rss>\n"
    )


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------


def load_issues() -> list[Issue]:
    """content/ の原稿をすべて読む。原稿がなければ空のリスト（サイトは「記事なし」の状態で生成する）。"""
    paths = sorted(CONTENT_DIR.glob("*.md")) if CONTENT_DIR.exists() else []
    issues: list[Issue] = []
    seen: dict[str, str] = {}
    for path in paths:
        issue = parse_issue(path)
        if issue.slug in seen:
            raise SystemExit(f"{path.name}: {seen[issue.slug]} と date が重複しています（1日1ファイル）")
        seen[issue.slug] = path.name
        issues.append(issue)
    issues.sort(key=lambda i: i.date)
    for n, iss in enumerate(issues, 1):
        iss.number = n
    return issues


def check(issues: list[Issue]) -> None:
    for iss in issues:
        print(f"No.{iss.number} {iss.title_ja}  {len(iss.stories)}本 / 読了 約{iss.reading_minutes}分")
        for i, st in enumerate(iss.stories, 1):
            print(f"  {i:02d} {st.title}")
            print(f"     書き出し {len(st.lead)}字" + ("（要約の後）" if st.lead_after_summary else ""))
            for block in st.body:
                if isinstance(block, Heading):
                    print(f"     ### [{block.kind}] {block.text}")
                elif isinstance(block, Quote):
                    print(f"     > {block.text[:40]}" + (f"（{block.cite}）" if block.cite else ""))
                elif isinstance(block, ListBlock):
                    print(f"     箇条書き {len(block.items)}項目")
                elif isinstance(block, TableBlock):
                    print(f"     表 {len(block.header)}列×{len(block.rows)}行")
                else:
                    print(f"     {'注記' if block.note else '段落'} {len(block.text)}字")
            print(f"     本文 計{st.text_length()}字")
            for src in st.sources:
                print(f"     出典 {src}")


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    print(f"生成: {path.relative_to(ROOT)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--check",
        nargs="?",
        const="",
        metavar="YYYY-MM-DD",
        help="原稿を解析して構造と [注意] を表示するだけで、ファイルを書かない。日付を付けるとその号だけを見る",
    )
    parser.add_argument("--url", metavar="YYYY-MM-DD", help="その号の公開URLを表示して終了する")
    args = parser.parse_args()

    site = json.loads(SITE_FILE.read_text(encoding="utf-8"))
    if args.url:
        if not DATE_RE.match(args.url):
            raise SystemExit("--url には YYYY-MM-DD を指定してください")
        print(f"{site['base_url']}articles/{args.url}.html")
        return
    issues = load_issues()
    if args.check is not None:
        targets = [iss for iss in issues if not args.check or iss.slug == args.check]
        if args.check and not targets:
            raise SystemExit(f"content/{args.check}.md がありません")
        if not issues:
            print("content/ に原稿（YYYY-MM-DD.md）がありません")
        for iss in targets:
            warn_issue(iss, f"{iss.slug}.md")
        check(targets)
        return

    # 生成時の [注意] は最新号だけに出す（過去の号は公開済みで、書き直しの対象ではないため）
    if issues:
        warn_issue(issues[-1], f"{issues[-1].slug}.md")
    for i, iss in enumerate(issues):
        prev = issues[i - 1] if i > 0 else None
        nxt = issues[i + 1] if i + 1 < len(issues) else None
        write(ARTICLES_DIR / f"{iss.slug}.html", build_article(site, iss, prev, nxt))
    # 原稿のない記事HTMLは削除する（生成物は原稿から決まる）
    slugs = {iss.slug for iss in issues}
    if ARTICLES_DIR.exists():
        for stale in sorted(ARTICLES_DIR.glob("*.html")):
            if stale.stem not in slugs:
                stale.unlink()
                print(f"削除: {stale.relative_to(ROOT)}（原稿がありません）")
    write(ROOT / "index.html", build_index(site, issues))
    write(ROOT / "latest.html", build_latest_redirect(issues[-1] if issues else None))
    write(ROOT / "feed.xml", build_feed(site, issues))


if __name__ == "__main__":
    main()
