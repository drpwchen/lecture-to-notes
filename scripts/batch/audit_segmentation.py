# -*- coding: utf-8 -*-
r"""Audit an existing segmentation against the course's real timeline.

    python audit_segmentation.py <course_dir> [--timeline PATH] [--proposal PATH]
                                 [--json OUT] [--gap-min 8] [--chain-sec 5]

==The question this answers: after the clip order was fixed, is the existing
proposal still usable, or must it be regenerated?== Re-proposing every course
is expensive; trusting a proposal that was written against a wrong order is
worse. This is the mechanical part of that judgement — it reports evidence and
a verdict, and never rewrites the proposal itself.

Checks
------
1. **Staleness** — was the proposal written before the timeline / before the
   manifest was reordered? A proposal that predates the reorder was reasoned
   against an order that never happened.
2. **Coverage** — every source on the timeline should appear somewhere in the
   proposal text. Sources nobody mentions are orphans (the 289 unused photos of
   the 2024-05 course were exactly this).
3. **Continuity** — clips that chain end-to-start within `--chain-sec` are ONE
   recording split by the camera; a proposal that cuts between them, or that
   merges across a `--gap-min` break, is claiming something the clock denies.
4. **Parallel tracks** — two audio-bearing sources covering the same wall-clock
   window are the same session recorded twice, not two sessions.
5. **Photo coverage** — recordings that have photos taken during them, so an
   "audio only, no slides" segment can be recognised as a Path B-images one.

==Verdicts are advisory.== A REGENERATE verdict means the evidence a human
reasoned from has changed, not that the conclusions are necessarily wrong.
"""
import argparse, datetime, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from _common import atomic_write_json  # noqa: E402

