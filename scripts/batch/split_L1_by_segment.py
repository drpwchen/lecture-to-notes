#!/usr/bin/env python3
"""split_L1_by_segment.py <course_dir> [--granularity coarse|fine]

Split a course's L1 note into per-segment files for the L2/L3 gen agents.

  <course>/_L1/L1_coarse.md   (sections headed `## clip NN — <clipname>`)
  <course>/_seg/segments.json (each seg has clips[]  — OR only a `time` string)
      → <course>/_seg/L1_segNN.md   (that segment's clip sections, in seg order)

A clip shared by two segments (a talk that starts mid-clip) is emitted into BOTH
segment files — the L2 agent is told its time window and covers only that range.
Also stages the two files export_web.py reads from non-obvious locations.

Time-sliced courses (a single long lecture cut into many time windows — most
didactic MRI/X-ray talks) have EMPTY `clips`/`files` and carry their clip refs
ONLY inside the `time` string (`00_00000, 00:03-00:24；01_00001, 00:00-04:21`).
Splitting purely on `clips` then wrote 5-line empty stubs (hit 2026-07-24 on both
施庭芳 MRI-spine courses). Fallback: when `clips` is empty, parse the clip NAMES
out of `time`, map them to indices via the L1 headers, and BACKFILL `clips` in
segments.json so downstream (relabel_prep static detection, dispatch_pack) agrees.
"""
import json, re, shutil, sys
from pathlib import Path

def clips_from_time(time_str, name2idx):
    """Recover clip indices from a segment `time` string, in appearance order.

    Search each known clip NAME as a substring — robust to every `time` format
    seen (`NAME, MM:SS`, `NAME(29:51) → NAME2, 全`, `；`-joined lists) where a
    strict comma parse breaks. Longest names first so no name is masked by a
    shorter one that is its prefix.
    """
    time_str = time_str or ""
    hits = []
    for name in sorted(name2idx, key=len, reverse=True):
        pos = time_str.find(name)
        if pos >= 0:
            hits.append((pos, name2idx[name]))
    out = []
    for _, idx in sorted(hits):
        if idx not in out:
            out.append(idx)
    return out


def main(course, gran="coarse"):
    d = Path(course)
    l1p = d / "_L1" / f"L1_{gran}.md"
    if not l1p.exists():                       # legacy layout
        l1p = d / "_intermediate" / f"L1_{gran}.md"
    text = l1p.read_text(encoding="utf-8")

    parts = re.split(r"^(## clip (\d+) —\s*(.*))$", text, flags=re.M)
    header = parts[0]
    sections = {}
    name2idx = {}
    for i in range(1, len(parts), 4):
        idx = int(parts[i + 1])
        sections[idx] = parts[i] + parts[i + 3]
        name2idx[parts[i + 2].strip()] = idx   # clip folder name → manifest idx

    segp = d / "_seg" / "segments.json"
    segs = json.loads(segp.read_text(encoding="utf-8"))
    backfilled = 0
    for s in segs:
        clips = s.get("clips") or []
        if not clips and s.get("time"):        # time-sliced course → recover from `time`
            clips = clips_from_time(s["time"], name2idx)
            if clips:
                s["clips"] = clips             # backfill so downstream agrees
                backfilled += 1
        body = [header.rstrip(), ""]
        missing = []
        for ci in clips:
            if ci in sections:
                body.append(sections[ci].rstrip())
            else:
                missing.append(ci)
        out = d / "_seg" / f"L1_seg{s['seg']:02d}.md"
        out.write_text("\n\n".join(body) + "\n", encoding="utf-8")
        flag = "  ⚠️ EMPTY (no clips resolved)" if len(body) <= 2 else ""
        print(f"  seg{s['seg']:02d} clips={clips} chars={out.stat().st_size:>7,}"
              + (f"  ⚠️ MISSING clip sections {missing}" if missing else "") + flag)

    if backfilled:
        segp.write_text(json.dumps(segs, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  backfilled clips[] into {backfilled} segments of segments.json")

    # export_web.py reads segments.json from _intermediate/seg/ and the manifest
    # from _raw/ — stage both now so the export step can't fail on a missing file.
    (d / "_intermediate" / "seg").mkdir(parents=True, exist_ok=True)
    (d / "_raw").mkdir(exist_ok=True)
    shutil.copy2(d / "_seg" / "segments.json", d / "_intermediate" / "seg" / "segments.json")
    shutil.copy2(d / "manifest.json", d / "_raw" / "manifest.json")
    print(f"  staged _intermediate/seg/segments.json + _raw/manifest.json")


if __name__ == "__main__":
    g = sys.argv[sys.argv.index("--granularity") + 1] if "--granularity" in sys.argv else "coarse"
    main(sys.argv[1], g)
