# -*- coding: utf-8 -*-
"""Untrusted-content boundary for the synced HTML viewer (export_web.py).

Everything a note carries into the page is DATA, not markup we wrote: the
transcript is ASR of whatever the speaker said, slide text is OCR of whatever
was on screen, and the Stage F synthesis is an LLM's paraphrase of both. A
lecture on web security that says "<script>" out loud, a slide showing an
<iframe> snippet, or a hostile file someone hands you must all reach the reader
as visible text — never as live HTML in a page that can play local media.

Four independent layers, each enough on its own to stop the obvious attack:

  sanitize_fragment(html)   allowlist re-serialisation of pandoc's output.
                            Unknown/dangerous tags are ESCAPED (shown as text,
                            not silently dropped — a transcript that mentions a
                            tag keeps every word); event handlers, `style` beyond
                            a few layout properties, and non-allowlisted URLs
                            are removed; unbalanced tags cannot leak out of the
                            note's own <section>.
  json_for_script(obj)      JSON that cannot terminate the <script> element it is
                            embedded in (`</script>`, `<!--`, U+2028/9).
  is_safe_relative_url(u)   media/image URLs must stay relative to the page — no
                            scheme, no host, no absolute path.
  csp_meta(script_text)     a restrictive Content-Security-Policy: no network
                            fetches at all, and the ONE inline script pinned by
                            its SHA-256, so anything that slips past the first
                            three layers still cannot execute.

Standard library only; no third-party sanitizer to install or keep patched.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from html.parser import HTMLParser
from urllib.parse import unquote

# ---------------------------------------------------------------------------
# URL allowlist
# ---------------------------------------------------------------------------
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")
# Browsers strip these before parsing a URL, so "java\tscript:" is "javascript:".
_URL_STRIP = re.compile(r"[\x00-\x20\x7f]")
LINK_SCHEMES = {"http", "https", "mailto"}


def _url_core(u: str) -> str:
    return _URL_STRIP.sub("", html.unescape(u or ""))


def is_safe_relative_url(u, allow_parent: bool = False) -> bool:
    """True when `u` is a relative path that stays on the page's own origin.

    Rejects any scheme (javascript:, data:, http:, file:, C:), protocol-relative
    `//host`, absolute `/path`, backslashes (browsers read `\\` as `/`), and —
    unless `allow_parent` — any `..` segment, percent-encoded or not.
    """
    if not isinstance(u, str) or not u.strip():
        return False
    core = _url_core(u)
    if not core or "\\" in core or core.startswith("/"):
        return False
    if _SCHEME.match(core):
        return False
    decoded = unquote(core)
    if "\\" in decoded or decoded.startswith("/") or _SCHEME.match(decoded):
        return False
    if not allow_parent:
        path = decoded.split("?", 1)[0].split("#", 1)[0]
        if any(seg == ".." for seg in path.split("/")):
            return False
    return True


def is_safe_link(u) -> bool:
    """href allowlist for <a>: in-page #fragment, http(s), mailto, or relative."""
    if not isinstance(u, str):
        return False
    core = _url_core(u)
    if not core:
        return False
    if core.startswith("#"):
        return True
    m = _SCHEME.match(core)
    if m:
        return m.group(1).lower() in LINK_SCHEMES
    return is_safe_relative_url(u, allow_parent=True)


# ---------------------------------------------------------------------------
# HTML fragment sanitizer
# ---------------------------------------------------------------------------
VOID_TAGS = {"br", "hr", "img", "col", "wbr", "input"}

ALLOWED_TAGS = {
    # text structure (everything pandoc's HTML writer emits for our notes)
    "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre",
    "ul", "ol", "li", "dl", "dt", "dd", "div", "span", "section", "figure",
    "figcaption", "details", "summary",
    # inline
    "a", "em", "strong", "b", "i", "u", "s", "del", "ins", "sub", "sup", "mark",
    "code", "kbd", "samp", "var", "small", "abbr", "cite", "q", "dfn", "wbr",
    # media: images only (video lives in the viewer's own <video>, not in notes)
    "img",
    # tables
    "table", "caption", "colgroup", "col", "thead", "tbody", "tfoot", "tr",
    "th", "td",
    # pandoc task lists: `- [ ]` -> <input type="checkbox" disabled="">
    "input",
}

GLOBAL_ATTRS = {"id", "class", "title", "lang", "dir", "role", "aria-hidden",
                "aria-label", "tabindex"}

TAG_ATTRS = {
    # data-* here are exactly the hooks export_web.preprocess emits for the viewer
    "a": {"href", "data-file", "data-t", "data-time-source", "data-nav"},
    "img": {"src", "alt", "width", "height", "loading"},
    "ol": {"start", "type", "reversed"},
    "li": {"value"},
    "td": {"colspan", "rowspan", "style", "align"},
    "th": {"colspan", "rowspan", "style", "align", "scope"},
    "col": {"span", "style", "width"},
    "colgroup": {"span"},
    "input": {"type", "checked", "disabled"},
    "span": {"style", "data-cites"}, "div": {"style"}, "p": {"style"}, "tr": {"style"},
    "table": {"style"}, "details": {"open"},
}

