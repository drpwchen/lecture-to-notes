# -*- coding: utf-8 -*-
"""Export a finished course (D: work dir, deliverable-first layout) into an I:
deliverable VIEW. ONE webpage at the course root + a same-named support folder:

  <course root>/
    影片筆記整合.html          layout2 two-way-sync viewer (the ONLY webpage, outermost).
                              Video playing -> matching note block auto-highlights & scrolls;
                              click (Vn MM:SS) -> seek; 3 read modes; resizable panes.
    影片筆記整合/               support folder (open as an Obsidian vault):
      媒體/圖片/  friendly-named slides  (NN_<中文主題>_<seq>.jpg)
      媒體/影片/  friendly-named browser-playable clips (NN_<中文主題>.mp4)
      markdown/  友善命名 + 連結改寫 (00 目錄 / NN <中文> 逐字稿/整理稿)
      pdf/       同名 pdf
      _media_map.json  the rename map (debug / re-run stability)
      _timeline.json   embedded TIMELINE, dumped for debugging (viewer reads inline copy)

Design: the D: COURSE dir is the SOURCE OF TRUTH and keeps STABLE machine names
(cNN_frame_XXXX.jpg, L2_segNN_slug.md, 00004.MTS). Friendly Chinese names live ONLY
in this I: view, produced by ONE deterministic rename map (build_media_map) and applied
consistently to: copied slide images, copied+rewritten markdown (filenames + internal
![[...]] image embeds + [[...]] note wikilinks), pdf names, and the HTML <img src> +
TIMELINE media_src. No 筆記成果 wrapper, no .figures, no portable package.

The layout2 viewer CSS/JS live verbatim as assets in scripts/layout2/{viewer.css,viewer.js};
this generator only builds the per-course TIMELINE manifest + the synced note HTML, so a UI
tweak = edit the asset, not the generator.

TIMELINE manifest (embedded in the HTML as `const TIMELINE = {...}`):
  media_parts  - one per source video referenced
  segments     - one per confirmed segment (summary_section_id + replay_section_id)
  chapters     - one per note section (s{seg}-l2 / s{seg}-l3)
  note_blocks  - one per <li> bullet; each carries media_file + start_sec/end_sec +
                 section_kind (transcript_index=L2 / summary_note=L3) + segment_id.
                 start_sec parsed from the bullet's `(Vn MM:SS)`; bullets without a
                 timestamp inherit the previous block's time (time_source=inherited).

Per-segment display fields from _intermediate/seg/segments.json:
  title_zh (中文短標題, fallback slug), region (側欄分組), display_order (側欄排序).
Course name/date: --name / --date, else read from _HUB frontmatter.

Video conversion (auto-selective): web-native clips (mp4+AAC) are used as-is (referenced at
the course root); only non-playable containers or non-AAC audio (==AVCHD .MTS = AC-3 → silent
in browser==) get an FFmpeg pass into 媒體/影片/<friendly>.mp4 (recorded in TIMELINE media_src).
Default re-encodes video to ==H.265 x265 CRF== (screen recordings: 43–51% smaller than H.264
on motion segments, measured 2026-08-03) + audio→AAC. Playback needs HEVC decode support on
the viewing machine (Windows: HEVC Video Extensions + hardware decode) — fine for the
default self-use case; pass `--codec h264` when sharing to machines you can't verify.
`--no-compress` skips re-encoding entirely (copy video, fast, full size). Originals never
modified.

Usage:
  python export_web.py <course_dir> [--out DIR] [--name NAME] [--date DATE]
                       [--author NAME] [--no-compress] [--codec hevc|h264] [--crf N]
                       [--remux] [--no-remux]
  # --out      : override the support-folder path (HTML is written beside it as <name>.html).
  # --author   : name in the page footer. Default = config.yaml `export.author`;
  #              empty (the shipped default) omits the footer entirely.
  # --no-compress : ship video streams as-is (old default). Compression is ON by default.
  # --codec    : hevc (default, smallest) or h264 (universal playback, for sharing).
  # --crf      : quality. Default 24 for hevc, 18 for h264 (≈ visually lossless each).
  # --remux    : force ALL clips through ffmpeg.   --no-remux : ship originals untouched.

Requires pandoc + ffmpeg/ffprobe on PATH (checked up front, before any work).
"""
import json, os, re, sys, subprocess, shutil, html, hashlib, tempfile, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
ASSET_DIR = os.path.join(HERE, "layout2")

sys.path.insert(0, HERE)
from _common import atomic_write_json, load_config, require_binaries

# Versioning — manifest schema is ADDITIVE ONLY (never remove/rename a field) so any
# future viewer reads any past manifest, and any deployed HTML keeps working forever.
# Upgrade path = regenerate from the (standardized) COURSE dir, NOT in-place migration.
SCHEMA_VERSION = 3          # 2: media_src + version stamps · 3: slide_blocks (slide-layer search)
VIEWER_VERSION = "layout2/2026.08.04"

CODEC_LABEL = {"hevc": "H.265", "h264": "H.264"}
CRF_DEFAULT = {"hevc": 24, "h264": 18}   # measured ≈-equivalents, 2026-08-03

# Video compatibility — only formats the browser can't play get -c copy remuxed.
WEB_PLAYABLE_EXT = {".mp4", ".m4v", ".mov", ".webm", ".ogv", ".ogg"}
REMUX_EXT = {".mts", ".m2ts", ".ts", ".avi", ".wmv", ".mkv", ".flv", ".mpg", ".mpeg", ".3gp"}


def needs_remux(fn):
    return os.path.splitext(fn)[1].lower() in REMUX_EXT

def build_argparser():
    ap = argparse.ArgumentParser()
    ap.add_argument("course")
    ap.add_argument("--out", default=None, help="override support-folder path (HTML written beside it as <name>.html)")
    ap.add_argument("--name", default=None)
    ap.add_argument("--date", default=None)
    ap.add_argument("--author", default=None,
                    help="name for the page footer (default: config.yaml export.author; "
                         "empty = no footer)")
    ap.add_argument("--remux", action="store_true", help="force-process ALL clips to mp4 (default: auto — only non-web-playable formats)")
    ap.add_argument("--no-remux", action="store_true", help="never process (even .MTS); deploy original filenames as-is")
    ap.add_argument("--compress", action="store_true",
                    help="(default since 2026-08-03; kept for backward compatibility)")
    ap.add_argument("--no-compress", action="store_true",
                    help="ship video streams as-is (copy, fast, full size) — the pre-2026-08 behavior")
    ap.add_argument("--codec", choices=["hevc", "h264"], default="hevc",
                    help="compression codec. hevc (x265, default): 43-51%% smaller on motion "
                         "segments but playback needs HEVC decode on the viewing machine. "
                         "h264 (x264): universal playback — use when sharing to machines "
                         "you can't verify.")
    ap.add_argument("--crf", type=int, default=None,
                    help="quality for compression. Default 24 for hevc, 18 for h264 "
                         "(≈ visually lossless each; measured equivalents 2026-08-03).")
    return ap


FFPROBE = "ffprobe"
WEB_AUDIO = {"aac", "mp3", "opus", "vorbis"}
PANDOC, FFMPEG = "pandoc", "ffmpeg"


class ProbeError(RuntimeError):
    """ffprobe could not be run, or failed on this file."""


def audio_codec(path):
    """Audio codec name, "" when the file genuinely has NO audio stream.

    A probe FAILURE raises instead of returning "": the two were indistinguishable
    before, so a broken/absent ffprobe read as "not a web audio codec" for every
    file and quietly re-encoded the entire course.
    """
    try:
        r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "a:0",
                            "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
                           capture_output=True, text=True)
    except OSError as e:
        raise ProbeError(f"cannot run {FFPROBE}: {e}") from e
    if r.returncode != 0:
        raise ProbeError(f"ffprobe failed on {os.path.basename(path)}: "
                         f"{(r.stderr or '').strip()[:200]}")
    return r.stdout.strip().lower()


def audio_codec_or_unknown(path, problems):
    """audio_codec() that records probe failures instead of raising.

    Returns None on failure — callers treat unknown as "not known-playable" (the
    conservative choice) but the run reports every file it could not read.
    """
    try:
        return audio_codec(path)
    except ProbeError as e:
        problems.append(str(e))
        return None


MEDIA_REL = {}


def media_abspath(fn):
    """Absolute path of a source clip, honouring the manifest's subfolder layout.

    `segments.json` records `files` as bare basenames, but a manifest `src` may be
    a path *relative to the course root* (`Video\\MVI_8929.MP4`, `Lec-錄音\\x.mp3`).
    Joining the basename flatly then misses every clip and the export silently
    ships with `video=0/N` (hit on US-neck-dysyonia 2026-08-02, whose source dir
    keeps videos in `Video/` and audio in `Lec-錄音/`). MEDIA_REL is built from
    the manifest in main(); an unknown name falls back to the flat join, so
    flat-layout courses behave exactly as before.
    """
    return os.path.join(COURSE_ROOT, MEDIA_REL.get(fn, fn))


