"""Stage D: VLM enrichment for canonical slides only (ollama minicpm-v:8b).

Renamed from ``ocr_slides.py`` on 2026-08-02 — this stage never did OCR, and the
misnomer was the source of the "three duplicate OCR scripts" confusion. The VLM
returns STRUCTURED JSON signals (content_type, visual_complexity, etc.) — NOT a
free-form text dump. Real OCR lives in quick_ocr.py (Stage B, RapidOCR) and
ocr_surya.py (Stage B2, Surya). ``ocr_slides.py`` stays as a deprecation shim.

Decisions about embedding stay with Claude synthesis (Stage F).

Conservative posture: do NOT diagnose, do NOT infer educational intent
that is not visible. Clinical images get labels/arrows/measurements only.

Pre-skip gate
-------------
Before each canonical slide is sent to the VLM, we evaluate a 4-condition
AND-gate using entropy metrics from Stage B::

    ocr_text_density   < 0.05   AND
    quick_text_len     < 20     AND
    file_size_kb       < 80     AND
    laplacian_variance < 100

When all four are true, the slide is "decorative low-entropy" — a title
card, section divider, blank slide, or near-blank build frame. Skipping
costs the slide nothing meaningful (it gets dummy decorative signals so
Stage F's scoring will tier it down). Raw metrics are still logged so we
can revisit threshold choices later without re-running VLM.

Timeout
-------
HTTP timeout is a tuple ``(connect=10s, inference=120s)``. A connect
failure is a network/setup error and should fail fast; a long inference
is normal for dense anatomy slides on a busy GPU.

Usage:
    python vlm_signals.py <out_dir> [--model minicpm-v:8b]
                          [--num-ctx 4096]
                          [--connect-timeout 10] [--inference-timeout 120]
                          [--no-skip-gate] [--allow-empty]
                          [--force]

Environment:
    OLLAMA_URL      full generate endpoint (default http://localhost:11434/api/generate)
    VLM_MAX_WIDTH   downscale width before the call, 0 disables (default 900)

Input:
    <out_dir>/slides_dedup.json     (from Stage C; superset of Stage B fields)
    <out_dir>/slides/*.jpg

Output:
    <out_dir>/slides_vlm.json       (pipeline_stage="vlm")
    <out_dir>/progress_vlm.jsonl    (event log)
"""
import argparse
import base64
import json
import os
import re
import sys
import time

try:
    import requests
except ImportError:
    # Never pip-install into the caller's interpreter at runtime: it mutates an
    # environment the user did not consent to change, and fails opaquely behind
    # a proxy or in a read-only venv.
    print("ERROR: the 'requests' package is required by Stage D but is not "
          "installed.\n  Fix with: pip install requests", file=sys.stderr)
    sys.exit(2)

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from _common import atomic_write_json  # noqa: E402

try:
    from _log import StageLogger
except Exception:
    StageLogger = None  # type: ignore


# Overridable so ollama can live on another host (or a tunnelled port).
OLLAMA_URL = os.environ.get("OLLAMA_URL") or "http://localhost:11434/api/generate"


PROMPT = """You are analyzing one medical lecture slide. Return ONLY valid JSON.
No markdown. No prose. No explanation.

Do NOT transcribe full text. Do NOT diagnose. Do NOT infer educational intent
that is not explicitly visible. OCR text comes from a separate engine — your job
is structural/semantic analysis only.

Required JSON fields:
- content_type: a LIST from:
  [flowchart, ultrasound, xray, mri, anatomy, table, chart,
   scatter_plot, kaplan_meier, title, text, decorative]
  Multi-type OK (e.g. ["title","flowchart"]).
- apparent_educational_function: a LIST from:
  [diagnostic_algorithm, staging_schema, treatment_protocol,
   anatomical_relationship, summary, decorative, evidence_table]
  Infer ONLY from visible structure. If unclear, return [].
- visible_labels: AT MOST 10-15 important short labels (titles, headers,
  axes, anatomy labels, flowchart node names, key terminology).
  DO NOT transcribe paragraphs, bullet lists, or body text.
- contains_measurement: bool
- contains_algorithm: bool (true if arrows/decision nodes form a flow)
- contains_clinical_imaging: bool
- text_redundancy: 0-1, how much of visual content is just rewording of text
- visual_complexity: 0-1, density of visual elements
- non_textual_information_density: 0-1, info beyond plain text (arrows,
  US findings, geometric relationships)
- model_confidence: 0-1, self-assessed

Clinical image rules (X-ray/US/MRI):
- DO NOT name pathology or make a diagnosis
- ONLY note labels, arrows, measurements, modality, anatomical region

Return ONLY the JSON object."""


