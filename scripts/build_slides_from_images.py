# -*- coding: utf-8 -*-
"""Path B-images bridge: a folder of slide photos/screenshots -> slides_raw.json
+ slides_dedup.json, so Stage D (VLM) and Stage E (grounding) can run without
either the video frame stages (A/B/C) or a PDF deck.

This is the "the user photographed the screen / exported the deck as PNGs" path.
It is deliberately a sibling of build_slides_from_pdf.py and emits the same
schema, with the same three post-fix behaviours that file learned the hard way:

  * ==slide_id is 1-based== (Path A numbers from 1; a 0-based bridge made every
    [[EMBED sN]], attachment name and human-facing sN off by one, silently).
  * ==No --audio-duration-sec means timestamps 0 + no_audio: true==, never None
    — Stage D does int(timestamp_start) and int(None) killed the run on slide 1.
  * ==ocr_text_density is MEASURED== (RapidOCR bbox area / image area), never a
    character-count proxy: Stage D's skip gate is calibrated on the bbox scale,
    so a fabricated scale pre-skips normal slides as "decorative". Without
    RapidOCR the density is null, which every downstream gate reads as
    "unknown, therefore has text" — never as a reason to skip.

Ordering — the thing that actually goes wrong with dropped photos
----------------------------------------------------------------
A-Z filename ordering and hand-edited file times have both silently reordered
material before. So ordering here is EXIF DateTimeOriginal first (the same
probe media_capture_index.py uses — imported from it, not re-implemented),
falling back to file mtime, falling back to natural-sorted filename. The source
used is recorded per image in ``order_source`` and summarised on stdout, and
when the chosen order disagrees with plain filename order on more than 20% of
pairs you get a warning: that disagreement is exactly the case where one of the
two orderings is wrong and a human has to look.

==mtime is a hypothesis, not evidence.== A zip/Drive/Immich round-trip rewrites
it. When ordering falls back to mtime this prints a warning telling you to
verify the order visually before synthesising a note from it.

Images are COPIED into <out_dir>/slides/ under their ORIGINAL filenames (no
frame_NNNN renaming): downstream stages read <out_dir>/slides/<filename>, and
keeping the original name is what lets a human match a slide in the note back
to the photo they took. Nothing is moved or deleted.

Usage:
    python build_slides_from_images.py <img_dir> -o <out_dir>
                                       [--audio-duration-sec N]
                                       [--ext .jpg,.png]
                                       [--density-mode ocr|none]
                                       [--density-budget-sec 240]
"""
import argparse
import datetime
import json
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from _common import (atomic_write_json, fmt_hms, laplacian_variance,  # noqa: E402
                     rapidocr_engine, rapidocr_text_ex)
# Capture-time probing lives in media_capture_index; importing it (rather than
# copying the EXIF logic) is what keeps "how we read a capture time" a single
# fact. Import is side-effect free — that module only touches ffprobe in main().
from media_capture_index import probe_photo, to_local  # noqa: E402

BASE_EXTS = (".jpg", ".jpeg", ".png")
OPTIONAL_EXTS = {".heic": "pillow_heif", ".heif": "pillow_heif",
                 ".tif": None, ".tiff": None}

# Above this fraction of pairwise disagreements between the chosen order and
# plain filename order, the two orderings tell different stories and a human
# must decide which is right.
ORDER_DISAGREEMENT_WARN_FRAC = 0.20


def _heif_available():
    try:
        import pillow_heif  # noqa: F401
        return True
    except ImportError:
        return False


def default_exts():
    exts = list(BASE_EXTS)
    if _heif_available():
        exts += [".heic", ".heif"]
    return exts


def natural_key(name):
    """Filename sort key that reads digit runs as numbers.

    Plain lexical sort puts IMG_10 before IMG_9, which silently reorders a deck
    photographed in sequence — the failure this whole module is defensive about.
    """
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", name)]


def probe_capture(path, utc_offset):
    """(datetime|None, "exif"|"mtime"|"none", error|None) for one image."""
    info = probe_photo(path)
    dt, _src = to_local(info.get("creation_raw"), utc_offset)
    if dt is not None:
        return dt, "exif", info.get("error")
    try:
        return (datetime.datetime.fromtimestamp(os.path.getmtime(path)),
                "mtime", info.get("error"))
    except OSError as e:
        return None, "none", info.get("error") or str(e)