def resolve_course_root(manifest, manifest_path, course):
    """Pick the directory that actually holds the source videos.

    The manifest records an absolute path from the machine that built it (a D:/I:
    drive here). Trusting it blindly on another machine either crashes or, worse,
    writes the deliverable into whatever happens to exist at that path. Try the
    recorded path, then the manifest's own parent, then the course dir, and prefer
    a candidate that really contains the referenced clips.
    """
    recorded = manifest.get("path", "") or ""
    wanted = [c.get("src") for c in (manifest.get("clips") or []) if c.get("src")]
    candidates = [(recorded, "manifest path")]
    for path, why in ((os.path.dirname(os.path.abspath(manifest_path)), "manifest's own directory"),
                      (os.path.abspath(course), "course directory")):
        if path not in [c for c, _ in candidates]:
            candidates.append((path, why))

    dirs = [(p, why) for p, why in candidates if p and os.path.isdir(p)]
    for p, why in dirs:
        if not wanted or any(os.path.exists(os.path.join(p, fn)) for fn in wanted):
            if why != "manifest path":
                print(f"[course-root] manifest path {recorded!r} unusable; "
                      f"using the {why}: {p}")
            return p
    if dirs:
        p, why = dirs[0]
        print(f"WARNING: no candidate directory contains the manifest's clips; "
              f"falling back to the {why}: {p}", file=sys.stderr)
        return p
    print(f"ERROR: cannot locate the course's source-video directory.\n"
          f"  manifest 'path' = {recorded!r} (not a directory here)\n"
          f"  Tried: {', '.join(p for p, _ in candidates if p)}\n"
          "  Fix the 'path' field in _raw/manifest.json, or move the course dir "
          "next to its videos.", file=sys.stderr)
    sys.exit(2)


def pick_hub(course):
    """The single _HUB_ note. 0 -> exit 2; >1 -> first by name, with a warning."""
    hubs = sorted(f for f in os.listdir(course) if f.startswith("_HUB_"))
    if not hubs:
        print(f"ERROR: no _HUB_*.md found in {course} — this is the course index "
              "note the export is built around. Generate it first (see "
              "reference/segmented-mode.md), or point at the right course dir.",
              file=sys.stderr)
        sys.exit(2)
    if len(hubs) > 1:
        print(f"WARNING: {len(hubs)} _HUB_ files in {course} ({', '.join(hubs)}); "
              f"using {hubs[0]}", file=sys.stderr)
    return hubs[0]


# WP3: course-type badge on the home view. course_type is stamped into the manifest
# by the batch control plane's course-type tooling (5-type taxonomy) — keep this
# badge map in sync with it. Missing course_type -> no badge (older manifests).
_TYPE_BADGE = {
    "didactic": ("投影片教學", "#2c7a7b"), "us-demo": ("超音波示範", "#1f6f54"),
    "workshop-paired": ("工作坊", "#c0392b"), "case-discussion": ("案例討論", "#b5651d"),
    "conference": ("研討會", "#8e44ad"),
}


def type_badge_html():
    ct = MANIFEST.get("course_type")
    if not ct:
        return ""
    label, color = _TYPE_BADGE.get(ct, (ct, "#1f6f54"))
    cl = COURSE_NAME.lower()
    if ct == "didactic":
        if "mri" in cl:
            label, color = "MRI 判讀", "#5b6ee1"
        elif re.search(r"x-?ray|x光", cl):
            label, color = "X 光判讀", "#8a6d3b"
    return f'<span class="type-badge" style="background:{color}">{esc(label)}</span>'

def normalize_segments(segs, course, manifest, hub_text):
    """Input-contract adapter: backfill MISSING fields so old/variant courses (e.g. the
    2016 schema with only clips/no files/no slug/no display fields) feed the generator
    unchanged. Only fills absent keys — never overwrites hand-tuned values. Returns
    (segs, changed)."""
    idx2src = {c["idx"]: c.get("src", "") for c in manifest.get("clips", [])}
    l2dir = os.path.join(course, "L2")
    slug_by_seg = {}
    if os.path.isdir(l2dir):
        for f in os.listdir(l2dir):
            m = re.match(r'L2_seg(\d+)_(.+)\.md$', f)
            if m:
                slug_by_seg[int(m.group(1))] = m.group(2)
    # best-effort 主題 from the _HUB segmentation table:  | NN | … | 講者 | 主題 | 類型 |
    topic_by_seg = {}
    for m in re.finditer(r'^\|\s*0?(\d+)\s*\|[^\n]*?\|([^|\n]+)\|[^|\n]*\|\s*$', hub_text, flags=re.M):
        try:
            topic_by_seg[int(m.group(1))] = m.group(2).strip()
        except ValueError:
            pass
    changed = False
    for s in segs:
        n = s["seg"]
        if not s.get("files") and s.get("clips") is not None:
            s["files"] = [idx2src[c] for c in s["clips"] if c in idx2src]; changed = True
        if not s.get("slug"):
            s["slug"] = slug_by_seg.get(n, f"seg{n:02d}"); changed = True
        if "make_l3" not in s:
            s["make_l3"] = os.path.exists(os.path.join(course, "L3", f"L3_seg{n:02d}_{s['slug']}.md")); changed = True
        if not s.get("title_zh"):
            s["title_zh"] = (topic_by_seg.get(n) or s.get("topic") or s["slug"])[:40]; changed = True
        if "region" not in s:
            s["region"] = ""; changed = True
        if "display_order" not in s:
            s["display_order"] = n; changed = True
    return segs, changed


esc = lambda s: html.escape(s)
escq = lambda s: html.escape(s, quote=True)


def disp(s):  return s.get('title_zh') or s.get('slug')


def safe_fname(s):
    """Filesystem-safe filename: map path-illegal chars to fullwidth/dash (keep CJK readable)."""
    for a, b in (("/", "／"), ("\\", "＼"), (":", "："), ("*", "＊"), ("?", "？"),
                 ('"', "＂"), ("<", "＜"), (">", "＞"), ("|", "｜")):
        s = s.replace(a, b)
    return s.strip()
def order(s): return s.get('display_order', s['seg'])
def region(s): return s.get('region', '')


# ORDER / RANK are built in main(). Continuous 1..N display rank in play order:
# WP2-2 (2026-07-09) — the DISPLAYED segment number must be this rank, NOT the raw
# `seg` id, since segments can carry non-monotonic ids after re-segmentation (e.g.
# 20181028: seg ids …11,16,12,13,14,17,15), which made the home view number jump
# around even though the ORDER itself was correct. `segid(seg)` (= s{seg}) stays the
# raw id as the stable nav key; only the label uses RANK.


def zh_slug(s):
    """Compact, filesystem-safe slug from a 中文 title for media filenames: drop spaces,
    middots, arrows, dashes and path-illegal chars; keep CJK. Capped for sane filenames."""
    s = safe_fname(s or "")
    s = re.sub(r'[\s　·・.,，。、:：;；/／＼\\|｜→←↔—–\-_(){}\[\]【】（）「」『』]+', '', s)
    return (s[:16] or "seg")


# WP1-1 (2026-07-09): match ANY embedded raster image, not just cNN_frame_XXXX.jpg.
# Previously only video-frame embeds were mapped/copied/rewritten; PDF-page embeds
# (pdf_caseN_pX.jpg), deck pages (page_NN.jpg), etc. fell through as literal ![[...]]
# text and rendered as broken images with `images=0`. Group 1 = the embedded
# filename (with extension); the optional `|123` is Obsidian's display-width spec.
EMBED_RE = re.compile(r'!\[\[([^\]|]+?\.(?:jpg|jpeg|png|webp))(?:\|\d+)?\]\]', re.I)


def build_media_map(course, order, by):
    """ONE deterministic rename map: D: machine names -> I: friendly 中文 names.
      images : {<embed filename> -> "NN_<zh_slug(title)>_<seq><ext>"}  (owned by first
               embedding segment; boundary frames keep the earlier segment's name)
      videos : {<original filename> -> "NN_<zh_slug(first-using seg title)>.mp4"}  (NN = play
               order across the unique referenced files)
      notes  : {L2_segNN_slug -> "NN <title> 逐字稿", L3_... -> "NN <title> 整理稿",
                _HUB_<course> -> "00 目錄"}
      owners : {<embed filename> -> seg}  which segment's note embedded it first;
               used to disambiguate identical basenames across segments.
    """
    rank = {seg: i + 1 for i, seg in enumerate(order)}
    images, videos, notes, owners = {}, {}, {}, {}
    for seg in order:
        s = by[seg]; nn = rank[seg]; title = disp(s); seq = 0
        for kind in (['l2', 'l3'] if s.get('make_l3', True) else ['l2']):
            K = 'L2' if kind == 'l2' else 'L3'
            base = f"{K}_seg{seg:02d}_{s['slug']}"
            srcf = os.path.join(course, K, base + ".md")
            if not os.path.exists(srcf):
                continue
            kindzh = "逐字稿" if kind == 'l2' else "整理稿"
            notes[base] = safe_fname(f"{nn:02d} {title} {kindzh}")
            with open(srcf, encoding="utf-8") as fh:
                md = fh.read()
            for m in EMBED_RE.finditer(md):
                fr = m.group(1)
                if fr in images:
                    continue  # already owned by an earlier segment (boundary clip)
                seq += 1
                ext = os.path.splitext(fr)[1].lower() or ".jpg"
                images[fr] = f"{nn:02d}_{zh_slug(title)}_{seq:02d}{ext}"
                owners[fr] = seg
    notes[os.path.splitext(HUB)[0]] = "00 目錄"
    # Friendly video names are titled after whichever segment "owns" (first
    # references) each file. The 全場總整理 overview segment sorts first
    # (display_order 0) but its files[] is the union of every clip in the
    # course — if it claimed ownership here, EVERY video in the course would
    # get renamed "NN_全場總整理.mp4", breaking the resumable-cache match
    # (output_ok looks for the OLD name) on any re-export where the raw
    # source no longer exists to re-encode under the new name (hit
    # 2026-08-04 on 3 already-delivered courses: media_src came back empty,
    # orphaning the correctly-named mp4s still sitting on disk). Content
    # segments claim ownership first; the overview only claims a file none
    # of them reference (shouldn't normally happen).
    firstseg, seen = {}, []
    content_order = [seg for seg in order if by[seg].get('slug') != 'overview']
    overview_order = [seg for seg in order if by[seg].get('slug') == 'overview']
    for seg in content_order + overview_order:
        for fn in by[seg].get('files', []):
            if fn not in firstseg:
                firstseg[fn] = seg; seen.append(fn)
    for i, fn in enumerate(seen, 1):
        videos[fn] = f"{i:02d}_{zh_slug(disp(by[firstseg[fn]]))}.mp4"
    return images, videos, notes, owners


