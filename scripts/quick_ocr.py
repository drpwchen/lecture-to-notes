"""Stage B: cheap OCR for every extracted frame (RapidOCR, CPU).

Purpose: provide cheap text + title guess + density signal for Stage C
(semantic dedup) and Stage E (transcript grounding). Also computes
*entropy metrics* (file size, Laplacian variance) used by Stage D as
inputs to the 4-condition VLM pre-skip gate. Recording the raw metrics
for *every* slide (not just the skipped ones) is essential for later
threshold tuning + false-negative analysis.

RapidOCR is REQUIRED here, not optional: with no OCR text every slide looks
decorative to the Stage D skip gate, so a machine missing the package used to
produce a confidently empty note and exit 0. Missing engine => exit 2.

Usage:
    python quick_ocr.py <out_dir> [--force]

Inputs:
    <out_dir>/slides/timestamps.json
    <out_dir>/slides/frame_NNNN.jpg

Output:
    <out_dir>/slides_raw.json
    [
      {
        "slide_id": int,
        "pipeline_stage": "raw",
        "filename": "frame_NNNN.jpg",
        "timestamp_start": int,      # = timestamps.json `first_ts`
        "timestamp_end": int,        # = timestamps.json `timestamp`
        "time_str": "MM:SS-MM:SS",
        "missing_frame": bool,       # true = image absent; entry kept as a
                                     #   placeholder so slide_ids stay contiguous
        "ocr": {
          "quick_text": str,
          "quick_title_guess": str,
          "ocr_text_density": float,  # bbox area / image area
          "parse_valid": bool,        # false = the OCR call FAILED (not "no text")
          "ocr_error": str | null,    # why parse_valid is false
          "model_confidence": float
        },
        "entropy": {
          "file_size_kb": float,        # disk size — text slides compress
          "laplacian_variance": float,  # 3x3 Laplacian variance on 512w grayscale
          "quick_text_len": int         # convenience for downstream skip rule
        }
      }
    ]
"""
import argparse
import json
import os
import statistics
import sys
import time

try:
    from _common import (RAPIDOCR_INSTALL_HINT, atomic_write_json, fmt_hms,
                         laplacian_variance, rapidocr_engine, rapidocr_text_ex)
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _common import (RAPIDOCR_INSTALL_HINT, atomic_write_json, fmt_hms,
                         laplacian_variance, rapidocr_engine, rapidocr_text_ex)

try:
    from _log import StageLogger
except Exception:
    StageLogger = None  # type: ignore


def get_image_size(path):
    from PIL import Image
    with Image.open(path) as img:
        return img.size  # (w, h)


# ----------------------------------------------------------------------
# Entropy metrics — feed VLM pre-skip gate (Stage D) and threshold tuning.
# Logged for every slide, regardless of skip decision.
# ----------------------------------------------------------------------

def file_size_kb(path):
    try:
        return round(os.path.getsize(path) / 1024.0, 1)
    except OSError:
        return 0.0


def bbox_area(bbox):
    """Polygon area via shoelace formula. bbox is [[x,y], [x,y], [x,y], [x,y]]."""
    n = len(bbox)
    if n < 3:
        return 0.0
    a = 0.0
    for i in range(n):
        x1, y1 = bbox[i]
        x2, y2 = bbox[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def bbox_height(bbox):
    ys = [pt[1] for pt in bbox]
    return max(ys) - min(ys)


def bbox_top_y(bbox):
    return min(pt[1] for pt in bbox)


def derive_title_guess(ocr_results, img_height):
    """Title = topmost text with largest font, length < 60.

    Largest font = bbox height in top 35% of image.
    """
    if not ocr_results:
        return ""

    top_band = img_height * 0.35
    candidates = []
    for bbox, text, conf in ocr_results:
        if not text or not text.strip():
            continue
        if len(text) > 60:
            continue
        try:
            if bbox_top_y(bbox) > top_band:
                continue
            h = bbox_height(bbox)
        except (TypeError, ValueError, IndexError):
            continue
        candidates.append((h, text.strip(), conf))

    if not candidates:
        return ""

    # Largest bbox height wins
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][1]


def derive_density(ocr_results, img_w, img_h):
    if not ocr_results or img_w <= 0 or img_h <= 0:
        return 0.0
    total = 0.0
    for r in ocr_results:
        try:
            total += bbox_area(r[0])
        except (TypeError, ValueError, IndexError):
            continue
    return min(total / (img_w * img_h), 1.0)


def derive_confidence(ocr_results):
    if not ocr_results:
        return 0.0
    confs = [r[2] for r in ocr_results if len(r) >= 3]
    if not confs:
        return 0.0
    return float(statistics.mean(confs))


