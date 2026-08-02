"""PaddleOCR-VL adapter — FALLBACK candidate (generative VLM-OCR, strong Chinese).

⚠️ Generative decoder: watch repetition_score / runaway. Used only where Surya's
Chinese recall is poor. Runs in its OWN isolated venv (paddleocr / paddlex /
paddlepaddle-gpu) — classic Paddle conflicts with the main torch env, hence the
strict subprocess + venv boundary.

PaddleOCR-VL exposes a pipeline that returns layout blocks + recognized text +
reading order. The exact import path depends on the installed paddleocr version;
this adapter targets the PaddleOCRVL pipeline and degrades to a diagnostic on
failure so the call can be fixed post-install.

Usage (from the paddle venv):  python paddleocr_vl_adapter.py <img1> [<img2> ...]
"""
import sys, json, time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ENGINE = "paddleocr_vl"


def get_pipeline():
    # Newer paddleocr exposes PaddleOCRVL; API verified post-install.
    from paddleocr import PaddleOCRVL
    return PaddleOCRVL()


def _blocks_from_result(res):
    """Normalize a PaddleOCRVLResult into ([(text, bbox_xywh), ...], reading_order).

    Real structure (PaddleOCR-VL-1.5): res.json['res']['parsing_res_list'], each
    item = {block_label, block_content, block_bbox:[x0,y0,x1,y1], block_order, ...}.
    PaddleOCR-VL is generative and exposes no per-block confidence.
    """
    blocks, order = [], []
    j = getattr(res, "json", None)
    rd = (j.get("res") if isinstance(j, dict) and "res" in j else j) or {}
    items = rd.get("parsing_res_list") or []
    for idx, it in enumerate(items):
        if isinstance(it, str):
            blocks.append((it, None)); order.append(idx); continue
        if not isinstance(it, dict):
            continue
        text = it.get("block_content") or it.get("text") or ""
        bb = it.get("block_bbox") or it.get("bbox")
        if bb and len(bb) == 4:
            x0, y0, x1, y1 = bb
            bb = [x0, y0, x1 - x0, y1 - y0]
        blocks.append((str(text), bb))
        order.append(it.get("block_order") if it.get("block_order") is not None else idx)
    # build reading_order: indices into blocks sorted by block_order
    ro = sorted(range(len(blocks)), key=lambda i: (order[i] if isinstance(order[i], (int, float)) else i))
    return blocks, ro


def run_one(pipe, path):
    t = time.time()
    blocks, ro, err = [], [], None
    try:
        results = pipe.predict(path)
        for res in (results if isinstance(results, (list, tuple)) else [results]):
            bl, order = _blocks_from_result(res)
            base = len(blocks)
            for text, bbox in bl:
                blocks.append({"text": text, "bbox": bbox, "confidence": 0.0})
            ro.extend(base + i for i in order)
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:200]}"
    out = {
        "fixture": path.replace("\\", "/").split("/")[-1],
        "engine": ENGINE, "blocks": blocks,
        "reading_order": ro or list(range(len(blocks))),
        "labels": [],
        "page_confidence": 0.0,  # PaddleOCR-VL exposes no per-block confidence
        "latency_s": round(time.time() - t, 3),
    }
    if err:
        out["error"] = err
    return out


def main():
    try:
        pipe = get_pipeline()
    except Exception as e:
        print(f"PADDLEOCR-VL INIT FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(3)
    for path in sys.argv[1:]:
        print(json.dumps(run_one(pipe, path), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