def build_note_by_kindseg(note_map):
    """seg-number index (one L2 + one L3 per seg) so cross-links survive a wrong slug
    suffix in the source note (e.g. `[[L3_seg11_elbow-medial-ulnar-n]]` when the file
    is `...-nerve`)."""
    out = {}
    for b, fr in note_map.items():
        m = re.match(r'(L[23])_seg(\d+)_', b)
        if m:
            out[(m.group(1), int(m.group(2)))] = fr
    return out


def vmap_for(seg): return {f"V{i+1}": fn for i, fn in enumerate(by[seg]['files'])}
def secs(m, s): return int(m) * 60 + int(s)
def fmt_clock(t): return f"{int(t)//60:02d}:{int(t)%60:02d}"
def sectionid(kind, seg): return f"s{seg}-{'l2' if kind == 'l2' else 'l3'}"
def segid(seg): return f"s{seg}"


def resolve_link(t):
    t = t.strip()
    if t.startswith("_HUB"):
        return None  # hub not a panel in layout2
    m = re.match(r'L([23])_seg(\d+)_', t)
    return f"s{int(m.group(2))}-l{m.group(1)}" if m else None


def preprocess(md, seg):
    """Markdown -> HTML-ready markdown (frontmatter/callout/figure/highlight/wikilink/timestamp)."""
    md = re.sub(r'^---\n.*?\n---\n', '', md, count=1, flags=re.S)
    # drop production-only L2/L3 tier prefix from displayed headings ("# L3 總整理 …" -> "# 總整理 …")
    md = re.sub(r'^(#{1,3})\s*L[23]\s+', r'\1 ', md, flags=re.M)
    # strip the trailing "## 課程 Hub\n[[_HUB…]]" backlink section — not useful inside the HTML viewer
    md = re.sub(r'(?:\n-{3,}\s*)?\n#{1,3}\s*課程 Hub\b.*$', '\n', md, flags=re.S)
    vm = vmap_for(seg) if seg else {}
    # callout header -> bold line; append a blank quote line so a following "> - bullet" list is
    # NOT lazily absorbed into the title paragraph (pandoc would otherwise render bullets as inline text)
    md = re.sub(r'^> \[!(\w+)\]\s*(.*)$',
                lambda m: f"> **【{m.group(1).upper()}】{(' ' + m.group(2)) if m.group(2) else ''}**\n>",
                md, flags=re.M)
    md = EMBED_RE.sub(
                lambda m: f'<img class="fig" src="{IMG_URL_PREFIX}{IMG_MAP.get(m.group(1), m.group(1))}" loading="lazy">', md)
    # inline prose mentions of a frame-id (e.g. "見 `c04_frame_0003.jpg`") -> friendly name
    md = re.sub(r'c\d+_frame_\d+(?:\.jpg)?',
                lambda m: IMG_MAP.get(m.group(0) if m.group(0).endswith('.jpg') else m.group(0) + '.jpg', m.group(0)), md)
    md = re.sub(r'==([^=]+)==', r'<mark>\1</mark>', md)

    def ts(m):
        # group(1)=Vn (via V-map) OR group(2)=raw filename (old courses, e.g. 00004.MTS)
        # group(3)=optional `⚠️approx` marker (approximate time — still jumpable, but flagged)
        v, fn_raw, approx, mm, ss = (m.group(1), m.group(2), m.group(3),
                                     m.group(4), m.group(5))
        if v:
            fn = vm.get(v); label = v
        else:
            fn = fn_raw; label = os.path.splitext(fn_raw)[0]
        if not fn:
            return m.group(0)
        # Approx timestamps used to NOT match the old regex (the `⚠️approx` token sat
        # between the code and mm:ss) so they rendered as dead plain text. Now they become
        # clickable too, but carry data-time-source="approx" (dashed pill via viewer.css)
        # and a `~` prefix so the reader knows the time is only approximate.
        extra = ' data-time-source="approx" title="時間為約略值（agent 推估）"' if approx else ''
        clk = "~" if approx else ""
        return (f'<a class="ts" data-file="{escq(fn)}" data-t="{secs(mm, ss)}"{extra}>'
                f'▶ {label} {clk}{mm}:{ss}</a>')

    # supports `(V1 00:01)` (V-map), `(00004.MTS 03:12)` (raw filename, legacy), and the
    # approx variant `(V1 ⚠️approx 01:30)` / `(V1 approx 01:30)` (group 3 captures the marker)
    # Audio extensions belong here too: an audio-only session (a recorder file with
    # photographed slides) cites `(錄音-240526_1119.mp3 03:23)`, and while those were
    # missing from this list the citation stayed dead plain text — no error anywhere,
    # since a non-match is indistinguishable from prose.
    md = re.sub(r'`?\((?:(V\d+)|([^()]+?\.(?:MTS|mts|mp4|MP4|mov|MOV|m2ts|M2TS|avi|AVI'
                r'|mp3|MP3|m4a|M4A|wav|WAV|aac|AAC)))'
                r' +(?:(⚠️?\s*approx|approx)\s*)?(\d+):(\d{2})\)`?', ts, md)

    def wl(m):
        sid = resolve_link(m.group(1)); a = m.group(2) or m.group(1)
        return f'<a class="nav" data-nav="{sid}">{esc(a)}</a>' if sid else esc(a)

    md = re.sub(r'\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|([^\]]+))?\]\]', wl, md)
    return md


