"""Stage C: semantic dedup using text-subset + layout similarity.

Stage A (extract_slides.py) already removed near-identical frames via
perceptual hash on center crop. This stage catches what phash misses:
  - Progressive reveal animations (bullet 1 → 1+2 → 1+2+3)
  - Slides whose layout barely changes but content grows

Dual-signal fusion:
  1. Text subset: RapidFuzz partial_ratio between quick_text values.
     If a ⊆ b (partial_ratio >= 88), they describe the same content.
  2. Layout: SSIM (scikit-image) + 32-bin grayscale histogram
     (Bhattacharyya distance). Fused as
     layout_similarity = 0.6 * SSIM + 0.4 * (1 - bhattacharyya).
     Without scikit-image the layout criterion is switched OFF (not approximated
     by the histogram alone) — see try_import_skimage.

Merge rule:
  Adjacent canonical candidates (n, n+1) merge into one semantic_group if
    (text_subset OR layout_similarity > 0.85)
    AND time_gap < 60s

Canonical pick within a group:
  Slide with the longest quick_text. If tie, the LAST slide (latest reveal).

Usage:
    python dedup_semantic.py <out_dir> [--force]
                             [--layout-threshold 0.85] [--text-threshold 88]
                             [--max-gap-seconds 60]

Inputs:
    <out_dir>/slides_raw.json       (from Stage B)
    <out_dir>/slides/*.jpg          (for SSIM/histogram)

Output:
    <out_dir>/slides_dedup.json     (pipeline_stage="dedup")
"""
import argparse
import json
import os
import sys
import time

from _common import atomic_write_json

try:
    from _log import StageLogger
except Exception:
    StageLogger = None  # type: ignore


# FALLBACK ONLY — the live list is `dedup.ui_chrome_tokens` in config.yaml, read
# at startup by _load_ui_tokens(); edit config, not this list.
#
# These entries are zh-TW Zoom screen-share chrome AS RapidOCR misreads it, which
# is why some look like typos ("劇天" for 聊天, "最手"/"翠手"/"卑手" for 舉手,
# "照相格" for 照相機): they are the observed OCR output, not correct Chinese.
# They exist because canonical-frame selection ranks by quick_text length, so a
# frame heavy in conferencing UI text could out-score a frame with more actual
# slide content (2026-07-04 fix). Other locales / Teams / Meet / WebEx chrome
# belong in config.yaml `dedup.ui_chrome_tokens`.
_DEFAULT_UI_CHROME_TOKENS = ["聊天", "劇天", "人員", "舉手", "最手", "翠手", "卑手",
                   "取得控制權", "控制權", "檢視", "检视", "照相機", "照相格", "照相",
                   "簽到", "保密", "視訊", "麥克風", "共享", "邀請", "錄製", "反應",
                   "暫停", "結束會議", "停止共享", "新的共享", "多雲", "時睛", "未證",
                   "靜音", "解除"]

# Mutable module global; overridden by config.yaml in main(). Default = above.
_ZOOM_UI_TOKENS = list(_DEFAULT_UI_CHROME_TOKENS)


def _load_ui_tokens(config_path):
    """Return the ui_chrome_tokens list from config.yaml `dedup:`, falling back to
    the built-in default. Missing pyyaml / file / key all degrade to the default."""
    if not config_path or not os.path.isfile(config_path):
        return list(_DEFAULT_UI_CHROME_TOKENS)
    try:
        import yaml
    except ImportError:
        print("WARNING: pyyaml not installed; using built-in ui_chrome_tokens.",
              file=sys.stderr)
        return list(_DEFAULT_UI_CHROME_TOKENS)
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        toks = (cfg.get("dedup") or {}).get("ui_chrome_tokens")
        if isinstance(toks, list) and toks:
            return [str(t) for t in toks]
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: failed to load ui_chrome_tokens from {config_path}: {e}; "
              "using default.", file=sys.stderr)
    return list(_DEFAULT_UI_CHROME_TOKENS)


def clean_quick_text(text):
    """Strip lines containing screen-share UI chrome tokens before using quick_text
    length as a canonical-selection signal — otherwise the frame with the MOST
    conferencing-UI noise (not the most slide content) can win the pick."""
    if not text:
        return text
    lines = [ln for ln in text.splitlines()
              if not any(tok in ln for tok in _ZOOM_UI_TOKENS)]
    return "\n".join(lines)


