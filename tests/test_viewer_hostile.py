"""End-to-end: export the hostile fixture course and inspect the page.

tests/fixtures/hostile_course is a tiny course in which every string — course
title, segment title, transcript bullets, the Stage F note, slide OCR text — is
an attack on the viewer. The export must succeed, keep every legitimate feature
(timecode pills, figures, sync blocks, search data), and produce a page in which
none of the payloads can run or fetch anything.

Needs pandoc + ffmpeg/ffprobe on PATH (export_web's preflight); skipped
otherwise. The browser test additionally needs Playwright with a local Chrome,
Edge or bundled Chromium, and only ever opens file:// URLs.
"""
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import zlib
from html.parser import HTMLParser

import pytest

from conftest import FIXTURES, SCRIPTS
from _html_safety import is_safe_link, is_safe_relative_url, script_hash

NEEDS = ("pandoc", "ffmpeg", "ffprobe")
# CI sets this so a missing tool FAILS the job instead of skipping: a security
# test that silently skips reports success about something it never looked at.
REQUIRED = os.environ.get("LTN_REQUIRE_VIEWER_E2E") == "1"
_missing = [b for b in NEEDS if shutil.which(b) is None]
if _missing and REQUIRED:
    raise RuntimeError(f"LTN_REQUIRE_VIEWER_E2E=1 but not on PATH: {_missing}")
pytestmark = pytest.mark.skipif(
    bool(_missing), reason="export_web needs pandoc + ffmpeg/ffprobe on PATH")


def _png():
    def chunk(t, b):
        return (struct.pack(">I", len(b)) + t + b
                + struct.pack(">I", zlib.crc32(t + b) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
            + chunk(b"IEND", b""))


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    course = tmp_path_factory.mktemp("hostile") / "course"
    shutil.copytree(os.path.join(FIXTURES, "hostile_course"), course)
    (course / "figures").mkdir()
    (course / "figures" / "c01_frame_0001.png").write_bytes(_png())
    (course / "talk.mp4").write_bytes(b"")          # in-place clip; never decoded
    r = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "export_web.py"), str(course),
         "--no-remux", "--author", ""],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    html_path = course / "影片筆記整合.html"
    return {"course": course, "html_path": html_path,
            "page": html_path.read_text(encoding="utf-8"),
            "stderr": r.stderr}


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags, self.scripts, self._in_script = [], [], False
        self.csp = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.tags.append((tag, a))
        if tag == "script":
            self._in_script = True
            self.scripts.append("")
        if tag == "meta" and (a.get("http-equiv") or "").lower() == "content-security-policy":
            self.csp = a.get("content")

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_script:
            self.scripts[-1] += data


def parse(page):
    p = _Page()
    p.feed(page)
    p.close()
    return p


def test_exactly_one_script_and_it_is_ours(exported):
    p = parse(exported["page"])
    assert sum(1 for t, _ in p.tags if t == "script") == 1
    assert p.scripts[0].startswith("const TIMELINE = ")
    for bad in ("</script", "<!--"):
        assert bad not in p.scripts[0]


def test_csp_pins_that_script(exported):
    p = parse(exported["page"])
    assert p.csp, "no Content-Security-Policy meta"
    assert "default-src 'none'" in p.csp
    assert script_hash(p.scripts[0]) in p.csp
    # the CSP meta must come before any script or untrusted content
    page = exported["page"]
    assert page.index("Content-Security-Policy") < page.index("<script")
    assert page.index("Content-Security-Policy") < page.index("<body")


def test_no_executable_or_fetching_markup(exported):
    p = parse(exported["page"])
    head_meta = [a for t, a in p.tags if t == "meta"]
    assert len(head_meta) == 3                       # charset, CSP, viewport
    assert not any("refresh" in (a.get("http-equiv") or "").lower() for a in head_meta)
    assert sum(1 for t, _ in p.tags if t == "style") == 1   # the viewer's own
    for tag, attrs in p.tags:
        assert tag not in {"iframe", "object", "embed", "svg", "math", "form",
                           "base", "link", "noscript", "textarea"}, tag
        for name, value in attrs.items():
            assert not name.startswith("on"), (tag, name)
            if name == "href":
                assert is_safe_link(value), value
            if name == "src":
                assert is_safe_relative_url(value), value
            if name == "style":
                assert "url(" not in (value or "").lower()


def test_payload_text_is_visible(exported):
    page = exported["page"]
    body = page.split("<body", 1)[1].split("<script>", 1)[0]
    assert "&lt;script&gt;window.__pwned=&#x27;l2-script&#x27;&lt;/script&gt;" in body \
        or "&lt;script&gt;window.__pwned='l2-script'&lt;/script&gt;" in body
    assert "&lt;iframe" in body
    assert "惡意課程" in page


def test_legit_features_survive(exported):
    page = exported["page"]
    p = parse(page)
    sync = [a for t, a in p.tags if t == "li" and "data-sync-block" in a]
    pills = [a for t, a in p.tags if t == "a" and a.get("class") == "ts"]
    figs = [a for t, a in p.tags if t == "img" and a.get("class") == "fig"]
    # all 7 timed L2 bullets stay separate bullets — the unclosed <script> in
    # bullet 3 must not swallow 4-7 — plus the timed L3 bullets
    l2 = [a for a in sync if a.get("data-section-id", "").endswith("-l2")]
    assert len(l2) == 7
    assert {a["data-start"] for a in l2} == {"5", "10", "15", "20", "25", "30", "35"}
    assert any(a.get("data-file") == "talk.mp4" for a in pills)
    assert len(figs) == 1
    src = figs[0]["src"]
    assert "%23" in src and "%25" in src             # '#' and '%' from the title, encoded
    img_dir = exported["course"] / "影片筆記整合" / "媒體" / "圖片"
    assert len(list(img_dir.iterdir())) == 1