def md2html(md, seg=None):
    # A unique temp file per fragment: the old fixed <tmp>/frag.md meant two
    # concurrent exports overwrote each other's input (and, on a shared temp dir,
    # anyone could pre-create that name).
    fd, tmp = tempfile.mkstemp(prefix="export_web_frag_", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(preprocess(md, seg))
        r = subprocess.run([PANDOC, "-f", "markdown+raw_html-yaml_metadata_block", "-t", "html",
                            "--wrap=none", tmp], capture_output=True, text=True, encoding="utf-8")
    finally:
        try:
            os.remove(tmp)  # our own scratch file, created two lines above
        except OSError:
            pass
    if r.returncode != 0:
        print(f"WARNING: pandoc failed on a fragment: "
              f"{(r.stderr or '').strip()[:300]}", file=sys.stderr)
    return r.stdout


def strip_tags(s):
    s = re.sub(r'<[^>]+>', '', s)
    s = html.unescape(s)
    return re.sub(r'\s+', ' ', s).strip()


# ---------------------------------------------------------------------------
# Block-sync: wrap every <li> in a rendered section with sync metadata and emit
# the matching note_blocks. start_sec from the bullet's own a.ts; bullets without
# one inherit the previous block (time_source=inherited).
# ---------------------------------------------------------------------------

def wrap_section(frag, seg, kind):
    """Inject data-sync-block attrs into each <li>; return (html, blocks). media_file is
    always the ORIGINAL source filename (stable key); remux indirection lives in media_src."""
    sid = sectionid(kind, seg)
    section_kind = "transcript_index" if kind == 'l2' else "summary_note"
    blocks = []
    state = {"n": 0, "file": None, "start": None, "from": None}

    def wrap(m):
        pos = m.end()
        nxt = re.search(r'<li[ >]|<ul|<ol|</li>', frag[pos:])
        window = frag[pos: pos + (nxt.start() if nxt else len(frag) - pos)]
        tsm = re.search(r'<a class="ts" data-file="([^"]+)" data-t="(\d+)"', window)
        if tsm:
            f0 = html.unescape(tsm.group(1)); st = int(tsm.group(2))
            src = "precise"; inh = None
            state["file"], state["start"], state["from"] = f0, st, None
        elif state["file"] is not None:
            f0, st = state["file"], state["start"]
            src = "inherited"; inh = state["from"]
        else:
            return m.group(0)  # bullet before any timestamp -> not synced
        state["n"] += 1
        bid = f"{sid}-b{state['n']:04d}"
        if src == "precise":
            state["from"] = bid
        blocks.append({
            "block_id": bid, "section_id": sid, "chapter_id": sid, "segment_id": segid(seg),
            "media_file": f0, "start_sec": float(st), "end_sec": None,
            "time_source": src, "inherited_from_block_id": inh,
            "text": strip_tags(window), "section_kind": section_kind, "source_order": state["n"],
        })
        a = (f' data-sync-block="{bid}" data-section-id="{sid}" data-chapter-id="{sid}"'
             f' data-segment-id="{segid(seg)}" data-file="{escq(f0)}" data-start="{st}"'
             f' data-end="" data-time-source="{src}"')
        return f'<li{a}>'

    return re.sub(r'<li>', wrap, frag), blocks


def move_ts_to_front(frag):
    """For L2 transcript bullets, move the first `<a class="ts">…</a>` to the START of each
    <li> content so the clickable timestamp sits at the sentence's left edge (easier to tap).
    Transcript lists are flat; skip any <li> that contains a nested list to stay safe. Only the
    DISPLAY order changes — sync blocks/text are untouched."""
    def f(m):
        open_tag, body = m.group(1), m.group(2)
        if '<ul' in body or '<ol' in body:
            return m.group(0)
        tsm = re.search(r'\s*<a class="ts".*?</a>', body, flags=re.S)
        if not tsm:
            return m.group(0)
        ts = tsm.group(0).strip()
        rest = (body[:tsm.start()] + body[tsm.end():]).strip()
        return f'{open_tag}{ts} {rest}'
    return re.sub(r'(<li[^>]*>)(.*?)(?=</li>)', f, frag, flags=re.S)


def compute_end(blocks):
    """end_sec = next block's start in the same media_file within the same section; last +45s cap."""
    by_key = {}
    for b in blocks:
        by_key.setdefault((b["section_id"], b["media_file"]), []).append(b)
    for grp in by_key.values():
        grp.sort(key=lambda b: b["start_sec"])
        for i, b in enumerate(grp):
            b["end_sec"] = grp[i + 1]["start_sec"] if i + 1 < len(grp) else b["start_sec"] + 45.0
    # write data-end back is optional; the JS reads end_sec from TIMELINE, not the DOM


# ---------------------------------------------------------------------------
# Render every section once (raw pandoc frag), cached; wrapping is mapper-specific.
# raw_frag / sec_meta / present are filled by render_sections() from main().
# ---------------------------------------------------------------------------
raw_frag = {}          # sid -> raw pandoc html
sec_meta = {}          # sid -> (seg, kind)
present = set()        # sids that exist


def render_sections():
    for seg in ORDER:
        s = by[seg]
        for kind in (['l2', 'l3'] if s.get('make_l3', True) else ['l2']):
            K = 'L2' if kind == 'l2' else 'L3'
            srcf = os.path.join(COURSE, K, f"{K}_seg{seg:02d}_{s['slug']}.md")
            if not os.path.exists(srcf):
                continue
            sid = sectionid(kind, seg)
            with open(srcf, encoding="utf-8") as fh:
                raw_frag[sid] = md2html(fh.read(), seg)
            sec_meta[sid] = (seg, kind)
            present.add(sid)


# Slide-search filters (calibrated 2026-08-03 on 3 real courses: a hands-on
# workshop must yield ~nothing — its VLM summaries are per-scene "Instructor
# palpating…" noise and its OCR is ultrasound-machine UI overlay — while a
# didactic course must keep nearly every text slide):
#   OCR path    — needs ≥14 "wordy" chars (letter-words/CJK runs, so device
#                 overlays like "ML6-15 FR 9.0" don't qualify) AND a frame not
#                 typed as a live scene. Workshop 2598→82, didactic 241/328 kept.
#   VLM path    — one_line_summary only for deliberate information graphics
#                 (table/flowchart/diagram/…), never for scene descriptions.
_SLIDE_INFO_GFX = {"table", "flowchart", "diagram", "algorithm", "chart"}
_SLIDE_SCENE = {"ultrasound", "decorative"}


def _wordy_chars(t):
    """Chars in letter-words (≥3) and CJK runs (≥2) — the 'reads like language'
    signal that machine-UI OCR fragments lack."""
    n = sum(len(w) for w in re.findall(r"[A-Za-z]{3,}", t))
    n += sum(len(r) for r in re.findall(r"[㐀-鿿]{2,}", t))
    return n


def _slide_text(g):
    """Search corpus for one grounded slide, or '' when the frame is a live scene /
    carries no language: real OCR text (Stage B2 clean_text over Stage B
    quick_text) for text-bearing slides, VLM one-line summary for labeled
    information graphics."""
    ocr = g.get("ocr") or {}
    vlm = g.get("vlm_signals") or {}
    cts = set(vlm.get("content_type") or [])
    text = str(ocr.get("clean_text") or "").strip() or str(ocr.get("quick_text") or "").strip()
    if text and _wordy_chars(text) >= 14 and not (cts & _SLIDE_SCENE):
        pass
    elif cts & _SLIDE_INFO_GFX:
        text = str(vlm.get("one_line_summary") or "").strip()
    else:
        return ""
    return re.sub(r"\s+", " ", text).strip()[:240]


def build_slide_blocks():
    """Slide-layer search entries from clips/*/slides_grounded.json (Stage E output).

    The transcript layer only knows what was SAID; terms that appear solely ON a
    slide (a table header, a classification name) are invisible to note-block
    search. This walks every manifest clip's grounding file and emits one entry
    per canonical slide with any text signal. No image is copied: a search hit
    jumps the player to the slide's own display window, so the slide is on screen
    in the video itself. Courses without grounding files (pre-pipeline imports)
    yield [] and nothing changes.
    """
    out = []
    for c in (MANIFEST.get("clips") or []):
        src, name = c.get("src"), c.get("name")
        if not src or not name:
            continue
        gpath = os.path.join(COURSE, "clips", name, "slides_grounded.json")
        if not os.path.isfile(gpath):
            continue
        try:
            with open(gpath, encoding="utf-8") as fh:
                grounded = json.load(fh)
        except (OSError, ValueError) as e:
            print(f"WARNING: unreadable {gpath}: {e} — slide search skips this clip",
                  file=sys.stderr)
            continue
        if isinstance(grounded, dict):
            grounded = next((v for v in grounded.values() if isinstance(v, list)), [])
        media_file = os.path.basename(src)
        idx = c.get("idx", 0)
        prev_text = None
        for g in grounded:
            if not isinstance(g, dict):
                continue
            if not (g.get("dedup") or {}).get("is_canonical", True):
                continue
            text = _slide_text(g)
            if not text or text == prev_text:   # scene-dedup survivors can still repeat text
                continue
            prev_text = text
            out.append({
                "media_file": media_file,
                "start_sec": float(g.get("timestamp_start") or 0),
                "end_sec": float(g.get("timestamp_end") or 0) or None,
                "frame": f"c{idx:02d}_{g.get('filename', '')}",
                "text": text,
            })
    out.sort(key=lambda s: (s["media_file"], s["start_sec"]))
    return out


def build_timeline(src_resolver):
    """Return (sections_wrapped: {sid:html}, timeline_dict).
    src_resolver(filename) -> relative URL for that source video, or None to fall back to
    media_root. Used to point remuxed clips at 媒體/影片/<friendly>.mp4 while keeping
    media_file = the original filename."""
    sections_wrapped, all_blocks, chapters = {}, [], []
    for sid, (seg, kind) in sec_meta.items():
        h, blocks = wrap_section(raw_frag[sid], seg, kind)
        if kind == 'l2':
            h = move_ts_to_front(h)
        sections_wrapped[sid] = h
        all_blocks.extend(blocks)
    compute_end(all_blocks)
    # chapters: one per section
    for sid, (seg, kind) in sec_meta.items():
        bl = [b for b in all_blocks if b["section_id"] == sid]
        if not bl:
            continue
        st = min(b["start_sec"] for b in bl); en = max(b["end_sec"] for b in bl)
        chapters.append({
            "chapter_id": sid, "section_id": sid,
            "title": f"{'逐字稿索引' if kind == 'l2' else '總整理'} — seg{seg:02d} {disp(by[seg])}",
            "group": region(by[seg]), "section_kind": ("transcript_index" if kind == 'l2' else "summary_note"),
            "source_order": order(by[seg]), "media_file": bl[0]["media_file"],
            "start_sec": st, "end_sec": en,
            "first_block_id": bl[0]["block_id"], "block_count": len(bl),
        })
    # media_parts (original filenames) + media_src (per-file URL when resolver overrides)
    seen, media_parts, media_src = set(), [], {}
    for seg in ORDER:
        for fn in by[seg].get('files', []):
            if fn in seen:
                continue
            seen.add(fn)
            url = src_resolver(fn)
            media_parts.append({"file": fn, "relative_path": url or fn, "exists": True, "duration_sec": None})
            if url:
                media_src[fn] = url
    # segments
    segments = []
    for seg in ORDER:
        s = by[seg]
        l2 = sectionid('l2', seg); l3 = sectionid('l3', seg)
        rep = l2 if l2 in present else (l3 if l3 in present else None)
        if not rep:
            continue
        summ = l3 if (s.get('make_l3', True) and l3 in present) else ""
        anchor = next((b for b in all_blocks if b["section_id"] == (summ or rep)), None)
        if anchor is None:
            anchor = next((b for b in all_blocks if b["section_id"] == rep), None)
        rep_ch = next((c for c in chapters if c["chapter_id"] == rep), None)
        sids = [x for x in (summ, rep) if x]
        # total duration = Σ over each media_file of (max end − min start) across this seg's blocks
        # (L2+L3 union per file) → correctly totals a seg spanning V1/V3/V4. NOTE: derived from
        # timestamped bullets only, so a clip with no speech/no timestamp is NOT counted (we accept
        # this minor under-count; using whole-file ffprobe would OVER-count files shared by 2 segs).
        seg_blocks = [b for b in all_blocks if b["segment_id"] == segid(seg)]
        _bf = {}
        for b in seg_blocks:
            _bf.setdefault(b["media_file"], []).append(b)
        duration_sec = sum(max(x["end_sec"] for x in g) - min(x["start_sec"] for x in g)
                           for g in _bf.values()) if _bf else 0.0
        segments.append({
            "segment_id": segid(seg), "segment_number": f"seg{RANK[seg]:02d}", "title": disp(s),
            "group": region(s), "group_label": region(s).upper(), "source_order": order(s),
            "media_file": anchor["media_file"] if anchor else (rep_ch["media_file"] if rep_ch else ""),
            "start_sec": anchor["start_sec"] if anchor else (rep_ch["start_sec"] if rep_ch else 0.0),
            "end_sec": rep_ch["end_sec"] if rep_ch else 0.0,
            "duration_sec": round(duration_sec, 1),
            "media_files": list(by[seg].get('files', [])),
            "summary_section_id": summ, "replay_section_id": rep,
            "summary_first_block_id": next((b["block_id"] for b in all_blocks if b["section_id"] == summ), None) if summ else None,
            "replay_first_block_id": next((b["block_id"] for b in all_blocks if b["section_id"] == rep), None),
            "section_ids": sids,
        })
    timeline = {
        "schema_version": SCHEMA_VERSION, "viewer_version": VIEWER_VERSION,
        "profile": "interactive_html", "layout": "segment-drawer-right",
        "course": COURSE_NAME, "date": COURSE_DATE,
        "media_root": ".", "media_root_relative_to_html": ".", "media_src": media_src,
        "media_parts": media_parts, "chapters": chapters, "segments": segments,
        "note_blocks": all_blocks, "slide_blocks": build_slide_blocks(),
        "jump_links": [], "package_profile": "interactive_html",
    }
    return sections_wrapped, timeline


# ---------------------------------------------------------------------------
# Page assembly
# ---------------------------------------------------------------------------
def read_asset(name):
    path = os.path.join(ASSET_DIR, name)
    if not os.path.isfile(path):
        print(f"ERROR: viewer asset missing: {path}\n"
              "  The layout2 viewer assets ship next to this script "
              "(scripts/layout2/); the export cannot build a page without them.",
              file=sys.stderr)
        sys.exit(2)
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def render_panels(sections_wrapped, kind):
    cls = "summary-section" if kind == 'l3' else "replay-section"
    skind = "summary_note" if kind == 'l3' else "transcript_index"
    out = []
    for seg in ORDER:
        sid = sectionid(kind, seg)
        if sid not in sections_wrapped:
            continue
        out.append(f'<section id="{sid}" class="note-panel {cls}" data-section-kind="{skind}" '
                   f'data-segment-id="{segid(seg)}">{sections_wrapped[sid]}</section>')
    return "\n".join(out)


def render_drawer(timeline):
    seg_by_id = {s["segment_id"]: s for s in timeline["segments"]}
    cards, cur = [], None
    for seg in ORDER:
        s = seg_by_id.get(segid(seg))
        if not s:
            continue
        grp = s["group_label"] or ""
        if grp != cur:
            cur = grp
            cards.append(f'<div class="segment-group">{esc(cur)}</div>')
        kinds = []
        if s["summary_section_id"]:
            kinds.append("總整理")
        kinds.append("逐字索引")
        dur = s.get("duration_sec") or 0
        meta = " / ".join(kinds) + (f" · 總長 {fmt_clock(dur)}" if dur else "")
        cards.append(
            f'<button class="segment-card" type="button" data-segment-id="{s["segment_id"]}" '
            f'data-file="{escq(s["media_file"])}" data-t="{s["start_sec"]}">'
            f'<span class="segment-number">{esc(s["segment_number"])}</span>'
            f'<span class="segment-title">{esc(s["title"])}</span>'
            f'<span class="segment-meta">{esc(meta)}</span></button>')
    return "\n".join(cards)


def render_home(timeline):
    """Landing overview panel: course title + one card per segment (number, total length, and the
    seg's first summary bullet as a one-line gist). Lives inside summary-pane; shown via
    body.home-view at startup, dismissed when a seg/card/timestamp is clicked."""
    segs = timeline["segments"]
    first_summary = {}
    for b in timeline["note_blocks"]:
        if b["section_kind"] == "summary_note":
            first_summary.setdefault(b["segment_id"], b.get("text", ""))
    rows, cur = [], None
    for seg in ORDER:
        s = next((x for x in segs if x["segment_id"] == segid(seg)), None)
        if not s:
            continue
        grp = s["group_label"] or ""
        if grp != cur:
            cur = grp
            if cur:
                rows.append(f'<div class="home-group">{esc(cur)}</div>')
        gist = first_summary.get(s["segment_id"], "")
        if len(gist) > 90:
            gist = gist[:90] + "…"
        dur = s.get("duration_sec") or 0
        durtxt = f'<span class="home-dur">{fmt_clock(dur)}</span>' if dur else ""
        rows.append(
            f'<button class="home-card" type="button" data-segment-id="{s["segment_id"]}" '
            f'data-file="{escq(s["media_file"])}" data-t="{s["start_sec"]}">'
            f'<span class="home-card-head"><span class="home-num">{esc(s["segment_number"])}</span>{durtxt}</span>'
            f'<span class="home-title">{esc(s["title"])}</span>'
            f'<span class="home-gist">{esc(gist)}</span></button>')
    return (f'<section id="home" class="note-panel home-panel">'
            f'<h1 class="home-course">{esc(COURSE_NAME)}</h1>'
            f'<div class="home-sub">{type_badge_html()}{esc(COURSE_DATE)} · 共 {len(segs)} 段</div>'
            f'<div class="home-grid">{"".join(rows)}</div></section>')


def make_page(sections_wrapped, timeline, media_root):
    timeline = dict(timeline)
    timeline["media_root"] = media_root
    timeline["media_root_relative_to_html"] = media_root
    summary_panels = render_panels(sections_wrapped, 'l3')
    replay_panels = render_panels(sections_wrapped, 'l2')
    drawer = render_drawer(timeline)
    home_panel = render_home(timeline)
    head_date = f' · {esc(COURSE_DATE)}' if COURSE_DATE else ''
    # No author configured -> no footer at all. The author name lives in
    # config.yaml `export.author` (or --author), never in the source.
    footer = (f'<footer class="page-foot">{esc(AUTHOR)} 設計 · viewer {VIEWER_VERSION}</footer>'
              if AUTHOR else '')
    tjson = json.dumps(timeline, ensure_ascii=False)
    body = f"""<body class="mode-split home-view">
<header class="page-head">
  <div class="ph-meta"><span class="ph-title">{esc(COURSE_NAME)}</span><span class="ph-date">{head_date}</span>{type_badge_html()}</div>
  <div class="ph-search">
    <input class="ph-search-input" id="search-input" type="search" placeholder="🔍 搜尋筆記＋投影片（按 / 快速聚焦）" autocomplete="off" spellcheck="false" />
    <div class="search-results" id="search-results"></div>
  </div>
  <div class="ph-tools">
    <button class="ph-btn" id="copy-link" type="button" title="複製目前播放位置的連結（?f=影片&t=秒）">🔗</button>
    <button class="ph-btn" id="font-dec" type="button" title="縮小字體（老花友善）">A−</button>
    <button class="ph-btn" id="font-inc" type="button" title="放大字體">A＋</button>
    <button class="ph-btn" id="home-btn" data-go-home type="button" title="回首頁">🏠</button>
    <button class="ph-btn" id="drawer-toggle" data-toggle-drawer type="button" aria-label="切換 Segments 側欄" title="開合 Segments 側欄">☰</button>
  </div>
</header>
<div class="app-v3" data-layout="segment-drawer-right">
  <section class="video-pane" id="video-pane">
    <div class="float-bar" id="float-bar">
      <span class="drag-handle" id="drag-handle" title="拖曳移動影片框">⠿ 拖曳移動</span>
      <button class="float-min" id="video-min-toggle" data-toggle-min type="button" title="縮小／還原影片框">－</button>
    </div>
    <video id="player" controls preload="metadata"></video>
    <div class="controls">
      <button id="toggle-scroll" class="control-button active" type="button">自動捲動：開</button>
      <button id="video-float-toggle" class="control-button" data-toggle-float type="button" title="影片框：長條 ↔ 浮動">浮動</button>
    </div>
    <div class="now" id="now">選一個 segment 或時間碼開始播放。</div>
    <span class="rz nw" data-corner="nw" title="拖曳縮放"></span><span class="rz ne" data-corner="ne" title="拖曳縮放"></span>
    <span class="rz sw" data-corner="sw" title="拖曳縮放"></span><span class="rz se" data-corner="se" title="拖曳縮放"></span>
  </section>
  <div class="col-resizer" id="col-resizer" aria-label="調整影片寬度"></div>
  <main class="note-workspace">
    <div class="note-toolbar">
      <div class="title" id="segment-title">Segment</div>
      <div class="modebar">
        <button type="button" data-mode="split" class="active">同顯</button>
        <button type="button" data-mode="summary">總整理</button>
        <button type="button" data-mode="replay">逐字稿</button>
      </div>
    </div>
    <div class="split-notes">
      <section class="note-half summary-pane" id="summary-pane">
        <div class="pane-label">SUMMARY / 總整理</div>
        <div id="summary-empty" class="empty-panel">這個 segment 沒有總整理區塊。</div>
        {home_panel}
        {summary_panels}
      </section>
      <div class="split-resizer" id="split-resizer" aria-label="調整總整理與逐字稿高度"></div>
      <section class="note-half replay-pane" id="replay-pane">
        <div class="pane-label">REPLAY / 逐字稿索引</div>
        <div id="replay-empty" class="empty-panel">這個 segment 沒有逐字稿索引。</div>
        {replay_panels}
      </section>
    </div>
  </main>
  <aside class="segment-drawer" id="segment-drawer">
    <div class="drawer-head"><strong>Segments</strong></div>
    {drawer}
  </aside>
</div>
{footer}
<script>const TIMELINE = {tjson};
{JS_LOGIC}</script>
</body>"""
    return (f'<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(COURSE_NAME)} — 影片筆記整合</title><style>{CSS}</style></head>\n{body}\n</html>')


# ---------------------------------------------------------------------------
# Image source resolution
# ---------------------------------------------------------------------------
_IMG_INDEX = None   # basename -> [full paths], lazily walked over the whole course work dir
_IMG_AMBIGUOUS = []  # basenames that resolved to several DIFFERENT images (fatal, reported by check_embeds)


def _file_digest(path):
    """Content hash — two candidates with the same bytes are not a real ambiguity."""
    h = hashlib.md5()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return path  # unreadable: treat as its own distinct content
    return h.hexdigest()


def _seg_context_tokens(seg):
    """Path fragments that mark a file as belonging to this segment."""
    s = by.get(seg) or {}
    toks = {f"seg{seg:02d}", f"seg{seg}"}
    if s.get("slug"):
        toks.add(str(s["slug"]))
    for fn in s.get("files", []) or []:
        toks.add(os.path.splitext(os.path.basename(fn))[0])
    return {t.lower() for t in toks if t}


def resolve_image_src(fr, seg=None):
    """Locate an embedded image on disk. Fast path: the FIGURE_ROOTS in order —
    `figures/` then `_L1/figures/` (build_L1 writes the latter; the former was the
    only root checked until 2026-08-05, so on every course built by the batch
    pipeline the fast path could never hit and every embed fell through to the walk).
    Fallback (WP1-1): a one-time recursive walk of the course work dir so non-frame
    embeds (PDF pages under _decks/, page images under _seg/clips/*/frames, etc.)
    resolve too.

    ==Basename collisions across figure generations are FATAL, not a coin flip.==
    Embeds are basename-only. A course whose manifest was reordered has two
    generations of `cNN_frame_XXXX.jpg` on disk (old numbering in `_L1/_stale_figures`,
    new in `_L1/figures`) and the SAME basename then means two DIFFERENT pictures.
    The old behaviour picked one — silently when segment context happened to scope it,
    with a warning otherwise — and shipped 124 wrong-but-plausible figures on
    US-nerve-track (2026-08-05) under a green `all embeds mapped + copied ✓`.
    Now: identical bytes → fine; different bytes → say so and refuse to guess
    (returns None, so check_embeds fails the export loudly).
    """
    global _IMG_INDEX
    for _root in FIGURE_ROOTS:
        p = os.path.join(_root, fr)
        if os.path.exists(p):
            return p
    if _IMG_INDEX is None:
        idx = {}
        exts = (".jpg", ".jpeg", ".png", ".webp")
        for root, _dirs, files in os.walk(COURSE):
            for f in files:
                if f.lower().endswith(exts):
                    idx.setdefault(f, []).append(os.path.join(root, f))
        _IMG_INDEX = idx
    cands = sorted(_IMG_INDEX.get(fr) or [])
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]

    # Same bytes under several paths is not an ambiguity — collapse before judging.
    by_digest = {}
    for c in cands:
        by_digest.setdefault(_file_digest(c), []).append(c)
    if len(by_digest) == 1:
        return cands[0]

    toks = _seg_context_tokens(seg) if seg is not None else set()
    scoped = [c for c in cands if any(t in c.lower() for t in toks)] if toks else []
    if len(scoped) == 1:
        # Legitimate case: generic names (page_001.png) repeated per segment dir.
        # Still noisy on purpose — this used to resolve silently.
        print(f"WARNING: {fr} matches {len(cands)} DIFFERENT images; using the one "
              f"scoped to seg{seg:02d}: {scoped[0]}\n"
              + "".join(f"           candidate: {c}\n" for c in cands), file=sys.stderr)
        return scoped[0]

    _IMG_AMBIGUOUS.append(fr)
    print(f"ERROR: {fr} matches {len(by_digest)} DIFFERENT images in the course dir "
          f"and the segment context does not disambiguate — refusing to guess.\n"
          + "".join(f"           candidate: {c}\n" for c in cands)
          + "         Fix the source tree (e.g. stage the intended generation in "
            "<course>/figures/) and re-run; do NOT let the exporter pick.\n",
          file=sys.stderr)
    return None