# --------------------------- optional deps ------------------------------------

def try_import_rapidfuzz():
    try:
        from rapidfuzz import fuzz
        return fuzz
    except ImportError:
        print("WARNING: rapidfuzz not installed; text-subset dedup disabled.",
              file=sys.stderr)
        return None


def try_import_skimage():
    """SSIM loader. Without it the layout criterion is DISABLED, not degraded —
    see compute_layout_similarity for why the histogram-only fallback was unsafe."""
    try:
        from skimage.metrics import structural_similarity as ssim
        return ssim
    except ImportError:
        print(
            "\n" + "!" * 72 + "\n"
            "WARNING: scikit-image is NOT installed — LAYOUT-BASED MERGING IS "
            "DISABLED.\n"
            "  What still works : text-subset merging (rapidfuzz), if installed.\n"
            "  What degrades    : slides that differ only in layout (progressive\n"
            "                     reveals with little OCR text) will NOT be merged,\n"
            "                     so expect MORE canonical slides than usual.\n"
            "  Why not fall back: the histogram-only similarity this used to fall\n"
            "                     back to scores same-template white slides ~0.95,\n"
            "                     which collapsed ~120 distinct frames into ~5.\n"
            "                     A wrong merge silently deletes content; a missed\n"
            "                     merge only costs a duplicate.\n"
            "  Fix              : pip install scikit-image\n"
            + "!" * 72 + "\n",
            file=sys.stderr)
        return None


# --------------------------- layout signals -----------------------------------

def load_gray(img_path, size=256):
    """Load image as size×size grayscale numpy array (uint8)."""
    import numpy as np
    from PIL import Image
    with Image.open(img_path) as img:
        img = img.convert("L").resize((size, size))
        return np.array(img, dtype="uint8")


def histogram_bhattacharyya(a, b, bins=32):
    """Bhattacharyya distance ∈ [0, 1] between two grayscale arrays."""
    import numpy as np
    hist_a, _ = np.histogram(a, bins=bins, range=(0, 256), density=False)
    hist_b, _ = np.histogram(b, bins=bins, range=(0, 256), density=False)
    sum_a, sum_b = hist_a.sum(), hist_b.sum()
    if sum_a == 0 or sum_b == 0:
        return 1.0
    p = hist_a / sum_a
    q = hist_b / sum_b
    bc = float(np.sum(np.sqrt(p * q)))
    bc = max(0.0, min(bc, 1.0))
    return float(np.sqrt(1.0 - bc))  # 0=identical, 1=disjoint


def compute_layout_similarity(prev_gray, curr_gray, ssim_fn):
    """Returns (ssim_value, bhattacharyya, fused_similarity).

    fused_similarity is None when SSIM is unavailable: histogram similarity alone
    cannot tell two different slides sharing one template apart (both are ~95%
    white), so the caller must treat None as "no layout opinion" and never merge
    on it. The histogram number is still returned for diagnostics.
    """
    if ssim_fn is None:
        bhatt = histogram_bhattacharyya(prev_gray, curr_gray)
        return None, bhatt, None
    s = float(ssim_fn(prev_gray, curr_gray, data_range=255))
    bhatt = histogram_bhattacharyya(prev_gray, curr_gray)
    fused = 0.6 * s + 0.4 * (1.0 - bhatt)
    return s, bhatt, fused


# --------------------------- text signal --------------------------------------

def text_subset_ratio(a, b, fuzz_lib):
    """Returns max(partial_ratio(a, b), partial_ratio(b, a)) / 100 ∈ [0, 1].

    partial_ratio finds the best substring alignment; high value means
    a ⊆ b or b ⊆ a.
    """
    if fuzz_lib is None:
        return 0.0
    a = (a or "").strip()
    b = (b or "").strip()
    if not a or not b:
        return 0.0
    r1 = fuzz_lib.partial_ratio(a, b)
    r2 = fuzz_lib.partial_ratio(b, a)
    return max(r1, r2) / 100.0


# --------------------------- main grouping ------------------------------------

