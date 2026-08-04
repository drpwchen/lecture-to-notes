# -*- coding: utf-8 -*-
r"""Build ONE real-time timeline for a course, across every capture device.

    python course_timeline.py <course_dir> [--source-dir DIR] [--photos DIR ...]
        [--clock-offset SELECTOR=SECONDS ...] [--reorder-manifest] [--out PATH]

==Why this exists.== Segmentation used to be ordered by the manifest `clips`
array, which is the filename sort. Filenames are not time:

  - a camcorder restarts its counter, so a two-day conference has two `00000`
  - a voice recorder's `240526_1119.mp3` sorts between `00011.MTS` and
    `00012.MTS`, dropping the afternoon into the middle of the morning
  - `<speaker>-1-` was written on the SECOND file and `-2-` on the first
  - photos are a third device nobody ordered at all

On the 2024-05 Conference-Y batch this put Day-2 afternoon audio ahead of Day-2
morning video inside `L1_coarse.md`, so every downstream agent read the course
in an order that never happened.

==The rule this script enforces: real capture time is the ordering authority;
the filename is only a label.== It reads each source's own clock — AVCHD MDPM
for `.MTS`, container tags for MOV/MP4, the embedded timestamp for recorder
filenames, EXIF for photos — applies MEASURED per-device corrections, and emits
one timeline plus, optionally, a manifest reordered to match.

==Devices disagree and the script will not guess.== It reports each device's
raw claim and flags disagreement; a `--clock-offset` must come from a
measurement (`reference/multi-camera.md` §device-clock-calibration), never from
a hunch. Without one, a wrong device clock produces a confidently wrong
timeline — which is the failure this whole file exists to prevent.
"""
import argparse, datetime, json, os, shutil, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from media_capture_index import (PHOTO, apply_clock_offsets, parse_clock_offsets,  # noqa: E402
                                 parse_filename_time, probe_photo, probe_video,
                                 refine_seconds_with_mtime, to_local)
from _common import atomic_write_json  # noqa: E402


def resolve_source_dir(course_dir, override):
    if override:
        return override
    man = os.path.join(course_dir, "manifest.json")
    if os.path.isfile(man):
        with open(man, encoding="utf-8") as fh:
            p = (json.load(fh) or {}).get("path")
        if p and os.path.isdir(p):
            return p
    return course_dir


def probe_one(path, utc_offset):
    ext = os.path.splitext(path)[1].lower()
    if ext in PHOTO:
        info, kind = probe_photo(path), "photo"
    else:
        info, kind = probe_video(path), "media"
    dt, src = to_local(info.get("creation_raw"), utc_offset)
    src = info.get("time_source_hint") or src
    if not dt:
        # A recorder that writes no tag still names the file from its clock.
        dt = parse_filename_time(os.path.basename(path))
        if dt:
            dt, src = refine_seconds_with_mtime(path, dt)
    return {"file": os.path.basename(path), "path": path, "kind": kind,
            "duration_s": round(info.get("duration_s") or 0, 1),
            "start": dt.isoformat(sep=" ") if dt else None,
            "end": ((dt + datetime.timedelta(seconds=info.get("duration_s") or 0)
                     ).isoformat(sep=" ") if dt else None),
            "time_source": src if dt else None,
            "model": info.get("model")}


