"""Crop multi-up PDF handouts into individual slides via auto-detection
of inter-slide whitespace.

Many lecture handouts ship as N-up PDFs where each PDF page contains a
grid of slide thumbnails (2-up, 4-up, 6-up). Hard-coded grid boundaries
do not generalize — each presenter's PowerPoint template uses different
margins. This script ==detects slide frames per page== from the rendered
image's ink-density profile.

Algorithm (what the code actually does):

1. Render a page at --detect-zoom to a numpy grayscale array.
2. Compute "ink density" per row and per column (255 - mean(gray)).
3. Sweep a fixed grid of (ink threshold x minimum gap run) settings. For each,
   split rows/cols into content bands separated by quiet gaps, drop bands too
   thin to be a slide (< 6% of page height / < 10% of width — this is what
   removes date stamps and page-number footers), and score the result: closer
   to --expected-rows/--expected-cols is better, and a plausible 1-4 x 1-4 grid
   gets a bonus. Highest score wins. There is NO iterative threshold relaxation
   loop — the sweep is the whole search.
4. Sample up to 5 middle pages, take the most common (rows, cols) shape as the
   consensus, and use the MEDIAN band edges of the agreeing samples as the
   canonical layout for the rest of the document.
5. Page 0 and the last page are detected individually rather than forced onto
   the consensus: they are usually a 1-up title or summary, and stamping a 2x2
   grid on them produced four fake slides that flowed into the whole pipeline.
   A page that detects as 1x1 is emitted whole, as a single slide.
6. Each (row band x col band) intersection is cropped and saved at --zoom.

Usage:
    python crop_multiup_pdf.py <pdf_path> <out_dir> \\
        [--zoom 3.0] [--detect-zoom 1.0] \\
        [--expected-rows N] [--expected-cols M] \\
        [--pad 0.02] [--debug]

Key flags:
- --expected-rows / --expected-cols: hint when auto-detect picks the wrong
  count (e.g. a sparse slide that is mostly whitespace). These bias the
  scoring and override the consensus shape.
- --pad: fractional outer padding added to each cell (default 2%) so
  borders / captions are not clipped.
- --debug: print the winning threshold/gap per sampled page.

Outputs slide_NNN.jpg (1-based, zero-padded to 3) plus _crop_meta.json. Any
slide_*.jpg left by a previous run of THIS tool in the output directory is
removed first, so the images and the metadata never disagree.
"""
import argparse, fitz, json, os, sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    import numpy as np
except ImportError:
    raise SystemExit("numpy required: pip install numpy")

SLIDE_RE_PREFIX = "slide_"


def page_to_gray_array(page, zoom):
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    return arr


def find_content_bands(profile, gap_threshold, min_gap_run):
    """Given a 1D ink-density profile (higher = darker = content),
    return list of (start, end) ranges that are above threshold,
    separated by gaps of >= min_gap_run length below threshold."""
    above = profile > gap_threshold
    bands = []
    in_band = False
    start = 0
    gap_len = 0
    for i, a in enumerate(above):
        if a:
            if not in_band:
                start = i
                in_band = True
            gap_len = 0
        else:
            if in_band:
                gap_len += 1
                if gap_len >= min_gap_run:
                    bands.append((start, i - gap_len + 1))
                    in_band = False
                    gap_len = 0
    if in_band:
        bands.append((start, len(above)))
    return bands


def detect_grid(arr, expected_rows=None, expected_cols=None, debug=False):
    """Detect (row_bands, col_bands) on a single page array, in pixel coords.

    Returns None when no candidate setting produced any band at all (a blank
    page, or a render that came back uniform). Callers must handle None —
    unpacking it is how `--expected-rows` on an undetectable page used to raise
    a bare TypeError from the very flag documented as the fix for bad detection.
    """
    H, W = arr.shape
    # ink density: invert so content is high
    row_ink = 255.0 - arr.mean(axis=1)
    col_ink = 255.0 - arr.mean(axis=0)

    candidates_t = [5, 8, 12, 18, 25, 35]
    candidates_g = [int(0.015 * H), int(0.025 * H), int(0.04 * H)]
    best = None
    # -inf, not -1: with --expected-rows/-cols set, every candidate scores
    # negative (-10 per band off), so nothing ever beat a -1 floor and `best`
    # stayed None.
    best_score = float("-inf")
    for t in candidates_t:
        for g in candidates_g:
            rb = find_content_bands(row_ink, t, g)
            cb = find_content_bands(col_ink, t, max(g, int(0.015 * W)))
            # filter out tiny bands (footer date stamps, page numbers)
            rb = [(s, e) for s, e in rb if (e - s) > 0.06 * H]
            cb = [(s, e) for s, e in cb if (e - s) > 0.10 * W]
            if not rb or not cb:
                continue
            score = 0
            if expected_rows is not None:
                score -= abs(len(rb) - expected_rows) * 10
            if expected_cols is not None:
                score -= abs(len(cb) - expected_cols) * 10
            # prefer 2-4 rows + 1-3 cols
            if 1 <= len(rb) <= 4 and 1 <= len(cb) <= 4:
                score += 5
            if score > best_score:
                best_score = score
                best = (rb, cb, t, g)
    if best is None:
        return None
    rb, cb, t, g = best
    if debug:
        print(f"  threshold={t} gap_run={g} -> rows={len(rb)} cols={len(cb)}")
    return rb, cb


