# -*- coding: utf-8 -*-
"""Assemble a SINGLE-talk lecture dir into the layout export_web.py expects.

    python build_single_talk_web.py <lecture_dir> --plan plan.json
        [--video FILE] [--name NAME] [--date DATE] [--course-type TYPE]
        [--hub-slug SLUG] [--force] [--export]

export_web.py was written for multi-talk workshop folders, but a single talk
exports fine (verified 2026-08-03): ==every segment's files[] points at the SAME
video and timestamps are absolute seconds== — media_parts dedups by filename, so
the video is processed once and playback is continuous across segments. This
script automates the assembly that was done by hand that day:

  _raw/manifest.json                one clip, idx 1 → "V1"
  _intermediate/seg/segments.json   ==a LIST, not a dict== (normalize_segments
                                    crashes on a dict), one entry per plan row
  L2/L2_segNN_<slug>.md             transcript.json sliced per segment, bullets
                                    grouped in 30-second buckets, `(V1 MM:SS)`
                                    timecodes (the ONLY format the viewer parses)
  _HUB_<slug>.md                    skeleton: frontmatter + meta + segment table
  L3/ figures/                      created empty

==L3 content is NOT generated here== — Stage F (Claude synthesis) writes
L3/L3_segNN_<slug>.md per segment. Two format traps for that writer, both
measured 2026-08-03: image embeds must be ==basename only== (`![[fig.jpg]]`,
never a vault path — resolve_image_src indexes figures/ by basename), and
"内容分段" means ==more segments in the plan, not more headings== (the viewer
builds exactly two chapters per segment: L2 + L3).

An ==overview segment (全場總整理) is auto-appended== (user rule 2026-08-03:
many segments still need one place listing everything): display_order 0 so it
sorts first in the drawer, time range = the whole talk, ==L3 only — no L2 is
written for it== (the viewer's segment card falls back to the L3 section when
L2 is absent; the whole-talk transcript would only duplicate every other L2).
The _HUB is markdown-export-only — it never appears as a viewer panel, which is
why the overview must be a segment. Stage F writes its
L3/L3_segNN_overview.md: 5–8 whole-talk clinical pearls each carrying a
`(V1 MM:SS)` timecode, then a one-line takeaway per content segment.
--no-overview opts out.

The plan file is a JSON list, one row per segment:
  [{"seg": 1, "start": "00:00", "end": "01:00", "slug": "opening-problem",
    "title_zh": "開場：三個問題", "region": "命題", "type": "discussion"}, ...]
start/end accept "MM:SS", "H:MM:SS" or plain seconds. region/type are optional.

Existing files are never overwritten without --force, so re-running after fixing
the plan is safe. --export runs export_web.py at the end (only sensible once the
L3 files exist).
"""
import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

VIDEO_EXT = (".mp4", ".mov", ".mkv", ".avi", ".m4v", ".mts", ".m2ts", ".webm")


def parse_ts(v):
    """"MM:SS" / "H:MM:SS" / number -> int seconds."""
    if isinstance(v, (int, float)):
        return int(v)
    parts = str(v).strip().split(":")
    if not all(re.fullmatch(r"\d+", p) for p in parts) or not 1 <= len(parts) <= 3:
        raise ValueError(f"unparsable timestamp: {v!r}")
    parts = [int(p) for p in parts]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts
    return h * 3600 + m * 60 + s


def fmt_mmss(sec):
    """Absolute seconds -> MM:SS with total minutes (75:30 past an hour — the
    viewer regex takes any digit count for minutes but exactly two for seconds)."""
    return f"{int(sec) // 60:02d}:{int(sec) % 60:02d}"


def load_plan(path):
    with open(path, encoding="utf-8") as fh:
        plan = json.load(fh)
    if not isinstance(plan, list) or not plan:
        raise SystemExit(f"ERROR: plan must be a non-empty JSON list: {path}")
    rows = []
    for i, r in enumerate(plan, 1):
        missing = [k for k in ("slug", "title_zh", "start", "end") if k not in r]
        if missing:
            raise SystemExit(f"ERROR: plan row {i} missing {missing}")
        rows.append({
            "seg": int(r.get("seg", i)),
            "start": parse_ts(r["start"]), "end": parse_ts(r["end"]),
            "slug": r["slug"], "title_zh": r["title_zh"],
            "region": r.get("region", ""), "type": r.get("type", "lecture"),
        })
    rows.sort(key=lambda r: r["seg"])
    slugs = [r["slug"] for r in rows]
    if len(set(slugs)) != len(slugs):
        raise SystemExit("ERROR: duplicate slugs in plan")
    for a, b in zip(rows, rows[1:]):
        if b["start"] < a["end"]:
            raise SystemExit(f"ERROR: seg {a['seg']} and {b['seg']} overlap "
                             f"({fmt_mmss(a['end'])} > {fmt_mmss(b['start'])})")
    return rows