def scan_photos(dirs, utc_offset):
    rows = []
    for spec in dirs or []:
        label, _, d = spec.partition("=") if "=" in spec else ("", "", spec)
        if not os.path.isdir(d):
            sys.exit(f"ERROR: --photos not a directory: {d}")
        for e in sorted(os.scandir(d), key=lambda e: e.name):
            if e.is_file() and os.path.splitext(e.name)[1].lower() in PHOTO:
                r = probe_one(e.path, utc_offset)
                r["source"] = label or os.path.basename(os.path.normpath(d))
                rows.append(r)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("course_dir")
    ap.add_argument("--source-dir", help="original material dir (default: "
                                         "manifest.json 'path', else course_dir)")
    ap.add_argument("--photos", action="append", metavar="[LABEL=]DIR",
                    help="a stills folder; repeatable. Photos are a capture "
                         "device like any other and belong on the timeline.")
    ap.add_argument("--clock-offset", action="append", metavar="SELECTOR=SECONDS",
                    help="MEASURED per-device correction, e.g. '*.MTS=-86730'")
    ap.add_argument("--utc-offset", type=float, default=8)
    ap.add_argument("--out", help="default <course_dir>/_seg/real_timeline.json")
    ap.add_argument("--reorder-manifest", action="store_true",
                    help="rewrite manifest.json clips in real-time order "
                         "(backs up to manifest.json.bak-timeline-<date>). "
                         "==Refuses when any clip has no trustworthy time.==")
    a = ap.parse_args()

    course = a.course_dir
    man_path = os.path.join(course, "manifest.json")
    if not os.path.isfile(man_path):
        sys.exit(f"ERROR: no manifest.json in {course}")
    with open(man_path, encoding="utf-8") as fh:
        man = json.load(fh)
    src_dir = resolve_source_dir(course, a.source_dir)
    if not os.path.isdir(src_dir):
        sys.exit(f"ERROR: source dir not found: {src_dir}\n"
                 "  pass --source-dir; the timeline needs the ORIGINAL files "
                 "(transcoded copies have had their capture time stripped)")

    rows, missing = [], []
    for i, c in enumerate(man.get("clips", [])):
        p = os.path.join(src_dir, c["src"])
        if not os.path.isfile(p):
            hit = [e.path for e in os.scandir(src_dir) if e.name == c["src"]]
            p = hit[0] if hit else None
        if not p:
            missing.append(c["src"])
            rows.append({"file": c["src"], "kind": "media", "start": None,
                         "manifest_idx": i, "clip_name": c.get("name"),
                         "time_source": None, "error": "source file not found"})
            continue
        r = probe_one(p, a.utc_offset)
        r["manifest_idx"], r["clip_name"] = i, c.get("name")
        rows.append(r)
    photos = scan_photos(a.photos, a.utc_offset)

    offsets = parse_clock_offsets(a.clock_offset)
    n_shift = apply_clock_offsets(rows + photos, offsets)

    timed = [r for r in rows if r.get("start")]
    untimed = [r for r in rows if not r.get("start")]
    chrono = sorted(timed, key=lambda r: r["start"])

    # --- what the reordering would change -----------------------------
    reorder = [{"from_idx": r["manifest_idx"], "to_idx": i, "file": r["file"]}
               for i, r in enumerate(chrono) if r["manifest_idx"] != i]

    # --- photos -> which recording was running -------------------------
    for ph in photos:
        t = ph.get("start")
        ph["during"] = [r["file"] for r in timed
                        if t and r["start"] <= t <= (r["end"] or r["start"])] or None

    days = sorted({r["start"][:10] for r in timed})
    out = {
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "course": man.get("name") or os.path.basename(os.path.normpath(course)),
        "source_dir": src_dir,
        "clock_offsets_applied": [{"selector": s, "seconds": v} for s, v in offsets],
        "days": days,
        "sources": chrono + untimed,
        "photos": photos,
        "manifest_order_differs": reorder,
        "notes": [
            "real capture time is the ordering authority; the filename is a label",
            "a clock_offset is a MEASURED correction — never a guess; the "
            "device's own claim stays in capture_raw_start",
            "photos are a capture device too: 'during' says which recording was "
            "running when each frame was shot",
        ],
    }
    if missing:
        out["notes"].append(f"{len(missing)} clip(s) not found under {src_dir}: "
                            + ", ".join(missing[:8]))
    atomic_write_json(a.out or os.path.join(course, "_seg", "real_timeline.json"),
                      out, indent=1)

    for r in chrono:
        print("%-19s -> %-8s %6.1fm %-9s %s" % (
            r["start"], (r["end"] or "")[11:], (r["duration_s"] or 0) / 60,
            r["time_source"] or "?", r["file"]))
    for r in untimed:
        why = r.get("error") or "no capture clock in this file"
        print("%-19s    %-40s !! %s" % ("(unknown)", r["file"], why))
    print("\n%d clips (%d days: %s), %d photos, %d clock offset(s) applied"
          % (len(rows), len(days), ", ".join(days), len(photos), n_shift))
    if photos:
        orphan = sum(1 for p in photos if not p.get("during"))
        print("photos land inside a recording: %d/%d (%d outside any window)"
              % (len(photos) - orphan, len(photos), orphan))
    if reorder:
        print("!! manifest order != real order — %d clip(s) move:" % len(reorder))
        for m in reorder[:12]:
            print("     idx %2d -> %2d  %s" % (m["from_idx"], m["to_idx"], m["file"]))
        if not a.reorder_manifest:
            print("   re-run with --reorder-manifest to fix the manifest itself")
    elif untimed:
        # Saying "order already matches" here would be the exact silent degrade
        # this tool exists to stop: with nothing timed there is nothing to
        # compare, and a clean-looking verdict reads as a verified one.
        print("!! NOT CHECKED: %d of %d clip(s) have no capture time%s — this "
              "course's order is UNVERIFIED, not confirmed"
              % (len(untimed), len(rows),
                 " (source files gone; delivered courses often keep only the "
                 "web export)" if missing else ""))
    else:
        print("manifest order verified against real capture time: already correct")

    if a.reorder_manifest:
        if untimed:
            sys.exit("REFUSED: %d clip(s) have no trustworthy capture time; "
                     "reordering would silently invent an order for them. "
                     "Establish their time first (see multi-camera.md)."
                     % len(untimed))
        bak = man_path + ".bak-timeline-" + datetime.date.today().strftime("%Y%m%d")
        if not os.path.exists(bak):
            shutil.copy2(man_path, bak)
        by_file = {r["file"]: r for r in chrono}
        man["clips"] = sorted(man["clips"],
                              key=lambda c: by_file[c["src"]]["start"])
        for i, c in enumerate(man["clips"]):
            c["idx"] = i
            c["capture_start"] = by_file[c["src"]]["start"]
        atomic_write_json(man_path, man, indent=1)
        print("manifest reordered by real time; backup -> %s" % os.path.basename(bak))
        print("==rebuild anything derived from clip order== (build_L1.py, and "
              "any _seg/ proposal written against the old order)")


if __name__ == "__main__":
    main()
