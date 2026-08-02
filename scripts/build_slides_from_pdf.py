"""Path B bridge: build slides_raw.json + slides_dedup.json from a PDF/HTML/docx
lecture's pdf_text.json, so Stage D (VLM) and Stage E (grounding) can run
without the video-frame Stages A/B/C.

For PDF/HTML/docx lectures the page text is already canonical (extracted by
fitz / html parser), so every page becomes one canonical slide — no dedup.

Accepts both pdf_text.json schemas seen in the wild:
  - {"page": N, "text": "..."}                      -> image page_NN.jpg
  - {"slide_id": N, "image_filename": "...", "text"} -> image as named
An optional intro file (pdf_text_intro.json) is prepended if present.

Entropy metrics come from _common (the same laplacian_variance Stage B uses, so
one threshold means one thing everywhere — this file used to carry a divergent
copy with a different resample filter and variance range).

`ocr_text_density` is measured, not guessed: RapidOCR runs on each rendered page
to get real bbox-area-over-page-area, the scale Stage D's skip gate is calibrated
on. The old character-count proxy was on a completely different scale, so a normal
slide under ~250 characters could be pre-skipped as decorative. Without RapidOCR
the density is written as null, which every downstream gate treats as "unknown,
therefore has text" (never as a reason to skip).

Timestamps: evenly spaced across --audio-duration-sec when given. Without it,
there is no audio to align to, so timestamps are 0 and each slide carries
`no_audio: true` — Stage D/E format 0 fine, whereas the previous `null` reached
`int(None)` and killed the documented Path B command on its first slide.

Usage:
    python build_slides_from_pdf.py <lecture_dir> [--audio-duration-sec N]
                                    [--density-mode ocr|none]
                                    [--density-budget-sec 240]
"""
import argparse, json, os, re, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from _common import atomic_write_json, fmt_hms, laplacian_variance, rapidocr_text_ex  # noqa: E402

# More than this fraction of pages with no rendered image means the render step
# failed, not that a few docx pages were text-only.
MISSING_RENDER_FATAL_FRAC = 0.5


def _bbox_area(bbox):
    """Polygon area via shoelace. Same formula quick_ocr.py uses for density."""
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


def measure_density(img_path, engine):
    """Real OCR text density (bbox area / image area), or None if unmeasurable."""
    if engine is None:
        return None
    try:
        from PIL import Image
        with Image.open(img_path) as im:
            w, h = im.size
    except Exception:
        return None
    if w <= 0 or h <= 0:
        return None
    _text, _conf, items, err = rapidocr_text_ex(img_path, engine)
    if err:
        return None
    total = sum(_bbox_area(it[0]) for it in items)
    return round(min(total / (w * h), 1.0), 3)


def _resolve_image(item, idx, slides_dir):
    """Return the image filename for a pdf_text item, trying both schemas."""
    fn = item.get("image_filename")
    if fn:
        return fn
    page = item.get("page", idx + 1)
    m = re.findall(r"\d+", str(page))
    num = int(m[0]) if m else (idx + 1)
    for cand in (f"page_{num:02d}.jpg", f"page_{num:03d}.jpg", f"frame_{num:04d}.jpg"):
        if os.path.isfile(os.path.join(slides_dir, cand)):
            return cand
    return f"page_{num:02d}.jpg"