# ids the viewer's own page uses — a note heading must not shadow them
# (document.getElementById / querySelector return the first match).
RESERVED_IDS = {
    "player", "now", "home", "search-input", "search-results", "copy-link",
    "font-dec", "font-inc", "home-btn", "drawer-toggle", "video-pane",
    "float-bar", "drag-handle", "video-min-toggle", "toggle-scroll",
    "video-float-toggle", "col-resizer", "segment-title", "summary-pane",
    "summary-empty", "split-resizer", "replay-pane", "replay-empty",
    "segment-drawer",
}
_SECTION_ID = re.compile(r"^s\d+(?:-l[23])?$")

_DIGITS = re.compile(r"^\d{1,6}$")
_CLASS_TOKEN = re.compile(r"^[A-Za-z0-9_\-]+$")
_STYLE_DECL = {
    "text-align": re.compile(r"^(left|right|center|justify)$"),
    "vertical-align": re.compile(r"^(top|middle|bottom|baseline)$"),
    "width": re.compile(r"^\d{1,4}(\.\d{1,3})?(%|px|em|rem)$"),
    "font-weight": re.compile(r"^(normal|bold|[1-9]00)$"),
    "font-style": re.compile(r"^(normal|italic)$"),
    "color": re.compile(r"^(#[0-9A-Fa-f]{3,8}|[A-Za-z]{3,20}|rgba?\([\d\s.,%]{5,40}\))$"),
    "background-color": re.compile(r"^(#[0-9A-Fa-f]{3,8}|[A-Za-z]{3,20}|rgba?\([\d\s.,%]{5,40}\))$"),
}

_ATTR_RULES = {
    "data-t": _DIGITS.match,
    "data-time-source": re.compile(r"^(approx|precise|inherited)$").match,
    "data-nav": re.compile(r"^s\d+-l[23]$").match,
    "width": re.compile(r"^\d{1,5}%?$").match,
    "height": re.compile(r"^\d{1,5}%?$").match,
    "loading": re.compile(r"^(lazy|eager)$").match,
    "start": re.compile(r"^-?\d{1,6}$").match,
    "value": re.compile(r"^-?\d{1,6}$").match,
    "span": _DIGITS.match, "colspan": _DIGITS.match, "rowspan": _DIGITS.match,
    "align": re.compile(r"^(left|right|center|justify)$").match,
    "scope": re.compile(r"^(row|col|rowgroup|colgroup)$").match,
    "dir": re.compile(r"^(ltr|rtl|auto)$").match,
    "lang": re.compile(r"^[A-Za-z]{1,8}(-[A-Za-z0-9]{1,8})*$").match,
    "role": re.compile(r"^[a-z\-]{1,40}$").match,
    "aria-hidden": re.compile(r"^(true|false)$").match,
    "tabindex": re.compile(r"^(-1|0)$").match,
}
_FLAG_ATTRS = {"checked", "disabled", "reversed", "open"}


def _clean_style(v: str):
    kept = []
    for decl in v.split(";"):
        if not decl.strip():
            continue
        prop, sep, val = decl.partition(":")
        prop, val = prop.strip().lower(), val.strip()
        rule = _STYLE_DECL.get(prop)
        if sep and rule and rule.match(val):
            kept.append(f"{prop}: {val}")
    return "; ".join(kept) or None


