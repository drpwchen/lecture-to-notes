"""Unit tests for scripts/_html_safety.py — the viewer's untrusted-content boundary.

Every payload here is something a transcript, a slide's OCR text or an LLM
paraphrase could plausibly contain. The rule under test: it is shown as text,
never executed, and never makes the page fetch anything.
"""
import base64
import hashlib
import json
from html.parser import HTMLParser

import pytest

from _html_safety import (csp_meta, csp_policy, is_safe_link, is_safe_relative_url,
                          json_for_script, sanitize_fragment, script_hash)


class _Tags(HTMLParser):
    """Collect (tag, attrs) for every element in a fragment."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    handle_startendtag = handle_starttag


def elements(fragment):
    p = _Tags()
    p.feed(fragment)
    p.close()
    return p.tags


def assert_inert(fragment):
    """No executable or fetching markup survives."""
    for tag, attrs in elements(fragment):
        assert tag not in {"script", "style", "iframe", "object", "embed", "svg",
                           "math", "form", "base", "meta", "link", "noscript",
                           "textarea", "frame", "frameset"}, tag
        for name, value in attrs.items():
            assert not name.startswith("on"), (tag, name)
            if name == "href":
                assert is_safe_link(value), value
            if name == "src":
                assert is_safe_relative_url(value), value
            if name == "style":
                assert "url(" not in (value or "").lower(), value


# --------------------------------------------------------------------------
# sanitize_fragment: hostile input
# --------------------------------------------------------------------------
HOSTILE = [
    "<script>alert(1)</script>",
    "<SCRIPT SRC=//evil.example/x.js></SCRIPT>",
    '<img src=x onerror="alert(1)">',
    "<img src=x onerror=alert(1)//",
    '<svg onload="alert(1)"><circle r="1"/></svg>',
    '<iframe src="javascript:alert(1)"></iframe>',
    '<object data="https://evil.example/x.swf"></object>',
    '<embed src="https://evil.example/y">',
    '<form action="https://evil.example"><input name="q"><button>go</button></form>',
    '<base href="https://evil.example/">',
    '<meta http-equiv="refresh" content="0;url=https://evil.example">',
    '<link rel="stylesheet" href="https://evil.example/a.css">',
    "<style>body{background:url(https://tracker.example/c)}</style>",
    '<span style="background:url(https://tracker.example/y)">x</span>',
    '<a href="javascript:alert(1)">x</a>',
    '<a href="  JaVaScRiPt:alert(1)">x</a>',
    '<a href="jav&#x61;script:alert(1)">x</a>',
    '<a href="java\tscript:alert(1)">x</a>',
    '<a href="vbscript:msgbox(1)">x</a>',
    '<a href="data:text/html,<script>alert(1)</script>">x</a>',
    '<img src="data:image/svg+xml,%3Csvg onload=alert(1)%3E">',
    '<img src="https://tracker.example/pixel.gif">',
    '<img src="//tracker.example/pixel.gif">',
    '<img src="/etc/passwd">',
    '<img src="C:/Windows/win.ini">',
    '<img src="..\\..\\secret.png">',
    '<noscript><p title="</noscript><img src=x onerror=alert(1)>"></noscript>',
    "<textarea></textarea><img src=x onerror=alert(1)>",
    '<math><mi xlink:href="javascript:alert(1)">x</mi></math>',
    '<b onclick="alert(1)" onmouseover="alert(2)">bold</b>',
    "<!-- <script>alert(1)</script> -->",
    "<![CDATA[<script>alert(1)</script>]]>",
    '<details open ontoggle="alert(1)"><summary>s</summary>x</details>',
    '<input type="text" autofocus onfocus="alert(1)">',
    '<a href="#x" style="position:fixed;top:0;left:0;width:100%;height:100%">overlay</a>',
]


@pytest.mark.parametrize("payload", HOSTILE)
def test_hostile_payload_is_inert(payload):
    assert_inert(sanitize_fragment(payload))


def test_script_shown_as_text_not_dropped():
    # A lecture on web security says "<script>" out loud; the reader must see it.
    out = sanitize_fragment("<p>講者說 <script>alert(1)</script> 這樣</p>")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out
    assert "講者說" in out and "這樣" in out


def test_unclosed_script_does_not_swallow_the_rest():
    frag = "<ul><li>這個 <script> 沒關</li><li>第二句</li><li>第三句</li></ul>"
    out = sanitize_fragment(frag)
    assert out.count("<li>") == 3
    assert "第三句" in out


def test_stray_closing_tags_cannot_escape_the_fragment():
    out = sanitize_fragment("<p>a</p></li></ul></section></main><p>b</p>")
    # allowlisted closers with nothing open are dropped; an unknown one (<main>
    # is the viewer's, never a note's) is shown as text like any unknown tag
    assert out == "<p>a</p>&lt;/main&gt;<p>b</p>"


def test_unbalanced_open_tags_are_closed():
    out = sanitize_fragment("<table><tr><td>x")
    assert out == "<table><tr><td>x</td></tr></table>"


def test_reserved_and_section_ids_dropped():
    out = sanitize_fragment('<div id="player">x</div><h2 id="s3-l2">y</h2><h2 id="ok-id">z</h2>')
    assert 'id="player"' not in out
    assert 'id="s3-l2"' not in out
    assert 'id="ok-id"' in out


def test_mailto_http_fragment_links_kept():
    for href in ("https://example.org/a?b=1", "http://example.org", "mailto:a@b.c",
                 "#footnote-1", "other-note.html"):
        assert f'href="{href}"' in sanitize_fragment(f'<a href="{href}">x</a>')


def test_style_allowlist_keeps_layout_only():
    out = sanitize_fragment('<td style="text-align: center; position: fixed">x</td>')
    assert 'style="text-align: center"' in out
    assert "position" not in out
    out = sanitize_fragment('<span style="color: red">x</span>')
    assert 'style="color: red"' in out


def test_task_list_checkbox_kept_read_only():
    out = sanitize_fragment('<li><input type="checkbox" checked="" />done</li>')
    assert '<input type="checkbox" checked="" disabled="" />' in out
    assert "<input" not in sanitize_fragment('<input type="text" value="x">')


# --------------------------------------------------------------------------
# sanitize_fragment: what export_web emits must round-trip unchanged
# --------------------------------------------------------------------------
LEGIT = [
    '<a class="ts" data-file="00004.MTS" data-t="263">▶ V1 04:23</a>',
    '<a class="ts" data-file="a &amp; b.mp4" data-t="5" data-time-source="approx" '
    'title="時間為約略值（agent 推估）">▶ V1 ~00:05</a>',
    '<a class="nav" data-nav="s3-l2">逐字稿</a>',
    '<img class="fig" src="%E5%BD%B1/01_C%23_01.jpg" loading="lazy">',
    "<ul>\n<li>第一點 <mark>重點</mark></li>\n<li>第二點</li>\n</ul>",
    '<h2 id="重點整理">重點整理</h2>',
    "<blockquote>\n<p><strong>【NOTE】 標題</strong></p>\n</blockquote>",
    '<table>\n<thead>\n<tr>\n<th style="text-align: left;">a</th>\n</tr>\n</thead>\n</table>',
    '<p>x<sup>1</sup> y<sub>2</sub><br />\nz</p>',
    '<a href="#fn1" class="footnote-ref" id="fnref1" role="doc-noteref"><sup>1</sup></a>',
    '<pre class="sourceCode python"><code class="sourceCode python">x = 1</code></pre>',
    '<details open=""><summary>s</summary>x</details>',
]


@pytest.mark.parametrize("frag", LEGIT)
def test_exporter_markup_survives(frag):
    out = sanitize_fragment(frag)
    # same elements, same attributes (style may be normalised)
    a, b = elements(frag), elements(out)
    assert [t for t, _ in a] == [t for t, _ in b]
    for (_, x), (_, y) in zip(a, b):
        x = {k: v for k, v in x.items() if k != "style"}
        y = {k: v for k, v in y.items() if k != "style"}
        assert x == {k: (v if v is not None else "") for k, v in y.items()} or x == y


def test_ts_pill_keeps_exact_shape_for_wrap_section():
    # export_web.wrap_section matches this exact attribute order and quoting.
    frag = '<li><a class="ts" data-file="V&amp;1.mp4" data-t="42">▶ V1 00:42</a> 文字</li>'
    out = sanitize_fragment(frag)
    assert out.startswith('<li><a class="ts" data-file="V&amp;1.mp4" data-t="42">')


def test_sanitize_is_idempotent():
    for payload in HOSTILE + LEGIT:
        once = sanitize_fragment(payload)
        assert sanitize_fragment(once) == once


def test_report_lists_escaped_tags():
    rep = []
    sanitize_fragment("<p><script>x</script><iframe></iframe></p>", rep)
    assert {"script", "iframe"} <= set(rep)


def test_unicode_edge_cases_preserved():
    s = "RTL \u202eevil\u202c zero\u200bwidth 🧠 中文 \u2028 \u2029"
    assert sanitize_fragment(f"<p>{s}</p>") == f"<p>{s}</p>"


# --------------------------------------------------------------------------
# URLs
# --------------------------------------------------------------------------
@pytest.mark.parametrize("u", [
    "影片筆記整合/媒體/圖片/01_x.jpg", "a%20b.png", "sub/dir/x.mp4", "x.jpg?v=1",
])
def test_relative_urls_accepted(u):
    assert is_safe_relative_url(u)


@pytest.mark.parametrize("u", [
    "", "   ", "javascript:alert(1)", "JAVASCRIPT:x", " java\nscript:x", "data:image/png;base64,AA",
    "https://x.example/a.png", "//x.example/a.png", "/abs.png", "\\\\server\\share\\a.png",
    "C:/x.png", "file:///etc/passwd", "../x.png", "a/../../x.png", "..%2fx.png",
    "%2e%2e/x.png", "a/%2e%2e/%2e%2e/x.png", None, 42,
])
def test_unsafe_urls_rejected(u):
    assert not is_safe_relative_url(u)


def test_parent_allowed_only_when_asked():
    assert is_safe_relative_url("../video/x.mp4", allow_parent=True)
    assert not is_safe_relative_url("../video/x.mp4")
    assert not is_safe_relative_url("https://x/a.mp4", allow_parent=True)


# --------------------------------------------------------------------------
# JSON in <script>
# --------------------------------------------------------------------------
def test_json_for_script_cannot_break_out():
    obj = {"t": "</script><script>alert(1)</script><!-- & \u2028 \u2029 >", "n": [1, 2.5]}
    s = json_for_script(obj)
    for bad in ("</script", "<!--", "<", ">", "&", "\u2028", "\u2029"):
        assert bad not in s
    assert json.loads(s) == obj


# --------------------------------------------------------------------------
# CSP
# --------------------------------------------------------------------------
def test_script_hash_matches_browser_normalisation():
    text = "const A = 1;\r\nconsole.log(A);\r"
    lf = "const A = 1;\nconsole.log(A);\n"
    want = "'sha256-" + base64.b64encode(hashlib.sha256(lf.encode()).digest()).decode() + "'"
    assert script_hash(text) == want == script_hash(lf)


def test_csp_policy_is_offline_and_hash_pinned():
    pol = csp_policy("x")
    assert "default-src 'none'" in pol
    assert script_hash("x") in pol
    assert "'unsafe-inline'" not in pol.split("script-src", 1)[1].split(";", 1)[0]
    assert "unsafe-eval" not in pol
    for directive in ("base-uri 'none'", "form-action 'none'"):
        assert directive in pol
    for directive in ("img-src", "media-src"):
        part = pol.split(directive, 1)[1].split(";", 1)[0]
        assert "http" not in part and "*" not in part and "data:" not in part
    assert csp_meta("x").startswith('<meta http-equiv="Content-Security-Policy" content="')