def pick_video(course, arg):
    if arg:
        p = arg if os.path.isabs(arg) else os.path.join(course, arg)
        if not os.path.isfile(p):
            raise SystemExit(f"ERROR: video not found: {p}")
        return p
    vids = sorted(f for f in os.listdir(course)
                  if os.path.splitext(f)[1].lower() in VIDEO_EXT)
    if len(vids) != 1:
        raise SystemExit(
            f"ERROR: {'no' if not vids else len(vids)} video files in {course} "
            f"— pass --video to disambiguate. Found: {vids[:8]}")
    return os.path.join(course, vids[0])


def slice_l2(transcript, start, end):
    """Transcript entries in [start,end) -> bullets grouped in 30 s buckets."""
    buckets = {}
    for t in transcript:
        if start <= t["start"] < end and t.get("text", "").strip():
            b = int(t["start"] // 30) * 30
            buckets.setdefault(b, []).append(t["text"].strip())
    return [f"- `(V1 {fmt_mmss(b)})` " + " ".join(txts)
            for b, txts in sorted(buckets.items())]


def write_once(path, content, force):
    if os.path.exists(path) and not force:
        print(f"  keep     {os.path.relpath(path)}  (exists — --force to overwrite)")
        return False
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    print(f"  wrote    {os.path.relpath(path)}")
    return True


def main():
    ap = argparse.ArgumentParser(
        description="Assemble a single-talk lecture dir for export_web.py "
                    "(manifest + segments.json + L2 slices + HUB skeleton).")
    ap.add_argument("course", help="lecture dir holding transcript.json")
    ap.add_argument("--plan", required=True,
                    help="JSON list of segments: seg/start/end/slug/title_zh[/region/type]")
    ap.add_argument("--video", default=None,
                    help="the talk's video (default: the single video file in the dir)")
    ap.add_argument("--name", default=None, help="course title (default: dir name)")
    ap.add_argument("--date", default=None, help="course date, shown in the HUB")
    ap.add_argument("--course-type", default=None,
                    help="manifest course_type (default: most common plan row type)")
    ap.add_argument("--hub-slug", default="index",
                    help="ASCII slug for the _HUB_<slug>.md filename (default: index)")
    ap.add_argument("--no-overview", action="store_true",
                    help="skip the auto-appended 全場總整理 overview segment")
    ap.add_argument("--force", action="store_true", help="overwrite existing outputs")
    ap.add_argument("--export", action="store_true",
                    help="run export_web.py afterwards (needs the L3 files written first)")
    A = ap.parse_args()

    course = os.path.abspath(A.course)
    tpath = os.path.join(course, "transcript.json")
    if not os.path.isfile(tpath):
        raise SystemExit(f"ERROR: {tpath} not found — transcribe first (Step 4)")
    with open(tpath, encoding="utf-8") as fh:
        transcript = json.load(fh)

    rows = load_plan(A.plan)
    video = pick_video(course, A.video)
    vbase = os.path.basename(video)
    name = A.name or os.path.basename(course)
    os.chdir(course)

    # tail-coverage check: a plan that stops early silently drops the end of the talk
    t_end = max((t["end"] for t in transcript), default=0)
    if t_end - rows[-1]["end"] > 60:
        print(f"WARNING: plan ends at {fmt_mmss(rows[-1]['end'])} but the transcript "
              f"runs to {fmt_mmss(t_end)} — the tail is not in any segment.")

    for d in ("_raw", os.path.join("_intermediate", "seg"), "L2", "L3", "figures"):
        os.makedirs(os.path.join(course, d), exist_ok=True)

    manifest = {"path": course,
                "clips": [{"idx": 1, "src": vbase, "name": "clip01"}],
                "course_type": A.course_type or max(
                    (r["type"] for r in rows),
                    key=[r["type"] for r in rows].count)}
    write_once(os.path.join(course, "_raw", "manifest.json"),
               json.dumps(manifest, ensure_ascii=False, indent=2), A.force)

    segments = [{
        "seg": r["seg"], "files": [vbase], "clips": [1], "slug": r["slug"],
        "make_l3": True, "title_zh": r["title_zh"], "region": r["region"],
        "display_order": i, "topic": r["title_zh"], "type": r["type"],
        "time": f"{fmt_mmss(r['start'])}–{fmt_mmss(r['end'])}",
    } for i, r in enumerate(rows, 1)]
    overview = None
    if not A.no_overview:
        # Whole-talk 總整理 as its own L3-only segment, sorted first in the
        # drawer. It must be a segment: the _HUB never renders in the viewer.
        overview = {
            "seg": max(r["seg"] for r in rows) + 1, "files": [vbase],
            "clips": [1], "slug": "overview", "make_l3": True,
            "title_zh": "全場總整理", "region": "總覽", "display_order": 0,
            "topic": "全場總整理", "type": rows[0]["type"],
            "time": f"00:00–{fmt_mmss(t_end)}",
        }
        segments.append(overview)
    write_once(os.path.join(course, "_intermediate", "seg", "segments.json"),
               json.dumps(segments, ensure_ascii=False, indent=2), A.force)

    n_bullets = 0
    for r in rows:
        bullets = slice_l2(transcript, r["start"], r["end"])
        n_bullets += len(bullets)
        if not bullets:
            print(f"WARNING: seg {r['seg']:02d} {r['slug']} has no transcript text "
                  f"in {fmt_mmss(r['start'])}–{fmt_mmss(r['end'])}")
        l2 = (f"---\ntags: [lecture/L2]\n---\n\n"
              f"# L2 {r['title_zh']} 逐字稿\n\n"
              f"> [!info] 本機 whisper 轉錄，==未經人工逐字校對==；"
              f"英文術語有辨識誤差，正確用詞以整理稿為準。\n\n"
              + "\n".join(bullets) + "\n")
        write_once(os.path.join(course, "L2", f"L2_seg{r['seg']:02d}_{r['slug']}.md"),
                   l2, A.force)

    table = "\n".join(
        f"| {r['seg']:02d} | {fmt_mmss(r['start'])}–{fmt_mmss(r['end'])} "
        f"| {r['title_zh']} | {r['region']} |" for r in rows)
    hub = (f"---\ntags: [lecture/hub]\n---\n\n# {name}\n\n"
           + (f"- 日期：{A.date}\n" if A.date else "")
           + f"- 錄影長度：{fmt_mmss(t_end)}\n\n"
           f"| 段 | 時間 | 主題 | 分類 |\n|---|---|---|---|\n{table}\n")
    write_once(os.path.join(course, f"_HUB_{A.hub_slug}.md"), hub, A.force)

    want_l3 = rows + ([overview] if overview else [])
    missing_l3 = [f"L3_seg{r['seg']:02d}_{r['slug']}.md" for r in want_l3
                  if not os.path.exists(os.path.join(course, "L3",
                                        f"L3_seg{r['seg']:02d}_{r['slug']}.md"))]
    print(f"\nassembled: {len(segments)} segments "
          f"({len(rows)} content{' + 1 overview' if overview else ''}), "
          f"{n_bullets} L2 bullets, video={vbase}")
    if missing_l3:
        print(f"next: Stage F writes {len(missing_l3)} L3 note(s) into L3/ "
              f"(==image embeds by basename only, timecodes `(V1 MM:SS)`==):")
        for m in missing_l3:
            print(f"  L3/{m}")
        if overview and any("overview" in m for m in missing_l3):
            print("  overview content by course shape (single talk: 5-8 pearls "
                  "with timecodes + one line per segment; series/workshop "
                  "variants -> reference/pipeline.md#web-export)")
    export_cmd = [sys.executable, os.path.join(HERE, "export_web.py"), course]
    if A.export:
        if missing_l3 and not A.force:
            raise SystemExit("ERROR: refusing --export with L3 files missing "
                             "(the viewer would ship without the 整理稿 layer). "
                             "Write them, or --force to export anyway.")
        print("\nrunning export_web.py …", flush=True)
        sys.exit(subprocess.run(export_cmd).returncode)
    else:
        print("then: " + " ".join(f'"{c}"' if " " in c else c for c in export_cmd))


if __name__ == "__main__":
    main()