def group_slides(slides, slides_dir, fuzz_lib, ssim_fn,
                 text_threshold, layout_threshold, max_gap_seconds,
                 max_span_seconds=None):
    """Assign each slide a semantic_group_id; returns enriched list.

    Merges adjacent slides where:
      (text overlap >= text_threshold/100) OR (layout similarity > layout_threshold)
      AND time gap < max_gap_seconds

    Canonical = longest quick_text in the group (tie: latest).
    """
    if not slides:
        return slides

    grays = {}

    def gray(slide):
        if slide["filename"] in grays:
            return grays[slide["filename"]]
        try:
            g = load_gray(os.path.join(slides_dir, slide["filename"]))
        except Exception as e:
            print(f"  WARN: cannot load {slide['filename']} for layout: {e}",
                  file=sys.stderr)
            g = None
        grays[slide["filename"]] = g
        return g

    enriched = []
    current_group = []
    group_id = 0

    def flush_group():
        nonlocal group_id
        if not current_group:
            return
        group_id += 1

        # Canonical: longest CLEANED quick_text (Zoom-UI chrome stripped first,
        # 2026-07-04 fix); tie -> latest (last in time order)
        canonical_idx = 0
        max_len = -1
        for i, sl in enumerate(current_group):
            tl = len(clean_quick_text(sl["ocr"].get("quick_text") or ""))
            if tl > max_len or (tl == max_len and i > canonical_idx):
                max_len = tl
                canonical_idx = i
        canonical_filename = current_group[canonical_idx]["filename"]

        group_start = current_group_start
        group_end = max(s["timestamp_end"] for s in current_group)

        for i, sl in enumerate(current_group):
            is_canon = (i == canonical_idx)
            d = sl.setdefault("dedup", {})
            d.setdefault("phash_group", sl["slide_id"])
            d["semantic_group"] = group_id
            d["is_canonical"] = is_canon
            d["superseded_by"] = (canonical_filename if not is_canon else None)
            # Per-slide layout signals were computed against prev; keep them
            if is_canon:
                # Canonical timestamp range covers the whole group
                sl["timestamp_start"] = group_start
                sl["timestamp_end"] = group_end
                gs_i, ge_i = int(group_start), int(group_end)
                sl["time_str"] = (
                    f"{gs_i // 60:02d}:{gs_i % 60:02d}-"
                    f"{ge_i // 60:02d}:{ge_i % 60:02d}"
                )
            sl["pipeline_stage"] = "dedup"
            enriched.append(sl)

    current_group = [slides[0]]
    # Running min of the open group's start, so the span check below stays O(n)
    # overall instead of rescanning the group on every slide.
    current_group_start = slides[0]["timestamp_start"]
    # Seed the first slide's dedup info with no-prev
    slides[0].setdefault("dedup", {}).update({
        "phash_group": slides[0]["slide_id"],
        "ssim_to_prev": None,
        "histogram_similarity": None,
        "ocr_overlap_ratio": None,
        "layout_similarity": None,
    })

    for i in range(1, len(slides)):
        prev = current_group[-1]
        curr = slides[i]

        text_overlap = text_subset_ratio(
            prev["ocr"].get("quick_text", ""),
            curr["ocr"].get("quick_text", ""),
            fuzz_lib
        )

        prev_g = gray(prev)
        curr_g = gray(curr)
        if prev_g is not None and curr_g is not None:
            ssim_v, bhatt, fused = compute_layout_similarity(prev_g, curr_g, ssim_fn)
        else:
            ssim_v, bhatt, fused = None, None, None

        time_gap = curr["timestamp_start"] - prev["timestamp_end"]

        curr.setdefault("dedup", {}).update({
            "phash_group": curr["slide_id"],
            "ssim_to_prev": (round(ssim_v, 3) if ssim_v is not None else None),
            "histogram_similarity": (round(1.0 - bhatt, 3) if bhatt is not None else None),
            "ocr_overlap_ratio": round(text_overlap, 3),
            "layout_similarity": (round(fused, 3) if fused is not None else None),
        })

        text_merge = text_overlap >= (text_threshold / 100.0)
        # fused is None when SSIM is unavailable or an image failed to load — no
        # layout opinion, so never merge on it (see try_import_skimage warning).
        layout_merge = fused is not None and fused > layout_threshold
        within_gap = time_gap < max_gap_seconds
        # Span cap: a static-layout live screen (e.g. an ultrasound machine UI)
        # stays >layout_threshold for minutes at fine sampling, chaining the whole
        # segment into one canonical and dropping every needle/scan moment. Cap the
        # group's time span so such segments still emit periodic canonical frames.
        within_span = (max_span_seconds is None or
                       (curr.get("timestamp_end", curr["timestamp_start"])
                        - current_group_start) <= max_span_seconds)

        if (text_merge or layout_merge) and within_gap and within_span:
            current_group.append(curr)
            current_group_start = min(current_group_start, curr["timestamp_start"])
        else:
            flush_group()
            current_group = [curr]
            current_group_start = curr["timestamp_start"]

    flush_group()
    return enriched