def discordant_pair_frac(order_a, order_b):
    """Fraction of pairs the two orderings disagree about (Kendall-tau style).

    Both arguments are lists of the same items. Returns 0.0 for <2 items.
    """
    n = len(order_a)
    if n < 2:
        return 0.0
    rank_b = {item: i for i, item in enumerate(order_b)}
    disc = 0
    total = 0
    for i in range(n):
        for j in range(i + 1, n):
            total += 1
            if rank_b[order_a[i]] > rank_b[order_a[j]]:
                disc += 1
    return disc / total if total else 0.0


def _bbox_area(bbox):
    """Polygon area via shoelace — the same formula quick_ocr.py uses."""
    try:
        n = len(bbox)
        if n < 3:
            return 0.0
        a = 0.0
        for i in range(n):
            x1, y1 = bbox[i]
            x2, y2 = bbox[(i + 1) % n]
            a += x1 * y2 - x2 * y1
        return abs(a) / 2.0
    except (TypeError, ValueError, IndexError):
        return 0.0


def _bbox_height(bbox):
    ys = [pt[1] for pt in bbox]
    return max(ys) - min(ys)


def _bbox_top_y(bbox):
    return min(pt[1] for pt in bbox)


def derive_title_guess(items, img_h):
    """Topmost-and-largest text under 60 chars — quick_ocr.py's rule verbatim."""
    if not items or img_h <= 0:
        return ""
    top_band = img_h * 0.35
    cands = []
    for it in items:
        bbox, text = it[0], it[1]
        if not text or not text.strip() or len(text) > 60:
            continue
        try:
            if _bbox_top_y(bbox) > top_band:
                continue
            h = _bbox_height(bbox)
        except (TypeError, ValueError, IndexError):
            continue
        cands.append((h, text.strip()))
    if not cands:
        return ""
    cands.sort(key=lambda c: c[0], reverse=True)
    return cands[0][1]


def image_size(path):
    from PIL import Image
    with Image.open(path) as im:
        return im.size


