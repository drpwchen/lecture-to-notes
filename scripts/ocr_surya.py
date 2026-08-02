"""Stage B2 — high-quality OCR (Surya) on canonical, text-bearing slides.

Runs AFTER dedup_semantic.py (Stage C marks canonical) and BEFORE grounding.
RapidOCR (quick_ocr.py / Stage B) stays as the cheap triage pass that already
filled `ocr.quick_text` + entropy; this stage adds the *good* text for synthesis.

Routing (image-type-aware, using Stage-B signals — no VLM needed):
    ocr.source == "pdf"                 -> keep pdf text, skip OCR     (no rework, Path B)
    quick_text_len < L AND density < D  -> skip OCR  (pure MRI/photo/decorative)
    otherwise                           -> Surya     (dense text / table / algorithm)
    Surya error OR page_conf < thresh   -> RapidOCR fallback           (handwritten etc.)

Surya runs in its OWN venv (subprocess) so its torch/transformers stay isolated.
The interpreter comes from --surya-python, then env LECTURE_SURYA_PYTHON, then
config.yaml ocr_engine.surya_python. When it is absent or does not exist, this
stage does NOT abort: it warns loudly and routes every slide to RapidOCR, so a
machine without Surya still gets Stage B2 text.

The fallback tree is intentionally shallow (surya -> rapidocr): no ensemble,
voting, or multi-layer retry. Schema additions are minimal — downstream only
needs text:  ocr.clean_text, ocr.ocr_engine, ocr.ocr_confidence.

Outputs:
    <out_dir>/slides_dedup.json          updated in place (atomically) — this is
                                         what Stage D/E read
    <out_dir>/slides_dedup.pre_b2.json   one-time snapshot of the pre-B2 file, so
                                         a bad OCR run has a resume point
    <out_dir>/slides_ocr.json            this stage's own result, for inspection

Usage:
    python ocr_surya.py <out_dir> [--config <config.yaml>]
                        [--surya-python <venv_python>] [--conf-threshold 0.6]
                        [--timeout SECONDS] [--resume]
"""
import argparse, json, os, subprocess, sys, tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from _common import (atomic_write_json,  # noqa: E402
                     load_config, rapidocr_text_ex)

# Production adapter (moved out of ocr_bench/ on 2026-08-02 — production must not
# depend on the benchmark tree).
SURYA_ADAPTER = os.path.join(HERE, "adapters", "surya_adapter.py")

# Base allowance + per-image budget. Surya on a cold GPU spends most of the base
# on model load; 10s/image is ~4x the observed steady-state rate on this card.
TIMEOUT_BASE_S = 120
TIMEOUT_PER_IMAGE_S = 10


def load_cfg(path):
    """ocr_engine section of the EXACT config file the caller named.

    The previous version kept only dirname(path) and always read that
    directory's config.yaml — so ``--config config.example.yaml`` silently
    loaded config.yaml instead (WP-V finding C1: wrong config file + wrong
    engine provenance). A directory argument still means "<dir>/config.yaml".
    """
    p = os.path.abspath(path)
    try:
        if os.path.isdir(p):
            cfg = load_config(p)
        else:
            if not os.path.isfile(p):
                print(f"ERROR: --config file not found: {p}", file=sys.stderr)
                sys.exit(2)
            cfg = load_config(os.path.dirname(p), filename=os.path.basename(p))
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)
    return (cfg.get("ocr_engine") or {})


def resolve_surya_python(arg_value, cfg):
    """--surya-python > env LECTURE_SURYA_PYTHON > config. None if unusable."""
    for src, val in (("--surya-python", arg_value),
                     ("LECTURE_SURYA_PYTHON", os.environ.get("LECTURE_SURYA_PYTHON")),
                     ("config.yaml ocr_engine.surya_python", cfg.get("surya_python"))):
        val = (val or "").strip() if isinstance(val, str) else val
        if not val:
            continue
        if os.path.isfile(val):
            return val
        print(f"WARNING: {src} points at '{val}', which is not a file — ignoring.",
              file=sys.stderr)
    return None


def join_blocks(d):
    blocks = d.get("blocks", [])
    order = d.get("reading_order") or list(range(len(blocks)))
    return "\n".join(blocks[i].get("text", "") for i in order
                     if 0 <= i < len(blocks) and blocks[i].get("text"))