def detect_grid_frac(page, detect_zoom, expected_rows, expected_cols, debug=False):
    """detect_grid on one page, returned as page fractions. None if undetectable."""
    arr = page_to_gray_array(page, detect_zoom)
    H, W = arr.shape
    got = detect_grid(arr, expected_rows, expected_cols, debug=debug)
    if got is None:
        return None
    rb, cb = got
    return ([(s / H, e / H) for s, e in rb], [(s / W, e / W) for s, e in cb])


def crop_page(page, rows, cols, zoom, pad_frac, slide_id_start):
    """Crop one page given row/col bands as page fractions and render each cell
    at the output zoom."""
    pw, ph = page.rect.width, page.rect.height
    meta = []
    sid = slide_id_start
    pixs = []
    for r0, r1 in rows:
        for c0, c1 in cols:
            sid += 1
            # apply padding (fraction of page)
            r0p = max(0.0, r0 - pad_frac)
            r1p = min(1.0, r1 + pad_frac)
            c0p = max(0.0, c0 - pad_frac)
            c1p = min(1.0, c1 + pad_frac)
            clip = fitz.Rect(c0p * pw, r0p * ph, c1p * pw, r1p * ph)
            sub = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip)
            pixs.append((sid, sub, (r0p, r1p, c0p, c1p)))
            meta.append({"slide_id": sid, "row_frac": (r0p, r1p),
                          "col_frac": (c0p, c1p)})
    return pixs, meta


def clear_previous_slides(out_dir):
    """Remove slide_*.jpg this tool wrote on an earlier run.

    Pattern-scoped and confined to the output directory the user just named:
    without it, a rerun that detects fewer slides leaves the extra images from
    the old run behind, and _crop_meta.json no longer describes the directory.
    """
    removed = 0
    for f in os.listdir(out_dir):
        stem, ext = os.path.splitext(f)
        if ext.lower() != ".jpg" or not stem.startswith(SLIDE_RE_PREFIX):
            continue
        if not stem[len(SLIDE_RE_PREFIX):].isdigit():
            continue
        os.remove(os.path.join(out_dir, f))
        removed += 1
    if removed:
        print(f"  cleared {removed} slide_*.jpg from a previous run")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf_path")
    ap.add_argument("out_dir")
    ap.add_argument("--zoom", type=float, default=3.0,
                    help="output render zoom (default 3.0)")
    ap.add_argument("--detect-zoom", type=float, default=1.0,
                    help="lower-res render used for gap detection")
    ap.add_argument("--expected-rows", type=int, default=None)
    ap.add_argument("--expected-cols", type=int, default=None)
    ap.add_argument("--pad", type=float, default=0.02,
                    help="extra padding around each cell (frac of page); "
                         "bump to 0.025+ if captions/citations at slide bottom "
                         "get clipped")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    if not os.path.isfile(args.pdf_path):
        print(f"ERROR: PDF not found: {args.pdf_path}", file=sys.stderr)
        sys.exit(2)

    os.makedirs(args.out_dir, exist_ok=True)
    clear_previous_slides(args.out_dir)

    doc = fitz.open(args.pdf_path)
    try:
        run(doc, args)
    finally:
        # Windows keeps the PDF locked until this closes.
        doc.close()


