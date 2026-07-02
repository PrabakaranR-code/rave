"""Main-content extraction from raw HTML — an in-house readability heuristic.

Approach: parse HTML into a lightweight tree (stdlib html.parser only), score
candidate containers by text density (total text length discounted by link
density), pick the best container, and emit its block-level text with
boilerplate (nav/header/footer/aside/forms) stripped. Also pulls the page
title and a best-effort publication date from meta tags, <time> elements, or
visible date strings.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser

SKIP_ENTIRELY = {
    "script", "style", "noscript", "template", "svg", "iframe",
    "nav", "header", "footer", "aside", "form", "button", "select",
}
BLOCK_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "pre", "blockquote",
    "td", "th", "dd", "dt", "figcaption",
}
CANDIDATE_TAGS = {"article", "main", "section", "div", "body", "td"}
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
POSITIVE_HINTS = ("article", "content", "main", "post", "body", "entry", "text")
NEGATIVE_HINTS = ("comment", "sidebar", "widget", "share", "related", "promo", "ad-")

DATE_META_KEYS = {
    "article:published_time", "og:published_time", "article:modified_time",
    "date", "dc.date", "dc.date.issued", "datepublished", "publish-date",
    "publication_date", "sailthru.date", "parsely-pub-date",
}

_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ["january", "february", "march", "april", "may", "june", "july",
         "august", "september", "october", "november", "december"]
    )
}
_ISO_RE = re.compile(r"\b(20\d{2}|19\d{2})-(\d{1,2})-(\d{1,2})")
_MDY_RE = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b")
_DMY_RE = re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b")


def parse_date(s: str) -> date | None:
    """Best-effort parse of a date string to a datetime.date (None on failure)."""
    if not s:
        return None
    s = s.strip()
    m = _ISO_RE.search(s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = _MDY_RE.search(s)
    if m:
        mon = _month_num(m.group(1))
        if mon:
            try:
                return date(int(m.group(3)), mon, int(m.group(2)))
            except ValueError:
                return None
    m = _DMY_RE.search(s)
    if m:
        mon = _month_num(m.group(2))
        if mon:
            try:
                return date(int(m.group(3)), mon, int(m.group(1)))
            except ValueError:
                return None
    return None


def _month_num(name: str) -> int | None:
    name = name.lower()
    for full, num in _MONTHS.items():
        if full.startswith(name[:3]) and (len(name) <= 3 or full.startswith(name)):
            return num
    return None


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    parent: "_Node | None" = None
    children: list = field(default_factory=list)  # _Node or str

    def own_and_descendant_text(self) -> str:
        parts: list[str] = []
        for c in self.children:
            if isinstance(c, str):
                parts.append(c)
            else:
                parts.append(c.own_and_descendant_text())
        return "".join(parts)

    def iter_nodes(self):
        yield self
        for c in self.children:
            if isinstance(c, _Node):
                yield from c.iter_nodes()

    def hint_string(self) -> str:
        return " ".join(
            (self.attrs.get("id", ""), self.attrs.get("class", ""))
        ).lower()


class _TreeBuilder(HTMLParser):
    """Tolerant HTML → tree parser that drops boilerplate subtrees early."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {})
        self.cur = self.root
        self.title = ""
        self.meta_dates: list[str] = []
        self.time_dates: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = {k: (v or "") for k, v in attrs}
        if tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").lower()
            if key in DATE_META_KEYS or key == "datepublished":
                if attrs.get("content"):
                    self.meta_dates.append(attrs["content"])
            return
        if tag == "time" and attrs.get("datetime"):
            self.time_dates.append(attrs["datetime"])
        if tag in VOID_TAGS:
            return
        if tag == "title":
            self._in_title = True
        if self._skip_depth or tag in SKIP_ENTIRELY:
            self._skip_depth += 1
            return
        node = _Node(tag, attrs, parent=self.cur)
        self.cur.children.append(node)
        self.cur = node

    def handle_endtag(self, tag):
        if tag in VOID_TAGS or tag == "meta":
            return
        if tag == "title":
            self._in_title = False
        if self._skip_depth:
            self._skip_depth -= 1
            return
        # pop up to the nearest matching open tag; ignore stray end tags
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS and tag != "meta":
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self._skip_depth or not data.strip():
            return
        self.cur.children.append(data)


@dataclass
class ExtractedPage:
    title: str
    text: str
    date: str  # ISO publication date if found, else ""
    blocks: list[str] = field(default_factory=list)


def _link_text_len(node: _Node) -> int:
    total = 0
    for n in node.iter_nodes():
        if n.tag == "a":
            total += len(n.own_and_descendant_text())
    return total


def _score(node: _Node) -> float:
    text = re.sub(r"\s+", " ", node.own_and_descendant_text()).strip()
    if len(text) < 80:
        return 0.0
    link_density = min(1.0, _link_text_len(node) / max(1, len(text)))
    score = len(text) * (1.0 - link_density) ** 2
    hints = node.hint_string()
    if node.tag in ("article", "main") or any(h in hints for h in POSITIVE_HINTS):
        score *= 1.5
    if any(h in hints for h in NEGATIVE_HINTS):
        score *= 0.3
    return score


def _blocks_of(container: _Node) -> list[str]:
    blocks: list[str] = []
    for n in container.iter_nodes():
        if n.tag not in BLOCK_TAGS:
            continue
        text = re.sub(r"\s+", " ", n.own_and_descendant_text()).strip()
        if not text:
            continue
        is_heading = n.tag.startswith("h")
        if not is_heading:
            if len(text) < 25:
                continue
            link_density = _link_text_len(n) / max(1, len(text))
            if link_density > 0.5:
                continue
        blocks.append(text)
    # de-duplicate while preserving order (nested block tags can repeat text)
    seen: set[str] = set()
    out = []
    for b in blocks:
        if b not in seen:
            seen.add(b)
            out.append(b)
    return out


def extract(html: str) -> ExtractedPage:
    """Extract title, main text blocks, and a best-effort publication date."""
    builder = _TreeBuilder()
    try:
        builder.feed(unescape_entities_safe(html))
        builder.close()
    except Exception:
        pass  # keep whatever was parsed before the error

    candidates = [
        n for n in builder.root.iter_nodes() if n.tag in CANDIDATE_TAGS
    ]
    best = max(candidates, key=_score, default=None)
    blocks = _blocks_of(best) if best is not None and _score(best) > 0 else []
    if not blocks:  # sparse page fallback: every block on the page
        blocks = _blocks_of(builder.root)
    text = "\n\n".join(blocks)

    title = re.sub(r"\s+", " ", builder.title).strip()
    if not title:
        for n in builder.root.iter_nodes():
            if n.tag == "h1":
                title = re.sub(r"\s+", " ", n.own_and_descendant_text()).strip()
                break

    pub = None
    for cand in builder.meta_dates + builder.time_dates:
        pub = parse_date(cand)
        if pub:
            break
    if not pub:  # visible date near the top of the article
        pub = parse_date(text[:600])
    return ExtractedPage(
        title=title, text=text, date=pub.isoformat() if pub else "", blocks=blocks
    )


def unescape_entities_safe(html: str) -> str:
    # html.parser handles entities with convert_charrefs; only normalize nbsp
    # here so density scores aren't skewed by entity soup in malformed pages.
    return html.replace("&nbsp;", " ")