def run_surya(surya_python, image_paths, timeout_s):
    """Call the Surya adapter in its venv; return ({basename: result}, error).

    Image paths go through a temp manifest file, never argv: Windows caps a
    command line at 32,767 characters and a long lecture with CJK paths blows
    past it (WinError 206).
    """
    if not image_paths:
        return {}, None

    fd, manifest = tempfile.mkstemp(prefix="surya_manifest_", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(image_paths))
        try:
            proc = subprocess.run(
                [surya_python, os.path.abspath(SURYA_ADAPTER), "--manifest", manifest],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout_s)
        except subprocess.TimeoutExpired:
            print(f"  [surya] adapter timed out after {timeout_s}s on "
                  f"{len(image_paths)} images — every slide falls back to RapidOCR. "
                  f"Raise --timeout if this machine is just slow.", file=sys.stderr)
            return {}, "surya_timeout"
        except OSError as e:
            print(f"  [surya] cannot launch {surya_python}: {e}", file=sys.stderr)
            return {}, "surya_launch_failed"
    finally:
        # Our own temp file, created this run.
        try:
            os.remove(manifest)
        except OSError:
            pass

    out, n_bad_lines = {}, 0
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            n_bad_lines += 1
            continue
        # "image" is the current key; "fixture" is the legacy benchmark name,
        # accepted for one release so an older adapter still matches.
        key = d.get("image") or d.get("fixture")
        if key:
            out[key] = d
        else:
            n_bad_lines += 1
    if n_bad_lines:
        print(f"  [surya] {n_bad_lines} malformed/unkeyed JSON line(s) ignored",
              file=sys.stderr)

    if proc.returncode != 0:
        # Always report, even when partial results came back: a mid-run OOM
        # after 40 of 250 images used to be completely silent.
        missing = len(image_paths) - len(out)
        print(f"  [surya] adapter exited {proc.returncode} with {len(out)}/"
              f"{len(image_paths)} results ({missing} missing -> RapidOCR "
              f"fallback).\n  stderr tail: {(proc.stderr or '')[-500:]}",
              file=sys.stderr)
        return out, "surya_crashed"
    return out, None