VLM_SIGNAL_KEYS = {
    "content_type": list,
    "apparent_educational_function": list,
    "visible_labels": list,
    "contains_measurement": bool,
    "contains_algorithm": bool,
    "contains_clinical_imaging": bool,
    "text_redundancy": (int, float),
    "visual_complexity": (int, float),
    "non_textual_information_density": (int, float),
}


# ----------------------------------------------------------------------
# Pre-skip gate
# ----------------------------------------------------------------------

DEFAULT_SKIP_THRESHOLDS = {
    "text_density_max": 0.05,
    "text_len_max": 20,
    "file_size_kb_max": 80,
    "laplacian_variance_max": 100,
}


def evaluate_skip(slide, thresholds=None):
    """4-condition AND-gate. Returns (should_skip, reasons, metrics).

    Slides written before this gate existed lack the entropy block; treat
    those as "do not skip" so we don't regress earlier runs.
    """
    t = thresholds or DEFAULT_SKIP_THRESHOLDS
    ocr = slide.get("ocr", {}) or {}
    entropy = slide.get("entropy", {}) or {}

    density = ocr.get("ocr_text_density")
    text_len = entropy.get("quick_text_len")
    if text_len is None:
        text_len = len(ocr.get("quick_text", "") or "")
    file_size_kb = entropy.get("file_size_kb")
    lap_var = entropy.get("laplacian_variance")

    metrics = {
        "ocr_text_density": density,
        "quick_text_len": text_len,
        "file_size_kb": file_size_kb,
        "laplacian_variance": lap_var,
    }

    # Missing metrics → don't skip (Stage B was older than this gate).
    if (density is None or file_size_kb is None or lap_var is None):
        return False, ["entropy_metrics_missing"], metrics

    cond1 = density < t["text_density_max"]
    cond2 = text_len < t["text_len_max"]
    cond3 = file_size_kb < t["file_size_kb_max"]
    cond4 = lap_var < t["laplacian_variance_max"]

    reasons = []
    if cond1:
        reasons.append(f"density={density:.3f}<{t['text_density_max']}")
    if cond2:
        reasons.append(f"text_len={text_len}<{t['text_len_max']}")
    if cond3:
        reasons.append(f"file_kb={file_size_kb}<{t['file_size_kb_max']}")
    if cond4:
        reasons.append(f"lap_var={lap_var}<{t['laplacian_variance_max']}")

    should_skip = cond1 and cond2 and cond3 and cond4
    return should_skip, reasons, metrics


def make_skipped_signals():
    """Default VLM signals for a pre-skipped slide.

    Marked as `decorative` so Stage F's scoring tiers it down naturally.
    """
    return {
        "content_type": ["decorative"],
        "apparent_educational_function": [],
        "visible_labels": [],
        "contains_measurement": False,
        "contains_algorithm": False,
        "contains_clinical_imaging": False,
        "text_redundancy": 0.0,
        "visual_complexity": 0.0,
        "non_textual_information_density": 0.0,
    }


# ----------------------------------------------------------------------
# Ollama VLM call
# ----------------------------------------------------------------------

# Downscaling the frame before the VLM call cuts minicpm-v's adaptive-tiling
# visual-token count (~halves it at 900px), which directly shortens the image
# prefill — the dominant per-frame cost on this 8 GB card (mmproj runs on CPU,
# ~35 ms/token, so fewer visual tokens = proportionally faster). 900px keeps
# slide text / anatomy labels legible; below ~900 minicpm's slice tier is the
# same so there's no further gain. Override with VLM_MAX_WIDTH=0 to disable.
def _parse_max_width():
    raw = os.environ.get("VLM_MAX_WIDTH", "900")
    try:
        val = int(raw)
    except ValueError:
        print(f"ERROR: VLM_MAX_WIDTH must be an integer (got {raw!r}). "
              "Use 0 to disable downscaling.", file=sys.stderr)
        sys.exit(2)
    if val < 0:
        print(f"ERROR: VLM_MAX_WIDTH must be >= 0 (got {val}).", file=sys.stderr)
        sys.exit(2)
    return val


VLM_MAX_WIDTH = _parse_max_width()