def rewrite_links(md):
    """Rewrite a note's internal references to the friendly I: names so it indexes cleanly in
    Obsidian: every image embed ![[name.ext]] (ANY raster image, WP1-1) + every cNN_frame
    frame-id inline prose mention -> friendly image name; note wikilinks (L2/L3/HUB) ->
    friendly note name. Video filename mentions (e.g. 00004.MTS) are LEFT untouched — they
    name the real source recordings (still the sync key)."""
    # image embeds (any extension) -> friendly renamed embed; PRESERVE the optional
    # Obsidian display-width suffix (|600) so the markdown copy stays identical.
    md = re.sub(r'!\[\[([^\]|]+?\.(?:jpg|jpeg|png|webp))(\|\d+)?\]\]',
                lambda m: f'![[{IMG_MAP.get(m.group(1), m.group(1))}{m.group(2) or ""}]]',
                md, flags=re.I)
    # inline prose frame-id mentions (not embeds) -> friendly name
    md = re.sub(r'c\d+_frame_\d+(?:\.jpg)?',
                lambda m: IMG_MAP.get(m.group(0) if m.group(0).endswith('.jpg') else m.group(0) + '.jpg', m.group(0)), md)

    def wl(m):
        tgt, sec, alias = m.group(1).strip(), m.group(2) or '', m.group(3) or ''
        fr = NOTE_MAP.get(tgt)
        if not fr:  # fall back to the seg-number index (tolerate a wrong slug suffix)
            mm = re.match(r'(L[23])_seg(\d+)_', tgt)
            if mm:
                fr = NOTE_BY_KINDSEG.get((mm.group(1), int(mm.group(2))))
        if not fr:
            return m.group(0)
        return f'[[{fr}{sec}{("|" + alias) if alias else ""}]]'

    return re.sub(r'(?<!!)\[\[([^\]|#]+)(#[^\]|]+)?(?:\|([^\]]+))?\]\]', wl, md)


