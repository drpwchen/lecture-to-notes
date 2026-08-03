#!/usr/bin/env python
"""Vendored 2026-08-02 from vault 96Claude/scripts/audit_note.py — keep in sync;
the vault copy remains canonical for other skills. Vendored (rather than
imported through a shim) so this skill is self-contained on a machine that has
no vault: the old shim resolved a private path and died with ModuleNotFoundError.
Stdlib only.

General vault-note auditor — mechanical quality checks for Claude-written notes.

Shared by /textbook-to-note (self-check) and usable for any 52Medicine note.
Mirrors the lecture-to-notes auditor's FAIL/WARN philosophy:
  FAIL = structural / mechanical defect that should block writing/finalize.
  WARN = judgment item surfaced for review (accept or fix, never silently ignore).

Modes:
  generic      — vault-wide rules only (default)
  textbook     — generic + `(citation` placeholder + writing-style + template checks
  lecture      — generic + 總整理/逐投影片 structure (old combined-note format)
  lecture-seg  — segmented L2/L3/HUB structure (reference/segmented-mode.md): L3 needs a
                 `# L3 …`/`# 總整理` heading but NO `# 逐投影片筆記`; L2 needs segNN +
                 timestamped bullets, no Resource/Pearls; HUB needs a seg table +
                 resolving [[L2/L3]] links. Images resolve against the course dir.

Template (for --mode textbook, optional, sharpens required-heading checks):
  disease | exam | technique | tool | drug | anatomy | atlas

Usage:
  python audit_note.py "<note.md>" --mode textbook [--template disease] [--vault <root>]
Exit 0 if no FAILs (WARNs allowed), 1 otherwise.
"""
import argparse, glob, json, os, re, sys
from collections import Counter

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

CJK = r"[㐀-鿿豈-﫿]"
IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg")
RARE_ACRONYMS = ["PHCA", "IGHL", "CHL", "ILBCN", "RFD", "GRF", "vAHD"]

# Required top-level headings beyond the universal `# Resource`, by template.
TEMPLATE_HEADINGS = {
    "disease":   ["Background", "Evaluation", "Management"],
    "exam":      ["Principle", "Indication", "Technique", "Interpretation", "Pitfalls"],
    "technique": ["Mechanism", "Indication", "Protocol", "Evidence"],
    "tool":      ["Purpose", "Procedure", "Scoring"],
    "drug":      ["Indication", "Mechanism", "Dosing", "Adverse"],
    "anatomy":   [],   # free structure
    "atlas":     [],   # free structure
}


def load(path):
    return open(path, encoding="utf-8").read()


def split_frontmatter(t):
    if t.startswith("---\n"):
        end = t.find("\n---", 4)
        if end != -1:
            return t[: end + 4], t[end + 4:]
    return "", t


def img_embeds(text):
    out = []
    for m in re.finditer(r"!\[\[([^\]]+)\]\]", text):
        inner = m.group(1)
        parts = inner.split("|")
        if parts[0].strip().lower().endswith(IMG_EXT):
            out.append((m.start(), inner, parts))
    return out


def ref_resolves(target, vault, seg_root=None):
    """An image embed target resolves if the file exists (path form) or is found
    under 99Attachment or figures/ (bare). For segmented lecture notes, `seg_root`
    is the course work dir — frames live in `<course>/figures/` (or elsewhere in
    the course tree), NOT the vault, so search there first."""
    target = target.strip()
    if "/" in target:
        if os.path.exists(os.path.join(vault, target)):
            return True
        if seg_root and os.path.exists(os.path.join(seg_root, target)):
            return True
        return False
    # Bare filename: for segmented notes, search the course dir first.
    if seg_root:
        if os.path.exists(os.path.join(seg_root, "figures", target)):
            return True
        if glob.glob(os.path.join(seg_root, "**", target), recursive=True):
            return True
    # Vault forms: 99Attachment (vault notes) and figures/ (lecture-to-notes)
    hits = glob.glob(os.path.join(vault, "99Attachment", "**", target), recursive=True)
    if hits:
        return True
    hits = glob.glob(os.path.join(vault, "figures", target), recursive=False)
    return bool(hits)


def detect_seg_kind(path, text):
    """Classify a segmented lecture note as 'hub' | 'l3' | 'l2' from filename first,
    then the top heading. Returns None if it doesn't look segmented."""
    base = os.path.basename(path)
    parent = os.path.basename(os.path.dirname(path))
    if base.startswith("_HUB_") or "_HUB_" in base:
        return "hub"
    if base.startswith("L3_") or parent == "L3":
        return "l3"
    if base.startswith("L2_") or parent == "L2":
        return "l2"
    # Fall back to heading text
    if re.search(r"^#\s+.*Hub\s*$", text, re.M) or re.search(r"^#\s+.*—\s*Hub", text, re.M):
        return "hub"
    if re.search(r"^#\s+L3\b", text, re.M):
        return "l3"
    if re.search(r"^#\s+L2\b", text, re.M):
        return "l2"
    return None


