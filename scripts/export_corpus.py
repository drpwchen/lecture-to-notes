"""Export finished lecture notes as a searchable corpus (optional, off by default).

The pipeline's notes are written for a person reading them in Obsidian:
frontmatter, callouts, embedded slides, wikilinks. A retrieval index wants
something else — one plain Markdown file per topic, each saying where it came
from. This script turns one course's finished notes into that shape, by rules
alone (no model, no network, same input -> same bytes):

    <corpus-root>/
    ├── corpus.json                {"format": "t2n-corpus/1", "kind": "lecture"}
    └── <course-dir>/
        ├── _meta.md               course bookkeeping (kept out of search results)
        ├── 01_<section>.md        one entry per topic section of the note
        └── 02_<section>.md

The folder format is the `t2n-corpus/1` contract that ask-your-library indexes
(https://github.com/drpwchen/ask-your-library/blob/main/docs/corpus-format.md):
each entry starts with an H1 and a `Source:` line,

    Source: 2025-02-15 Dr. Wu《Upper-limb nerve ultrasound》· course_dir · c01

Mapping from a note (`reference/note-spec.md#structure`, or an `L3/` file):

* `# 總整理` — each `##` section becomes one entry; its `###` become `##`.
  A note without a `# 總整理` heading uses every `##` under its title instead.
* Sections shorter than --min-chars are merged into the next entry; a section
  longer than --max-chars is split at its `###` headings.
* `# 逐投影片筆記` / `# 逐段筆記` — consecutive slides packed into entries up to
  --max-chars, minus every line whose text 總整理 already carries (so a fact is
  found once, not twice). --summary-only skips them.
* Dropped: frontmatter, the Clinical Pearls callout (a summary of the body),
  `# Resource` / `# Archives`, image embeds, `[[EMBED sN]]` placeholders,
  comments, horizontal rules. Kept as text: other callouts (the `> [!type]`
  wrapper removed), tables, `==highlights==`. `[[link|alias]]` becomes alias.
* Timecodes (`` `MM:SS` ``, `` `(V1 MM:SS)` ``, `[MM:SS]`) become `[cNN MM:SS]`;
  `cNN` is one recording, listed in `_meta.md`. Untimed lines stay untimed —
  no timecode is ever invented.
* `## 縮寫表` and the speaker/date lines under the title go to `_meta.md`.

Turning it on:
    python scripts/export_corpus.py NOTE.md [NOTE2.md ...] --corpus-root DIR
or set `rag.corpus_root` in config.yaml (or env LECTURE_CORPUS_ROOT) and run it
without --corpus-root. With none of the three it prints that the export is off
and exits 0 having written nothing, so it is safe as an unconditional last step.

Then index with ask-your-library (not part of this repo):
    <ask-your-library-venv-python> <ask-your-library>/server/corpus.py check DIR
    <ask-your-library-venv-python> <ask-your-library>/server/corpus.py add DIR

Exit codes: 0 ok / export off, 1 nothing exportable, 2 refused (bad input,
an existing course folder without --replace, a corpus.json of another kind).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import UnsafePathError, contained_path, safe_basename  # noqa: E402
from _log import __version__  # noqa: E402

CORPUS_FORMAT = "t2n-corpus/1"
CORPUS_KIND = "lecture"
META_FILENAME = "_meta.md"
SPEC_URL = "https://github.com/drpwchen/ask-your-library/blob/main/docs/corpus-format.md"

# H1 headings with a fixed role. Compared case-insensitively after stripping.
SUMMARY_H1 = ("總整理", "summary", "overview")
SLIDE_H1 = ("逐投影片筆記", "逐段筆記", "slide notes", "slide-by-slide notes", "segment notes")
SKIP_H1 = ("resource", "resources", "archives", "archive")
ABBREV_H2 = ("縮寫表", "abbreviations", "abbreviation table")

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
_TC = r"\d{1,2}:\d{2}(?::\d{2})?"
TC_V_RE = re.compile(r"`?\(V(\d+)\s+(" + _TC + r")(?:\s*[–~-]\s*(" + _TC + r"))?\)`?")
TC_TICK_RE = re.compile(r"`(" + _TC + r")(?:\s*[–~-]\s*(" + _TC + r"))?`")
TC_BRACKET_RE = re.compile(r"\[(" + _TC + r")(?:\s*[–~-]\s*(" + _TC + r"))?\](?!\()")
EMBED_RE = re.compile(r"!\[\[[^\]]*\]\]|!\[[^\]]*\]\([^)]*\)")
PLACEHOLDER_RE = re.compile(r"\[\[EMBED\s+s\d+[^\]]*\]\]")
WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
CALLOUT_RE = re.compile(r"^\[!(\w[\w-]*)\][+-]?\s*(.*)$")
HR_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
DATE_ISO_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
DATE_FILE_RE = re.compile(r"^(\d{4})[-_]?(\d{2})[-_]?(\d{2})(?!\d)")
HEADER_DATE_RE = re.compile(r"(?:日期|date)\s*[:：]\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", re.I)
HEADER_SPEAKER_RE = re.compile(r"^\s*[-*]?\s*(?:講者|主講|speaker)\s*[:：]\s*(.+?)\s*$", re.I)
UNSAFE_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f#^\[\]]+')


class Refused(Exception):
    """Input or destination the exporter will not touch (exit 2)."""


# ----------------------------------------------------------------------
# Parsing a note
# ----------------------------------------------------------------------

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Top-level `key: value` pairs of a YAML frontmatter, and the body after it.

    Deliberately minimal (no pyyaml): only scalar keys are read, which is all
    this exporter needs (title / speaker / date)."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    meta = {}
    for line in text[4:end].splitlines():
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*?)\s*$", line)
        if m and m.group(2):
            meta[m.group(1)] = m.group(2).strip().strip("'\"")
    return meta, text[end + 5:]


def _norm(h: str) -> str:
    return re.sub(r"\s+", " ", h).strip().lower()


def _is(h: str, names) -> bool:
    n = _norm(h)
    return any(n == x or n.startswith(x + " ") for x in names)


@dataclass
class Block:
    """A heading and the lines under it, up to the next heading of the same or
    a higher level. `lines` excludes the heading line itself."""
    level: int
    title: str
    lines: list = field(default_factory=list)


def split_at(lines: list[str], level: int) -> tuple[list[str], list[Block]]:
    """Lines before the first heading of `level`, and one Block per such heading.
    Headings inside code fences do not count."""
    pre, blocks, fence = [], [], False
    for ln in lines:
        if FENCE_RE.match(ln):
            fence = not fence
        m = None if fence else HEADING_RE.match(ln)
        if m and len(m.group(1)) == level:
            blocks.append(Block(level, m.group(2).strip()))
            continue
        (blocks[-1].lines if blocks else pre).append(ln)
    return pre, blocks


@dataclass
class Note:
    path: Path
    index: int                  # 1-based order on the command line
    title: str                  # course title for the Source line
    speaker: str
    date: str
    header_lines: list          # speaker / date / venue lines under the title
    abbrev_lines: list          # ## 縮寫表 block(s)
    sections: list              # [Block level 2]
    slide_sections: list        # [Block level 2] from 逐投影片筆記 / 逐段筆記
    skipped: list               # what was left out, for _meta.md
    repeats_dropped: int = 0    # slide-note lines already in 總整理


def _strip_speaker_prefix(title: str, speaker: str) -> tuple[str, str]:
    """`Speaker — Topic` -> (speaker, topic). Leaves a title without a dash alone."""
    m = re.match(r"^(.+?)\s+[—–]\s+(.+)$", title) or re.match(r"^(.+?)\s+-\s+(.+)$", title)
    if not m:
        return "", title
    head, rest = m.group(1).strip(), m.group(2).strip()
    sp = clean_speaker(speaker) if speaker else ""
    # Only treat the head as a speaker when it looks like a name (short) or
    # matches the frontmatter speaker; "Part 1 — Anatomy" stays a title.
    if (sp and (head in sp or sp in head)) or (not sp and len(head) <= 12 and not re.search(r"\d", head)):
        return head, rest
    return "", title


def clean_speaker(s: str) -> str:
    """Name only: affiliations in brackets and anything after a comma dropped."""
    s = re.sub(r"[（(][^）)]*[）)]", "", s or "")
    s = re.split(r"[,，;；/、]", s)[0]
    return s.strip().strip("：:").strip()


def _date_from(text: str) -> str:
    m = DATE_ISO_RE.search(text or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def parse_note(path: Path, index: int, args) -> Note:
    raw = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    meta, body = parse_frontmatter(raw)
    lines = body.split("\n")
    pre1, h1s = split_at(lines, 1)
    skipped = []

    summary_blocks, slide_blocks, other = [], [], []
    for b in h1s:
        if _is(b.title, SUMMARY_H1):
            summary_blocks.append(b)
        elif _is(b.title, SLIDE_H1):
            slide_blocks.append(b)
        elif _is(b.title, SKIP_H1):
            skipped.append(f"`# {b.title}`")
        else:
            other.append(b)
    # A pipeline note (it has 總整理 or 逐投影片筆記) opens with its title H1; any
    # further plain H1 is a topic of its own. A note without those headings and
    # with one plain H1 is titled by it; with several, every H1 is a topic.
    template = bool(summary_blocks or slide_blocks)
    title_block = other[0] if other and (template or len(other) == 1) else None
    extra_h1 = [b for b in other if b is not title_block]

    if any(CALLOUT_RE.match(ln.lstrip("> ").strip()) and "summary" in ln.lower() for ln in pre1):
        skipped.append("Clinical Pearls callout (a summary of the sections below)")

    # Header (speaker/date lines) and stray H2s that sit under the title H1.
    title_pre, title_h2 = split_at(title_block.lines if title_block else [], 2)
    abbrev = [b for b in title_h2 if _is(b.title, ABBREV_H2)]
    title_h2 = [b for b in title_h2 if not _is(b.title, ABBREV_H2)]

    sections = []
    for b in summary_blocks:
        pre, h2 = split_at(b.lines, 2)
        abbrev += [x for x in h2 if _is(x.title, ABBREV_H2)]
        h2 = [x for x in h2 if not _is(x.title, ABBREV_H2)]
        if any(ln.strip() for ln in pre):
            name = b.title if not _is(b.title, SUMMARY_H1) else "Overview"
            sections.append(Block(2, name, pre))
        sections += h2
    sections = title_h2 + sections + extra_h1

    slide_sections = []
    for b in slide_blocks:
        _pre, h2 = split_at(b.lines, 2)
        slide_sections += h2
        if args.summary_only:
            skipped.append(f"`# {b.title}` ({len(h2)} sections; --summary-only)")

    # Title / speaker / date, most specific source first.
    fm_title = meta.get("title", "")
    h1_title = title_block.title if title_block else ""
    speaker_hdr = ""
    for ln in title_pre:
        m = HEADER_SPEAKER_RE.match(ln)
        if m:
            speaker_hdr = m.group(1)
            break
    fm_speaker = meta.get("speaker", "")
    stem = re.sub(r"^\d{4}[-_]?\d{2}[-_]?\d{2}[\s_-]*", "", path.stem) or path.stem
    sp_from_title, topic = _strip_speaker_prefix(fm_title or h1_title or stem, fm_speaker or speaker_hdr)
    speaker = args.speaker or clean_speaker(fm_speaker) or clean_speaker(speaker_hdr) or sp_from_title or "講者未載明"
    title = args.title or topic

    date = ""
    if args.date:
        date = _date_from(args.date) or args.date
    if not date:
        date = _date_from(meta.get("date", ""))
    if not date:
        for ln in title_pre:
            m = HEADER_DATE_RE.search(ln)
            if m:
                date = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
                break
    if not date:
        m = DATE_FILE_RE.match(path.name)
        if m:
            date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    date = date or "日期未載明"

    abbrev_lines = []
    for b in abbrev:
        abbrev_lines += b.lines
    return Note(path, index, title, speaker, date,
                [ln for ln in title_pre if ln.strip() and not HR_RE.match(ln)],
                abbrev_lines, sections, slide_sections, skipped)


# ----------------------------------------------------------------------
# Cleaning a section body
# ----------------------------------------------------------------------

class ClipMap:
    """(note index, recording number) -> cNN, assigned in first-use order over
    the notes as given on the command line. A bare timecode is recording 1."""

    def __init__(self):
        self.codes: dict[tuple[int, int], str] = {}

    def code(self, note: int, rec: int) -> str:
        key = (note, rec)
        if key not in self.codes:
            self.codes[key] = f"c{len(self.codes) + 1:02d}"
        return self.codes[key]


def _tc(code: str, a: str, b: str | None) -> str:
    return f"[{code} {a}–{b}]" if b else f"[{code} {a}]"


def convert_timecodes(line: str, note: int, clips: ClipMap, used: set) -> str:
    def v(m):
        c = clips.code(note, int(m.group(1)))
        used.add(c)
        return _tc(c, m.group(2), m.group(3))

    def bare(m):
        c = clips.code(note, 1)
        used.add(c)
        return _tc(c, m.group(1), m.group(2))

    line = TC_V_RE.sub(v, line)
    line = TC_TICK_RE.sub(bare, line)
    return TC_BRACKET_RE.sub(bare, line)


def _wikilink(m) -> str:
    if m.group(2):
        return m.group(2).strip()
    target = m.group(1).split("#", 1)[0].strip()
    return target.rsplit("/", 1)[-1] or m.group(1).split("#", 1)[-1].strip()


def clean_lines(lines: list[str], note: int, clips: ClipMap, used: set,
                shift: int = 0) -> list[str]:
    """Obsidian note lines -> plain Markdown lines for an entry.

    shift: how many levels to raise every heading (`###` -> `##` for shift=1).
    """
    out, fence = [], False
    text = "\n".join(lines)
    text = re.sub(r"%%.*?%%", "", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    for ln in text.split("\n"):
        if FENCE_RE.match(ln):
            fence = not fence
            out.append(ln.rstrip())
            continue
        if fence:
            out.append(ln.rstrip())
            continue
        # Callouts: drop the `> ` quoting and the `[!type]` marker, keep the text.
        depth = 0
        while re.match(r"^\s*>\s?", ln):
            ln = re.sub(r"^\s*>\s?", "", ln, count=1)
            depth += 1
        if depth:
            cm = CALLOUT_RE.match(ln.strip())
            if cm:
                ln = f"**{cm.group(2).strip()}**" if cm.group(2).strip() else ""
        if PLACEHOLDER_RE.search(ln):
            ln = PLACEHOLDER_RE.sub("", ln)
        if EMBED_RE.search(ln):
            ln = EMBED_RE.sub("", ln)
            if not re.sub(r"[\s>*_-]", "", ln):
                continue
        if HR_RE.match(ln):
            continue
        hm = HEADING_RE.match(ln)
        if hm and shift:
            level = max(2, len(hm.group(1)) - shift)
            ln = "#" * level + " " + hm.group(2).strip()
        ln = WIKILINK_RE.sub(_wikilink, ln)
        ln = convert_timecodes(ln, note, clips, used)
        out.append(ln.rstrip())
    # Collapse runs of blank lines; trim both ends.
    res = []
    for ln in out:
        if not ln.strip() and (not res or not res[-1].strip()):
            continue
        res.append(ln)
    while res and not res[-1].strip():
        res.pop()
    return res


# ----------------------------------------------------------------------
# Sections -> entries
# ----------------------------------------------------------------------

@dataclass
class Entry:
    title: str
    note: Note
    parts: list                  # [(heading or None, [lines])]
    clips: set = field(default_factory=set)

    def size(self) -> int:
        return sum(len("\n".join(p[1])) for p in self.parts)


def _section_entry(b: Block, note: Note, clips: ClipMap) -> Entry:
    used: set = set()
    # A timecode in a heading moves to the first body line (no source on a heading line).
    head_tc = [m.group(0) for m in TC_V_RE.finditer(b.title)]
    title = TC_V_RE.sub("", b.title)
    head_tc += [m.group(0) for m in TC_TICK_RE.finditer(title)]
    title = TC_TICK_RE.sub("", title).strip(" —–-")
    lines = ([" ".join(head_tc), ""] if head_tc else []) + b.lines
    body = clean_lines(lines, note.index, clips, used, shift=b.level - 1)
    return Entry(WIKILINK_RE.sub(_wikilink, title) or "Untitled", note, [(None, body)], used)


def _split_large(e: Entry, max_chars: int) -> list[Entry]:
    """Split one oversized single-section entry at its (now level-2) headings."""
    if e.size() <= max_chars or len(e.parts) != 1:
        return [e]
    pre, h2 = split_at(e.parts[0][1], 2)
    if not h2:
        return [e]
    pieces = []
    cur = Entry(e.title, e.note, [(None, pre)] if any(x.strip() for x in pre) else [], set())
    for b in h2:
        block = [f"## {b.title}"] + b.lines
        if cur.parts and cur.size() + len("\n".join(block)) > max_chars:
            pieces.append(cur)
            cur = Entry(f"{e.title} — {b.title}", e.note, [], set())
        cur.parts.append((None, block))
    pieces.append(cur)
    for p in pieces:
        txt = "\n".join("\n".join(x[1]) for x in p.parts)
        p.clips = {c for c in e.clips if f"[{c} " in txt}
    return pieces


def _merge_small(entries: list[Entry], min_chars: int) -> list[Entry]:
    """Greedy: an entry under min_chars absorbs the following entry of the same
    note. Each absorbed section keeps its own heading as an H2."""
    def absorb(prev: Entry, e: Entry) -> None:
        if len(prev.parts) == 1 and prev.parts[0][0] is None:
            prev.parts = [(prev.title, _demote(prev.parts[0][1]))]
        prev.parts += [(e.title, _demote(p[1])) if p[0] is None else p for p in e.parts]
        prev.title = _join_titles(prev.title, e.title)
        prev.clips |= e.clips

    out: list[Entry] = []
    for e in entries:
        prev = out[-1] if out else None
        if prev and prev.note is e.note and prev.size() < min_chars:
            absorb(prev, e)
        else:
            out.append(e)
    # A short last section has nothing after it to absorb: it joins the one before.
    if len(out) > 1 and out[-1].size() < min_chars and out[-2].note is out[-1].note:
        absorb(out[-2], out.pop())
    return out


def _demote(lines: list[str]) -> list[str]:
    """Push headings one level down (## -> ###) so a merged section nests under its own ##."""
    out, fence = [], False
    for ln in lines:
        if FENCE_RE.match(ln):
            fence = not fence
        m = None if fence else HEADING_RE.match(ln)
        out.append("#" * min(6, len(m.group(1)) + 1) + " " + m.group(2) if m else ln)
    return out


def _join_titles(a: str, b: str, cap: int = 100) -> str:
    t = f"{a}；{b}"
    return t if len(t) <= cap else (a if a.endswith("…") else a + "…")


SHINGLE = 8           # characters per shingle for the overlap test
DUP_SHARE = 0.8       # a slide-note line this much inside 總整理 is a repeat
DUP_MIN_CHARS = 12    # shorter lines are never judged (too little text to tell)
_NORM_DROP = re.compile(r"\[c\d+ [^\]]*\]|[#>*=`_|~\-\s]+")


def _shingles(text: str) -> set:
    t = _NORM_DROP.sub("", text).lower()
    return {t[i:i + SHINGLE] for i in range(len(t) - SHINGLE + 1)}


def _drop_repeats(lines: list[str], seen: set) -> tuple[list[str], int]:
    """Drop lines whose text is already in `seen` (shingles of the 總整理
    entries), then headings left with nothing under them. Returns (lines, dropped)."""
    kept, dropped, fence = [], 0, False
    for ln in lines:
        if FENCE_RE.match(ln):
            fence = not fence
        if not fence and not HEADING_RE.match(ln) and len(_NORM_DROP.sub("", ln)) >= DUP_MIN_CHARS:
            sh = _shingles(ln)
            if sh and len(sh & seen) >= DUP_SHARE * len(sh):
                dropped += 1
                continue
        kept.append(ln)
    out: list[str] = []
    for i, ln in enumerate(kept):           # empty headings go
        m = HEADING_RE.match(ln)
        if m:
            lvl, has = len(m.group(1)), False
            for nxt in kept[i + 1:]:
                mm = HEADING_RE.match(nxt)
                if mm and len(mm.group(1)) <= lvl:
                    break
                if nxt.strip() and not mm:
                    has = True
                    break
            if not has:
                continue
        out.append(ln)
    res = []
    for ln in out:
        if not ln.strip() and (not res or not res[-1].strip()):
            continue
        res.append(ln)
    while res and not res[-1].strip():
        res.pop()
    return res, dropped


def _has_text(line: str) -> bool:
    """A line that says something: not blank, not a heading, not only timecodes."""
    if not line.strip() or HEADING_RE.match(line):
        return False
    return bool(re.sub(r"\[c\d+ [^\]]*\]|[\s*-]", "", line))


def _slide_entries(note: Note, clips: ClipMap, max_chars: int, seen: set) -> list[Entry]:
    out: list[Entry] = []
    cur: Entry | None = None
    for b in note.slide_sections:
        e = _section_entry(b, note, clips)
        body, dropped = _drop_repeats(e.parts[0][1], seen)
        note.repeats_dropped += dropped
        if not any(_has_text(ln) for ln in body):
            continue    # nothing left but its heading / timecode: every line was a repeat
        block = [f"## {e.title}"] + _demote(body)
        if cur is None or cur.size() + len("\n".join(block)) > max_chars:
            cur = Entry("", note, [], set())
            cur.first = e.title  # type: ignore[attr-defined]
            out.append(cur)
        cur.parts.append((None, block))
        txt = "\n".join(block)
        cur.clips |= {c for c in e.clips if f"[{c} " in txt}
        cur.last = e.title  # type: ignore[attr-defined]
    for c in out:
        first, last = c.first, c.last  # type: ignore[attr-defined]
        span = first if first == last else f"{first} … {last}"
        c.title = f"{note.title} — slide notes: {span}"
        if len(c.title) > 120:
            c.title = c.title[:119] + "…"
    return out


def build_entries(notes: list[Note], args) -> tuple[list[Entry], ClipMap]:
    clips = ClipMap()
    entries: list[Entry] = []
    for n in notes:
        mine = [_section_entry(b, n, clips) for b in n.sections]
        mine = [e for e in mine if any(ln.strip() for p in e.parts for ln in p[1])]
        split = []
        for e in mine:
            split += _split_large(e, args.max_chars)
        mine = _merge_small(split, args.min_chars)
        entries += mine
        if not args.summary_only:
            seen: set = set()
            for e in mine:
                for _h, lines in e.parts:
                    seen |= _shingles("\n".join(lines))
            entries += _slide_entries(n, clips, args.max_chars, seen)
    return entries, clips


# ----------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------

def _source_value(e: Entry, course_dir: str) -> str:
    title = e.note.title.replace("《", "〈").replace("》", "〉")
    speaker = e.note.speaker.replace("《", "〈").replace("》", "〉").replace("·", " ")
    s = f"{e.note.date} {speaker}《{title}》· {course_dir}"
    if e.clips:
        s += " · " + ", ".join(sorted(e.clips))
    return s


def render_entry(e: Entry, course_dir: str) -> str:
    out = [f"# {e.title}", "", f"Source: {_source_value(e, course_dir)}", ""]
    for heading, lines in e.parts:
        if heading:
            out += [f"## {heading}", ""]
        out += lines + [""]
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + "\n"


def safe_name(title: str, cap: int = 60) -> str:
    """A file-name slug: letters, digits (CJK included), `-` and `.` kept; every
    other character — spaces, `；` `—` `/` `（` `&` and the like — becomes `_`,
    runs collapsed. Over `cap` it is cut at the last `_` when that keeps at
    least half the cap (no half words), else at the cap (CJK has no spaces)."""
    out = []
    for ch in title:
        cat = unicodedata.category(ch)
        keep = ch in "-." or cat[0] in "LN" or cat == "Mn"
        out.append(ch if keep and not UNSAFE_NAME_CHARS.match(ch) else "_")
    s = re.sub(r"_+", "_", "".join(out)).strip("._-")
    if len(s) > cap:
        cut = s.rfind("_", 0, cap + 1)
        s = s[:cut] if cut >= cap // 2 else s[:cap]
    return s.rstrip("._-") or "entry"


def entry_filenames(entries: list[Entry]) -> list[str]:
    width = max(2, len(str(len(entries))))
    names = []
    for i, e in enumerate(entries, 1):
        name = f"{i:0{width}d}_{safe_name(e.title)}.md"
        names.append(safe_basename(name, "entry file name"))
    return names


def render_meta(course_dir: str, notes: list[Note], entries: list[Entry],
                names: list[str], clips: ClipMap, args) -> str:
    first = notes[0]
    speakers = list(dict.fromkeys(n.speaker for n in notes))
    dates = sorted({n.date for n in notes})
    out = [f"# Course meta — {course_dir}", "",
           "Bookkeeping for this course folder; not lecture content (search excludes this file).", "",
           "## Course", "",
           f"- Title: {first.title}",
           f"- Date: {', '.join(dates)}",
           f"- Speaker(s): {', '.join(speakers)}"]
    for n in notes:
        if n.header_lines:
            out += ["", f"From `{n.path.name}`:", ""]
            out += clean_lines(n.header_lines, n.index, ClipMap(), set())
    out += ["", "## Materials", "",
            "One code per recording; `[cNN MM:SS]` in an entry points into that recording.", "",
            "| Code | Source note | Recording |", "|---|---|---|"]
    by_note = {}
    for (ni, rec), code in sorted(clips.codes.items(), key=lambda kv: kv[1]):
        by_note.setdefault(ni, []).append((code, rec))
        out.append(f"| {code} | {_cell(notes[ni - 1].path.name)} | V{rec} |")
    for n in notes:
        if n.index not in by_note:
            out.append(f"| — | {_cell(n.path.name)} | (no timecodes in the note) |")
    out += ["", "## Source notes → entries", "", "| Source note | Entries |", "|---|---|"]
    for n in notes:
        mine = [names[i] for i, e in enumerate(entries) if e.note is n]
        out.append(f"| {_cell(n.path.name)} | {_cell(', '.join(mine) or '(none)')} |")
    abbrev = [(n, n.abbrev_lines) for n in notes if any(x.strip() for x in n.abbrev_lines)]
    if abbrev:
        out += ["", "## Abbreviations", ""]
        for n, lines in abbrev:
            out += clean_lines(lines, n.index, ClipMap(), set()) + [""]
        while out and not out[-1].strip():
            out.pop()
    out += ["", "## Export notes", "",
            f"- Written by lecture-to-notes `scripts/export_corpus.py` {__version__} "
            f"(rule-based; min {args.min_chars} / max {args.max_chars} chars per entry"
            + (", 總整理 only" if args.summary_only else "") + ").",
            "- Always left out: image embeds, slide placeholders, frontmatter, comments.",
            "- Lines without a timecode in the note carry none here; none are invented."]
    for n in notes:
        for s in n.skipped:
            out.append(f"- `{n.path.name}`: not exported — {s}.")
        if n.repeats_dropped:
            out.append(f"- `{n.path.name}`: {n.repeats_dropped} slide-note line(s) already in "
                       "總整理 were not repeated.")
    return "\n".join(out) + "\n"


def _cell(s: str) -> str:
    return s.replace("|", "\\|")


# ----------------------------------------------------------------------
# Destination
# ----------------------------------------------------------------------

def resolve_root(args) -> str:
    if args.corpus_root:
        return args.corpus_root
    env = (os.environ.get("LECTURE_CORPUS_ROOT") or "").strip()
    if env:
        return env
    try:
        from _common import load_config
        cfg = load_config()
    except Exception as e:  # pyyaml missing etc.
        print(f"note: config.yaml not read ({e})", file=sys.stderr)
        cfg = {}
    return str(((cfg.get("rag") or {}).get("corpus_root") or "")).strip()


def ensure_corpus_json(root: Path, dry: bool) -> str:
    p = root / "corpus.json"
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise Refused(f"{p} is not readable JSON: {e}")
        if d.get("format") != CORPUS_FORMAT or d.get("kind") != CORPUS_KIND:
            raise Refused(f"{p} declares format={d.get('format')!r} kind={d.get('kind')!r}; "
                          f"a lecture export needs {CORPUS_FORMAT!r} / {CORPUS_KIND!r}. "
                          "Use a separate corpus root for each kind.")
        return "kept"
    if not dry:
        root.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"format": CORPUS_FORMAT, "kind": CORPUS_KIND}, ensure_ascii=False) + "\n",
                     encoding="utf-8", newline="\n")
    return "created"