def copy_images():
    """1. media images — only the frames actually embedded (in IMG_MAP), renamed friendly."""
    for fr, friendly in IMG_MAP.items():
        sp = resolve_image_src(fr, IMG_OWNER.get(fr))
        if sp:
            shutil.copy(sp, os.path.join(MEDIA_IMG, friendly))


def copy_notes():
    """2. notes (markdown) with link rewrite + matching pdf rename."""
    with open(os.path.join(MD_OUT, "00 目錄.md"), "w", encoding="utf-8") as fh:
        fh.write(rewrite_links(hub_fm))
    hubpdf = os.path.join(COURSE, "pdf", HUB.replace(".md", ".pdf"))
    if os.path.exists(hubpdf):
        shutil.copy(hubpdf, os.path.join(PDF_OUT, "00 目錄.pdf"))
    for seg in ORDER:
        s = by[seg]
        for kind in (['l2', 'l3'] if s.get('make_l3', True) else ['l2']):
            K = 'L2' if kind == 'l2' else 'L3'
            base = f"{K}_seg{seg:02d}_{s['slug']}"
            srcf = os.path.join(COURSE, K, base + ".md")
            if not os.path.exists(srcf):
                continue
            friendly = NOTE_MAP[base]
            with open(srcf, encoding="utf-8") as fh:
                src_md = fh.read()
            with open(os.path.join(MD_OUT, friendly + ".md"), "w", encoding="utf-8") as fh:
                fh.write(rewrite_links(src_md))
            pdfsrc = os.path.join(COURSE, "pdf", base + ".pdf")
            if os.path.exists(pdfsrc):
                shutil.copy(pdfsrc, os.path.join(PDF_OUT, friendly + ".pdf"))


