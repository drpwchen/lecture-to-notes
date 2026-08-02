"""RapidOCR adapter — baseline engine (current Stage B). Runs in the main env
(no separate venv needed). Emits canonical OCR schema as JSONL on stdout.

Usage: python rapidocr_adapter.py <img1> [<img2> ...]
"""
import sys, json, time, io

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ENGINE = "rapidocr"


def get_engine():
    from rapidocr_onnxruntime import RapidOCR
    return RapidOCR()


def run_one(engine, path):
    t = time.time()
    blocks, confs = [], []
    try:
        result, _ = engine(path)
        for item in (result or []):
            box, text, score = item[0], item[1], (item[2] if len(item) > 2 else None)
            try:
                xs = [p[0] for p in box]; ys = [p[1] for p in box]
                bbox = [min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)]
            except Exception:
                bbox = None
            conf = float(score) if score is not None else 0.0
            blocks.append({"text": str(text), "bbox": bbox, "confidence": round(conf, 4)})
            confs.append(conf)
        err = None
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:200]}"
    out = {
        "fixture": path.replace("\\", "/").split("/")[-1],
        "engine": ENGINE,
        "blocks": blocks,
        "reading_order": list(range(len(blocks))),  # RapidOCR returns top->bottom
        "labels": [],
        "page_confidence": round(sum(confs) / len(confs), 4) if confs else 0.0,
        "latency_s": round(time.time() - t, 3),
    }
    if err:
        out["error"] = err
    return out


def main():
    engine = get_engine()
    for path in sys.argv[1:]:
        print(json.dumps(run_one(engine, path), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
