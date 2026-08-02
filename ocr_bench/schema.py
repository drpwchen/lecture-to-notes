"""Canonical OCR adapter output schema + validator.

Every OCR engine adapter (rapidocr / surya / paddleocr_vl / future) MUST emit
this shape, one JSON object per image. The pipeline owns this schema; engines
are adapters. Downstream (grounding, VLM semantic tagging) consumes `blocks`
joined as text + `reading_order`, never engine-native markdown.
"""

REQUIRED = {
    "fixture": str,        # source image filename
    "engine": str,         # adapter id
    "blocks": list,        # [{"text": str, "bbox": [x,y,w,h]|null, "confidence": float}]
    "reading_order": list, # indices into blocks, top->bottom reading order
    "labels": list,        # short key labels (optional content; may be [])
    "page_confidence": (int, float),  # mean/agg confidence 0..1
    "latency_s": (int, float),
}


def validate(d):
    """Return (ok: bool, reason: str). Schema-validity is KPI #1."""
    if not isinstance(d, dict):
        return False, "not a dict"
    for k, t in REQUIRED.items():
        if k not in d:
            return False, f"missing {k}"
        if not isinstance(d[k], t):
            return False, f"{k} wrong type ({type(d[k]).__name__})"
    for i, b in enumerate(d["blocks"]):
        if not isinstance(b, dict) or "text" not in b:
            return False, f"block {i} malformed"
    if not all(isinstance(i, int) for i in d["reading_order"]):
        return False, "reading_order not all ints"
    return True, "ok"


def full_text(d):
    """Join blocks in reading order into one text string."""
    blocks = d.get("blocks", [])
    order = d.get("reading_order") or list(range(len(blocks)))
    parts = []
    for i in order:
        if 0 <= i < len(blocks):
            t = blocks[i].get("text", "")
            if t:
                parts.append(t)
    return "\n".join(parts)
