"""Live feature & release showcases synced in-place from GitHub files or releases."""
from __future__ import annotations

import asyncio
import hashlib
import html
from html.parser import HTMLParser
import json
import logging
import posixpath
import re
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from urllib.parse import quote, urlsplit

import discord

from .checks import UserError
from .embeds import BLUE, _COMMENT_RE, _ts, truncate
from .github import GitHubError
from .models import Showcase

if TYPE_CHECKING:
    from .main import TrackerBot

log = logging.getLogger(__name__)

MANUAL_REFRESH_COOLDOWN = 15
CHUNK_LIMIT = 3800
MAX_MESSAGES = 5
COLOR = BLUE

_ATX_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_INLINE_CODE_RE = re.compile(r"(?<!`)(`+)(?!`)((?:(?!\n\n).)+?)(?<!`)\1(?!`)")
_LINKED_IMAGE_RE = re.compile(
    r"\[!\[([^\]]*)\]\(([^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\)\]\(([^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\)"
)
_STANDALONE_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
_IMAGE_OR_IMG_TAG_RE = re.compile(
    r"<img\b([^>]*)>|!\[[^\]]*\]\(([^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\)",
    re.IGNORECASE | re.DOTALL,
)
_IMG_SRC_RE = re.compile(
    r"""\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))""",
    re.IGNORECASE,
)
_MD_REL_LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)\s]+)((?:\s+[\"'][^\"']*[\"'])?)\)")
_BR_SENTINEL = "\x00BR\x00"
_BLANK_LINES_RE = re.compile(r"\n{3,}")

_BADGE_HOSTS = frozenset({
    "img.shields.io",
    "shields.io",
    "badgen.net",
    "badge.fury.io",
    "codecov.io",
    "coveralls.io",
    "travis-ci.org",
    "travis-ci.com",
    "circleci.com",
    "deps.rs",
})
_EMBEDDABLE_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

_HTML_TAGS = frozenset({
    "a", "abbr", "address", "area", "article", "aside", "audio",
    "b", "base", "bdi", "bdo", "big", "blockquote", "body", "br", "button",
    "canvas", "caption", "center", "cite", "code", "col", "colgroup",
    "data", "datalist", "dd", "del", "details", "dfn", "dialog", "dir", "div", "dl", "dt",
    "em", "embed",
    "fieldset", "figcaption", "figure", "font", "footer", "form",
    "h1", "h2", "h3", "h4", "h5", "h6", "head", "header", "hgroup", "hr", "html",
    "i", "iframe", "img", "input", "ins",
    "kbd",
    "label", "legend", "li", "link",
    "main", "map", "mark", "marquee", "menu", "meta", "meter",
    "nav", "nobr", "noframes", "noscript",
    "object", "ol", "optgroup", "option", "output",
    "p", "param", "picture", "pre", "progress",
    "q",
    "rp", "rt", "ruby",
    "s", "samp", "script", "search", "section", "select", "small", "source",
    "span", "strike", "strong", "style", "sub", "summary", "sup", "svg",
    "table", "tbody", "td", "template", "textarea", "tfoot", "th", "thead", "time", "title", "tr", "track", "tt",
    "u", "ul",
    "var", "video",
    "wbr",
})
_VOID_TAGS = frozenset({
    "br", "hr", "img", "source", "input", "meta", "link",
    "area", "base", "col", "embed", "param", "track", "wbr",
})
_DROP_TAGS = frozenset({"style", "script", "noscript", "svg", "template"})
_BLOCK_PASSTHROUGH_TAGS = frozenset({
    "div", "details", "section", "article", "header", "footer", "main",
    "nav", "aside", "center", "figure", "figcaption", "dl", "dt", "dd",
})


def _protect_code(text: str) -> tuple[str, list[str], list[str]]:
    """Replace fenced code blocks and inline code spans with placeholders."""
    fences: list[str] = []
    out_lines: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    fence_buf: list[str] = []

    for line in text.splitlines(keepends=True):
        stripped = line.lstrip(" \t")
        if not in_fence:
            m = re.match(r"^(`{3,}|~{3,})", stripped)
            if m:
                marker = m.group(1)
                if marker[0] == "~" or "`" not in stripped[len(marker):]:
                    in_fence = True
                    fence_char = marker[0]
                    fence_len = len(marker)
                    fence_buf = [line]
                    continue
            out_lines.append(line)
        else:
            fence_buf.append(line)
            close_m = re.match(rf"^({re.escape(fence_char)}{{{fence_len},}})[ \t]*\r?\n?$", stripped)
            if close_m:
                idx = len(fences)
                fences.append("".join(fence_buf))
                trailing_nl = "\n" if fences[-1].endswith("\n") else ""
                fences[-1] = fences[-1].rstrip("\r\n")
                out_lines.append(f"\x00FENCE{idx}\x00{trailing_nl}")
                in_fence = False
                fence_buf = []

    if in_fence and fence_buf:
        idx = len(fences)
        fences.append("".join(fence_buf))
        out_lines.append(f"\x00FENCE{idx}\x00")

    protected = "".join(out_lines)
    inline: list[str] = []

    def _sub_inline(match: re.Match[str]) -> str:
        idx = len(inline)
        inline.append(match.group(0))
        return f"\x00CODE{idx}\x00"

    protected = _INLINE_CODE_RE.sub(_sub_inline, protected)
    return protected, fences, inline


def _restore_code(text: str, fences: list[str], inline: list[str]) -> str:
    """Restore placeholders created by ``_protect_code``."""
    for idx, snippet in enumerate(inline):
        text = text.replace(f"\x00CODE{idx}\x00", snippet)
    for idx, block in enumerate(fences):
        text = text.replace(f"\x00FENCE{idx}\x00", block)
    return text


def _resolve_repo_url(
    href: str,
    *,
    owner: str | None,
    name: str | None,
    ref: str | None,
    file_path: str | None,
    raw: bool = False,
) -> str | None:
    """Resolve relative GitHub paths/anchors against the repository source file."""
    href = (href or "").strip()
    if not href:
        return None
    if href.startswith(("http://", "https://", "mailto:")):
        return href
    if href.startswith("//"):
        return f"https:{href}"
    if not owner or not name:
        return None

    branch = quote(ref or "main", safe="/")
    base_file = (file_path or "").lstrip("/")

    if href.startswith("#"):
        if base_file:
            return f"https://github.com/{owner}/{name}/blob/{branch}/{quote(base_file, safe='/')}{href}"
        return f"https://github.com/{owner}/{name}{href}"

    parsed = urlsplit(href)
    if parsed.scheme:
        return None

    rel_path = parsed.path
    if rel_path.startswith("/"):
        joined = rel_path.lstrip("/")
    else:
        base_dir = posixpath.dirname(base_file) if base_file else ""
        joined = posixpath.join(base_dir, rel_path) if base_dir else rel_path

    norm = posixpath.normpath(joined)
    suffix = (f"?{parsed.query}" if parsed.query else "") + (f"#{parsed.fragment}" if parsed.fragment else "")
    if norm in ("", "."):
        if raw:
            return None
        return f"https://github.com/{owner}/{name}/tree/{branch}{suffix}"
    if norm == ".." or norm.startswith("../"):
        return None

    quoted_path = quote(norm, safe="/")
    if raw:
        return f"https://raw.githubusercontent.com/{owner}/{name}/{branch}/{quoted_path}"
    return f"https://github.com/{owner}/{name}/blob/{branch}/{quoted_path}{suffix}"


def _is_badge_url(url: str) -> bool:
    """Return True if URL looks like a README status badge or SVG icon."""
    lower = (url or "").strip().lower()
    if not lower:
        return False
    parsed = urlsplit(lower)
    host = parsed.hostname or ""
    path = parsed.path or ""
    if any(host == bh or host.endswith(f".{bh}") for bh in _BADGE_HOSTS):
        return True
    if host == "snyk.io" and "/test/" in path:
        return True
    if host == "goreportcard.com" and path.startswith("/badge"):
        return True
    if ("/actions/workflows/" in path or "/workflows/" in path) and path.endswith("/badge.svg"):
        return True
    if path.endswith(".svg"):
        return True
    return False


def _is_embeddable_image(url: str) -> bool:
    """Return True if URL points to a raster image Discord embeds can display."""
    lower = (url or "").strip().lower()
    if not lower.startswith(("http://", "https://")) or _is_badge_url(lower):
        return False
    parsed = urlsplit(lower)
    path = parsed.path or ""
    host = parsed.hostname or ""
    if path.endswith(_EMBEDDABLE_IMAGE_EXTS):
        return True
    if host == "user-images.githubusercontent.com":
        return True
    if "github.com/user-attachments/assets/" in lower:
        return True
    return False


def extract_hero_image(
    text: str | None,
    *,
    owner: str | None = None,
    name: str | None = None,
    ref: str | None = None,
    file_path: str | None = None,
) -> str | None:
    """Find the first non-badge raster image URL in markdown/HTML for Discord embed artwork."""
    if not text:
        return None
    protected, _, _ = _protect_code(text)
    stripped = _COMMENT_RE.sub("", protected)
    for match in _IMAGE_OR_IMG_TAG_RE.finditer(stripped):
        img_attrs = match.group(1)
        if img_attrs is not None:
            src_match = _IMG_SRC_RE.search(img_attrs)
            if not src_match:
                continue
            raw_url = html.unescape(src_match.group(1) or src_match.group(2) or src_match.group(3) or "")
        else:
            raw_url = (match.group(2) or "").strip("<>")
        resolved = _resolve_repo_url(
            raw_url, owner=owner, name=name, ref=ref, file_path=file_path, raw=True,
        )
        if resolved and _is_embeddable_image(resolved):
            return resolved
    return None


def _wrap_inline(inner: str, prefix: str, suffix: str) -> str:
    if not inner:
        return ""
    core = inner.strip()
    if not core:
        return " " if (" " in inner or "\n" in inner or "\t" in inner) else ""
    lead = inner[: len(inner) - len(inner.lstrip())]
    trail = inner[len(inner.rstrip()) :]
    return f"{lead}{prefix}{core}{suffix}{trail}"


class _HtmlToDiscordMarkdown(HTMLParser):
    """Convert real HTML elements in GitHub markdown/releases to Discord markdown.

    Unknown angle-bracket tags (Discord timestamps ``<t:...>``, mentions ``<@...>``,
    autolinks ``<https://...>``, generics ``Vec<String>``) are preserved verbatim.
    """

    def __init__(self, *, url_resolver: Callable[[str], str | None] | None = None):
        super().__init__(convert_charrefs=False)
        self._url_resolver = url_resolver
        self._stack: list[dict[str, Any]] = [{"tag": "__root__", "attrs": {}, "chunks": []}]
        self._drop_depth = 0

    def _emit(self, text: str) -> None:
        if text and self._drop_depth == 0:
            self._stack[-1]["chunks"].append(text)

    def _in_pre(self) -> bool:
        return any(frame["tag"] == "pre" for frame in self._stack)

    def _nearest(self, tags: tuple[str, ...]) -> dict[str, Any] | None:
        for frame in reversed(self._stack[1:]):
            if frame["tag"] in tags:
                return frame
        return None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t not in _HTML_TAGS:
            self._emit(self.get_starttag_text() or f"<{tag}>")
            return
        if t in _DROP_TAGS:
            self._drop_depth += 1
            return
        if self._drop_depth > 0:
            return
        if t in _VOID_TAGS:
            if t == "br":
                self._emit(_BR_SENTINEL)
            elif t == "hr":
                self._emit("\n\n")
            return

        if t == "li" and self._stack[-1]["tag"] == "li":
            self._pop_one()
        elif t == "p" and self._stack[-1]["tag"] == "p":
            self._pop_one()
        elif t in ("td", "th") and self._stack[-1]["tag"] in ("td", "th"):
            self._pop_one()
        elif t == "tr" and self._stack[-1]["tag"] == "tr":
            self._pop_one()

        attr_map = {k.lower(): (v or "") for k, v in attrs}
        frame: dict[str, Any] = {"tag": t, "attrs": attr_map, "chunks": []}
        if t == "ol":
            try:
                start = int(attr_map.get("start", "1") or "1")
            except ValueError:
                start = 1
            frame["count"] = start - 1
        elif t == "li":
            list_parent = self._nearest(("ol", "ul"))
            if list_parent and list_parent["tag"] == "ol":
                list_parent["count"] = list_parent.get("count", 0) + 1
                frame["prefix"] = f"{list_parent['count']}."
            else:
                frame["prefix"] = "-"
        elif t == "table":
            frame["rows"] = []
        elif t == "tr":
            frame["cells"] = []
        self._stack.append(frame)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t not in _HTML_TAGS:
            self._emit(self.get_starttag_text() or f"<{tag} />")
            return
        if self._drop_depth > 0 or t in _DROP_TAGS:
            return
        if t == "br":
            self._emit(_BR_SENTINEL)
        elif t == "hr":
            self._emit("\n\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t not in _HTML_TAGS:
            self._emit(f"</{tag}>")
            return
        if t in _DROP_TAGS:
            self._drop_depth = max(0, self._drop_depth - 1)
            return
        if self._drop_depth > 0 or t in _VOID_TAGS:
            return

        for idx in range(len(self._stack) - 1, 0, -1):
            if self._stack[idx]["tag"] == t:
                while len(self._stack) - 1 >= idx:
                    self._pop_one()
                return

    def handle_data(self, data: str) -> None:
        self._emit(data)

    def handle_entityref(self, name: str) -> None:
        if self._drop_depth == 0:
            self._emit(html.unescape(f"&{name};").replace("\xa0", " "))

    def handle_charref(self, name: str) -> None:
        if self._drop_depth == 0:
            self._emit(html.unescape(f"&#{name};").replace("\xa0", " "))

    def handle_comment(self, data: str) -> None:
        return

    def _pop_one(self) -> None:
        frame = self._stack.pop()
        rendered = self._render_frame(frame)
        if rendered:
            self._stack[-1]["chunks"].append(rendered)

    def _render_frame(self, frame: dict[str, Any]) -> str:
        t = frame["tag"]
        attrs = frame["attrs"]
        inner = "".join(frame["chunks"])

        if t in ("b", "strong"):
            return _wrap_inline(inner, "**", "**")
        if t in ("i", "em"):
            return _wrap_inline(inner, "*", "*")
        if t == "u":
            return _wrap_inline(inner, "__", "__")
        if t in ("s", "del", "strike"):
            return _wrap_inline(inner, "~~", "~~")
        if t in ("code", "kbd", "samp", "var"):
            if self._in_pre():
                return inner
            return _wrap_inline(inner, "`", "`")
        if t == "pre":
            body = inner.replace(_BR_SENTINEL, "\n").strip("\n")
            if not body.strip():
                return ""
            return f"\n```\n{body}\n```\n"
        if t in ("h1", "h2", "h3"):
            title = re.sub(r"[ \t\r\n]+", " ", inner.replace(_BR_SENTINEL, " ")).strip()
            return f"\n{'#' * int(t[1])} {title}\n\n" if title else ""
        if t in ("h4", "h5", "h6"):
            title = re.sub(r"[ \t\r\n]+", " ", inner.replace(_BR_SENTINEL, " ")).strip()
            return f"\n**{title}**\n\n" if title else ""
        if t == "summary":
            title = re.sub(r"[ \t\r\n]+", " ", inner.replace(_BR_SENTINEL, " ")).strip()
            return f"\n**{title}**\n" if title else ""
        if t == "a":
            if not inner:
                return ""
            core = inner.strip()
            if not core:
                return " " if (" " in inner or "\n" in inner or "\t" in inner) else ""
            lead = inner[: len(inner) - len(inner.lstrip())]
            trail = inner[len(inner.rstrip()) :]
            label = re.sub(r"[ \t\r\n]+", " ", core).strip()
            if not label:
                return ""
            href = attrs.get("href", "").strip()
            resolved = self._url_resolver(href) if (href and self._url_resolver) else href
            if resolved and resolved.startswith(("http://", "https://")):
                return f"{lead}[{label}]({resolved}){trail}"
            return f"{lead}{label}{trail}"
        if t == "p":
            collapsed = re.sub(r"[ \t]*\n[ \t]*", " ", inner)
            collapsed = re.sub(r" {2,}", " ", collapsed).strip()
            if not collapsed:
                return ""
            return f"\n\n{collapsed}\n\n"
        if t == "blockquote":
            body = inner.replace(_BR_SENTINEL, "\n").strip()
            if not body:
                return ""
            quoted = "\n".join(f"> {line}" if line.strip() else ">" for line in body.splitlines())
            return f"\n\n{quoted}\n\n"
        if t == "li":
            body = inner.strip()
            if not body:
                return ""
            depth = max(0, sum(1 for f in self._stack[1:] if f["tag"] in ("ul", "ol")) - 1)
            indent = "  " * depth
            return f"\n{indent}{frame.get('prefix', '-')} {body}"
        if t in ("ul", "ol"):
            body = inner.strip("\n")
            return f"\n{body}\n\n" if body.strip() else ""
        if t in ("td", "th"):
            cell = re.sub(r"[ \t\r\n]+", " ", inner.replace(_BR_SENTINEL, " ")).strip()
            tr_frame = self._nearest(("tr",))
            if tr_frame is not None:
                tr_frame["cells"].append((t == "th", cell))
                return ""
            return f" {cell} " if cell else ""
        if t == "tr":
            cells: list[tuple[bool, str]] = frame.get("cells", [])
            table_frame = self._nearest(("table",))
            if table_frame is not None:
                table_frame["rows"].append(cells)
                return ""
            non_empty = [c for _, c in cells if c]
            return f"\n- {' · '.join(non_empty)}" if non_empty else ""
        if t in ("thead", "tbody", "tfoot"):
            return inner
        if t == "table":
            rows = [row for row in frame.get("rows", []) if any(c for _, c in row)]
            if not rows:
                return ""
            has_header = len(rows) > 1 and all(is_h for is_h, _ in rows[0])
            lines: list[str] = []
            if has_header:
                headers = [c for _, c in rows[0]]
                for row in rows[1:]:
                    vals = [c for _, c in row]
                    if len(headers) == 2 and len(vals) >= 2:
                        c0, c1 = vals[0], vals[1]
                        if c0 and c1:
                            lines.append(f"- **{c0}:** {c1}")
                        elif c0 or c1:
                            lines.append(f"- {c0 or c1}")
                    else:
                        first = vals[0] if vals else ""
                        rest_parts = []
                        for idx in range(1, len(vals)):
                            if not vals[idx]:
                                continue
                            hdr = headers[idx] if idx < len(headers) else ""
                            rest_parts.append(f"{hdr}: {vals[idx]}" if hdr else vals[idx])
                        if first and rest_parts:
                            lines.append(f"- **{first}** — {' · '.join(rest_parts)}")
                        elif first:
                            lines.append(f"- {first}")
                        elif rest_parts:
                            lines.append(f"- {' · '.join(rest_parts)}")
            else:
                for row in rows:
                    non_empty = [c for _, c in row if c]
                    if non_empty:
                        lines.append(f"- {' · '.join(non_empty)}")
            return f"\n\n" + "\n".join(lines) + "\n\n" if lines else ""
        if t in _BLOCK_PASSTHROUGH_TAGS:
            body = inner.strip("\n")
            return f"\n{body}\n" if body.strip() else ""
        return inner

    def convert(self, text: str) -> str:
        self.feed(text)
        self.close()
        while len(self._stack) > 1:
            self._pop_one()
        return "".join(self._stack[0]["chunks"])


def clean_markdown(
    text: str | None,
    *,
    owner: str | None = None,
    name: str | None = None,
    ref: str | None = None,
    file_path: str | None = None,
) -> str:
    """Normalize GitHub markdown/HTML for clean display inside Discord embeds."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text, fences, inline = _protect_code(text)
    text = _COMMENT_RE.sub("", text)

    def _resolve(href: str) -> str | None:
        return _resolve_repo_url(href, owner=owner, name=name, ref=ref, file_path=file_path, raw=False)

    if "<" in text or "&" in text:
        text = _HtmlToDiscordMarkdown(url_resolver=_resolve).convert(text)

    text = re.sub(r"[ \t]*\x00BR\x00[ \t]*", "\n", text)

    def _sub_linked_image(match: re.Match[str]) -> str:
        alt, img_url, link_url = match.group(1).strip(), match.group(2), match.group(3)
        if _is_badge_url(img_url):
            return ""
        resolved = _resolve(link_url)
        if alt and resolved and resolved.startswith(("http://", "https://")):
            return f"[{alt}]({resolved})"
        return ""

    text = _LINKED_IMAGE_RE.sub(_sub_linked_image, text)
    text = _STANDALONE_IMAGE_RE.sub("", text)

    if owner and name:
        def _sub_rel_link(match: re.Match[str]) -> str:
            label, href, title = match.group(1), match.group(2), match.group(3)
            if href.startswith(("http://", "https://", "mailto:")):
                return match.group(0)
            resolved = _resolve(href)
            if resolved:
                return f"[{label}]({resolved}{title})"
            return label

        text = _MD_REL_LINK_RE.sub(_sub_rel_link, text)

    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = _BLANK_LINES_RE.sub("\n\n", text)
    text = _restore_code(text, fences, inline)
    return text.strip()


def _normalize_heading(raw: str) -> str:
    plain = _MD_LINK_RE.sub(r"\1", raw).strip()
    plain = plain.strip("*_`~ ")
    return plain


def _heading_matches(raw: str, targets: set[str]) -> bool:
    plain = _normalize_heading(raw).lower()
    if plain in targets:
        return True
    stripped = re.sub(r"^[^\w]+", "", plain).strip()
    return bool(stripped and stripped in targets)


def extract_section(markdown: str, headings: str | None) -> str | None:
    """Extract matching ATX heading section(s) from markdown.

    ``headings`` is a comma-separated list of heading titles (case-insensitive).
    Returns ``markdown`` unchanged when ``headings`` is None/blank, or ``None``
    when no requested heading appears.
    """
    if not headings or not headings.strip():
        return markdown
    targets = {_normalize_heading(h).lower() for h in headings.split(",") if h.strip()}
    if not targets:
        return markdown

    sections: list[str] = []
    capture_level: int | None = None
    buf: list[str] = []
    in_fence = False

    def _flush() -> None:
        nonlocal capture_level, buf
        if capture_level is not None:
            body = "\n".join(buf).strip()
            if body:
                sections.append(body)
        capture_level = None
        buf = []

    for line in markdown.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            if capture_level is not None:
                buf.append(line)
            continue
        if not in_fence:
            match = _ATX_RE.match(line)
            if match:
                level = len(match.group(1))
                title = match.group(2)
                if capture_level is not None and level <= capture_level:
                    _flush()
                if _heading_matches(title, targets):
                    if capture_level is None:
                        capture_level = level
                        buf = []
                        continue
                if capture_level is not None:
                    buf.append(line)
                continue
        if capture_level is not None:
            buf.append(line)

    _flush()
    return "\n\n".join(sections) if sections else None


def split_markdown_chunks(text: str, *, limit: int = CHUNK_LIMIT,
                          max_chunks: int = MAX_MESSAGES) -> tuple[list[str], bool]:
    """Split markdown cleanly into ``<= max_chunks`` chunks each ``<= limit`` chars."""
    text = text.strip()
    if not text:
        return [""], False

    units: list[str] = []
    for para in text.split("\n\n"):
        if len(para) <= limit:
            units.append(para)
        else:
            for line in para.splitlines():
                if len(line) <= limit:
                    units.append(line)
                else:
                    for i in range(0, len(line), limit):
                        units.append(line[i:i + limit])

    chunks: list[str] = []
    current = ""
    for unit in units:
        sep = "\n\n" if current else ""
        if len(current) + len(sep) + len(unit) <= limit:
            current += sep + unit
        else:
            if current:
                chunks.append(current)
            current = unit
    if current:
        chunks.append(current)

    truncated = len(chunks) > max_chunks
    if truncated:
        notice = "\n… *(truncated — view the full page on GitHub)*"
        last = chunks[max_chunks - 1]
        if len(last) + len(notice) > limit:
            last = last[:max(0, limit - len(notice) - 1)].rstrip()
        chunks = chunks[:max_chunks - 1] + [last + notice]
    return chunks, truncated


def _build_showcase_embeds(sc: Showcase, body: str, *, title: str, source_url: str,
                           footer: str, interval: int,
                           hero_image: str | None = None) -> tuple[list[discord.Embed], str]:
    chunks, truncated = split_markdown_chunks(body)
    if truncated:
        link_line = f"\n\n[Read the full source on GitHub →]({source_url})"
        if len(chunks[-1]) + len(link_line) <= 4096:
            chunks[-1] += link_line

    embeds: list[discord.Embed] = []
    for idx, chunk in enumerate(chunks):
        embed = discord.Embed(
            title=truncate(title, 256) if idx == 0 else None,
            url=source_url if idx == 0 else None,
            description=truncate(chunk, 4096),
            color=COLOR,
        )
        embeds.append(embed)
    if hero_image:
        embeds[0].set_image(url=hero_image)
    embeds[-1].set_footer(text=truncate(footer, 2048))

    basis = "\n".join([
        sc.source, title, source_url, str(interval), sc.file_path or "",
        sc.branch or "", sc.section or "", sc.release_kind, str(sc.max_releases),
        hero_image or "",
        *chunks,
    ])
    return embeds, hashlib.sha256(basis.encode("utf-8")).hexdigest()


def render_file_showcase(sc: Showcase, text: str, *, default_branch: str,
                         interval: int) -> tuple[list[discord.Embed], str]:
    """Render embeds for a repository markdown file showcase."""
    ref = sc.branch or default_branch
    hero_source = extract_section(text, sc.section) if sc.section else text
    hero_image = extract_hero_image(
        hero_source, owner=sc.owner, name=sc.name, ref=ref, file_path=sc.file_path,
    )
    cleaned = clean_markdown(
        text, owner=sc.owner, name=sc.name, ref=ref, file_path=sc.file_path,
    )
    if sc.section:
        extracted = extract_section(cleaned, sc.section)
        body = extracted if extracted else f"*(No `{sc.section}` section found in `{sc.file_path}`.)*"
    else:
        body = cleaned or f"*(`{sc.file_path}` is empty.)*"

    title = sc.title or f"✨ {sc.full_name} — {sc.file_path}"
    source_url = sc.source_url(default_branch)
    minutes = max(1, interval // 60)
    footer = f"Synced from {sc.file_path} · Updates every {minutes} min · Last update"
    return _build_showcase_embeds(sc, body, title=title, source_url=source_url,
                                  footer=footer, interval=interval, hero_image=hero_image)


def render_releases_showcase(sc: Showcase, releases: list[dict[str, Any]], *,
                             interval: int,
                             default_branch: str = "main") -> tuple[list[discord.Embed], str]:
    """Render embeds for a GitHub releases showcase."""
    filtered: list[dict[str, Any]] = []
    for rel in releases:
        if rel.get("draft"):
            continue
        is_pre = bool(rel.get("prerelease"))
        if sc.release_kind == "stable" and is_pre:
            continue
        if sc.release_kind == "prerelease" and not is_pre:
            continue
        filtered.append(rel)
    selected = filtered[:sc.max_releases]

    blocks: list[str] = []
    for rel in selected:
        tag = rel.get("tag_name") or ""
        name = (rel.get("name") or "").strip()
        heading_label = f"{tag} — {name}" if name and name != tag else (tag or name or "Release")
        rel_url = rel.get("html_url") or sc.source_url()
        pre_badge = " *(pre-release)*" if rel.get("prerelease") else ""
        ts = _ts(rel.get("published_at") or rel.get("created_at"))
        date_suffix = f" · <t:{int(ts.timestamp())}:d>" if ts else ""
        header = f"### [{truncate(heading_label, 140)}]({rel_url}){pre_badge}{date_suffix}"

        rel_ref = rel.get("target_commitish") or default_branch
        cleaned = clean_markdown(rel.get("body"), owner=sc.owner, name=sc.name, ref=rel_ref)
        if sc.section:
            sec = extract_section(cleaned, sc.section) if cleaned else None
            if not sec:
                continue
            blocks.append(f"{header}\n{sec}")
        else:
            blocks.append(f"{header}\n{cleaned or '*(No release notes provided.)*'}")

    if blocks:
        body = "\n\n".join(blocks)
    elif not selected:
        body = f"*(No published `{sc.release_kind}` releases found for **{sc.full_name}**.)*"
    else:
        body = f"*(None of the latest {len(selected)} release(s) had a `{sc.section}` section.)*"

    title = sc.title or f"🚀 {sc.full_name} — Releases"
    minutes = max(1, interval // 60)
    footer = f"Synced from GitHub Releases ({sc.release_kind}) · Updates every {minutes} min · Last update"
    return _build_showcase_embeds(sc, body, title=title, source_url=sc.source_url(),
                                  footer=footer, interval=interval)


class RefreshShowcaseButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"sc:refresh:(?P<id>\d+)",
):
    """Persistent refresh button on showcase boards."""

    def __init__(self, showcase_id: int):
        super().__init__(
            discord.ui.Button(
                label="Refresh",
                emoji="🔄",
                style=discord.ButtonStyle.secondary,
                custom_id=f"sc:refresh:{showcase_id}",
            )
        )
        self.showcase_id = showcase_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction,
                             item: discord.ui.Button, match: re.Match[str]):  # type: ignore[override]
        return cls(int(match.group("id")))

    async def callback(self, interaction: discord.Interaction) -> None:
        manager = getattr(interaction.client, "showcases", None)
        if manager is None:
            raise UserError("Showcases aren't initialized yet.")
        await manager.handle_refresh(interaction, self.showcase_id)


def showcase_view(sc: Showcase, *, default_branch: str = "main") -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(RefreshShowcaseButton(sc.id))
    view.add_item(discord.ui.Button(
        label="View on GitHub",
        style=discord.ButtonStyle.link,
        url=sc.source_url(default_branch),
    ))
    return view


class ShowcaseManager:
    """Fetches, renders, and keeps channel showcase messages edited in place."""

    def __init__(self, bot: TrackerBot):
        self.bot = bot
        self._lock = asyncio.Lock()
        self._cache: dict[str, tuple[str | None, Any]] = {}
        self._manual: dict[int, float] = {}

    async def _cached(self, key: str,
                      fetcher: Callable[[str | None], Awaitable[tuple[Any, str | None]]],
                      *, expected_type: type | None = None) -> Any:
        etag, data = self._cache.get(key, (None, None))
        if etag is None and hasattr(self.bot.db, "get_http_cache"):
            row = await self.bot.db.get_http_cache(key)
            if row is not None:
                etag, data = row
                if expected_type is not None and not isinstance(data, expected_type):
                    etag, data = None, None
                elif (
                    expected_type is str
                    and isinstance(data, str)
                    and len(data) >= 2
                    and data[0] == '"'
                    and data[-1] == '"'
                ):
                    try:
                        decoded = json.loads(data)
                    except ValueError:
                        decoded = None
                    if isinstance(decoded, str):
                        etag, data = None, None
                if etag is not None:
                    self._cache[key] = (etag, data)
        fresh, new_etag = await fetcher(etag)
        if fresh is not None:
            data = fresh
            self._cache[key] = (new_etag, data)
            if new_etag and hasattr(self.bot.db, "set_http_cache"):
                await self.bot.db.set_http_cache(key, new_etag, data)
        return data

    async def _record_error(self, sc: Showcase, err: str) -> None:
        await self.bot.db.set_showcase_error(sc.id, err)
        sc.last_error = err
        if hasattr(self.bot, "send_alert"):
            await self.bot.send_alert(
                sc.guild_id, f"sc:{sc.id}", f"Showcase #{sc.id} error",
                f"**{sc.describe()}** in <#{sc.channel_id}>\n{err}",
            )

    async def refresh_all(self) -> None:
        active = self.bot._active_guild_ids() if hasattr(self.bot, "_active_guild_ids") else None
        for sc in await self.bot.db.list_showcases():
            if active is not None and sc.guild_id not in active:
                continue
            await self.refresh(sc)

    async def refresh(self, sc: Showcase, *, force: bool = False) -> None:
        """Re-render one showcase, updating or reconciling its channel messages."""
        async with self._lock:
            db = self.bot.db
            repo = await db.get_repo(sc.guild_id, sc.owner, sc.name)
            default_branch = repo.default_branch if repo else "main"
            interval = getattr(getattr(self.bot, "poller", None), "interval", 300)

            try:
                if sc.source == "releases":
                    per_page = max(30, min(100, sc.max_releases * 2 if sc.release_kind != "all" else sc.max_releases))
                    cache_key = f"sc:releases:{sc.full_name.lower()}:{per_page}"
                    releases = await self._cached(
                        cache_key,
                        lambda e: self.bot.github.list_releases(sc.owner, sc.name, per_page=per_page, etag=e),
                        expected_type=list,
                    )
                    embeds, digest = render_releases_showcase(
                        sc, releases or [], interval=interval, default_branch=default_branch,
                    )
                else:
                    ref = sc.branch or default_branch
                    path = sc.file_path or "README.md"
                    cache_key = f"sc:file:{sc.full_name.lower()}:{ref}:{path}"
                    text = await self._cached(
                        cache_key,
                        lambda e: self.bot.github.get_file_content(sc.owner, sc.name, path, ref=ref, etag=e),
                        expected_type=str,
                    )
                    embeds, digest = render_file_showcase(
                        sc, text or "", default_branch=default_branch, interval=interval,
                    )
            except GitHubError as exc:
                await self._record_error(sc, f"GitHub: {exc}")
                return

            changed = digest != sc.last_hash
            last_change = time.time() if changed or sc.last_change is None else sc.last_change
            embeds[-1].timestamp = datetime.fromtimestamp(last_change, tz=timezone.utc)
            if sc.message_ids and not changed and not force and not sc.last_error:
                return

            try:
                channel = self.bot.get_channel(sc.channel_id) or await self.bot.fetch_channel(sc.channel_id)
            except (discord.NotFound, discord.Forbidden):
                await self._record_error(sc, "Channel is gone or I can't see it")
                return
            if not isinstance(channel, discord.TextChannel):
                await self._record_error(sc, "Channel is not a text channel")
                return

            view = showcase_view(sc, default_branch=default_branch)
            old_ids = sc.message_ids
            new_ids: list[int] = []
            need_pin = sc.pinned and not old_ids
            try:
                rebuilding = False
                for idx, embed in enumerate(embeds):
                    msg_view = view if idx == len(embeds) - 1 else None
                    if not rebuilding and idx < len(old_ids):
                        try:
                            await channel.get_partial_message(old_ids[idx]).edit(embed=embed, view=msg_view)
                            new_ids.append(old_ids[idx])
                            continue
                        except discord.NotFound:
                            rebuilding = True
                            # Delete remaining stale chunks so recreated messages stay in reading order.
                            for stale_id in old_ids[idx + 1:]:
                                try:
                                    await channel.get_partial_message(stale_id).delete()
                                except discord.HTTPException:
                                    pass
                            need_pin = sc.pinned and idx == 0
                    msg = await channel.send(embed=embed, view=msg_view)
                    new_ids.append(msg.id)
                    if idx == 0 and sc.pinned:
                        need_pin = True
                        try:
                            await msg.pin(reason="Showcase")
                            need_pin = False
                        except discord.HTTPException as exc:
                            log.warning("Couldn't pin showcase %s: %s", sc.id, exc)

                if not rebuilding and len(old_ids) > len(embeds):
                    for extra_id in old_ids[len(embeds):]:
                        try:
                            await channel.get_partial_message(extra_id).delete()
                        except discord.HTTPException:
                            pass

                if need_pin and new_ids:
                    try:
                        await channel.get_partial_message(new_ids[0]).pin(reason="Showcase")
                    except discord.HTTPException as exc:
                        log.warning("Couldn't pin showcase %s: %s", sc.id, exc)
            except discord.HTTPException as exc:
                await self._record_error(sc, f"Discord: {exc.text or exc}")
                return

            if new_ids != old_ids:
                await db.set_showcase_messages(sc.id, new_ids)
                sc.message_ids_json = json.dumps(new_ids)
            await db.set_showcase_state(sc.id, last_hash=digest, last_change=last_change, error=None)
            sc.last_hash, sc.last_change, sc.last_error = digest, last_change, None

    async def delete_messages(self, sc: Showcase) -> int:
        """Delete existing Discord showcase messages best-effort. Returns number deleted."""
        if not sc.message_ids:
            return 0
        channel = self.bot.get_channel(sc.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(sc.channel_id)
            except discord.HTTPException:
                return 0
        if not isinstance(channel, discord.TextChannel):
            return 0
        deleted = 0
        for mid in sc.message_ids:
            try:
                await channel.get_partial_message(mid).delete()
                deleted += 1
            except discord.HTTPException:
                pass
        return deleted

    async def message_deleted(self, message_id: int) -> None:
        """Reconcile a showcase if one of its Discord messages is deleted."""
        sc = await self.bot.db.find_showcase_by_message(message_id)
        if sc is None:
            return
        remaining = [mid for mid in sc.message_ids if mid != message_id]
        await self.bot.db.set_showcase_messages(sc.id, remaining)
        sc.message_ids_json = json.dumps(remaining) if remaining else None
        log.info("Showcase %s message %s deleted; reconciling", sc.id, message_id)
        await self.refresh(sc, force=True)

    async def handle_refresh(self, interaction: discord.Interaction, showcase_id: int) -> None:
        sc = await self.bot.db.get_showcase(showcase_id, interaction.guild_id)
        if sc is None:
            raise UserError("This showcase no longer exists.")
        last = self._manual.get(sc.id)
        if last is not None and time.monotonic() - last < MANUAL_REFRESH_COOLDOWN:
            await interaction.response.send_message(
                "It was just refreshed. Try again in a few seconds.", ephemeral=True,
            )
            return
        self._manual[sc.id] = time.monotonic()
        await interaction.response.defer()
        await self.refresh(sc, force=True)
        refreshed = await self.bot.db.get_showcase(sc.id)
        if refreshed and refreshed.last_error:
            await interaction.followup.send(f"❌ Couldn't refresh: {refreshed.last_error}", ephemeral=True)