def default_course_dir(path: Path) -> str:
    return safe_name(path.stem, cap=80)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("notes", nargs="+", help="finished note(s) of ONE course, in recording order")
    ap.add_argument("--corpus-root", help="corpus folder (default: env LECTURE_CORPUS_ROOT, then "
                                          "rag.corpus_root in config.yaml; none = export off)")
    ap.add_argument("--course-dir", help="course folder name (default: first note's file name)")
    ap.add_argument("--title", help="course title for the Source line (default: note H1 minus speaker)")
    ap.add_argument("--speaker", help="speaker for the Source line (default: frontmatter `speaker`)")
    ap.add_argument("--date", help="YYYY-MM-DD (default: frontmatter `date`, the 日期 line, the file name)")
    ap.add_argument("--summary-only", action="store_true",
                    help="export only 總整理; skip 逐投影片筆記 / 逐段筆記 entirely")
    ap.add_argument("--min-chars", type=int, default=600,
                    help="merge a section shorter than this into the next one (default 600)")
    ap.add_argument("--max-chars", type=int, default=3000,
                    help="split a section longer than this at its ### headings, and cap a "
                         "pack of slide notes at it (default 3000)")
    ap.add_argument("--replace", action="store_true",
                    help="the course folder exists: delete its .md files and write it again")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    args = ap.parse_args(argv)

    root_s = resolve_root(args)
    if not root_s:
        print("RAG export is off: no --corpus-root, LECTURE_CORPUS_ROOT or rag.corpus_root. "
              "Nothing written.")
        return 0
    root = Path(root_s)

    try:
        paths = [Path(p) for p in args.notes]
        for p in paths:
            if not p.is_file():
                raise Refused(f"not a file: {p}")
            if p.suffix.lower() != ".md":
                raise Refused(f"not a Markdown note: {p}")
        course_dir = safe_basename(args.course_dir or default_course_dir(paths[0]), "course folder")
        if course_dir == META_FILENAME or course_dir.startswith("."):
            raise Refused(f"course folder name {course_dir!r} is reserved")
        notes = [parse_note(p, i, args) for i, p in enumerate(paths, 1)]
        entries, clips = build_entries(notes, args)
        if not entries:
            print("nothing exportable: no ## sections with text in "
                  + ", ".join(p.name for p in paths), file=sys.stderr)
            return 1
        names = entry_filenames(entries)
        cdir = Path(contained_path(root, course_dir, what="course folder"))
        files = {names[i]: render_entry(e, course_dir) for i, e in enumerate(entries)}
        files[META_FILENAME] = render_meta(course_dir, notes, entries, names, clips, args)

        existing = sorted(x.name for x in cdir.glob("*.md")) if cdir.is_dir() else []
        if existing and not args.replace:
            raise Refused(f"{cdir} already holds {len(existing)} .md file(s); "
                          "re-run with --replace to rewrite this course")
        state = ensure_corpus_json(root, args.dry_run)
    except (Refused, UnsafePathError) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2

    print(f"corpus root: {root}  (corpus.json {state})")
    print(f"course: {course_dir}  — {len(entries)} entries from {len(notes)} note(s), "
          f"{len(clips.codes)} recording code(s)")
    for n, body in files.items():
        print(f"  {'(dry) ' if args.dry_run else ''}{n}  {len(body.encode('utf-8'))} B")
    if args.dry_run:
        return 0

    cdir.mkdir(parents=True, exist_ok=True)
    for name in existing:   # --replace: this course's previous export, regenerable
        (cdir / name).unlink()
    for name, body in files.items():
        Path(contained_path(cdir, name, what="entry file")).write_text(body, encoding="utf-8", newline="\n")
    print("\nNext (ask-your-library, " + SPEC_URL + "):")
    print(f'  <ask-your-library-venv-python> <ask-your-library>/server/corpus.py check "{root}"')
    print(f'  <ask-your-library-venv-python> <ask-your-library>/server/corpus.py add "{root}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