def main():
    ap = argparse.ArgumentParser(description="Path B: pdf_text.json -> slides_raw/dedup.json")
    ap.add_argument("lecture_dir")
    ap.add_argument("--audio-duration-sec", type=int, default=None,
                    help="If set, spread timestamps evenly across slides; else 0 "
                         "with no_audio=true")
    ap.add_argument("--density-mode", choices=("ocr", "none"), default="ocr",
                    help="'ocr' measures real text density with RapidOCR (default); "
                         "'none' writes null density (treated downstream as "
                         "'has text', never as a skip reason)")
    ap.add_argument("--density-budget-sec", type=int, default=240,
                    help="Stop measuring density after this many seconds and write "
                         "null for the rest (default 240, keeps long decks inside "
                         "a caller's 300s timeout)")
    args = ap.parse_args()

    d = args.lecture_dir
    slides_dir = os.path.join(d, "slides")
    pt_path = os.path.join(d, "pdf_text.json")
    if not os.path.isfile(pt_path):
        print(f"ERROR: {pt_path} not found", file=sys.stderr)
        sys.exit(2)

    pages = []
    intro_path = os.path.join(d, "pdf_text_intro.json")
    if os.path.isfile(intro_path):
        with open(intro_path, encoding="utf-8") as fh:
            pages += json.load(fh)
    with open(pt_path, encoding="utf-8") as fh:
        pages += json.load(fh)

    n = len(pages)
    if n == 0:
        print(f"ERROR: {pt_path} contains no pages", file=sys.stderr)
        sys.exit(2)

    # Pass 1: which pages actually have a rendered image. Doing this first means
    # per-slide duration divides by the number of slides we emit, not by the
    # pre-filter page count (which made every timestamp run short).
    resolved = []
    missing = []
    for i, item in enumerate(pages):
        fn = _resolve_image(item, i, slides_dir)
        img_path = os.path.join(slides_dir, fn)
        if os.path.isfile(img_path):
            resolved.append((i, item, fn, img_path))
        else:
            missing.append(fn)

    if missing:
        frac = len(missing) / n
        print(f"  {len(missing)}/{n} pages have no rendered image "
              f"(first few: {', '.join(missing[:5])})", file=sys.stderr)
        if frac > MISSING_RENDER_FATAL_FRAC:
            print(f"ERROR: {frac:.0%} of pages are missing their rendered image — "
                  "the page-render step failed. Re-render before building slides "
                  "(a deck with half its pages gone is not a usable lecture).",
                  file=sys.stderr)
            sys.exit(2)

    n_emitted = len(resolved)
    if n_emitted == 0:
        print("ERROR: no page images found; nothing to build", file=sys.stderr)
        sys.exit(2)

    per_slide_s = (args.audio_duration_sec / n_emitted) if args.audio_duration_sec else None
    has_audio = per_slide_s is not None

    engine = None
    if args.density_mode == "ocr":
        from _common import rapidocr_engine
        engine = rapidocr_engine()
        if engine is None:
            print("  density: RapidOCR unavailable -> writing null density "
                  "(downstream treats null as 'has text', so nothing gets "
                  "wrongly pre-skipped)", file=sys.stderr)

    out = []
    t0 = time.time()
    density_budget_hit = False
    for out_idx, (i, item, fn, img_path) in enumerate(resolved):
        text = (item.get("text") or "").strip()
        title = next((ln.strip()[:60] for ln in text.splitlines() if ln.strip()), "")

        if has_audio:
            ts_start = int(out_idx * per_slide_s)
            ts_end = int((out_idx + 1) * per_slide_s)
            time_str = f"{fmt_hms(ts_start)}-{fmt_hms(ts_end)}"
        else:
            ts_start = ts_end = 0
            time_str = ""

        if engine is not None and not density_budget_hit:
            if time.time() - t0 > args.density_budget_sec:
                density_budget_hit = True
                print(f"  density: {args.density_budget_sec}s budget reached at "
                      f"page {out_idx + 1}/{n_emitted}; remaining pages get null "
                      "density", file=sys.stderr)
                density = None
            else:
                density = measure_density(img_path, engine)
        else:
            density = None

        slide = {
            # 1-based, matching Path A (extract_slides numbers slides from 1).
            # The old `item.get("slide_id", i)` fallback was 0-based, so every
            # [[EMBED sN]], attachment name and human-facing sN in a PDF lecture
            # was off by one relative to a video lecture — silently.
            "slide_id": item.get("slide_id", out_idx + 1),
            "pipeline_stage": "dedup",
            "filename": fn,
            "source_page": item.get("page", i + 1),
            "timestamp_start": ts_start,
            "timestamp_end": ts_end,
            "time_str": time_str,
            # No audio to align against — consumers should not read these
            # timestamps as real positions in a recording.
            "no_audio": not has_audio,
            "ocr": {
                "quick_text": text,
                "quick_title_guess": title,
                "ocr_text_density": density,
                "parse_valid": True,
                "model_confidence": 1.0 if text else 0.0,
                "source": "pdf",
            },
            "entropy": {
                "file_size_kb": round(os.path.getsize(img_path) / 1024.0, 1),
                "laplacian_variance": laplacian_variance(img_path),
                "quick_text_len": len(text),
            },
            "dedup": {
                "phash_group": out_idx, "semantic_group": out_idx,
                "superseded_by": None,
                "is_canonical": True, "ssim_to_prev": None,
                "histogram_similarity": None, "ocr_overlap_ratio": None,
                "layout_similarity": None,
            },
        }
        out.append(slide)

    # slides_raw.json is Stage B's product and slides_dedup.json is Stage C's;
    # label each with the stage whose file it is, instead of stamping "dedup" on
    # both.
    raw = [dict(s, pipeline_stage="raw") for s in out]
    atomic_write_json(os.path.join(d, "slides_raw.json"), raw)
    atomic_write_json(os.path.join(d, "slides_dedup.json"), out)

    span = f"per-slide={per_slide_s:.1f}s" if has_audio else "no-audio (timestamps 0)"
    print(f"OK {os.path.basename(d)}: {n_emitted}/{n} pages -> raw + dedup "
          f"(all canonical, {span})")


if __name__ == "__main__":
    main()