# --------------------------- entrypoint ---------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Stage C: semantic dedup")
    parser.add_argument("out_dir", help="Lecture output directory")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite output (default behavior)")
    parser.add_argument("--text-threshold", type=int, default=88,
                        help="partial_ratio threshold for text-subset merge (default 88)")
    parser.add_argument("--layout-threshold", type=float, default=0.85,
                        help="Fused layout similarity threshold (default 0.85)")
    parser.add_argument("--max-gap-seconds", type=int, default=60,
                        help="Max time gap between merged slides (default 60s)")
    parser.add_argument("--max-span-seconds", type=int, default=None,
                        help="Cap a semantic group's total time span; forces "
                             "periodic canonical frames on static live screens "
                             "(US machine UI) at fine sampling. Default off.")
    parser.add_argument("--config", default=None,
                        help="Path to config.yaml (default: skill_root/config.yaml). "
                             "Reads dedup.ui_chrome_tokens for the UI-noise filter.")
    args = parser.parse_args()

    # WP1-6: load screen-share UI chrome tokens from config (default = built-in).
    global _ZOOM_UI_TOKENS
    config_path = args.config or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "config.yaml")
    _ZOOM_UI_TOKENS = _load_ui_tokens(config_path)

    raw_path = os.path.join(args.out_dir, "slides_raw.json")
    slides_dir = os.path.join(args.out_dir, "slides")
    out_path = os.path.join(args.out_dir, "slides_dedup.json")

    if not os.path.isfile(raw_path):
        print(f"ERROR: {raw_path} not found. Run quick_ocr.py first.", file=sys.stderr)
        sys.exit(2)

    with open(raw_path, "r", encoding="utf-8") as f:
        slides = json.load(f)

    log = StageLogger("dedup", args.out_dir,
                      extra={"total": len(slides),
                             "text_threshold": args.text_threshold,
                             "layout_threshold": args.layout_threshold,
                             "max_gap_seconds": args.max_gap_seconds}) \
        if StageLogger else None
    if log:
        log.start()

    fuzz_lib = try_import_rapidfuzz()
    ssim_fn = try_import_skimage()

    start = time.time()
    try:
        enriched = group_slides(
            slides, slides_dir, fuzz_lib, ssim_fn,
            text_threshold=args.text_threshold,
            layout_threshold=args.layout_threshold,
            max_gap_seconds=args.max_gap_seconds,
            max_span_seconds=args.max_span_seconds,
        )
    except Exception as e:
        if log:
            log.stage_done(success=False, error=str(e)[:500])
            log.close()
        raise

    atomic_write_json(out_path, enriched)

    canonical_count = sum(1 for s in enriched if s["dedup"].get("is_canonical"))
    elapsed = time.time() - start
    print(f"\nDone: {len(enriched)} input slides -> {canonical_count} canonical "
          f"({len(enriched) - canonical_count} superseded) in {elapsed:.1f}s")
    print(f"Output: {out_path}")

    if log:
        log.stage_done(success=True,
                       n_input=len(enriched),
                       n_canonical=canonical_count,
                       n_superseded=len(enriched) - canonical_count,
                       elapsed_s=round(elapsed, 1),
                       rapidfuzz_available=fuzz_lib is not None,
                       ssim_available=ssim_fn is not None)
        log.close()


if __name__ == "__main__":
    main()