def main():
    ap = argparse.ArgumentParser(description="Stage B2: Surya OCR with routing + RapidOCR fallback")
    ap.add_argument("out_dir")
    ap.add_argument("--config", default=os.path.join(HERE, "..", "config.yaml"))
    ap.add_argument("--surya-python", default=None)
    ap.add_argument("--conf-threshold", type=float, default=None)
    ap.add_argument("--timeout", type=int, default=None,
                    help=f"Seconds for the whole Surya subprocess (default: "
                         f"{TIMEOUT_BASE_S} + {TIMEOUT_PER_IMAGE_S}/image)")
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    cfg = load_cfg(args.config)
    surya_python = resolve_surya_python(args.surya_python, cfg)
    conf_thr = args.conf_threshold if args.conf_threshold is not None else cfg.get("confidence_threshold", 0.60)
    skip = cfg.get("skip_no_text", {}) or {}
    skip_len = skip.get("quick_text_len_max", 12)
    skip_density = skip.get("density_max", 0.03)

    dedup_path = os.path.join(args.out_dir, "slides_dedup.json")
    backup_path = os.path.join(args.out_dir, "slides_dedup.pre_b2.json")
    ocr_out_path = os.path.join(args.out_dir, "slides_ocr.json")
    slides_dir = os.path.join(args.out_dir, "slides")
    if not os.path.isfile(dedup_path):
        print(f"ERROR: {dedup_path} not found (run dedup_semantic.py first)", file=sys.stderr)
        sys.exit(2)

    if surya_python is None:
        print("WARNING: no usable Surya interpreter (checked --surya-python, "
              "LECTURE_SURYA_PYTHON, config.yaml ocr_engine.surya_python).\n"
              "  Stage B2 will run entirely on RapidOCR — text quality is lower "
              "but the pipeline continues.", file=sys.stderr)

    with open(dedup_path, encoding="utf-8") as fh:
        slides = json.load(fh)

    # One-time pre-B2 snapshot: this stage rewrites slides_dedup.json in place,
    # and Ctrl-C used to leave Stage C's product corrupt with no way back.
    if not os.path.isfile(backup_path):
        atomic_write_json(backup_path, slides)
        print(f"  [backup] pre-B2 snapshot -> {backup_path}")

    # Route
    surya_targets = []   # (slide, abs_path)
    n_pdf = n_skip = n_no_image = 0
    for s in slides:
        if not s.get("dedup", {}).get("is_canonical"):
            continue
        ocr = s.setdefault("ocr", {})
        if args.resume and ocr.get("ocr_engine"):
            continue
        src = ocr.get("source")
        qlen = (s.get("entropy", {}) or {}).get("quick_text_len")
        if qlen is None:
            qlen = len(ocr.get("quick_text", "") or "")
        density = ocr.get("ocr_text_density")
        if src == "pdf":
            ocr["clean_text"] = ocr.get("quick_text", "")
            ocr["ocr_engine"] = "pdf_text"
            ocr["ocr_confidence"] = 1.0
            n_pdf += 1
            continue
        # Skip only when BOTH signals say "no text". A missing density is not
        # evidence of emptiness, so an unknown density blocks the skip and the
        # slide gets OCR'd — the docstring's "quick_text_len < L AND density < D".
        # The old `density is None or density < D` skipped on text length alone
        # whenever Stage B had not recorded a density.
        if qlen < skip_len and density is not None and density < skip_density:
            ocr["clean_text"] = ""
            ocr["ocr_engine"] = "skipped_no_text"
            ocr["ocr_confidence"] = 0.0
            n_skip += 1
            continue
        path = os.path.join(slides_dir, s["filename"])
        if os.path.isfile(path):
            surya_targets.append((s, os.path.abspath(path)))
        else:
            n_no_image += 1
            ocr["clean_text"] = ocr.get("quick_text", "")
            ocr["ocr_engine"] = "quick_text_fallback"
            ocr["ocr_confidence"] = 0.0
            ocr["ocr_fallback_reason"] = "image_missing"

    print(f"Routing: {len(surya_targets)} -> Surya, {n_pdf} pdf-text, "
          f"{n_skip} skipped(no-text), {n_no_image} image-missing")

    # Surya pass (single subprocess call, image list via manifest file)
    timeout_s = args.timeout or (TIMEOUT_BASE_S + TIMEOUT_PER_IMAGE_S * len(surya_targets))
    if surya_python:
        results, surya_error = run_surya(surya_python,
                                         [p for _, p in surya_targets], timeout_s)
    else:
        results, surya_error = {}, "surya_unavailable"

    n_surya = n_fallback = 0
    for s, path in surya_targets:
        ocr = s["ocr"]
        base = os.path.basename(path)
        r = results.get(base)
        if r is None:
            reason = surya_error or "no_surya_result"
        elif r.get("error"):
            reason = f"surya_error: {str(r.get('error'))[:120]}"
        elif r.get("page_confidence", 0.0) < conf_thr:
            reason = f"low_confidence<{conf_thr}"
        else:
            reason = None

        if reason is None:
            ocr["clean_text"] = join_blocks(r)
            ocr["ocr_engine"] = "surya"
            ocr["ocr_confidence"] = round(r.get("page_confidence", 0.0), 4)
            ocr.pop("ocr_fallback_reason", None)
            n_surya += 1
        else:
            text, conf, _items, err = rapidocr_text_ex(path)
            if err:
                ocr["clean_text"] = ocr.get("quick_text", "")
                ocr["ocr_engine"] = "quick_text_fallback"
                ocr["ocr_confidence"] = 0.0
                print(f"  [fallback] {base}: rapidocr also failed ({err})",
                      file=sys.stderr)
            else:
                ocr["clean_text"] = text
                ocr["ocr_engine"] = "rapidocr_fallback"
                ocr["ocr_confidence"] = round(conf, 4)
            ocr["ocr_fallback_reason"] = reason
            n_fallback += 1

    if n_fallback and surya_error:
        print(f"  NOTE: {n_fallback} slide(s) fell back from Surya "
              f"({surya_error}). Text is still produced, at lower quality.",
              file=sys.stderr)

    atomic_write_json(dedup_path, slides)
    atomic_write_json(ocr_out_path, slides)

    print(f"Done: surya={n_surya} fallback={n_fallback} pdf={n_pdf} skipped={n_skip} "
          f"-> {dedup_path} (+ {ocr_out_path})")


if __name__ == "__main__":
    main()