def _img_b64(image_path):
    if VLM_MAX_WIDTH and VLM_MAX_WIDTH > 0:
        try:
            import io
            from PIL import Image
            im = Image.open(image_path).convert("RGB")
            if im.width > VLM_MAX_WIDTH:
                h = int(im.height * VLM_MAX_WIDTH / im.width)
                im = im.resize((VLM_MAX_WIDTH, h), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=90)
            return base64.b64encode(buf.getvalue()).decode("utf-8")
        except Exception:
            pass  # fall back to raw bytes if PIL/resize fails
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def call_ollama(image_path, model, num_ctx, connect_timeout, inference_timeout,
                temperature=0, top_p=0.8, top_k=20, repeat_penalty=1.1,
                num_predict=600, seed=42):
    img_b64 = _img_b64(image_path)
    payload = {
        "model": model,
        "prompt": PROMPT,
        "images": [img_b64],
        "stream": False,
        "format": "json",
        "options": {
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
            "repeat_penalty": repeat_penalty,
            "num_predict": num_predict,
            "num_ctx": num_ctx,
            "seed": seed,
            "stop": ["\n\n\n"],
        },
    }
    # (connect, read) tuple: 10s to establish socket, 120s for inference.
    resp = requests.post(OLLAMA_URL, json=payload,
                         timeout=(connect_timeout, inference_timeout))
    resp.raise_for_status()
    return resp.json().get("response", "").strip()


# Transient classes — worth retrying. Excludes 4xx (deterministic client
# error: bad request, model not loaded, etc — retry won't help).
_TRANSIENT_HTTP = {500, 502, 503, 504, 522, 524}

# WP1-2 soft-fail guard (2026-07-09): if MORE than this fraction of ATTEMPTED
# (non-skipped) slides come back vlm_failed, the run drops a flag and exits
# non-zero instead of silently returning 0 with empty vlm_signals. See main().
VLM_FAIL_RATIO_THRESHOLD = 0.20


