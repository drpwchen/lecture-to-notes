"""Surya adapter — DEFAULT candidate (deterministic layout + line-bounded rec,
returns boxes+text+confidence+reading order natively = low entropy, English-strong).

Runs in an ISOLATED venv (torch + surya-ocr). Emits canonical OCR schema as JSONL.

Surya's API has shifted across versions. This adapter tries the recent
DetectionPredictor + RecognitionPredictor API and prints the installed version +
a diagnostic to stderr if it can't bind, so the call can be adjusted post-install.

Moved 2026-08-02 from ocr_bench/adapters/ to scripts/adapters/: production
(ocr_surya.py Stage B2) must not depend on a benchmark tree. ocr_bench/run_bench.py
now points here too, so there is still exactly one copy.

Usage (from the surya venv):
    python surya_adapter.py <img1> [<img2> ...]
    python surya_adapter.py --manifest <file>   # one image path per line, UTF-8

The manifest form exists because Windows caps a command line at 32,767
characters: a 250-slide lecture with CJK directory names overflows that as a bare
argv list (WinError 206). ocr_surya.py always uses --manifest.
"""
import sys, json, time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ENGINE = "surya"


def get_predictors():
    # Surya 2 API: RecognitionPredictor wraps a FoundationPredictor.
    from surya.foundation import FoundationPredictor
    from surya.detection import DetectionPredictor
    from surya.recognition import RecognitionPredictor
    try:
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        device = "cpu"
    foundation = FoundationPredictor(device=device)
    return DetectionPredictor(device=device), RecognitionPredictor(foundation)


def _recognize(rec, det, image):
    """Try known call signatures across surya versions; return list of text lines."""
    # Newer API: rec(images, det_predictor=det) -> [OCRResult(text_lines=[...])]
    try:
        res = rec([image], det_predictor=det)
        return res[0].text_lines
    except TypeError:
        pass
    # Older API: rec(images, langs, det) with explicit langs
    res = rec([image], [None], det)
    return res[0].text_lines


def run_one(rec, det, path):
    from PIL import Image
    t = time.time()
    blocks, confs, err = [], [], None
    try:
        image = Image.open(path).convert("RGB")
        for line in _recognize(rec, det, image):
            text = getattr(line, "text", "") or ""
            conf = float(getattr(line, "confidence", 0.0) or 0.0)
            bbox = getattr(line, "bbox", None)
            if bbox and len(bbox) == 4:
                x0, y0, x1, y1 = bbox
                bbox = [x0, y0, x1 - x0, y1 - y0]
            blocks.append({"text": str(text), "bbox": bbox, "confidence": round(conf, 4)})
            confs.append(conf)
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:200]}"
    base = path.replace("\\", "/").split("/")[-1]
    out = {
        # "image" is the key going forward; "fixture" is benchmark vocabulary
        # that leaked into production. Both are emitted for one release so an
        # older ocr_surya.py still finds its results; readers prefer "image".
        "image": base,
        "fixture": base,
        "engine": ENGINE, "blocks": blocks,
        "reading_order": list(range(len(blocks))),  # surya returns reading order
        "labels": [],
        "page_confidence": round(sum(confs) / len(confs), 4) if confs else 0.0,
        "latency_s": round(time.time() - t, 3),
    }
    if err:
        out["error"] = err
    return out


def _image_paths(argv):
    if len(argv) >= 2 and argv[0] == "--manifest":
        with open(argv[1], encoding="utf-8") as fh:
            return [ln.strip() for ln in fh if ln.strip()]
    return list(argv)


def main():
    try:
        paths = _image_paths(sys.argv[1:])
    except OSError as e:
        print(f"MANIFEST READ FAILED: {e}", file=sys.stderr)
        sys.exit(2)
    try:
        det, rec = get_predictors()
    except Exception as e:
        import surya  # noqa
        print(f"SURYA INIT FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        print(f"surya version: {getattr(surya, '__version__', '?')}", file=sys.stderr)
        sys.exit(3)
    for path in paths:
        print(json.dumps(run_one(rec, det, path), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