P = datetime.datetime.fromisoformat


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("course_dir")
    ap.add_argument("--timeline", help="default <course>/_seg/real_timeline.json")
    ap.add_argument("--proposal", help="default <course>/_seg/proposal.md")
    ap.add_argument("--json", help="also write the findings as JSON")
    ap.add_argument("--gap-min", type=float, default=8.0,
                    help="a gap this long (minutes) is a session break")
    ap.add_argument("--chain-sec", type=float, default=5.0,
                    help="clips chaining within this are one split recording")
    a = ap.parse_args()

    course = a.course_dir
    tl_path = a.timeline or os.path.join(course, "_seg", "real_timeline.json")
    pr_path = a.proposal or os.path.join(course, "_seg", "proposal.md")
    man_path = os.path.join(course, "manifest.json")
    if not os.path.isfile(tl_path):
        sys.exit(f"ERROR: no timeline at {tl_path}\n"
                 "  run course_timeline.py first — there is nothing to audit "
                 "a proposal against until real capture times exist")
    tl = load(tl_path)
    timed = [s for s in tl["sources"] if s.get("start")]
    timed.sort(key=lambda s: s["start"])
    findings, verdict = [], "OK"

    def flag(level, code, msg, **extra):
        nonlocal verdict
        findings.append(dict(level=level, code=code, message=msg, **extra))
        if level == "REGENERATE" or (level == "REVIEW" and verdict == "OK"):
            verdict = level

    # --- 1. staleness --------------------------------------------------
    have_proposal = os.path.isfile(pr_path)
    text = ""
    if not have_proposal:
        flag("REGENERATE", "no-proposal", "no proposal.md — nothing was ever "
             "proposed for this course")
    else:
        with open(pr_path, encoding="utf-8") as fh:
            text = fh.read()
        pr_m, tl_m = os.path.getmtime(pr_path), os.path.getmtime(tl_path)
        if pr_m < tl_m:
            flag("REGENERATE", "predates-timeline",
                 "proposal.md is older than real_timeline.json — it was written "
                 "before real capture times were known (proposal %s, timeline %s)"
                 % (datetime.datetime.fromtimestamp(pr_m).strftime("%Y-%m-%d %H:%M"),
                    datetime.datetime.fromtimestamp(tl_m).strftime("%Y-%m-%d %H:%M")))
        baks = [f for f in os.listdir(os.path.dirname(man_path) or ".")
                if f.startswith("manifest.json.bak-timeline-")] \
            if os.path.isdir(os.path.dirname(man_path) or ".") else []
        if baks and os.path.isfile(man_path) and pr_m < os.path.getmtime(man_path):
            flag("REGENERATE", "predates-reorder",
                 "the manifest was reordered by real time AFTER this proposal "
                 "was written (%s) — its clip ordering reasoning is void"
                 % ", ".join(sorted(baks)[:3]))

    # --- 2. coverage ---------------------------------------------------
    if have_proposal:
        # Match on the stem as well as the full name: proposals routinely write
        # `<speaker>-1-00002` without the extension, and demanding the extension
        # reported every correctly-cited source as missing.
        unmentioned = [s["file"] for s in timed
                       if s["kind"] != "photo"
                       and s["file"] not in text
                       and os.path.splitext(s["file"])[0] not in text]
        if unmentioned:
            flag("REVIEW", "unmentioned-sources",
                 "%d recording(s) on the timeline are never named in the "
                 "proposal: %s" % (len(unmentioned), ", ".join(unmentioned[:6])),
                 files=unmentioned)

    # --- 3. continuity vs the proposed cuts ----------------------------
    media = [s for s in timed if s["kind"] != "photo"]
    chains, breaks = [], []
    for x, y in zip(media, media[1:]):
        if not x.get("end"):
            continue
        gap = (P(y["start"]) - P(x["end"])).total_seconds()
        if gap < 0:
            continue                      # overlap: handled as a parallel track
        if gap <= a.chain_sec:
            chains.append((x["file"], y["file"], round(gap, 1)))
        elif gap >= a.gap_min * 60:
            breaks.append((x["file"], y["file"], round(gap / 60, 1)))
    if chains:
        findings.append(dict(level="INFO", code="split-recordings",
                             message="%d adjacent pair(s) chain within %.0fs — "
                                     "each pair is ONE recording the camera "
                                     "split; a segment boundary between them "
                                     "needs a content reason"
                                     % (len(chains), a.chain_sec), pairs=chains))
    if breaks:
        findings.append(dict(level="INFO", code="natural-breaks",
                             message="%d gap(s) >= %.0f min — these are the "
                                     "boundaries the clock itself suggests"
                                     % (len(breaks), a.gap_min), gaps=breaks))

    # --- 4. parallel tracks --------------------------------------------
    par = []
    for i, x in enumerate(media):
        for y in media[i + 1:]:
            if not (x.get("end") and y.get("end")):
                continue
            lo, hi = max(P(x["start"]), P(y["start"])), min(P(x["end"]), P(y["end"]))
            ov = (hi - lo).total_seconds()
            if ov > 60:
                par.append((x["file"], y["file"], round(ov / 60, 1)))
    if par:
        flag("REVIEW", "parallel-tracks",
             "%d source pair(s) overlap in wall-clock time — the same session "
             "recorded twice, which must be ONE segment with two sources, not "
             "two segments: %s" % (len(par), "; ".join(
                 "%s ∥ %s (%.0f min)" % p for p in par[:4])), pairs=par)

    # --- 5. photos as a slide source -----------------------------------
    photos = [p for p in tl.get("photos", []) if p.get("start")]
    if photos:
        per = {}
        for p in photos:
            for f in (p.get("during") or ["(outside any recording)"]):
                per[f] = per.get(f, 0) + 1
        rich = {f: n for f, n in per.items() if n >= 3 and f != "(outside any recording)"}
        # Drop the recordings whose photos are already IN the pipeline: a
        # slides_raw.json listing the photo filenames means Path B-images was
        # run for that clip. Without this the audit keeps demanding work that
        # is finished, and an advisory nobody can clear gets ignored wholesale.
        done = []
        if rich and os.path.isfile(man_path):
            names = {p["file"] for p in photos}
            for c in load(man_path).get("clips", []):
                raw = os.path.join(course, "clips", c.get("name") or "",
                                   "slides_raw.json")
                if c.get("src") in rich and os.path.isfile(raw):
                    try:
                        got = {s.get("filename") for s in load(raw)}
                    except (ValueError, KeyError, TypeError):
                        continue
                    if got & names:
                        done.append(c["src"])
            for f in done:
                rich.pop(f, None)
        if done:
            findings.append(dict(
                level="INFO", code="photos-ingested",
                message="%d recording(s) already carry their photos as slides "
                        "(Path B-images done): %s" % (len(done), ", ".join(done))))
        if rich:
            # Count DISTINCT photos, not the sum over recordings: where a talk
            # was captured on video and audio at once, every photo of it sits
            # inside both windows and summing double-counts it.
            n_distinct = len({p["file"] for p in photos
                              if any(f in rich for f in (p.get("during") or []))})
            flag("REVIEW", "photos-available",
                 "%d photo(s) were taken during %d recording(s) — a recording "
                 "with photos is not slide-less, it is a Path B-images source: %s"
                 % (n_distinct, len(rich),
                    ", ".join("%s×%d" % (f, n) for f, n in
                              sorted(rich.items(), key=lambda kv: -kv[1])[:5])),
                 per_recording=rich)
        out = per.get("(outside any recording)", 0)
        if out:
            findings.append(dict(level="INFO", code="photos-outside",
                                 message="%d photo(s) fall outside every "
                                         "recording window — usually shot "
                                         "before recording started" % out))

    # --- report ---------------------------------------------------------
    # Re-derive the order from the manifest ON DISK. The timeline's
    # `manifest_order_differs` is a record of what the reordering run MOVED —
    # reading it as current state made a just-fixed course report as still
    # broken, which is the worst kind of false alarm: it asks for work already
    # done and casts doubt on a correct result.
    order_issue = []
    if os.path.isfile(man_path):
        # Compare on basenames: the timeline stores `file` as a basename while a
        # manifest `src` may carry a subdirectory (Demo-Prac\00008.MTS). Matching
        # the raw strings makes the intersection empty for those courses, so
        # order_issue comes out empty and the run falls through to the INFO
        # branch below — reporting "order was corrected" about a course nobody
        # checked. A silent pass is the one outcome this audit must never give.
        by_file = {os.path.basename(s["file"]): s for s in timed}
        cur = [os.path.basename(c["src"]) for c in load(man_path).get("clips", [])]
        want = [f for f in (os.path.basename(s["file"]) for s in timed) if f in cur]
        have = [f for f in cur if f in by_file]
        order_issue = [f for f, g in zip(have, want) if f != g]
    if order_issue:
        flag("REGENERATE", "manifest-order-wrong",
             "%d clip(s) are out of real-time order in manifest.json right now "
             "— run course_timeline.py --reorder-manifest first"
             % len(order_issue))
    elif tl.get("manifest_order_differs"):
        findings.append(dict(
            level="INFO", code="manifest-was-reordered",
            message="manifest clip order was corrected by real time (%d clip(s) "
                    "moved); anything written against the OLD order — proposal, "
                    "_L1/, segments.json — has to be rebuilt"
                    % len(tl["manifest_order_differs"])))
    untimed = [s["file"] for s in tl["sources"] if not s.get("start")]
    if untimed:
        flag("REVIEW", "unverifiable",
             "%d source(s) have no capture time, so their placement cannot be "
             "checked at all: %s" % (len(untimed), ", ".join(untimed[:6])),
             files=untimed)

    print("== %s" % tl.get("course", course))
    print("   %d timed source(s) over %s" % (len(timed), ", ".join(tl.get("days") or [])))
    for f in findings:
        print("   [%-10s] %s" % (f["level"], f["message"]))
    print("\nVERDICT: %s" % verdict)
    print({"OK": "  proposal is consistent with the real timeline",
           "REVIEW": "  usable, but the flagged points need a human decision",
           "REGENERATE": "  re-run the Step-1 segmentation agent against the "
                         "corrected order"}[verdict])
    if a.json:
        atomic_write_json(a.json, {"course": tl.get("course"), "verdict": verdict,
                                   "findings": findings}, indent=1)


if __name__ == "__main__":
    main()