def seg_course_root(path):
    """Course work dir for a segmented note: L2/L3 sit in `<course>/L2|L3/`, the
    Hub sits at `<course>/`. Used to resolve image embeds against `figures/`."""
    note_dir = os.path.dirname(os.path.abspath(path))
    if os.path.basename(note_dir) in ("L2", "L3"):
        return os.path.dirname(note_dir)
    return note_dir


# ---- C3 automation: caption ↔ frame token-overlap (lecture mode, --grounding) ----
_CJK_RUN = r"[㐀-鿿豈-﫿]{2,}"
_STOP = set((
    "the and for with this that from your you are was has have not but its "
    "all can may use used using show shows showed here there what when then "
    "case slide figure image view 投影片 圖片 如圖 顯示 如下 一個 這個 這是 對照 標示 標註"
).split())


def salient_tokens(text):
    """English/abbrev words (≥3 chars, non-numeric) + CJK bigrams, minus stopwords."""
    text = (text or "").lower()
    toks = set()
    for w in re.findall(r"[a-z][a-z0-9\-]{2,}", text):
        if w not in _STOP:
            toks.add(w)
    for run in re.findall(_CJK_RUN, text):
        for j in range(len(run) - 1):
            bg = run[j:j + 2]
            if bg not in _STOP:
                toks.add(bg)
    return toks


def load_grounding(gdir):
    """Map embed-basename → salient tokens of that frame's OCR text + VLM labels.
    Keys by both attachment_name and raw filename so either embed form resolves."""
    def _read(name):
        p = os.path.join(gdir, name)
        return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None

    grounded = _read("slides_grounded.json")
    final = _read("slides_final.json")
    if grounded is None:
        return None
    if isinstance(grounded, dict):
        grounded = next((v for v in grounded.values() if isinstance(v, list)), [])
    g_by_file = {r.get("filename"): r for r in grounded if isinstance(r, dict)}

    def corpus_for(g):
        ocr = g.get("ocr", {}) or {}
        vlm = g.get("vlm_signals", {}) or {}
        ret = g.get("retrieval", {}) or {}
        parts = [ocr.get("quick_text", ""), str(ocr.get("vlm_text", "")),
                 " ".join(str(x) for x in (vlm.get("visible_labels") or [])),
                 " ".join(str(x) for x in (ret.get("retrieval_keywords") or []))]
        return salient_tokens(" ".join(parts))

    out = {}
    for fn, g in g_by_file.items():
        if fn:
            out[os.path.basename(fn).lower()] = corpus_for(g)
    if isinstance(final, list):
        for fr in final:
            att, fn = fr.get("attachment_name"), fr.get("filename")
            if att and fn in g_by_file:
                out[os.path.basename(att).lower()] = corpus_for(g_by_file[fn])
    return out


# ---- transcript-coverage gate (anti-over-compression; borrowed from jieyu166's
# rad-workflow "Stage 1 coverage ratio", recalibrated for synthesis notes) ----
# ratio = non-ws chars of the note payload ÷ non-ws chars of the transcript.
# Synthesis legitimately compresses (corpus n=410 segments: median 0.36, p10 0.17,
# min 0.056), so the default floor 0.10 only trips the bottom ~2% — the
# "90-min transcript, one-screen note" collapse shape. WARN, never FAIL: a
# hands-on demo segment can be legitimately thin; the reviewer decides.
COVERAGE_FLOOR = 0.10


def _note_payload_chars(text):
    """Note chars that count toward coverage: strip frontmatter, image embeds,
    wikilink brackets, `(src MM:SS)` timestamps, URLs, then all whitespace."""
    text = re.sub(r"^---\n.*?\n---\n", "", text, count=1, flags=re.S)
    text = re.sub(r"!\[\[[^\]]+\]\]", "", text)
    text = re.sub(r"\[\[([^\]|]+)(\|[^\]]+)?\]\]", r"\1", text)
    text = re.sub(r"`?\([^()]{0,30}\d{1,2}:\d{2}\)`?", "", text)
    text = re.sub(r"https?://\S+", "", text)
    return len(re.sub(r"\s+", "", text))


def _transcript_chars(path):
    try:
        t = open(path, encoding="utf-8").read()
    except OSError:
        return 0
    t = re.sub(r"\[?\d{1,2}:\d{2}(:\d{2})?\]?", "", t)
    return len(re.sub(r"\s+", "", t))