def main():
    ap = argparse.ArgumentParser(
        description="Path B-images: a directory of slide images -> "
                    "slides_raw.json + slides_dedup.json")
    ap.add_argument("img_dir", help="directory holding the slide images")
    ap.add_argument("-o", "--out-dir", required=True,
                    help="lecture output directory (slides/ is created inside)")
    ap.add_argument("--audio-duration-sec", type=int, default=None,
                    help="if set, spread timestamps evenly across the images; "
                         "else timestamps are 0 with no_audio=true")
    ap.add_argument("--ext", default=None,
                    help="comma-separated extensions to accept "
                         "(default: .jpg,.jpeg,.png plus .heic/.heif when "
                         "pillow-heif is installed)")
    ap.add_argument("--density-mode", choices=("ocr", "none"), default="ocr",
                    help="'ocr' measures real text density with RapidOCR "
                         "(default); 'none' writes null density (downstream "
                         "reads null as 'has text', never as a skip reason)")
    ap.add_argument("--density-budget-sec", type=int, default=240,
                    help="stop measuring density after this many seconds and "
                         "write null for the rest (default 240)")
    ap.add_argument("--utc-offset", type=float, default=8,
                    help="hours to add to UTC-stamped capture times (default 8)")
    args = ap.parse_args()

    if not os.path.isdir(args.img_dir):
        print(f"ERROR: not a directory: {args.img_dir}", file=sys.stderr)
        sys.exit(2)

    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        print("ERROR: Pillow is required to read slide images.\n"
              "  Fix with: pip install Pillow   (pillow-heif too, for .heic)",
              file=sys.stderr)
        sys.exit(2)

    if args.ext:
        exts = [e.strip().lower() if e.strip().startswith(".")
                else "." + e.strip().lower()
                for e in args.ext.split(",") if e.strip()]
    else:
        exts = default_exts()

    if _heif_available():
        import pillow_heif
        pillow_heif.register_heif_opener()
    elif any(e in (".heic", ".heif") for e in exts):
        print("WARNING: .heic/.heif requested but pillow-heif is not installed "
              "— those files will fail to open. Fix: pip install pillow-heif",
              file=sys.stderr)

    files = sorted((e.name for e in os.scandir(args.img_dir)
                    if e.is_file() and os.path.splitext(e.name)[1].lower() in exts),
                   key=natural_key)
    if not files:
        print(f"ERROR: no images with extensions {','.join(exts)} in "
              f"{args.img_dir}", file=sys.stderr)
        sys.exit(2)

    # --- Ordering -----------------------------------------------------
    probed = []
    for name in files:
        src_path = os.path.join(args.img_dir, name)
        dt, src, err = probe_capture(src_path, args.utc_offset)
        probed.append({"name": name, "path": src_path, "dt": dt,
                       "order_source": src, "probe_error": err})

    n_exif = sum(1 for p in probed if p["order_source"] == "exif")
    n_mtime = sum(1 for p in probed if p["order_source"] == "mtime")
    n_none = sum(1 for p in probed if p["order_source"] == "none")

    distinct_times = {p["dt"] for p in probed if p["dt"] is not None}
    if n_none == len(probed) or (len(probed) > 1 and len(distinct_times) < 2):
        # Every image shares one timestamp (a bulk export/copy) or has none —
        # the timestamps carry no ordering information at all, so say so rather
        # than pretending an arbitrary stable sort was a capture-time order.
        ordered = list(probed)  # already natural-filename sorted
        for p in ordered:
            p["order_source"] = "filename"
        primary = "filename"
    else:
        ordered = sorted(
            probed,
            key=lambda p: (p["dt"] is None,
                           p["dt"] or datetime.datetime.min,
                           natural_key(p["name"])))
        primary = "exif" if n_exif else "mtime"
        for p in ordered:
            if p["dt"] is None:
                p["order_source"] = "filename"

    print(f"ordering: primary={primary}  "
          f"(exif={n_exif}, mtime={n_mtime}, none={n_none}, n={len(probed)})")
    if primary != "filename":
        frac = discordant_pair_frac([p["name"] for p in ordered],
                                    sorted(files, key=natural_key))
        if frac > ORDER_DISAGREEMENT_WARN_FRAC:
            print(f"!! WARNING: capture-time order and filename order disagree on "
                  f"{frac:.0%} of pairs (> {ORDER_DISAGREEMENT_WARN_FRAC:.0%}). "
                  "One of them is wrong — open the first few slides and check "
                  "before writing a note from this order.", file=sys.stderr)
    if n_mtime and primary == "mtime":
        print("!! WARNING: ordering fell back to file mtime for some/all images. "
              "mtime is rewritten by any zip/Drive/Immich round-trip, so this "
              "order is a HYPOTHESIS — verify it visually.", file=sys.stderr)
    perr = [p["name"] for p in probed if p["probe_error"]]
    if perr:
        print(f"   {len(perr)} image(s) had an EXIF read problem "
              f"(first few: {', '.join(perr[:5])})", file=sys.stderr)

    # --- Stage the images under out_dir/slides ------------------------
    slides_dir = os.path.join(args.out_dir, "slides")
    os.makedirs(slides_dir, exist_ok=True)
    for p in ordered:
        dst = os.path.join(slides_dir, p["name"])
        # Copy, never move: the user's originals stay untouched. Same-file is
        # the normal case when img_dir already IS out_dir/slides.
        if os.path.abspath(dst) != os.path.abspath(p["path"]):
            shutil.copy2(p["path"], dst)
        p["staged"] = dst

    # --- OCR / entropy ------------------------------------------------
    engine = None
    # Why there is no OCR text, kept distinguishable: "the user turned it off"
    # and "the engine is missing" look identical in the output otherwise, and
    # only one of them is something to go fix.
    no_ocr_reason = "ocr_disabled"
    if args.density_mode == "ocr":
        engine = rapidocr_engine()
        no_ocr_reason = "engine_unavailable"
        if engine is None:
            print("  OCR: RapidOCR unavailable -> quick_text empty, density null, "
                  "parse_valid false (downstream reads null density as 'has "
                  "text', so nothing is wrongly pre-skipped). "
                  "Fix: pip install rapidocr_onnxruntime", file=sys.stderr)

    n = len(ordered)
    per_slide_s = (args.audio_duration_sec / n) if args.audio_duration_sec else None
    has_audio = per_slide_s is not None

    out = []
    t0 = time.time()
    budget_hit = False
    for i, p in enumerate(ordered):
        img_path = p["staged"]

        if has_audio:
            ts_start = int(i * per_slide_s)
            ts_end = int((i + 1) * per_slide_s)
            time_str = f"{fmt_hms(ts_start)}-{fmt_hms(ts_end)}"
        else:
            ts_start = ts_end = 0
            time_str = ""

        text, conf, items, ocr_err = "", 0.0, [], no_ocr_reason
        if engine is not None:
            if not budget_hit and time.time() - t0 > args.density_budget_sec:
                budget_hit = True
                print(f"  OCR: {args.density_budget_sec}s budget reached at image "
                      f"{i + 1}/{n}; the rest get null density and empty text",
                      file=sys.stderr)
            if budget_hit:
                ocr_err = "density_budget_exhausted"
            else:
                text, conf, items, ocr_err = rapidocr_text_ex(img_path, engine)
                if ocr_err:
                    print(f"  WARN: OCR failed on {p['name']}: {ocr_err}",
                          file=sys.stderr)

        try:
            img_w, img_h = image_size(img_path)
        except Exception as e:  # noqa: BLE001 — unreadable image, per file
            print(f"  WARN: cannot read image {p['name']}: {e}", file=sys.stderr)
            img_w, img_h = 0, 0

        if ocr_err or img_w <= 0 or img_h <= 0:
            density = None
        else:
            total = sum(_bbox_area(it[0]) for it in items)
            density = round(min(total / (img_w * img_h), 1.0), 3)

        slide = {
            "slide_id": i + 1,               # 1-based, matching Path A
            "pipeline_stage": "dedup",
            "filename": p["name"],           # original name, staged as-is
            "source_page": i + 1,
            "timestamp_start": ts_start,
            "timestamp_end": ts_end,
            "time_str": time_str,
            "no_audio": not has_audio,
            # Extra vs the PDF bridge: how this slide's position was decided,
            # and the capture time it was decided from. Kept per slide because
            # a mixed-source deck is exactly where ordering goes wrong.
            "order_source": p["order_source"],
            "capture_time": p["dt"].isoformat(sep=" ") if p["dt"] else None,
            "ocr": {
                "quick_text": text,
                "quick_title_guess": derive_title_guess(items, img_h),
                "ocr_text_density": density,
                "parse_valid": ocr_err is None,
                "ocr_error": ocr_err,
                "model_confidence": round(float(conf or 0.0), 3),
                "source": "rapidocr",
            },
            "entropy": {
                "file_size_kb": round(os.path.getsize(img_path) / 1024.0, 1),
                "laplacian_variance": laplacian_variance(img_path),
                "quick_text_len": len(text),
            },
            # Every image the user handed over is canonical: they already did
            # the deduplication by choosing what to photograph.
            "dedup": {
                "phash_group": i, "semantic_group": i,
                "superseded_by": None,
                "is_canonical": True, "ssim_to_prev": None,
                "histogram_similarity": None, "ocr_overlap_ratio": None,
                "layout_similarity": None,
            },
        }
        out.append(slide)

    os.makedirs(args.out_dir, exist_ok=True)
    raw = [dict(s, pipeline_stage="raw") for s in out]
    atomic_write_json(os.path.join(args.out_dir, "slides_raw.json"), raw)
    atomic_write_json(os.path.join(args.out_dir, "slides_dedup.json"), out)

    span = (f"per-slide={per_slide_s:.1f}s" if has_audio
            else "no-audio (timestamps 0)")
    n_text = sum(1 for s in out if s["ocr"]["quick_text"])
    print(f"OK {os.path.basename(os.path.normpath(args.out_dir))}: {n} images -> "
          f"raw + dedup (all canonical, {span}, {n_text}/{n} with OCR text)")
    if not has_audio:
        print("   no --audio-duration-sec: timestamps are 0 and every slide "
              "carries no_audio=true — do NOT read them as positions in a "
              "recording. Pass the audio duration to spread them.")


if __name__ == "__main__":
    main()