def encode_one(src, out):
    aud = audio_codec_or_unknown(src, PROBE_PROBLEMS)
    acodec = ["-c:a", "copy"] if aud in WEB_AUDIO else ["-c:a", "aac", "-b:a", "192k"]
    # hevc gets `-tag:v hvc1` so QuickTime/Safari recognize the track.
    if not A.compress:
        vcodec = ["-c:v", "copy"]
    elif A.codec == "hevc":
        vcodec = ["-c:v", "libx265", "-crf", str(A.crf), "-preset", "medium",
                  "-tag:v", "hvc1", "-pix_fmt", "yuv420p"]
    else:
        vcodec = ["-c:v", "libx264", "-crf", str(A.crf), "-preset", "slow", "-pix_fmt", "yuv420p"]
    cmd = [FFMPEG, "-y", "-i", src, "-map", "0:v:0", "-map", "0:a:0", "-sn"] + vcodec + acodec + [out]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode == 0:
        return True
    # ffmpeg's stderr was discarded, so a failure was indistinguishable from a
    # skip — and the truncated output it leaves behind can still pass output_ok()
    # on the next run, shipping a half clip.
    tail = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-12:]
    print(f"  ffmpeg FAILED (exit {r.returncode}) on {os.path.basename(src)}:",
          file=sys.stderr)
    for line in tail:
        print(f"    {line}", file=sys.stderr)
    if os.path.exists(out) and os.path.dirname(os.path.abspath(out)) == os.path.abspath(MEDIA_VID):
        try:
            os.remove(out)  # our own partial output, inside the support folder
            print(f"    removed partial output {os.path.basename(out)}", file=sys.stderr)
        except OSError as e:
            print(f"    could not remove partial output {out}: {e}", file=sys.stderr)
    return False


def output_ok(out, src):
    """Resumable cache check: a prior good output exists?"""
    if not os.path.exists(out):
        return False
    if audio_codec_or_unknown(out, PROBE_PROBLEMS) not in WEB_AUDIO:  # old AC-3 'copy' output → re-do
        return False
    if A.compress and os.path.getsize(out) > os.path.getsize(src) * 0.85:   # not actually compressed
        return False
    return True


def convert_videos(refs):
    """3. Browser-playable conversion (auto-selective). A clip gets an FFmpeg pass if its
    container isn't web-playable, OR its audio isn't browser-decodable (==AVCHD .MTS use
    AC-3, which no browser decodes — must transcode to AAC==), OR --compress / --remux.
      video: compressed by default (hevc x265 / --codec h264 x264); --no-compress → copy (fast, full size)
      audio: AC-3/DTS/… → AAC 192k; already AAC/MP3 → copy.  PGS/other subtitles dropped.
    Output → 媒體/影片/<friendly>.mp4. Web-native clips (mp4/AAC) are used untouched at the root.
    Returns the set of original filenames now served from the support folder."""
    remuxed = set()
    if A.no_remux:
        return remuxed
    needs = [fn for fn in refs
             if (A.remux or A.compress or needs_remux(fn)
                 or audio_codec_or_unknown(media_abspath(fn),
                                           PROBE_PROBLEMS) not in WEB_AUDIO)]
    for i, fn in enumerate(needs, 1):
        src = media_abspath(fn)
        out = os.path.join(MEDIA_VID, VID_MAP[fn])
        if output_ok(out, src):
            remuxed.add(fn); continue
        print(f"[encode {i}/{len(needs)}] {fn} -> {VID_MAP[fn]}{' (%s CRF%d)' % (CODEC_LABEL[A.codec], A.crf) if A.compress else ''} …", flush=True)
        if os.path.exists(src) and encode_one(src, out):
            remuxed.add(fn)
    return remuxed


# WP1-1 fail-loudly (2026-07-09): after export, confirm EVERY image embed in EVERY
# rendered note was mapped AND its friendly file actually landed in 媒體/圖片. An embed
# that is unmapped (regex miss) or unresolved (source file not found on disk) used to
# ship silently as a broken image; now it prints each offender and exits non-zero so the
# batch/dispatcher notices instead of the professor.
def _iter_note_srcs():
    yield "HUB", hub_fm
    for _seg in ORDER:
        _s = by[_seg]
        for _kind in (['l2', 'l3'] if _s.get('make_l3', True) else ['l2']):
            _K = 'L2' if _kind == 'l2' else 'L3'
            _srcf = os.path.join(COURSE, _K, f"{_K}_seg{_seg:02d}_{_s['slug']}.md")
            if os.path.exists(_srcf):
                with open(_srcf, encoding="utf-8") as fh:
                    yield os.path.basename(_srcf), fh.read()


def check_embeds():
    """Returns 0, or 1 when some embed would render broken (caller exits with it)."""
    unmapped, unresolved, n_embeds = [], [], 0
    for label, text in _iter_note_srcs():
        for m in EMBED_RE.finditer(text):
            n_embeds += 1
            name = m.group(1)
            if name not in IMG_MAP:
                unmapped.append((label, name))
            elif not os.path.exists(os.path.join(MEDIA_IMG, IMG_MAP[name])):
                unresolved.append((label, name))
    if unmapped or unresolved:
        print(f"\n❌ export_web: {len(unmapped) + len(unresolved)} of {n_embeds} image "
              f"embeds did NOT make it into 媒體/圖片 (would render as broken images):")
        for label, name in unmapped:
            print(f"   [UNMAPPED  regex miss ] {label}: ![[{name}]]")
        for label, name in unresolved:
            print(f"   [UNRESOLVED src missing] {label}: ![[{name}]] -> {IMG_MAP[name]}")
        if _IMG_AMBIGUOUS:
            print(f"\n   ⚠️ {len(set(_IMG_AMBIGUOUS))} basename(s) were AMBIGUOUS, not missing: "
                  f"each matches several DIFFERENT images on disk (see the ERROR lines above). "
                  f"That is what a reordered manifest leaves behind — two generations of "
                  f"cNN_frame_XXXX.jpg sharing names. Stage the intended generation in "
                  f"<course>/figures/ and re-run.")
        return 1
    print(f"  embed-check: all {n_embeds} image embeds mapped + copied ✓")
    return 0


# WP2-6 (2026-07-09): a plain-text 分享說明.txt at the course root so the professor knows
# exactly what to copy when handing the course to someone else. The webpage needs THREE
# things travelling together: the .html, its same-named support folder, and the
# root-level source videos that are played in place (web-native clips referenced via
# media_root="." — the transcoded ones already live inside the support folder).
def _dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


def _human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