def _clean_attr(tag: str, name: str, value):
    """-> the value to keep (str), or None to drop the attribute."""
    if name in _FLAG_ATTRS:
        return ""
    if value is None:
        value = ""
    if name == "id":
        if not value or value in RESERVED_IDS or _SECTION_ID.match(value) \
                or any(c.isspace() for c in value):
            return None
        return value
    if name == "class":
        toks = [t for t in value.split() if _CLASS_TOKEN.match(t)]
        return " ".join(toks) or None
    if name == "href":
        return value if is_safe_link(value) else None
    if name == "src":
        return value if is_safe_relative_url(value) else None
    if name == "style":
        return _clean_style(value)
    if name == "type":
        if tag == "input":
            return "checkbox" if value.lower() == "checkbox" else None
        return value if value in {"1", "a", "A", "i", "I"} else None
    rule = _ATTR_RULES.get(name)
    if rule is not None:
        return value if rule(value) else None
    return value          # free text: title, alt, aria-label, data-file


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []
        self.escaped: list[str] = []      # tag names shown as text (for reporting)

    # Never switch into raw-text mode: <script>/<style> content is parsed (and
    # escaped) like any other text, so an unclosed <script> in a transcript
    # cannot swallow the rest of the note.
    def set_cdata_mode(self, *args, **kwargs):   # noqa: D401 - HTMLParser hook
        return None

    def _emit_tag(self, tag, attrs, self_closing):
        kept = []
        allowed = GLOBAL_ATTRS | TAG_ATTRS.get(tag, set())
        for name, value in attrs:
            name = name.lower()
            if name not in allowed:
                continue
            v = _clean_attr(tag, name, value)
            if v is None:
                continue
            kept.append(f' {name}="{html.escape(v, quote=True)}"')
        if tag == "img" and not any(k.startswith(" src=") for k in kept):
            return False        # an image with no allowed source is not worth a box
        if tag == "input" and ' type="checkbox"' not in kept:
            return False
        if tag == "input" and ' disabled=""' not in kept:
            kept.append(' disabled=""')   # read-only page: never an interactive control
        self.out.append(f"<{tag}{''.join(kept)}{' /' if self_closing else ''}>")
        return True

    def _escape_raw(self, tag):
        raw = self.get_starttag_text() or f"<{tag}>"
        self.out.append(html.escape(raw, quote=False))
        self.escaped.append(tag)

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag not in ALLOWED_TAGS:
            self._escape_raw(tag)
            return
        if self._emit_tag(tag, attrs, False) and tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if tag not in ALLOWED_TAGS:
            self._escape_raw(tag)
            return
        if tag in VOID_TAGS:
            self._emit_tag(tag, attrs, True)
        elif self._emit_tag(tag, attrs, False):
            self.out.append(f"</{tag}>")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag not in ALLOWED_TAGS:
            self.out.append(html.escape(f"</{tag}>", quote=False))
            self.escaped.append(f"/{tag}")
            return
        if tag in VOID_TAGS or tag not in self.stack:
            return              # stray close: would otherwise close the viewer's own elements
        while self.stack:
            t = self.stack.pop()
            self.out.append(f"</{t}>")
            if t == tag:
                break

    def handle_data(self, data):
        self.out.append(html.escape(data, quote=False))

    # comments, <!doctype>, <?pi?>, <![CDATA[ ]]>: invisible in a browser anyway; drop.
    def handle_comment(self, data):
        pass

    def handle_decl(self, decl):
        pass

    def handle_pi(self, data):
        pass

    def unknown_decl(self, data):
        pass

    def finish(self) -> str:
        self.close()
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


def sanitize_fragment(fragment: str, report: list | None = None) -> str:
    """Allowlist re-serialisation of an HTML fragment (pandoc output).

    Allowed tags keep allowed attributes in their original order, quoted with
    `"` — export_web's later regex passes (`<li>`, `<a class="ts" data-file=…
    data-t=…>`) rely on that shape. If `report` is a list, the names of tags
    that were escaped to text are appended to it.
    """
    p = _Sanitizer()
    p.feed(fragment or "")
    out = p.finish()
    if report is not None:
        report.extend(p.escaped)
    return out


# ---------------------------------------------------------------------------
# <script> embedding + CSP
# ---------------------------------------------------------------------------
def json_for_script(obj) -> str:
    """json.dumps that is safe inside an inline <script> element.

    `<`, `>` and `&` only ever occur inside JSON strings, where `\\u003c` etc.
    decode to the same characters — so the value is unchanged but the text can
    never contain `</script>` or `<!--`. U+2028/2029 are escaped for engines
    that predate ES2019.
    """
    s = json.dumps(obj, ensure_ascii=False)
    return (s.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
             .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def script_hash(script_text: str) -> str:
    """CSP source expression for one inline script.

    Browsers hash the element's text after HTML newline normalisation (CRLF and
    lone CR become LF), so hash that form — the file may be written with CRLF.
    """
    norm = script_text.replace("\r\n", "\n").replace("\r", "\n")
    digest = hashlib.sha256(norm.encode("utf-8")).digest()
    return "'sha256-" + base64.b64encode(digest).decode("ascii") + "'"


def csp_policy(*script_texts: str) -> str:
    """Offline-viewer policy: nothing from the network, nothing that runs
    except the exact inline script(s) this exporter wrote.

    - default-src 'none'       no fetch/XHR/websocket, fonts, frames, objects
    - script-src <hash>        only our inline script; no eval-from-markup,
                               no injected <script>, no inline event handlers
    - style-src 'unsafe-inline' the viewer's <style> plus the few layout styles
                               the sanitizer lets through (no url() survives it)
    - img-src / media-src      the page's own folder: 'self' for http(s);
                               `file:` because a file:// page has an opaque
                               origin in some browsers. Never a remote host.
    - base-uri / form-action   'none': a note cannot re-point relative URLs or
                               submit anything anywhere
    """
    hashes = " ".join(script_hash(t) for t in script_texts)
    return ("default-src 'none'; "
            f"script-src {hashes}; "
            "style-src 'unsafe-inline'; "
            "img-src 'self' file:; "
            "media-src 'self' file:; "
            "base-uri 'none'; "
            "form-action 'none'")


def csp_meta(*script_texts: str) -> str:
    return ('<meta http-equiv="Content-Security-Policy" content="'
            + html.escape(csp_policy(*script_texts), quote=True) + '">')