def run(doc, args):
    all_meta = []
    page_texts = {}
    slide_id = 0

    # ==Multi-page consensus calibration== — sample N mid pages,
    # detect grid on each, and use the median band edges as the
    # canonical layout for the interior pages. Slide cells should be the
    # same size on every page; using a single sample page can be
    # biased by that page's content distribution.
    n_pages = len(doc)
    if n_pages <= 1:
        sample_idxs = [0]
    elif n_pages <= 5:
        sample_idxs = list(range(n_pages))
    else:
        # Skip page 0 (often title 1-up) + last (often summary/blank).
        # Evenly spread across the middle.
        mid_start = 1
        mid_end = n_pages - 1
        sample_idxs = [int(mid_start + (mid_end - mid_start) * i / 4)
                        for i in range(5)]
        sample_idxs = sorted(set(sample_idxs))

    samples_rows = []  # list of (rows_frac_list) per sampled page
    samples_cols = []
    sample_shapes_match = []
    for s_idx in sample_idxs:
        got = detect_grid_frac(doc[s_idx], args.detect_zoom, args.expected_rows,
                               args.expected_cols, debug=args.debug)
        if got is None:
            print(f"  [WARN] sample page {s_idx+1}: no grid detected (blank page?)",
                  file=sys.stderr)
            continue
        rows_f, cols_f = got
        samples_rows.append(rows_f)
        samples_cols.append(cols_f)
        sample_shapes_match.append((len(rows_f), len(cols_f)))
        if args.debug:
            print(f"  sample page {s_idx+1}: rows={len(rows_f)} cols={len(cols_f)}")

    if not sample_shapes_match:
        print(f"ERROR: grid detection failed on all {len(sample_idxs)} sampled "
              "pages — no content bands were found at any threshold.\n"
              "  Try a higher --detect-zoom (e.g. 2.0) if the render is too "
              "coarse, or check that the PDF pages are not blank/vector-only.",
              file=sys.stderr)
        sys.exit(2)

    # Pick the most common (rows, cols) shape across samples
    from collections import Counter
    shape_counter = Counter(sample_shapes_match)
    (expected_rows, expected_cols), n_agree = shape_counter.most_common(1)[0]
    if args.expected_rows:
        expected_rows = args.expected_rows
    if args.expected_cols:
        expected_cols = args.expected_cols

    # Filter samples to those matching the consensus shape
    good_rows = [r for r in samples_rows if len(r) == expected_rows]
    good_cols = [c for c in samples_cols if len(c) == expected_cols]
    if not good_rows or not good_cols:
        print(f"ERROR: detection inconsistent across {len(sample_idxs)} samples "
              f"(shapes seen: {dict(shape_counter)}). "
              f"Force --expected-rows N --expected-cols M.", file=sys.stderr)
        sys.exit(2)

    # Median per band edge for consensus
    import statistics
    consensus_rows = []
    for i in range(expected_rows):
        starts = [r[i][0] for r in good_rows]
        ends = [r[i][1] for r in good_rows]
        consensus_rows.append((statistics.median(starts),
                                statistics.median(ends)))
    consensus_cols = []
    for i in range(expected_cols):
        starts = [c[i][0] for c in good_cols]
        ends = [c[i][1] for c in good_cols]
        consensus_cols.append((statistics.median(starts),
                                statistics.median(ends)))

    # ==Stability audit== — per-band stddev across samples (in % of
    # page dim). High stddev means margins drift across pages —
    # the consensus may not fit all slides equally well.
    print(f"Detected layout: {expected_rows} rows x {expected_cols} cols "
          f"(consensus from {len(good_rows)}/{len(sample_idxs)} sample pages)")
    print("  Row bands (frac of page H) with stability:")
    for i, (s, e) in enumerate(consensus_rows):
        starts_std = statistics.stdev([r[i][0] for r in good_rows]) if len(good_rows) > 1 else 0
        ends_std = statistics.stdev([r[i][1] for r in good_rows]) if len(good_rows) > 1 else 0
        flag = " [WARN]" if max(starts_std, ends_std) > 0.02 else ""
        print(f"    row {i+1}: [{s:.3f}, {e:.3f}]  "
              f"start_std={starts_std:.3f} end_std={ends_std:.3f}{flag}")
    print("  Col bands (frac of page W) with stability:")
    for i, (s, e) in enumerate(consensus_cols):
        starts_std = statistics.stdev([c[i][0] for c in good_cols]) if len(good_cols) > 1 else 0
        ends_std = statistics.stdev([c[i][1] for c in good_cols]) if len(good_cols) > 1 else 0
        flag = " [WARN]" if max(starts_std, ends_std) > 0.02 else ""
        print(f"    col {i+1}: [{s:.3f}, {e:.3f}]  "
              f"start_std={starts_std:.3f} end_std={ends_std:.3f}{flag}")

    # Pages the sampling logic deliberately excluded because they are usually
    # 1-up. Force the consensus grid on them and a title page becomes four fake
    # slides, so detect those individually instead.
    edge_pages = set()
    if n_pages > 5:
        edge_pages = {0, n_pages - 1} - set(sample_idxs)

    consensus_shape = (expected_rows, expected_cols)
    for pg_idx in range(n_pages):
        page = doc[pg_idx]
        page_texts[pg_idx + 1] = page.get_text().strip()

        rows, cols = consensus_rows, consensus_cols
        layout_src = "consensus"
        if pg_idx in edge_pages:
            got = detect_grid_frac(page, args.detect_zoom, None, None)
            if got is None:
                print(f"  [WARN] page {pg_idx+1}: no grid detected; using consensus")
            elif (len(got[0]), len(got[1])) == (1, 1):
                rows, cols = got
                layout_src = "per_page_1up"
                print(f"  page {pg_idx+1}: detected 1-up -> emitting as a single slide")
            elif (len(got[0]), len(got[1])) == consensus_shape:
                layout_src = "consensus_confirmed"
            else:
                rows, cols = got
                layout_src = f"per_page_{len(got[0])}x{len(got[1])}"
                print(f"  page {pg_idx+1}: detected {len(got[0])}x{len(got[1])} "
                      f"(consensus is {expected_rows}x{expected_cols}) -> using "
                      "the per-page grid")

        pixs, meta = crop_page(page, rows, cols, args.zoom, args.pad, slide_id)
        for sid, sub, _frac in pixs:
            sub.save(os.path.join(args.out_dir, f"slide_{sid:03d}.jpg"))
        for m in meta:
            m["page"] = pg_idx + 1
            # 3 digits, not 2: at 100+ sub-slides a 2-digit name sorts
            # lexically as slide_10 < slide_9, and every consumer here globs
            # and sorts by name.
            m["filename"] = f"slide_{m['slide_id']:03d}.jpg"
            m["layout_source"] = layout_src
            all_meta.append(m)
            slide_id = max(slide_id, m["slide_id"])

    # ==Post-crop audit== — verify output JPG dimensions are consistent for each
    # (row, col) position among pages that used the consensus grid. They should
    # be identical (modulo last-pixel rounding). Per-page layouts are excluded:
    # a 1-up title page is legitimately a different size.
    from PIL import Image
    pos_sizes = {}  # (row, col) -> list of (w, h)
    for m in all_meta:
        if not m["layout_source"].startswith("consensus"):
            continue
        sub_idx = (m["slide_id"] - 1) % (expected_rows * expected_cols)
        row_idx = sub_idx // expected_cols
        col_idx = sub_idx % expected_cols
        key = (row_idx, col_idx)
        try:
            with Image.open(os.path.join(args.out_dir, m["filename"])) as im:
                pos_sizes.setdefault(key, []).append(im.size)
        except Exception:
            pass
    any_drift = False
    for key, sizes in pos_sizes.items():
        unique = set(sizes)
        if len(unique) > 1:
            any_drift = True
            print(f"  [WARN] position (row={key[0]+1}, col={key[1]+1}): "
                  f"{len(unique)} distinct sizes — {unique}")
    if not any_drift:
        print(f"  Audit OK: all {sum(len(v) for v in pos_sizes.values())} "
              f"consensus-grid slides have consistent dims per position")

    meta_path = os.path.join(args.out_dir, "_crop_meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({"pdf": os.path.abspath(args.pdf_path),
                    "rows": expected_rows, "cols": expected_cols,
                    "n_slides": slide_id, "pad": args.pad,
                    "consensus_rows": consensus_rows,
                    "consensus_cols": consensus_cols,
                    "sample_pages_used": [i + 1 for i in sample_idxs],
                    # Stored once per page and referenced by each slide's
                    # "page" key. It used to be copied verbatim into every
                    # cell, which multiplied a 6-up handout's text by six.
                    "page_texts": page_texts,
                    "slides": all_meta}, f, ensure_ascii=False, indent=2)
    print(f"OK {slide_id} sub-slides -> {args.out_dir}")
    print(f"   metadata -> {meta_path}  (slide text: page_texts[str(slide.page)])")


if __name__ == "__main__":
    main()