def _is_transient(exc) -> bool:
    if isinstance(exc, (requests.exceptions.Timeout,
                        requests.exceptions.ConnectionError,
                        requests.exceptions.ChunkedEncodingError)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        try:
            return exc.response.status_code in _TRANSIENT_HTTP
        except AttributeError:
            return False
    return False


FALLBACK_PROMPT = """Analyze this medical lecture slide. Return ONLY valid JSON with these two fields:
{
  "content_type": [...],  // LIST from [flowchart, ultrasound, xray, mri, anatomy, table, chart, scatter_plot, kaplan_meier, title, text, decorative]
  "apparent_educational_function": [...]  // LIST from [diagnostic_algorithm, staging_schema, treatment_protocol, anatomical_relationship, summary, decorative, evidence_table] or []
}
NO other fields. NO prose. Return ONLY the JSON object."""


def call_ollama_fallback(image_path, model, num_ctx,
                          connect_timeout, inference_timeout, sampling=None):
    """Last-resort minimal-schema retry. Used when primary call fails to parse.
    Returns raw JSON string.

    ``sampling`` (the caller's --temperature/--seed/... choices) is honored:
    only the deliberately tighter fallback defaults it does not mention stay in
    force. Previously the argument was accepted and then ignored, so a run
    pinned to a specific seed silently used seed 42 on every fallback call.
    """
    sampling = sampling or {}
    img_b64 = _img_b64(image_path)
    options = {
        "temperature": 0,
        "top_p": 0.5,
        "top_k": 10,
        "repeat_penalty": 1.2,
        "num_predict": 200,  # very short
        "num_ctx": num_ctx,
        "seed": 42,
        "stop": ["\n\n"],
    }
    # num_predict stays at the fallback's short budget — the whole point of this
    # tier is a tiny two-field answer.
    for k, v in sampling.items():
        if k != "num_predict":
            options[k] = v
    payload = {
        "model": model,
        "prompt": FALLBACK_PROMPT,
        "images": [img_b64],
        "stream": False,
        "format": "json",
        "options": options,
    }
    resp = requests.post(OLLAMA_URL, json=payload,
                         timeout=(connect_timeout, inference_timeout))
    resp.raise_for_status()
    return resp.json().get("response", "").strip()


def call_ollama_with_retry(image_path, model, num_ctx,
                           connect_timeout, inference_timeout,
                           log, slide_id, max_retries=2, backoff_base=2.0,
                           sampling=None):
    """Wrap call_ollama with retry on transient HTTP / timeout / connection
    errors. Each retry doubles the wait (2s, 4s). Returns the raw response
    string on success; re-raises the last exception on final failure.
    """
    last_err = None
    sampling = sampling or {}
    for attempt in range(max_retries + 1):
        try:
            return call_ollama(image_path, model, num_ctx,
                               connect_timeout, inference_timeout,
                               **sampling)
        except Exception as e:  # noqa: BLE001
            if not _is_transient(e):
                # 4xx / image-read / other deterministic failure — bail now
                raise
            last_err = e
            if attempt >= max_retries:
                break
            # 2s, 4s, 8s... — start at backoff_base^1 so the first retry has
            # a meaningful gap rather than 1s.
            wait_s = backoff_base ** (attempt + 1)
            if log is not None:
                log.retry(reason="vlm_transient",
                          slide_id=slide_id,
                          attempt=attempt + 1,
                          wait_s=wait_s,
                          error=f"{type(e).__name__}: {str(e)[:200]}")
            time.sleep(wait_s)
    # Exhausted — re-raise the last transient error
    raise last_err


def extract_json(raw_text):
    """Try to parse JSON, with progressive repair fallbacks:
       1. direct json.loads
       2. strip code fences
       3. find first {...last }
       4. json_repair library (handles missing braces, trailing commas, etc.)
    """
    if not raw_text:
        return None
    s = raw_text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*", "", s, count=1).strip()
        if s.endswith("```"):
            s = s[:-3].strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(s[start:end + 1])
        except json.JSONDecodeError:
            pass
    # Last resort: json_repair library — handles truncated, missing braces,
    # trailing commas, unquoted keys. Tagged so caller knows it was repaired.
    try:
        import json_repair
        repaired = json_repair.loads(s if start < 0 else s[start:])
        if isinstance(repaired, dict) and repaired:
            return repaired
    except Exception:
        pass
    return None


def normalize_signals(parsed):
    """Pull VLM signal fields out, validate types, coerce missing → defaults."""
    signals = {}
    for k, expected in VLM_SIGNAL_KEYS.items():
        v = parsed.get(k) if isinstance(parsed, dict) else None
        if v is None:
            signals[k] = [] if expected is list else (False if expected is bool else 0.0)
            continue
        if expected is list:
            signals[k] = v if isinstance(v, list) else [str(v)]
        elif expected is bool:
            signals[k] = bool(v)
        else:
            try:
                signals[k] = float(v)
            except (TypeError, ValueError):
                signals[k] = 0.0
    return signals


def process_slide(slide, slides_dir, model, num_ctx,
                  connect_timeout, inference_timeout,
                  log=None, max_retries=2, sampling=None):
    """Run VLM on one slide with transient-error retry (2x with backoff).

    Sets vlm_skip=False on success/failure paths (caller handles the
    skip path before this function).
    """
    img_path = os.path.join(slides_dir, slide["filename"])
    slide["vlm_skip"] = False
    slide["skip_reason"] = None

    if not os.path.isfile(img_path):
        slide.setdefault("ocr", {}).update({
            "vlm_text": "",
            "source": "none",
            "parse_valid": False,
            "model_confidence": 0.0,
        })
        slide["vlm_signals"] = None
        slide["pipeline_stage"] = "vlm"
        return slide, "missing_file"

    try:
        raw = call_ollama_with_retry(
            img_path, model, num_ctx,
            connect_timeout, inference_timeout,
            log=log, slide_id=slide.get("slide_id"),
            max_retries=max_retries,
            sampling=sampling,
        )
    except Exception as e:
        slide.setdefault("ocr", {}).update({
            # vlm_text is a zombie: the slim prompt stopped asking for text, so
            # it is always "". Kept (empty) because ground_slides.py still reads
            # the key. Diagnostics go in vlm_error, not into a text field that
            # downstream concatenates into slide content.
            "vlm_text": "",
            "vlm_error": f"call_failed: {type(e).__name__}: {str(e)[:200]}",
            "source": "vlm_failed",
            "parse_valid": False,
            "model_confidence": 0.0,
        })
        slide["vlm_signals"] = None
        slide["pipeline_stage"] = "vlm"
        return slide, f"vlm_error: {type(e).__name__}"

    parsed = extract_json(raw)
    if parsed is None or not isinstance(parsed, dict):
        # Tiered fallback: re-call with ultra-minimal schema (classification only)
        try:
            raw2 = call_ollama_fallback(img_path, model, num_ctx,
                                        connect_timeout, inference_timeout,
                                        sampling=sampling)
            parsed = extract_json(raw2)
        except Exception:
            parsed = None
        if parsed is None or not isinstance(parsed, dict):
            slide.setdefault("ocr", {}).update({
                "vlm_text": "",
                "vlm_error": f"parse_failed; raw tail: {(raw or '')[-200:]}",
                "source": "vlm_failed",
                "parse_valid": False,
                "model_confidence": 0.0,
            })
            slide["vlm_signals"] = None
            slide["pipeline_stage"] = "vlm"
            return slide, "parse_failed"
        # fallback succeeded — fill missing fields with defaults
        parsed.setdefault("visible_labels", [])
        parsed.setdefault("contains_measurement", False)
        parsed.setdefault("contains_algorithm", False)
        parsed.setdefault("contains_clinical_imaging", False)
        parsed.setdefault("text_redundancy", 0.0)
        parsed.setdefault("visual_complexity", 0.0)
        parsed.setdefault("non_textual_information_density", 0.0)
        parsed.setdefault("model_confidence", 0.3)  # mark degraded
        parsed["_fallback_tier"] = "classification_only"

    try:
        confidence = float(parsed.get("model_confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    slide.setdefault("ocr", {}).update({
        # Always "": the slim prompt does not ask the VLM for text, so parsing a
        # vlm_text field out of the response was reading a value that has not
        # existed since the prompt was slimmed. The key itself stays because
        # ground_slides.py reads it (and treats "" correctly).
        "vlm_text": "",
        "vlm_error": None,
        "source": "vlm",
        "parse_valid": True,
        "model_confidence": round(confidence, 3),
    })
    slide["vlm_signals"] = normalize_signals(parsed)
    slide["pipeline_stage"] = "vlm"
    return slide, "success"


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Stage D: VLM enrichment with pre-skip gate")
    parser.add_argument("out_dir", help="Lecture output directory")
    parser.add_argument("--model", "-m", default="minicpm-v:8b",
                        help="Ollama VLM model (default: minicpm-v:8b)")
    parser.add_argument("--num-ctx", type=int, default=4096,
                        help="Ollama num_ctx (default: 4096)")
    parser.add_argument("--connect-timeout", type=int, default=10,
                        help="Ollama HTTP connect timeout in seconds (default 10)")
    parser.add_argument("--inference-timeout", type=int, default=120,
                        help="Ollama HTTP read (inference) timeout in seconds (default 120)")
    parser.add_argument("--no-skip-gate", action="store_true",
                        help="Disable the 4-condition pre-skip gate (run VLM on every canonical slide)")
    parser.add_argument("--max-retries", type=int, default=2,
                        help="Per-slide retry count for transient HTTP/timeout errors (default 2). Set 0 to disable.")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite output (default behavior)")
    parser.add_argument("--allow-empty", action="store_true",
                        help="Proceed when every canonical slide is pre-skipped as "
                             "decorative (zero VLM calls). Without this, that case "
                             "exits 2 rather than writing signal-free output.")
    parser.add_argument("--save-every", type=int, default=20,
                        help="Incrementally save slides_vlm.json every N slides (default 20). 0 disables.")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from existing slides_vlm.json: skip VLM calls for slides that already have vlm_signals populated")
    parser.add_argument("--verbose", action="store_true",
                        help="Print one line per slide (default: print every 20). JSONL log is always full.")
    parser.add_argument("--temperature", type=float, default=0, help="Sampling temperature")
    parser.add_argument("--top-p", type=float, default=0.8, help="Top-p sampling")
    parser.add_argument("--top-k", type=int, default=20, help="Top-k sampling")
    parser.add_argument("--repeat-penalty", type=float, default=1.1, help="Repetition penalty")
    parser.add_argument("--num-predict", type=int, default=600, help="Max output tokens (slim prompt — no vlm_text)")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed (deterministic)")
    args = parser.parse_args()

    # argparse accepts any int, so --max-retries -1 used to reach
    # `raise last_err` with last_err still None ("exceptions must derive from
    # BaseException"). 0 remains valid and means "no retry".
    if args.max_retries < 0:
        print(f"ERROR: --max-retries must be >= 0 (got {args.max_retries}); "
              "0 disables retrying.", file=sys.stderr)
        sys.exit(2)

    # Cooperative GPU pause button: yield while someone holds `gpu_lease pause`.
    # Best-effort: a missing config (or missing pyyaml) must not block the stage.
    # The wait is bounded inside wait_if_paused, so a stale flag cannot wedge us.
    try:
        from _common import load_config, resolve_pause_flag, wait_if_paused
        wait_if_paused(resolve_pause_flag(load_config()))
    except Exception as e:  # noqa: BLE001
        print(f"[vlm_signals] pause check skipped ({e})", file=sys.stderr)

    dedup_path = os.path.join(args.out_dir, "slides_dedup.json")
    slides_dir = os.path.join(args.out_dir, "slides")
    out_path = os.path.join(args.out_dir, "slides_vlm.json")

    if not os.path.isfile(dedup_path):
        print(f"ERROR: {dedup_path} not found. Run dedup_semantic.py first.",
              file=sys.stderr)
        sys.exit(2)

    with open(dedup_path, "r", encoding="utf-8") as f:
        slides = json.load(f)

    canonical = [s for s in slides if s.get("dedup", {}).get("is_canonical")]
    non_canonical = [s for s in slides if not s.get("dedup", {}).get("is_canonical")]

    # ==Guard hole closed (2026-08-02)== The fail-ratio guard at the bottom is
    # computed over ATTEMPTED slides, so a run with zero attempts divided by
    # nothing, skipped the check, and wrote a slides_vlm.json with no VLM signal
    # in it — exit 0. That is exactly the silent total content loss the guard
    # exists to catch. Both zero-attempt shapes are now caught up front, before
    # anything is written:
    if not slides:
        print(f"ERROR: {dedup_path} contains no slides. Stage C produced nothing "
              "— re-run dedup_semantic.py; do not continue with an empty deck.",
              file=sys.stderr)
        sys.exit(2)
    if not canonical:
        print(f"ERROR: {dedup_path} has {len(slides)} slides but none marked "
              "dedup.is_canonical, so there is nothing for Stage D to analyze. "
              "This normally means Stage C failed or wrote a partial file; "
              "re-run dedup_semantic.py.", file=sys.stderr)
        sys.exit(2)

    # Resume support: load prior slides_vlm.json if it exists; slides with
    # populated vlm_signals are skipped (carried forward unchanged).
    resume_done_ids = set()
    resume_map = {}
    if args.resume and os.path.isfile(out_path):
        try:
            with open(out_path, "r", encoding="utf-8") as f:
                prior = json.load(f)
            for ps in prior:
                vs = ps.get("vlm_signals") or {}
                # "Done" means vlm_signals dict has non-empty content_type OR slide was skipped
                if (ps.get("vlm_skip") or
                        (vs.get("content_type") and isinstance(vs.get("content_type"), list))):
                    resume_done_ids.add(ps["slide_id"])
                    resume_map[ps["slide_id"]] = ps
            print(f"  [resume] loaded {len(resume_done_ids)} prior VLM-completed slides from {out_path}")
        except Exception as e:
            print(f"  [resume] failed to load prior output: {e}; starting fresh")
            resume_done_ids = set()
            resume_map = {}

    log = StageLogger("vlm", args.out_dir,
                      extra={"total": len(canonical),
                             "model": args.model,
                             "num_ctx": args.num_ctx,
                             "skip_gate": not args.no_skip_gate}) \
        if StageLogger else None
    if log:
        log.start(connect_timeout=args.connect_timeout,
                  inference_timeout=args.inference_timeout)

    print(f"Stage D: {len(canonical)} canonical / {len(slides)} total slides "
          f"-> model={args.model}, num_ctx={args.num_ctx}")
    start = time.time()

    results = []
    n_vlm_called = 0
    n_skipped = 0
    n_vlm_success = 0
    n_vlm_failed = 0
    vlm_total_s = 0.0  # cumulative time across all VLM calls for ETA

    # Heartbeat thread runs only during the VLM loop (the only slow part).
    hb_ctx = log.heartbeat(interval_s=30.0) if log else _NullCtx()
    with hb_ctx:
        for idx, s in enumerate(canonical):
            slide_id = s["slide_id"]
            ts_start = s.get("timestamp_start", 0)

            # Resume: if this slide was already VLM'd in prior run, carry forward
            if slide_id in resume_done_ids:
                results.append(resume_map[slide_id])
                if (idx + 1) % 50 == 0:
                    print(f"  [resume-skip] up to slide {slide_id} (idx {idx+1})")
                continue

            should_skip, reasons, metrics = (
                (False, ["skip_gate_disabled"], {})
                if args.no_skip_gate
                else evaluate_skip(s)
            )

            if should_skip:
                s["vlm_skip"] = True
                s["skip_reason"] = "decorative_low_entropy"
                s["skip_metrics"] = metrics
                s["vlm_signals"] = make_skipped_signals()
                s.setdefault("ocr", {}).update({
                    "vlm_text": "",
                    "source": "skipped",
                    "parse_valid": False,
                    "model_confidence": 0.0,
                })
                s["pipeline_stage"] = "vlm"
                n_skipped += 1
                results.append(s)
                ts_s_i2 = int(ts_start)
                print(f"  Slide {slide_id:3d} [{ts_s_i2//60:02d}:{ts_s_i2%60:02d}] "
                      f"SKIP ({', '.join(reasons)})")
                if log:
                    log.emit("slide_skip", status="success",
                             slide_id=slide_id, idx=idx + 1,
                             reasons=reasons, metrics=metrics)
                continue

            t = time.time()
            n_vlm_called += 1
            s, status = process_slide(
                s, slides_dir, args.model, args.num_ctx,
                args.connect_timeout, args.inference_timeout,
                log=log, max_retries=args.max_retries,
                sampling={
                    "temperature": args.temperature,
                    "top_p": args.top_p,
                    "top_k": args.top_k,
                    "repeat_penalty": args.repeat_penalty,
                    "num_predict": args.num_predict,
                    "seed": args.seed,
                },
            )
            dt = time.time() - t
            vlm_total_s += dt
            valid = s["ocr"].get("parse_valid")
            ctypes_list = (s.get("vlm_signals") or {}).get("content_type", []) or []

            avg_slide_time = vlm_total_s / n_vlm_called
            queue_remaining = len(canonical) - (idx + 1)
            eta_s = round(queue_remaining * avg_slide_time, 1)

            if status == "success":
                n_vlm_success += 1
                if log:
                    log.emit("slide_done", status="success",
                             slide_id=slide_id, idx=idx + 1,
                             elapsed_s=round(dt, 1),
                             avg_slide_s=round(avg_slide_time, 1),
                             queue_remaining=queue_remaining,
                             eta_s=eta_s,
                             content_type=ctypes_list)
            else:
                n_vlm_failed += 1
                if log:
                    log.emit("slide_error", status="error",
                             slide_id=slide_id, idx=idx + 1,
                             elapsed_s=round(dt, 1),
                             avg_slide_s=round(avg_slide_time, 1),
                             queue_remaining=queue_remaining,
                             error=status)

            eta_str = f"ETA {eta_s/60:.1f}min" if queue_remaining else "done"
            # Default: print only every 20 slides (or on error/failure) to keep stdout terse.
            # JSONL log captures full per-slide detail regardless.
            if args.verbose or status != "success" or (idx + 1) % 20 == 0:
                ts_s_i = int(ts_start)
                print(f"  Slide {slide_id:3d} [{ts_s_i//60:02d}:{ts_s_i%60:02d}] "
                      f"{dt:5.1f}s valid={valid} status={status} "
                      f"avg={avg_slide_time:.1f}s remaining={queue_remaining} {eta_str}")
            results.append(s)

            if log and (idx + 1) % 10 == 0:
                log.progress(idx=idx + 1,
                             vlm_called=n_vlm_called,
                             skipped=n_skipped,
                             failed=n_vlm_failed,
                             avg_slide_s=round(avg_slide_time, 1),
                             queue_remaining=queue_remaining,
                             eta_s=eta_s)

            # Incremental save: write current results to disk atomically.
            # Includes processed canonical + non-canonical carryover.
            if args.save_every > 0 and (idx + 1) % args.save_every == 0:
                partial = list(results)
                for nc in non_canonical:
                    nc_copy = dict(nc)
                    nc_copy["pipeline_stage"] = "vlm"
                    nc_copy.setdefault("ocr", {}).setdefault("vlm_text", "")
                    nc_copy["ocr"].setdefault("source", "quick")
                    nc_copy["ocr"].setdefault("parse_valid", False)
                    nc_copy["ocr"].setdefault("model_confidence", 0.0)
                    nc_copy.setdefault("vlm_signals", None)
                    nc_copy.setdefault("vlm_skip", False)
                    nc_copy.setdefault("skip_reason", None)
                    partial.append(nc_copy)
                partial.sort(key=lambda x: x["slide_id"])
                tmp_path = out_path + ".tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(partial, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, out_path)
                print(f"  [incremental-save] saved {len(partial)} slides after idx={idx+1}")

    # Carry non-canonical through unchanged.
    for s in non_canonical:
        s["pipeline_stage"] = "vlm"
        s.setdefault("ocr", {}).setdefault("vlm_text", "")
        s["ocr"].setdefault("source", "quick")
        s["ocr"].setdefault("parse_valid", False)
        s["ocr"].setdefault("model_confidence", 0.0)
        s.setdefault("vlm_signals", None)
        s.setdefault("vlm_skip", False)
        s.setdefault("skip_reason", None)
        results.append(s)

    results.sort(key=lambda s: s["slide_id"])

    # Zero VLM calls attempted, and not because a prior run already did the work:
    # every canonical slide was pre-skipped as decorative. That can be legitimate
    # (a deck of pure title cards) but it is far more often a broken Stage B —
    # missing OCR text makes every slide look decorative to the gate. Refuse to
    # write signal-free output unless the operator says so explicitly.
    if n_vlm_called == 0 and not resume_done_ids:
        print(f"\nERROR: 0 VLM calls over {len(canonical)} canonical slides "
              f"({n_skipped} pre-skipped as decorative_low_entropy). Nothing was "
              f"written to {out_path}.\n"
              "  Most likely cause: Stage B produced no OCR text (RapidOCR "
              "missing/failing), which makes every slide look decorative to the "
              "pre-skip gate. Check slides_raw.json for empty quick_text.\n"
              "  If this deck really is all decorative, re-run with --allow-empty "
              "(or --no-skip-gate to VLM everything).", file=sys.stderr)
        if not args.allow_empty:
            if log:
                log.stage_done(success=False, n_canonical=len(canonical),
                               n_vlm_called=0, n_skipped=n_skipped,
                               error="all_canonical_pre_skipped")
                log.close()
            sys.exit(2)
        print("  --allow-empty given; writing the all-decorative result anyway.",
              file=sys.stderr)

    # Atomic: 20+ minutes of GPU work must not be lost to a truncated write.
    atomic_write_json(out_path, results)

    elapsed = time.time() - start
    print(f"\nDone: {n_vlm_called} VLM calls "
          f"({n_vlm_success} ok, {n_vlm_failed} failed), "
          f"{n_skipped} pre-skipped, {len(canonical)} canonical total "
          f"in {elapsed:.1f}s -> {out_path}")

    if log:
        log.stage_done(success=True,
                       n_canonical=len(canonical),
                       n_vlm_called=n_vlm_called,
                       n_vlm_success=n_vlm_success,
                       n_vlm_failed=n_vlm_failed,
                       n_skipped=n_skipped,
                       elapsed_s=round(elapsed, 1))
        log.close()

    # WP1-2 soft-fail guard (2026-07-09): a whole clip could previously finish
    # with EVERY VLM call failing (ollama down, model unloaded mid-run, etc) and
    # still exit 0 — build_L1 then quietly falls back to raw quick_text for the
    # whole clip and the content loss is invisible. Ratio is over ATTEMPTED
    # slides (n_vlm_called: excludes pre-skipped decorative frames + resume
    # carry-forwards) so a course that's legitimately mostly pre-skip doesn't
    # false-trip. slides_vlm.json is already fully written above, so a resume run
    # (--resume) re-attempts only the failed slides and clears the flag on pass.
    flag_path = os.path.join(args.out_dir, "_vlm_failed_ratio.flag")
    if n_vlm_called > 0:
        fail_ratio = n_vlm_failed / n_vlm_called
        if fail_ratio > VLM_FAIL_RATIO_THRESHOLD:
            with open(flag_path, "w", encoding="utf-8") as f:
                json.dump({
                    "n_vlm_called": n_vlm_called,
                    "n_vlm_failed": n_vlm_failed,
                    "n_vlm_success": n_vlm_success,
                    "n_skipped": n_skipped,
                    "fail_ratio": round(fail_ratio, 4),
                    "threshold": VLM_FAIL_RATIO_THRESHOLD,
                }, f, ensure_ascii=False, indent=2)
            print(f"\n❌ VLM fail ratio {fail_ratio:.0%} "
                  f"({n_vlm_failed}/{n_vlm_called} attempted) > "
                  f"{VLM_FAIL_RATIO_THRESHOLD:.0%} threshold — wrote "
                  f"{flag_path}, exiting 1")
            sys.exit(1)
    # Below threshold (or nothing attempted): clear any stale flag from a prior
    # failing run so a recovered clip stops alarming audit_batch.
    if os.path.exists(flag_path):
        os.remove(flag_path)


class _NullCtx:
    def __enter__(self): return self
    def __exit__(self, *a): return False


if __name__ == "__main__":
    main()