def test_timeline_round_trips_the_raw_text(exported):
    script = parse(exported["page"]).scripts[0]
    m = re.match(r"const TIMELINE = (.*?);\n", script, re.S)
    tl = json.loads(m.group(1))
    texts = " ".join(b["text"] for b in tl["note_blocks"])
    assert "<script>window.__pwned='l2-script'</script>" in texts
    # block text is whitespace-collapsed; the segment title is not
    assert " " in tl["segments"][0]["title"] and " " in tl["segments"][0]["title"]
    assert "</script><script>window.__pwned='ocr'</script>" in tl["slide_blocks"][0]["text"]
    assert "<img src=x onerror" in tl["course"]


def test_exporter_reports_what_it_escaped(exported):
    assert "[sanitize]" in exported["stderr"]


# --------------------------------------------------------------------------
# Real browser, file:// only. Proves the page runs (CSP does not break the
# viewer) and that nothing hostile runs — including markup injected AFTER the
# sanitizer, which only the CSP stands in front of.
# --------------------------------------------------------------------------
def _launch(p):
    for kw in ({"channel": "chrome"}, {"channel": "msedge"}, {}):
        try:
            return p.chromium.launch(headless=True, **kw)
        except Exception:  # noqa: BLE001 - try the next local browser
            continue
    return None


@pytest.fixture(scope="module")
def browser():
    if REQUIRED:
        import playwright.sync_api as sync_api     # ImportError fails the job
    else:
        sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        b = _launch(p)
        if b is None:
            if REQUIRED:
                pytest.fail("LTN_REQUIRE_VIEWER_E2E=1 but no browser Playwright can launch")
            pytest.skip("no local Chromium-family browser for Playwright")
        yield b
        b.close()


def _open(browser, path):
    page = browser.new_page()
    events = {"console": [], "errors": [], "dialogs": [], "requests": [], "failed": [],
              "finished": []}
    page.on("console", lambda m: events["console"].append(m.text))
    page.on("pageerror", lambda e: events["errors"].append(str(e)))
    page.on("dialog", lambda d: (events["dialogs"].append(d.message), d.dismiss()))
    page.on("request", lambda r: events["requests"].append(r.url))
    page.on("requestfailed", lambda r: events["failed"].append((r.url, r.failure)))
    page.on("requestfinished", lambda r: events["finished"].append(r.url))
    page.goto(path.as_uri())
    page.wait_for_timeout(400)
    return page, events


def test_browser_runs_viewer_and_no_payload(exported, browser):
    page, ev = _open(browser, exported["html_path"])
    assert page.evaluate("window.__pwned === undefined")
    assert not ev["dialogs"] and not ev["errors"], ev
    assert not [m for m in ev["console"] if "Content Security Policy" in m], ev["console"]
    assert all(u.startswith("file:") for u in ev["requests"]), ev["requests"]
    # the viewer's own script ran under the CSP: search renders results...
    page.fill("#search-input", "amp mark")
    page.wait_for_timeout(200)
    assert page.evaluate("document.querySelectorAll('#search-results .sr-item').length") >= 1
    # ...whose snippet escapes the text and highlights only real matches
    html_ = page.inner_html("#search-results")
    # (the old highlighter ran on the escaped string and produced "&<mark>amp</mark>;")
    assert "<mark>amp</mark> &amp; <mark>mark</mark>" in html_
    assert "&<mark>" not in html_
    assert page.evaluate("document.querySelectorAll('#search-results [onerror]').length") == 0
    # ...and a timecode pill drives the player
    page.click("a.ts >> nth=0")
    assert "talk.mp4" in page.inner_text("#now")
    page.fill("#search-input", "ocr")
    page.wait_for_timeout(200)
    assert page.evaluate("window.__pwned === undefined")
    page.close()


def test_csp_blocks_markup_that_bypasses_the_sanitizer(exported, browser, tmp_path):
    # Simulate a sanitizer bug: inject live markup straight into the page body.
    injected = ('<script>window.__pwned="inline"</script>'
                '<img src="x" onerror="window.__pwned=\'onerror\'">'
                '<img src="https://tracker.example/p.gif">')
    page_html = exported["page"].replace('<div class="app-v3"', injected + '<div class="app-v3"', 1)
    out = exported["html_path"].with_name("bypass.html")
    out.write_text(page_html, encoding="utf-8")
    page, ev = _open(browser, out)
    assert page.evaluate("window.__pwned === undefined")
    # the beacon never leaves the browser: refused by CSP, not by a failed DNS lookup
    assert not any("tracker.example" in u for u in ev["finished"]), ev["finished"]
    tracker = [(u, why) for u, why in ev["failed"] if "tracker.example" in u]
    assert tracker and all("csp" in (why or "").lower() for _, why in tracker), ev["failed"]
    csp_msgs = [m for m in ev["console"] if "Content Security Policy" in m]
    assert any("script" in m for m in csp_msgs) and any("img-src" in m for m in csp_msgs), csp_msgs
    # and the legitimate script still ran
    assert page.evaluate("document.querySelector('#segment-title').textContent.length > 0")
    page.close()