# ---- degenerate-repetition tripwire (companion to the coverage gate: a note
# could pass the char floor by repeating itself — rad-workflow's author hit
# exactly this "重複無意義的文字" failure in batch runs). Two signals, both
# calibrated on 560 accepted notes (410 lecture L3 + 150 vault 52Medicine):
#   line-dup   — a normalized CONTENT line repeated ≥4× (structural repeats are
#                excluded first: table rows, callout headers, backtick citations;
#                unexcluded, legit notes hit 8–14 via those). Corpus after
#                exclusion: 1/560 at 4, none higher. Real loops repeat 10–50×.
#   diversity  — unique char-8grams ÷ total < 0.70 (corpus min 0.735; a looping
#                paragraph lands 0.2–0.4). Only computed on ≥500 payload chars.
REPEAT_LINE_MAX = 3          # >3 (i.e. ≥4 occurrences) → WARN
REPEAT_DIVERSITY_FLOOR = 0.70


def repetition_signals(text):
    """(worst_line, count, diversity, payload_chars) for the note payload."""
    from collections import Counter
    t = re.sub(r"^---\n.*?\n---\n", "", text, count=1, flags=re.S)
    t = re.sub(r"!\[\[[^\]]+\]\]", "", t)
    t = re.sub(r"\[\[([^\]|]+)(\|[^\]]+)?\]\]", r"\1", t)
    t = re.sub(r"`?\([^()]{0,30}\d{1,2}:\d{2}\)`?", "", t)
    t = re.sub(r"https?://\S+", "", t)
    lines = Counter()
    for l in t.split("\n"):
        s = l.strip()
        if "|" in s or s.lstrip("> ").startswith("[!"):
            continue    # table rows / callout headers repeat legitimately
        s = re.sub(r"^[\s>*\-#\d.、]+", "", s).replace("`", "")
        s = re.sub(r"\s+", "", s)
        if len(s) >= 12:
            lines[s] += 1
    worst, count = ("", 0)
    if lines:
        worst, count = lines.most_common(1)[0]
    flat = re.sub(r"\s+", "", t)
    grams = [flat[i:i + 8] for i in range(len(flat) - 7)]
    diversity = (len(set(grams)) / len(grams)) if grams else 1.0
    return worst, count, diversity, len(flat)


