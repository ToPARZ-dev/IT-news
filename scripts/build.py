#!/usr/bin/env python3
"""ToPARZ IT News ビルドスクリプト。

content/YYYY-MM-DD.md（原稿）を読み、templates/ のテンプレートに流し込んで
articles/YYYY-MM-DD.html・index.html・latest.html・feed.xml を生成する。
標準ライブラリだけで動く。

使い方:
    python3 scripts/build.py           # すべて生成
    python3 scripts/build.py --check   # 原稿の解析結果だけ表示する（ファイルは書かない）
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

# 小見出し（### ）の語から種類を決める。先に一致したものを採用する。
KIND_KEYWORDS = (
    ("caution", ("注意", "制約", "留意", "未確認", "リスク", "課題", "限界", "懸念", "ただし", "落とし穴", "分からない", "わからない")),
    ("insight", ("分析", "示唆", "使いどころ", "見方", "考察", "影響", "意味", "なぜ", "どう", "どこで", "読み解", "注目", "ポイント")),
)
# 出典行：「出典」「参考」などの語そのもの、またはその語＋コロンで始まる行
SOURCE_LABEL_RE = re.compile(r"^(出典|参考資料|参考リンク|参考|参照|一次資料)\s*(?:[:：]|$)")

FRONT_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n", re.S)
# URL内の括弧は1段までなら許す（例: Wikipediaの記事名）
LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://(?:[^\s()]|\([^\s()]*\))+)\)")
TOP_ITEM_RE = re.compile(r"^- (.*)$")
SUB_ITEM_RE = re.compile(r"^(?: {2,}|\t+)- (.*)$")
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_]\w*)\s*\}\}")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# データ構造
# ---------------------------------------------------------------------------


@dataclass
class Paragraph:
    text: str


@dataclass
class Heading:
    text: str
    kind: str  # insight / caution / fact


@dataclass
class Quote:
    text: str


@dataclass
class ListBlock:
    items: list[tuple[str, list[str]]] = field(default_factory=list)  # (項目, 入れ子の項目)


Block = Union[Paragraph, Heading, Quote, ListBlock]


@dataclass
class Story:
    title: str
    lead: str = ""  # 見出し直後の段落（つかみ）
    body: list[Block] = field(default_factory=list)  # 出現順
    sources: list[str] = field(default_factory=list)  # 出典の文（リンクを含む）

    def text_length(self) -> int:
        total = len(self.title) + len(self.lead) + sum(len(s) for s in self.sources)
        for block in self.body:
            if isinstance(block, ListBlock):
                total += sum(len(t) + sum(len(s) for s in subs) for t, subs in block.items)
            else:
                total += len(block.text)
        return total


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
    for kind, words in KIND_KEYWORDS:
        if any(w in label for w in words):
            return kind
    return "fact"


def strip_source_prefix(text: str) -> str:
    return SOURCE_LABEL_RE.sub("", text).strip()


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
        self.list: ListBlock | None = None

    def flush(self) -> None:
        if self.para:
            text = join_lines(self.para)
            self.para = []
            if SOURCE_LABEL_RE.match(text):
                rest = strip_source_prefix(text)
                if rest:
                    self.story.sources.append(rest)
            elif not self.story.lead and not self.story.body:
                self.story.lead = text
            else:
                self.story.body.append(Paragraph(text))
        if self.quote:
            self.story.body.append(Quote(join_lines(self.quote)))
            self.quote = []
        self.list = None

    def line(self, line: str) -> None:
        if not line.strip():
            self.flush()
            return
        if SOURCE_LABEL_RE.match(line.strip()):
            # 出典行は前後に空行がなくても単独で扱う
            self.flush()
            rest = strip_source_prefix(line.strip())
            if rest:
                self.story.sources.append(rest)
            return
        if line.startswith("### "):
            self.flush()
            text = line[4:].strip()
            self.story.body.append(Heading(text, classify(text)))
            return
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
                rest = strip_source_prefix(text)
                if rest:
                    self.story.sources.append(rest)
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


def warn_issue(issue: Issue, name: str) -> None:
    for i, st in enumerate(issue.stories, 1):
        if not st.lead:
            print(f"[注意] {name} 記事{i}: 見出しの直後のリード段落がありません", file=sys.stderr)
        if not st.body:
            print(f"[注意] {name} 記事{i}: 本文がありません", file=sys.stderr)
        if not st.sources:
            print(f"[注意] {name} 記事{i}: 出典がありません", file=sys.stderr)
        if not any(isinstance(b, Heading) and b.kind == "caution" for b in st.body):
            print(f"[注意] {name} 記事{i}: 「ただし」などの注意の小見出しがありません", file=sys.stderr)
        if not any(isinstance(b, Quote) for b in st.body):
            print(f"[注意] {name} 記事{i}: 締めの一文（> で始まる引用）がありません", file=sys.stderr)


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


def render_sources(sources: list[str]) -> str:
    items = "".join(f'<span class="source-item">{inline(s, links="source")}</span>' for s in sources)
    return f'<footer class="story-source"><span class="label">出典</span>{items}</footer>'


def render_story(story: Story, index: int) -> str:
    out = [
        f'<article class="story" id="story-{index}">',
        f'<p class="story-num">{index:02d}</p>',
        f'<h2 class="story-title">{inline(story.title)}</h2>',
    ]
    if story.lead:
        out.append(f'<p class="story-lead">{inline(story.lead)}</p>')
    open_part = False
    for block in story.body:
        if isinstance(block, Heading):
            if open_part:
                out.append("</section>")
            out.append(f'<section class="part part-{block.kind}">')
            out.append(f'<h3 class="part-title">{inline(block.text)}</h3>')
            open_part = True
        elif isinstance(block, Quote):
            if open_part:
                out.append("</section>")
                open_part = False
            out.append(f'<blockquote class="takeaway"><p>{inline(block.text)}</p></blockquote>')
        elif isinstance(block, ListBlock):
            out.append(render_list(block))
        else:
            out.append(f"<p>{inline(block.text)}</p>")
    if open_part:
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


def render_digest(issue: Issue) -> str:
    return (
        '<nav class="digest" aria-labelledby="digest-title">'
        f'<h2 id="digest-title" class="digest-title">今日の{len(issue.stories)}本</h2>'
        f"<ol>{render_headline_list(issue)}</ol></nav>"
    )


def issue_description(issue: Issue) -> str:
    text = "今日の" + str(len(issue.stories)) + "本：" + "／".join(st.title for st in issue.stories)
    return text if len(text) <= 200 else text[:199] + "…"


def issue_meta_parts(issue: Issue) -> list[str]:
    parts = []
    scope = issue.meta.get("scope", "")
    if scope:
        parts.append(f"確認範囲：{html.escape(scope)}")
    parts.append(f"{len(issue.stories)}本")
    parts.append(f"読了 約{issue.reading_minutes}分")
    return parts


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
        warn_issue(issue, path.name)
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
            print(f"     リード {len(st.lead)}字")
            for block in st.body:
                if isinstance(block, Heading):
                    print(f"     ### [{block.kind}] {block.text}")
                elif isinstance(block, Quote):
                    print(f"     > {block.text[:40]}")
                elif isinstance(block, ListBlock):
                    print(f"     箇条書き {len(block.items)}項目")
                else:
                    print(f"     段落 {len(block.text)}字")
            for src in st.sources:
                print(f"     出典 {src}")


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    print(f"生成: {path.relative_to(ROOT)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="原稿を解析して構造を表示するだけで、ファイルを書かない")
    parser.add_argument("--url", metavar="YYYY-MM-DD", help="その号の公開URLを表示して終了する")
    args = parser.parse_args()

    site = json.loads(SITE_FILE.read_text(encoding="utf-8"))
    if args.url:
        if not DATE_RE.match(args.url):
            raise SystemExit("--url には YYYY-MM-DD を指定してください")
        print(f"{site['base_url']}articles/{args.url}.html")
        return
    issues = load_issues()
    if args.check:
        if not issues:
            print("content/ に原稿（YYYY-MM-DD.md）がありません")
        check(issues)
        return

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