def main():
    parser = argparse.ArgumentParser(description="Stage B: quick OCR for every frame")
    parser.add_argument("out_dir", help="Lecture output directory (containing slides/)")
    parser.add_argument("--force", action="store_true",
                        help="Accepted for compatibility. This stage always re-runs "
                             "and always overwrites slides_raw.json, so the flag is "
                             "currently a no-op; adding skip-if-exists behaviour "
                             "would change what existing callers get.")
    args = parser.parse_args()

    slides_dir = os.path.join(args.out_dir, "slides")
    ts_path = os.path.join(slides_dir, "timestamps.json")
    out_path = os.path.join(args.out_dir, "slides_raw.json")

    if not os.path.isfile(ts_path):
        print(f"ERROR: {ts_path} not found. Run extract_slides.py first.", file=sys.stderr)
        sys.exit(2)

    with open(ts_path, "r", encoding="utf-8") as f:
        slides = json.load(f)

    # Preflight: no engine, no stage. Degrading to empty text here poisons every
    # downstream gate silently, so this is fatal rather than a warning.
    engine = rapidocr_engine()
    if engine is None:
        print("ERROR: RapidOCR is unavailable, so Stage B cannot produce any text.\n"
              f"  {RAPIDOCR_INSTALL_HINT}", file=sys.stderr)
        sys.exit(2)

    log = StageLogger("quick_ocr", args.out_dir,
                      extra={"total": len(slides)}) if StageLogger else None
    if log:
        log.start()

    results = []
    n_missing = 0
    n_ocr_error = 0
    start = time.time()

    for idx, s in enumerate(slides):
        first_ts = s.get("first_ts", s["timestamp"])
        end_ts = s["timestamp"]
        time_str = f"{fmt_hms(first_ts)}-{fmt_hms(end_ts)}"
        img_path = os.path.join(slides_dir, s["filename"])

        if not os.path.isfile(img_path):
            # Keep a placeholder entry. Dropping it left holes in the slide_id
            # sequence, and downstream stages assume contiguity.
            n_missing += 1
            print(f"  MISSING: {s['filename']} (kept as placeholder)", file=sys.stderr)
            if log:
                log.item_error("file_not_found", slide_id=s.get("slide"),
                               filename=s["filename"])
            results.append({
                "slide_id": s["slide"],
                "pipeline_stage": "raw",
                "filename": s["filename"],
                "timestamp_start": first_ts,
                "timestamp_end": end_ts,
                "time_str": time_str,
                "missing_frame": True,
                "ocr": {
                    "quick_text": "",
                    "quick_title_guess": "",
                    "ocr_text_density": 0.0,
                    "parse_valid": False,
                    "ocr_error": "frame_file_missing",
                    "model_confidence": 0.0,
                },
                "entropy": {
                    "file_size_kb": 0.0,
                    "laplacian_variance": 0.0,
                    "quick_text_len": 0,
                },
            })
            continue

        try:
            img_w, img_h = get_image_size(img_path)
        except Exception as e:
            print(f"  WARN: cannot read image {s['filename']}: {e}", file=sys.stderr)
            img_w, img_h = (0, 0)

        quick_text, confidence, ocr_results, ocr_error = rapidocr_text_ex(
            img_path, engine)
        if ocr_error:
            n_ocr_error += 1
            print(f"  WARN: OCR failed on {s['filename']}: {ocr_error}",
                  file=sys.stderr)

        title_guess = derive_title_guess(ocr_results, img_h) if img_h > 0 else ""
        density = derive_density(ocr_results, img_w, img_h)
        if not ocr_results:
            confidence = derive_confidence(ocr_results)

        # Entropy metrics — fed to Stage D VLM pre-skip gate.
        fkb = file_size_kb(img_path)
        lvar = laplacian_variance(img_path)

        results.append({
            "slide_id": s["slide"],
            "pipeline_stage": "raw",
            "filename": s["filename"],
            "timestamp_start": first_ts,
            "timestamp_end": end_ts,
            "time_str": time_str,
            "missing_frame": False,
            "ocr": {
                "quick_text": quick_text,
                "quick_title_guess": title_guess,
                "ocr_text_density": round(density, 3),
                # parse_valid now means "the OCR call completed". Empty text with
                # parse_valid true = genuinely blank slide.
                "parse_valid": ocr_error is None,
                "ocr_error": ocr_error,
                "model_confidence": round(confidence, 3),
            },
            "entropy": {
                "file_size_kb": fkb,
                "laplacian_variance": lvar,
                "quick_text_len": len(quick_text),
            },
        })

        # Print only every 50 slides to keep stdout terse. JSONL log has full per-slide detail.
        if (idx + 1) % 50 == 0 or idx + 1 == len(slides):
            print(f"  Slide {s['slide']:3d} [{time_str}]: "
                  f"{len(quick_text):4d} chars, density={density:.2f}, "
                  f"conf={confidence:.2f}, size={fkb:.0f}KB, lap={lvar:.1f}")
        # Per-slide event so a mid-stage crash still leaves a trail of what
        # was processed (counter-proposal to converting slides_raw.json to
        # jsonl — keeps the consumer API intact).
        if log:
            log.emit("slide_done", status="error" if ocr_error else "success",
                     slide_id=s["slide"], idx=idx + 1,
                     density=round(density, 3),
                     text_len=len(quick_text),
                     file_kb=fkb,
                     lap_var=lvar,
                     ocr_error=ocr_error,
                     confidence=round(confidence, 3))

    atomic_write_json(out_path, results)

    elapsed = time.time() - start
    print(f"\nDone: {len(results)} slides in {elapsed:.1f}s -> {out_path}")
    if n_missing or n_ocr_error:
        print(f"  {n_missing} missing frame(s), {n_ocr_error} OCR failure(s) "
              "— these carry parse_valid=false and empty text")
    if log:
        log.stage_done(success=True, n_slides=len(results),
                       n_missing=n_missing, n_ocr_error=n_ocr_error,
                       elapsed_s=round(elapsed, 1))
        log.close()


if __name__ == "__main__":
    main()