def transcript_chars_for(mode, seg_kind, seg_root, grounding_dir, note_path):
    """Locate the transcript(s) this note synthesizes and return their char count.
    lecture mode: <grounding_dir>/transcript.txt (single-lecture pipeline layout).
    lecture-seg L3: the segment's clips' transcript.txt via segments.json+manifest.
    Returns 0 when nothing is found (check is skipped, never guessed)."""
    if mode == "lecture" and grounding_dir:
        return _transcript_chars(os.path.join(grounding_dir, "transcript.txt"))
    if mode == "lecture-seg" and seg_kind == "l3" and seg_root:
        m = re.search(r"L3_seg(\d+)_", os.path.basename(note_path))
        if not m:
            return 0
        segno = int(m.group(1))
        try:
            segs = json.load(open(os.path.join(seg_root, "_intermediate", "seg",
                                               "segments.json"), encoding="utf-8"))
            man = json.load(open(os.path.join(seg_root, "_raw", "manifest.json"),
                                 encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        clips = {c.get("idx"): c for c in man.get("clips", [])}
        src2clip = {os.path.basename(c["src"]): c
                    for c in man.get("clips", []) if c.get("src")}
        s = next((x for x in segs if x.get("seg") == segno), None)
        if not s:
            return 0
        cs = ([clips[i] for i in s.get("clips") or [] if i in clips]
              or [src2clip[f] for f in s.get("files") or [] if f in src2clip])
        return sum(_transcript_chars(os.path.join(seg_root, "clips", c["name"],
                                                  "transcript.txt"))
                   for c in cs if c.get("name"))
    return 0


def caption_neighborhood(lines, idx):
    """Caption text near an embed: adjacent italic line, callout block, or heading."""
    picked = []
    # upward: callout/heading/italic lines directly above (allow blanks to stop)
    j = idx - 1
    while j >= 0 and j >= idx - 4:
        s = lines[j].strip()
        if s.startswith((">", "*", "#")):
            picked.append(s)
            j -= 1
            continue
        break
    # downward: italic/callout line directly below
    j = idx + 1
    while j < len(lines) and j <= idx + 4:
        s = lines[j].strip()
        if s.startswith(("*", ">")):
            picked.append(s)
            j += 1
            continue
        if s == "":
            j += 1
            continue
        break
    return " ".join(picked)


def caption_frame_audit(lines, vault, grounding):
    """Return (n_fail, n_weak, n_ok, n_skip, fail_detail, weak_detail)."""
    fails, weaks, ok, skip = [], [], 0, 0
    for i, l in enumerate(lines):
        for _, inner, parts in img_embeds(l):
            base = os.path.basename(parts[0].strip()).lower()
            frame_toks = grounding.get(base)
            if frame_toks is None:
                continue  # not a lecture frame (e.g. textbook figure) — out of scope
            if len(frame_toks) < 3:
                skip += 1  # frame text too sparse to verify (label-less imaging)
                continue
            cap = caption_neighborhood(lines, i)
            cap_toks = salient_tokens(cap)
            overlap = cap_toks & frame_toks
            if not cap_toks:
                skip += 1
            elif len(overlap) == 0:
                fails.append((base, sorted(list(cap_toks))[:6]))
            elif len(overlap) == 1:
                weaks.append((base, sorted(list(overlap))))
            else:
                ok += 1
    return fails, weaks, ok, skip


def top_section_body(lines, title):
    start = None
    for i, l in enumerate(lines):
        if re.match(r"#\s+" + re.escape(title) + r"\s*$", l):
            start = i
            break
    if start is None:
        return None
    body = []
    for l in lines[start + 1:]:
        if re.match(r"#\s+\S", l) and not l.startswith("##"):
            break
        body.append(l)
    return body


def audit(path, vault, mode, template, grounding_dir=None, min_coverage=COVERAGE_FLOOR):
    t = load(path)
    fm, body = split_frontmatter(t)
    lines = t.split("\n")
    res = []

    def add(level, check, detail=""):
        res.append((level, check, detail))

    # Segmented lecture note kind (l3 / l2 / hub) drives which universal FAILs apply.
    seg_kind = detect_seg_kind(path, t) if mode == "lecture-seg" else None
    seg_root = seg_course_root(path) if mode == "lecture-seg" else None
    # L2/HUB legitimately carry no `# Resource`, no Pearls callout, and (early
    # courses) no frontmatter tags — so those universal FAILs would be false
    # positives on them. Only L3 (the synthesis note) keeps them as FAIL.
    is_seg_l2hub = mode == "lecture-seg" and seg_kind in ("l2", "hub")

    # ---------- Structural (FAIL) ----------
    if not is_seg_l2hub:
        has_resource = bool(re.search(r"^#\s+Resource\s*$", t, re.M))
        # Segmented L3: the WP1-4 spec does NOT list `# Resource` as required
        # (many later courses drop it as redundant with the video metadata header),
        # so surface it as WARN, not a blocking FAIL. All other modes keep FAIL.
        res_level = "PASS" if has_resource else ("WARN" if seg_kind == "l3" else "FAIL")
        add(res_level, "STRUCT # Resource",
            "missing `# Resource`" if not has_resource else "found")

        has_pearls = bool(re.search(r"^>\s*\[!summary\]", t, re.M))
        add("FAIL" if not has_pearls else "PASS", "STRUCT Clinical Pearls callout",
            "missing `> [!summary]` callout" if not has_pearls else "found")

    has_tags = bool(re.search(r"^tags:", fm, re.M)) if fm else False
    # tags: FAIL everywhere except L2/HUB where early courses lack them → WARN.
    tags_level = "WARN" if (is_seg_l2hub and not has_tags) else ("PASS" if has_tags else "FAIL")
    add(tags_level, "STRUCT frontmatter tags",
        "no `tags:` in frontmatter" if not has_tags else "found")

    # ---------- Segmented lecture structure (replaces lecture G1/G2) ----------
    if mode == "lecture-seg":
        if seg_kind == "l3":
            # L3 top heading = "# L3 …" or "# 總整理 …" (both seen in the corpus).
            # NO "# 逐投影片筆記" is required — that was the old-combined-note rule.
            ok_h = bool(re.search(r"^#\s+(L3\b|總整理)", t, re.M))
            add("FAIL" if not ok_h else "PASS", "SEG L3 heading",
                "missing `# L3 …` / `# 總整理 …` top heading" if not ok_h
                else "found")
            has_abbr = bool(re.search(r"縮寫", t))
            add("WARN" if not has_abbr else "PASS", "SEG L3 abbreviation table",
                "no 縮寫表/縮寫對照 — add unless pure demo" if not has_abbr else "found")
            has_hub = bool(re.search(r"課程 Hub|\[\[_HUB_", t))
            add("WARN" if not has_hub else "PASS", "SEG L3 Hub backlink",
                "no `## 課程 Hub` / [[_HUB_…]] backlink (WP2-4 backfill)" if not has_hub
                else "found")
        elif seg_kind == "l2":
            # Every L2 needs a title (FAIL if none). A segNN marker in the title is
            # the convention but some corpus L2 titles omit it → WARN, not FAIL.
            has_h1 = bool(re.search(r"^#\s+\S", t, re.M))
            has_seg = bool(re.search(r"^#\s+.*seg\s*\d+", t, re.M | re.I))
            if not has_h1:
                add("FAIL", "SEG L2 heading", "no top `# …` title heading")
            else:
                add("PASS" if has_seg else "WARN", "SEG L2 heading",
                    "found" if has_seg else "title present but no segNN marker")
            # L2 is a timestamp index: bullets carry `(<src> MM:SS)` — backticks are
            # the spec form but plain `(V1 00:00)` is also used in the corpus.
            n_ts = len(re.findall(r"\(?[^()]*\b\d{1,2}:\d{2}\)", t))
            add("FAIL" if n_ts == 0 else "PASS", "SEG L2 timestamped bullets",
                "no `(src MM:SS)` timestamps — L2 must be a jumpable index"
                if n_ts == 0 else f"{n_ts} timestamped bullets")
            has_hub = bool(re.search(r"課程 Hub|\[\[_HUB_", t))
            add("WARN" if not has_hub else "PASS", "SEG L2 Hub backlink",
                "no `## 課程 Hub` / [[_HUB_…]] backlink (WP2-4 backfill)" if not has_hub
                else "found")
        elif seg_kind == "hub":
            # Section wording varies across the corpus: 分段表 / 各段 Segments /
            # 課程分段索引 / Segmentation — accept any of the 分段/各段 forms.
            has_table = bool(re.search(r"分段|各段|Segmentation|Segments", t))
            add("FAIL" if not has_table else "PASS", "SEG Hub segmentation table",
                "no 分段/各段/Segmentation section" if not has_table else "found")
            # Wikilinks to L2/L3 must resolve to files under the course dir.
            seg_links = re.findall(r"\[\[(L[23]_seg[^\]|]+)", t)
            unresolved = []
            for lk in seg_links:
                stem = lk.strip()
                found = glob.glob(os.path.join(seg_root, "**", stem + ".md"),
                                  recursive=True) if seg_root else []
                if not found:
                    unresolved.append(stem)
            if seg_links:
                add("WARN" if unresolved else "PASS", "SEG Hub wikilinks resolve",
                    f"{len(unresolved)} dead: {unresolved[:4]}" if unresolved
                    else f"{len(seg_links)} L2/L3 links resolve")
        else:
            add("WARN", "SEG note kind",
                "could not classify as L2/L3/Hub from filename or heading")

    # Mode-specific required headings
    if mode == "lecture":
        for name, rx in [("# 總整理", r"^#\s+總整理\s*$"),
                         ("# 逐投影片筆記/逐段筆記", r"^#\s+(逐投影片筆記|逐段筆記)\s*$")]:
            ok = bool(re.search(rx, t, re.M))
            add("FAIL" if not ok else "PASS", f"STRUCT {name}", "missing" if not ok else "found")
    if mode == "textbook" and template in TEMPLATE_HEADINGS:
        missing = [h for h in TEMPLATE_HEADINGS[template]
                   if not re.search(r"^#\s+" + re.escape(h) + r"\b", t, re.M)]
        if TEMPLATE_HEADINGS[template]:
            add("FAIL" if missing else "PASS", f"STRUCT {template} headings",
                f"missing: {missing}" if missing else "all present")

    # ---------- Figures ----------
    refs = [parts[0] for _, _, parts in img_embeds(t)]
    missing_refs = [r for r in refs if not ref_resolves(r, vault, seg_root)]
    add("FAIL" if missing_refs else "PASS", "FIG broken image refs",
        f"{len(missing_refs)} missing: {missing_refs[:5]}" if missing_refs else f"{len(refs)} refs ok")

    embeds = img_embeds(t)
    no_width = [inner[:55] for _, inner, parts in embeds
                if not any(p.strip().isdigit() for p in parts[1:])]
    add("FAIL" if no_width else "PASS", "FIG embed |width present",
        f"{len(no_width)} widthless: {no_width[:4]}" if no_width else f"{len(embeds)} embeds sized")

    n_todo = t.count("[!todo]")
    add("FAIL" if n_todo else "PASS", "FIG leftover [!todo]", f"{n_todo} found" if n_todo else "none")

    # ---------- Terminology ----------
    n_corpse = t.count("屍體")
    add("FAIL" if n_corpse else "PASS", "TERM use 'cadaver' not 屍體", f"{n_corpse}× 屍體" if n_corpse else "ok")

    # rare/lab-specific acronyms not spelled out anywhere (drives both checks below)
    unexpanded = []
    for a in RARE_ACRONYMS:
        if re.search(r"\b" + re.escape(a) + r"\b", t):
            expanded = (bool(re.search(re.escape(a) + r"\s*\(", t))
                        or bool(re.search(r"\(" + re.escape(a) + r"\)", t))
                        or any(a in l and "|" in l for l in lines))
            if not expanded:
                unexpanded.append(a)

    # abbreviation table only nagged when uncommon acronyms are present;
    # common rehab abbreviations (AVN/IR/ER/NWB/ROM…) need no table.
    has_abbr = bool(re.search(r"縮寫|abbreviation", t, re.I)) and "|" in t
    if has_abbr:
        add("PASS", "TERM abbreviation table", "found")
    elif unexpanded:
        add("WARN", "TERM abbreviation table", f"add 縮寫表 for uncommon: {unexpanded}")
    else:
        add("PASS", "TERM abbreviation table", "no uncommon acronyms — table not required")

    add("WARN" if unexpanded else "PASS", "TERM rare acronyms spelled out",
        f"unexpanded: {unexpanded}" if unexpanded else "ok")

    # ---------- Headings & formatting ----------
    bad_head = []
    for i, l in enumerate(lines, 1):
        if re.match(r"#{1,6}\s", l):
            m_bt = re.search(r"`([^`]+)`\s*$", l)
            cite_bt = bool(m_bt) and not re.fullmatch(r"[\d:\-\s~–.]+", m_bt.group(1).strip())
            if cite_bt or re.search(r"(來源|出處|DOI|（s\d+）|\(s\d+\))\s*$", l):
                bad_head.append((i, l.strip()[:60]))
    add("FAIL" if bad_head else "PASS", "FMT no source on heading line",
        f"{len(bad_head)}: {bad_head[:3]}" if bad_head else "ok")

    # (no-HR `---` check removed 2026-05-25 — user relaxed the rule)

    bad_tbl = []
    for i, l in enumerate(lines, 1):
        if l.lstrip().startswith("|"):
            for wl in re.findall(r"\[\[[^\]]*\]\]", l):
                if "|" in wl and r"\|" not in wl:
                    bad_tbl.append((i, wl[:40]))
    add("FAIL" if bad_tbl else "PASS", "FMT escape | in table wikilinks",
        f"{len(bad_tbl)}: {bad_tbl[:3]}" if bad_tbl else "ok")

    # CJK hugging the OUTSIDE of a highlight breaks rendering (的==高亮==的).
    # Inside-touching (==中文==) is correct and required — don't flag it.
    cjk_eq = []
    for i, l in enumerate(lines, 1):
        for m in re.finditer(r"==([^=]+)==", l):
            before = l[m.start() - 1] if m.start() > 0 else " "
            after = l[m.end()] if m.end() < len(l) else " "
            if re.match(CJK, before) or re.match(CJK, after):
                cjk_eq.append(i)
                break
    add("WARN" if cjk_eq else "PASS", "FMT space around == (CJK hugs highlight)",
        f"lines {cjk_eq[:8]}" if cjk_eq else "ok")

    # unescaped comparison/pipe/hash inside ==highlight==
    bad_hl = []
    for i, l in enumerate(lines, 1):
        for hl in re.findall(r"==([^=]+)==", l):
            if re.search(r"(?<!\\)[<>]", hl) or re.search(r"(?<!\\)\|", hl) or re.search(r"(?<!\\)#", hl):
                bad_hl.append((i, hl[:30]))
    add("WARN" if bad_hl else "PASS", "FMT escape <>|# inside ==highlight==",
        f"{len(bad_hl)}: {bad_hl[:3]}" if bad_hl else "ok")

    fences = t.count("```")
    add("WARN" if fences else "PASS", "FMT no code blocks for clinical content",
        f"{fences//2} fenced block(s) — verify not clinical" if fences else "ok")

    # tab indent (not 2/4-space) on bullets — frontmatter YAML lists legitimately use 2-space (CLAUDE.md)
    fm_line_count = fm.count("\n") + 1 if fm else 0
    space_indent = [i for i, l in enumerate(lines, 1)
                    if i > fm_line_count and re.match(r" +[-*] ", l)]
    add("WARN" if space_indent else "PASS", "FMT tab-indent bullets (not spaces)",
        f"{len(space_indent)} space-indented bullets: lines {space_indent[:6]}" if space_indent else "ok")

    # empty / near-empty headings
    heads = []
    for i, l in enumerate(lines):
        m = re.match(r"(#{1,6})\s+(\S.*)$", l)
        if m:
            heads.append((i, len(m.group(1)), m.group(2)))
    empty_heads = []
    for idx, (i, level, htext) in enumerate(heads):
        nxt_i = heads[idx + 1][0] if idx + 1 < len(heads) else len(lines)
        nonblank = [b for b in lines[i + 1:nxt_i] if b.strip()]
        deeper = idx + 1 < len(heads) and heads[idx + 1][1] > level
        if not nonblank and not deeper:
            empty_heads.append((i + 1, htext[:40]))
    add("WARN" if empty_heads else "PASS", "FMT no empty headings",
        f"{len(empty_heads)}: {empty_heads[:3]}" if empty_heads else "ok")

    # ---------- lecture-specific (WARN) ----------
    if mode == "lecture":
        zz = top_section_body(lines, "總整理")
        if zz is not None:
            zz_imgs = len(img_embeds("\n".join(zz)))
            add("WARN" if zz_imgs == 0 else "PASS", "LEC figures in 總整理",
                "0 embeds in 總整理 — verify if imaging lecture (C1)" if zz_imgs == 0 else f"{zz_imgs} embeds")

        galleries, run_imgs, run_start = [], 0, None
        for i, l in enumerate(lines, 1):
            s = l.strip()
            n_img = len(img_embeds(l))
            if n_img:
                run_imgs += n_img
                if run_start is None:
                    run_start = i
            elif s == "" or s.startswith(">"):
                continue
            else:
                if run_imgs >= 3:
                    galleries.append((run_start, run_imgs))
                run_imgs, run_start = 0, None
        if run_imgs >= 3:
            galleries.append((run_start, run_imgs))
        add("WARN" if galleries else "PASS", "LEC no gallery dump",
            f"{galleries[:3]} (≥3 imgs, no prose between)" if galleries else "figures woven")

        long_titles = []
        for i, l in enumerate(lines):
            m = re.match(r">\s*\[!figure\]\s*(.*)$", l)
            if m:
                title = m.group(1).strip()
                nxt = lines[i + 1] if i + 1 < len(lines) else ""
                if len(title) > 45 and re.match(r">\s*!\[\[", nxt):
                    long_titles.append((i + 1, title[:50]))
        add("WARN" if long_titles else "PASS", "LEC fig callout short first line",
            f"{len(long_titles)} long: {long_titles[:3]}" if long_titles else "ok")

        n_speaker = t.count("🗣️")
        add("WARN" if n_speaker == 0 else "PASS", "LEC speaker 🗣️ elaboration",
            "0 🗣️ markers — speaker elaboration likely dropped" if n_speaker == 0 else f"{n_speaker} markers")

        # C3 automation: every embedded frame's caption must share vocabulary with
        # that frame's OCR text / VLM labels. Zero overlap → wrong/swapped figure.
        # Catches gross mismatches (e.g. caption "Lin ABC" on an OMERACT-table frame);
        # subtle vocab-sharing swaps still need the manual C3 read-verify.
        if grounding_dir:
            grounding = load_grounding(grounding_dir)
            if grounding is None:
                add("WARN", "LEC C3 caption↔frame match",
                    f"no slides_grounded.json in {grounding_dir} — cannot auto-verify")
            else:
                fails, weaks, okc, skipc = caption_frame_audit(lines, vault, grounding)
                if fails:
                    add("FAIL", "LEC C3 caption↔frame match",
                        f"{len(fails)} frame(s) whose caption shares NO term with the "
                        f"image OCR/labels — likely wrong/swapped figure: {fails[:4]}")
                elif weaks:
                    add("WARN", "LEC C3 caption↔frame match",
                        f"{len(weaks)} weak (1-token) overlap — verify: {weaks[:4]} "
                        f"(ok={okc}, unverifiable={skipc})")
                else:
                    add("PASS", "LEC C3 caption↔frame match",
                        f"{okc} verified, {skipc} sparse/unverifiable")
        else:
            add("WARN", "LEC C3 caption↔frame match",
                "pass --grounding <project_dir> to auto-verify captions against frame OCR (C3)")

    # ---------- degenerate repetition (all modes) ----------
    worst, count, diversity, payload_n = repetition_signals(t)
    rep_issues = []
    if count > REPEAT_LINE_MAX:
        rep_issues.append(f"line ×{count}: 「{worst[:40]}」")
    if payload_n >= 500 and diversity < REPEAT_DIVERSITY_FLOOR:
        rep_issues.append(f"8-gram diversity {diversity:.2f} < {REPEAT_DIVERSITY_FLOOR}")
    add("WARN" if rep_issues else "PASS", "REP degenerate repetition",
        ("; ".join(rep_issues) + " — looping/filler output; also defeats the "
         "coverage floor by padding") if rep_issues
        else f"max line-dup {count}, diversity {diversity:.2f}")

    # ---------- transcript coverage (lecture + lecture-seg L3) ----------
    if mode in ("lecture", "lecture-seg") and (mode == "lecture" or seg_kind == "l3"):
        tch = transcript_chars_for(mode, seg_kind, seg_root, grounding_dir, path)
        if tch > 0:
            nch = _note_payload_chars(t)
            ratio = nch / tch
            det = f"note {nch:,} / transcript {tch:,} chars = {ratio:.3f} (floor {min_coverage})"
            if ratio < min_coverage:
                add("WARN", "LEC transcript coverage",
                    f"{det} — note may be over-compressed for its transcript; "
                    "review, or accept for demo/hands-on segments")
            else:
                add("PASS", "LEC transcript coverage", det)
        elif mode == "lecture" and grounding_dir:
            add("WARN", "LEC transcript coverage",
                f"no transcript.txt in {grounding_dir} — coverage not verified")

    # ---------- textbook-specific ----------
    if mode == "textbook":
        cit = re.findall(r"\(citation\b", t)
        add("FAIL" if cit else "PASS", "TXT no (citation N) placeholder",
            f"{len(cit)} placeholder(s) — sanitize per Phase 2.5" if cit else "ok")

        if template == "disease":
            bg = top_section_body(lines, "Background")
            if bg is not None:
                bgtxt = "\n".join(bg).lower()
                # accept English or Chinese heading wording
                miss = [pair[0] for pair in (("Prognosis", "預後"), ("Complications", "併發症"))
                        if not any(w.lower() in bgtxt for w in pair)]
                add("WARN" if miss else "PASS", "TXT Prognosis/Complications in Background",
                    f"not found under Background: {miss}" if miss else "ok")
            # Management algorithm first sub-section
            mgmt = top_section_body(lines, "Management")
            if mgmt is not None:
                first_sub = next((l for l in mgmt if l.startswith("## ")), "")
                ok = bool(re.search(r"algorithm|處置原則|⭐", first_sub, re.I))
                add("WARN" if not ok else "PASS", "TXT Management algorithm first",
                    f"first ## is '{first_sub.strip()[:40]}' — should be ⭐ algorithm" if not ok else "ok")

        # CJK bullet length (>50 CJK chars). Skip Resource/Reference — full citations are meant to be long.
        resource_line = next((i for i, l in enumerate(lines, 1)
                              if re.match(r"#\s+Resource\s*$", l)), len(lines) + 1)
        long_bullets = []
        for i, l in enumerate(lines, 1):
            if i >= resource_line:
                break
            s = l.strip()
            if s.startswith(("- ", "* ")) and len(re.findall(CJK, s)) > 50:
                long_bullets.append(i)
        add("WARN" if long_bullets else "PASS", "TXT bullet length (≤50 CJK, excl. Resource)",
            f"{len(long_bullets)} long: lines {long_bullets[:6]}" if long_bullets else "ok")

        # repeated identical table blocks → DRY hint (write once + ![[#section]])
        tblocks, cur = [], []
        for l in lines:
            if l.lstrip().startswith("|"):
                cur.append(re.sub(r"\s+", "", l))
            else:
                if len(cur) >= 3:
                    tblocks.append("\n".join(cur))
                cur = []
        if len(cur) >= 3:
            tblocks.append("\n".join(cur))
        dup_tbl = [b.splitlines()[0][:40] for b, c in Counter(tblocks).items() if c > 1]
        add("WARN" if dup_tbl else "PASS", "TXT no duplicated tables (use ![[#section]])",
            f"{len(dup_tbl)} repeated: {dup_tbl[:3]}" if dup_tbl else "ok")

    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("note")
    ap.add_argument("--mode",
                    choices=["generic", "textbook", "lecture", "lecture-seg"],
                    default="generic")
    ap.add_argument("--template", choices=list(TEMPLATE_HEADINGS), default=None)
    ap.add_argument("--vault",
                    default=os.environ.get("CLAUDE_VAULT_ROOT")
                    or os.path.expanduser("~/Documents/Obsidian/Obsidian"))
    ap.add_argument("--grounding", default=None,
                    help="lecture project dir holding slides_grounded.json/slides_final.json; "
                         "enables C3 caption↔frame auto-verification + transcript coverage")
    ap.add_argument("--min-coverage", type=float, default=COVERAGE_FLOOR,
                    help="transcript-coverage WARN floor (note chars ÷ transcript chars); "
                         f"default {COVERAGE_FLOOR}, calibrated on 410 real segments "
                         "(median 0.36, p10 0.17) — trips only the bottom ~2%%")
    a = ap.parse_args()
    res = audit(a.note, a.vault, a.mode, a.template, a.grounding, a.min_coverage)
    print(f"\n=== AUDIT [{a.mode}{'/' + a.template if a.template else ''}]: {os.path.basename(a.note)} ===")
    fails = warns = 0
    for level, check, detail in res:
        mark = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗"}[level]
        print(f"  {mark} [{level}] {check}: {detail}")
        fails += level == "FAIL"
        warns += level == "WARN"
    print(f"--- {fails} FAIL, {warns} WARN ---")
    if warns:
        print("WARNs are judgment items: consciously accept or fix each, do not ignore.")
    if a.mode == "textbook":
        print("Manual-only (script can't verify): Reference uses `Author year — full citation` (no backticks); "
              "Evidence: sub-bullet on each treatment when --oe; nested tables avoided; co-note content excluded.")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