def write_share_readme(refs, remuxed):
    html_name = os.path.basename(HTML_OUT)
    sup_name = os.path.basename(SUPPORT.rstrip("/\\"))
    # root videos that must ship alongside the html (referenced in place, not in the support folder)
    root_vids = []
    for fn in refs:
        if fn in remuxed:
            continue
        p = media_abspath(fn)
        if os.path.exists(p):
            root_vids.append((fn, os.path.getsize(p)))

    html_sz = os.path.getsize(HTML_OUT)
    sup_sz = _dir_size(SUPPORT)
    vid_sz = sum(s for _, s in root_vids)
    total = html_sz + sup_sz + vid_sz

    lines = [
        f"《{COURSE_NAME}》影片筆記整合 — 分享說明",
        "=" * 40,
        "",
        "要把這份筆記交給別人時，請一併複製以下項目（三者缺一不可）：",
        "",
        f"  1. {html_name}                （網頁本體，用瀏覽器開啟）",
        f"  2. {sup_name}/                 （支援資料夾：圖片／影片／markdown／pdf，{_human(sup_sz)}）",
    ]
    if root_vids:
        lines.append("  3. 以下原始影片（放在與網頁同一層資料夾，播放時就地讀取）：")
        for fn, sz in root_vids:
            lines.append(f"       - {fn}  ({_human(sz)})")
    else:
        lines.append("  3.（本課所有影片皆已轉入支援資料夾，無需額外複製根目錄影片）")
    lines += [
        "",
        f"總大小約：{_human(total)}",
        "",
        "提醒：",
        "  • 三個項目要放在「同一層資料夾」內，網頁才能正確找到圖片與影片。",
        "  • 只複製 .html 而不帶支援資料夾／影片，會導致圖片破圖、影片無法播放。",
        f"  • 用瀏覽器（Chrome/Edge）直接雙擊開啟 {html_name} 即可，不需網路。",
    ]
    with open(os.path.join(COURSE_ROOT, "分享說明.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"  分享說明.txt written ({_human(total)} total: html {_human(html_sz)} + "
          f"support {_human(sup_sz)} + {len(root_vids)} root video(s) {_human(vid_sz)})")


# ---------------------------------------------------------------------------
# main — everything above is definitions only, so importing this module is free
# of side effects (it used to run the whole pipeline at import time).
# ---------------------------------------------------------------------------

def main(argv=None):
    global A, AUTHOR, COURSE, COURSE_ROOT, COURSE_NAME, COURSE_DATE, MANIFEST, MEDIA_REL
    global HUB, hub_fm, segs, by, ORDER, RANK
    global SUPPORT, HTML_OUT, MEDIA_IMG, MEDIA_VID, MD_OUT, PDF_OUT
    global IMG_URL_PREFIX, VID_URL_PREFIX, FIGURE_ROOTS
    global IMG_MAP, VID_MAP, NOTE_MAP, IMG_OWNER, NOTE_BY_KINDSEG
    global CSS, JS_LOGIC, PROBE_PROBLEMS

    A = build_argparser().parse_args(argv)
    # Compression is ON unless --no-compress; per-codec CRF default when --crf absent.
    A.compress = not A.no_compress
    if A.crf is None:
        A.crf = CRF_DEFAULT[A.codec]
    # Preflight before any work: a course half-exported because pandoc is missing
    # is worse than a clear refusal on line one.
    require_binaries(PANDOC, FFMPEG, FFPROBE)
    PROBE_PROBLEMS = []

    COURSE = A.course
    if not os.path.isdir(COURSE):
        print(f"ERROR: course directory not found: {COURSE}", file=sys.stderr)
        sys.exit(2)

    seg_path = os.path.join(COURSE, "_intermediate", "seg", "segments.json")
    if not os.path.isfile(seg_path):
        print(f"ERROR: {seg_path} not found — this is the segment list the export "
              "is built from (see reference/segmented-mode.md).", file=sys.stderr)
        sys.exit(2)
    with open(seg_path, encoding="utf-8") as fh:
        segs = json.load(fh)

    HUB = pick_hub(COURSE)
    with open(os.path.join(COURSE, HUB), encoding="utf-8") as fh:
        hub_fm = fh.read()

    def fm(key, default=""):
        m = re.search(rf'^{key}:\s*(.+)$', hub_fm, flags=re.M)
        return m.group(1).strip() if m else default

    COURSE_NAME = A.name or fm("course") or os.path.basename(COURSE.rstrip("/\\"))
    COURSE_DATE = A.date or fm("date")

    manifest_path = os.path.join(COURSE, "_raw", "manifest.json")
    if not os.path.isfile(manifest_path):
        print(f"ERROR: {manifest_path} not found (records the source-video "
              "directory and clip list).", file=sys.stderr)
        sys.exit(2)
    with open(manifest_path, encoding="utf-8") as fh:
        MANIFEST = json.load(fh)
    COURSE_ROOT = resolve_course_root(MANIFEST, manifest_path, COURSE)
    # basename -> manifest-relative src, so subfoldered layouts resolve (see media_src)
    MEDIA_REL = {os.path.basename(c["src"]): c["src"]
                 for c in (MANIFEST.get("clips") or []) if c.get("src")}

    # Footer author: --author wins, else config.yaml export.author, else none.
    if A.author is not None:
        AUTHOR = A.author.strip()
    else:
        try:
            AUTHOR = str(((load_config() or {}).get("export") or {}).get("author")
                         or "").strip()
        except Exception as e:  # noqa: BLE001 — a bad config must not block an export
            print(f"WARNING: could not read export.author from config.yaml ({e}); "
                  "no footer", file=sys.stderr)
            AUTHOR = ""

    SUPPORT = A.out or os.path.join(COURSE_ROOT, "影片筆記整合")
    HTML_OUT = SUPPORT.rstrip("/\\") + ".html"
    MEDIA_IMG = os.path.join(SUPPORT, "媒體", "圖片")
    MEDIA_VID = os.path.join(SUPPORT, "媒體", "影片")
    MD_OUT = os.path.join(SUPPORT, "markdown")
    PDF_OUT = os.path.join(SUPPORT, "pdf")
    sup_base = os.path.basename(SUPPORT.rstrip("/\\"))
    IMG_URL_PREFIX = sup_base + "/媒體/圖片/"   # relative to the HTML at the course root
    VID_URL_PREFIX = sup_base + "/媒體/影片/"
    # Ordered figure roots. `figures/` is the staging root a repair run can
    # populate to pin one generation; `_L1/figures/` is where build_L1 writes.
    FIGURE_ROOTS = [os.path.join(COURSE, "figures"),
                    os.path.join(COURSE, "_L1", "figures")]

    segs, changed = normalize_segments(segs, COURSE, MANIFEST, hub_fm)
    if changed:
        # The backfill is written to a SEPARATE file: rewriting the input tree made
        # this exporter a mutator of its own source of truth (with a .bak only on
        # the first run). segments.json stays exactly as the pipeline wrote it.
        norm_path = os.path.join(COURSE, "_intermediate", "segments.normalized.json")
        os.makedirs(os.path.dirname(norm_path), exist_ok=True)
        atomic_write_json(norm_path, segs, indent=1)
        print(f"[normalize] backfilled missing segment fields -> {norm_path} "
              "(input segments.json untouched)")
    by = {s['seg']: s for s in segs}
    ORDER = [s['seg'] for s in sorted(segs, key=order)]
    RANK = {seg: i + 1 for i, seg in enumerate(ORDER)}

    IMG_MAP, VID_MAP, NOTE_MAP, IMG_OWNER = build_media_map(COURSE, ORDER, by)
    NOTE_BY_KINDSEG = build_note_by_kindseg(NOTE_MAP)

    render_sections()
    CSS = read_asset("viewer.css")
    JS_LOGIC = read_asset("viewer.js")

    for d in (MEDIA_IMG, MEDIA_VID, MD_OUT, PDF_OUT):
        os.makedirs(d, exist_ok=True)

    copy_images()
    copy_notes()

    refs = sorted({fn for s in segs for fn in s.get('files', [])})
    remuxed = convert_videos(refs)

    def resolver_inplace(fn):
        # remuxed clips served from the support folder; web-native originals fall
        # back to media_root (".") — unless the manifest keeps them in a subfolder
        # (`Video\MVI_8929.MP4`), in which case media_root + basename is a dead
        # link and we must emit the real relative path. Hit on US-neck-dysyonia
        # 2026-08-02: Canon MP4/AAC needs no transcode, so every clip took the
        # fall-back branch and all 18 video links 404'd.
        if fn in remuxed:
            return VID_URL_PREFIX + VID_MAP[fn]
        rel = MEDIA_REL.get(fn, fn).replace("\\", "/")
        return rel if "/" in rel else None

    # 4. timeline + the single webpage (at the course root; media_root "." = course root)
    sw, tl = build_timeline(resolver_inplace)
    # media existence is VERIFIED, not assumed (2026-08-04): `exists` used to be
    # hard-coded True even when the resolved target was gone — US-nerve-track shipped
    # 11 dead in-place 錄音/*.mp3 references for a month with zero alarm after the
    # source folder was cleaned. Missing parts stay in the page (notes/timestamps are
    # still useful) but carry exists:false so the viewer shows a 來源已遺失 notice
    # instead of a silently broken player.
    html_dir = os.path.dirname(os.path.abspath(HTML_OUT))
    missing_media = []
    for _part in tl["media_parts"]:
        _target = os.path.normpath(os.path.join(html_dir, _part["relative_path"].replace("\\", "/")))
        _part["exists"] = os.path.exists(_target)
        if not _part["exists"]:
            missing_media.append(_part["file"])
    page = make_page(sw, tl, ".")
    # keep the previous delivered artifacts as .bak before overwriting (2026-08-04):
    # a buggy re-export once clobbered a course's only good _timeline/_media_map in
    # place, leaving nothing to diagnose or roll back to. One generation is enough.
    for _prev in (HTML_OUT,
                  os.path.join(SUPPORT, "_timeline.json"),
                  os.path.join(SUPPORT, "_media_map.json")):
        if os.path.exists(_prev):
            shutil.copy2(_prev, _prev + ".bak")
    with open(HTML_OUT, "w", encoding="utf-8") as fh:
        fh.write(page)

    # debug / re-run artifacts inside the support folder (viewer reads the inline TIMELINE)
    atomic_write_json(os.path.join(SUPPORT, "_timeline.json"), tl)
    atomic_write_json(os.path.join(SUPPORT, "_media_map.json"),
                      {"images": IMG_MAP, "videos": VID_MAP, "notes": NOTE_MAP,
                       "remuxed": sorted(remuxed)})

    nblk = len(tl["note_blocks"])
    vmode = f"compress {CODEC_LABEL[A.codec]} CRF{A.crf}" if A.compress else ("copy+AAC" if remuxed else "originals")
    print(f"OK 影片筆記整合 ({VIEWER_VERSION}, schema v{SCHEMA_VERSION}) -> {HTML_OUT}")
    print(f"  support={SUPPORT}")
    print(f"  sections={len(present)} segments={len(tl['segments'])} note_blocks={nblk} "
          f"slide_blocks={len(tl['slide_blocks'])} images={len(IMG_MAP)} "
          f"video={len(remuxed)}/{len(refs)} clips [{vmode}]")

    if PROBE_PROBLEMS:
        print(f"  ⚠️ ffprobe could not read {len(PROBE_PROBLEMS)} file(s) — their "
              "audio codec was treated as unknown (so they were re-encoded):",
              file=sys.stderr)
        for p in dict.fromkeys(PROBE_PROBLEMS):
            print(f"     {p}", file=sys.stderr)

    if missing_media:
        print(f"  ⚠️ {len(missing_media)} media part(s) UNRESOLVED on disk — the page "
              "marks them 來源已遺失 (exists:false). If this is a fresh export, the "
              "source files are gone; recover them before deleting anything else:",
              file=sys.stderr)
        for fn in missing_media:
            print(f"     {fn}", file=sys.stderr)

    rc = check_embeds()
    if rc:
        return rc
    write_share_readme(refs, remuxed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
