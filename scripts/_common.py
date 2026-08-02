"""Shared helpers for the lecture-to-notes pipeline scripts.

Created 2026-08-02 (WP4) as the portability foundation: every helper that was
duplicated (and had drifted) across stage scripts lives here in ONE canonical
form, and every machine-specific path/model reference resolves through
env -> config.yaml -> built-in default so the skill runs on a fresh machine.

Nothing here imports heavy deps at module scope — numpy/PIL/yaml/rapidocr are
imported lazily inside the functions that need them, so a script that only
wants `fmt_hms` does not pay for (or crash on) a missing OCR stack.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Iterable, Sequence

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)


def _die(msg: str) -> None:
    """Print an actionable message to stderr and exit 2 (the pipeline's
    'bad input / bad environment' code, as used by every stage script)."""
    print(msg, file=sys.stderr)
    sys.exit(2)


# ----------------------------------------------------------------------
# Segment / transcript I/O
# ----------------------------------------------------------------------

def load_segments(path: str) -> list[dict]:
    """Load a transcript segment list, accepting both on-disk shapes.

    The pipeline has two writers: transcript.json is a bare ``[{start, end,
    text}, ...]`` list, while some tools emit ``{"segments": [...]}``. Stage
    scripts disagreed about which they accepted (ground_slides /
    flag_asr_suspects crashed on the dict form). This is the single reader.

    Raises SystemExit(2) with an actionable message when the file is missing,
    unparseable, or neither shape.
    """
    if not os.path.isfile(path):
        _die(f"ERROR: segments file not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as e:
        _die(f"ERROR: cannot read segments from {path}: {e}")

    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("segments"), list):
        return data["segments"]
    _die(f"ERROR: {path} is neither a list of segments nor "
         f'{{"segments": [...]}} (got {type(data).__name__})')


def fmt_hms(seconds: float) -> str:
    """Seconds -> ``MM:SS`` under one hour, ``H:MM:SS`` at/over one hour.

    Existing transcript.txt files use ``[MM:SS]``; keeping that below 60 min
    means re-running a stage produces a clean diff.
    """
    total = int(seconds)
    if total < 0:
        total = 0
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def write_transcript_lines(segments: Iterable[dict], txt_path: str) -> None:
    """Canonical ``[MM:SS] text`` transcript writer (one line per segment).

    Text is ``.strip()``ed (retranscribe_segment's behavior wins) so a rerun
    over already-written output is idempotent.
    """
    with open(txt_path, "w", encoding="utf-8") as fh:
        for seg in segments:
            fh.write(f"[{fmt_hms(seg.get('start', 0.0))}] "
                     f"{(seg.get('text') or '').strip()}\n")


def atomic_write_json(path: str, obj: Any, **json_kw: Any) -> None:
    """Write JSON via ``path + '.tmp'`` then ``os.replace``.

    Prevents a crash mid-write from leaving a truncated slides_*.json that a
    later stage would happily parse as "fewer slides".
    """
    json_kw.setdefault("ensure_ascii", False)
    json_kw.setdefault("indent", 2)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, **json_kw)
    os.replace(tmp, path)


# ----------------------------------------------------------------------
# Environment / preflight
# ----------------------------------------------------------------------

def setup_nvidia_path() -> None:
    """Add NVIDIA DLL paths for CUDA on Windows (cublas, cudnn from pip)."""
    if sys.platform == "win32":
        import importlib.util
        for pkg in ("nvidia.cublas", "nvidia.cudnn"):
            spec = importlib.util.find_spec(pkg)
            if spec and spec.submodule_search_locations:
                for loc in spec.submodule_search_locations:
                    bin_dir = os.path.join(loc, "bin")
                    if os.path.isdir(bin_dir):
                        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")


_INSTALL_HINTS = {
    "ffmpeg": "install ffmpeg (Windows: winget install Gyan.FFmpeg / macOS: brew install ffmpeg) and put it on PATH",
    "ffprobe": "ffprobe ships with ffmpeg (Windows: winget install Gyan.FFmpeg / macOS: brew install ffmpeg)",
    "pandoc": "install pandoc (Windows: winget install JohnMacFarlane.Pandoc / macOS: brew install pandoc)",
    "nvidia-smi": "install the NVIDIA driver, or run this stage on CPU",
}


def require_binaries(*names: str) -> None:
    """``shutil.which`` preflight; exit 2 naming each missing binary + how to get it."""
    missing = [n for n in names if shutil.which(n) is None]
    if not missing:
        return
    for n in missing:
        hint = _INSTALL_HINTS.get(n, f"install {n} and put it on PATH")
        print(f"ERROR: required binary '{n}' not found — {hint}", file=sys.stderr)
    sys.exit(2)


def nvidia_smi_mem_mb() -> int | None:
    """Used VRAM (MB) on GPU 0. None if nvidia-smi is unavailable.

    Multi-GPU safe: nvidia-smi prints one row per GPU, so take the first.
    """
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--id=0", "--query-gpu=memory.used",
             "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL, text=True, timeout=3,
        ).strip().splitlines()
        if out:
            return int(out[0].strip())
    except (subprocess.SubprocessError, FileNotFoundError, ValueError, OSError):
        pass
    return None


# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------

def load_config(skill_dir: str | None = None, filename: str = "config.yaml") -> dict:
    """Load ``<skill_dir>/<filename>``. Returns {} if the file is absent.

    A missing pyyaml is reported as a pyyaml problem — never silently as
    "config not set", which previously sent people hunting the wrong bug.
    """
    skill_dir = skill_dir or SKILL_DIR
    cfg_path = os.path.join(skill_dir, filename)
    if not os.path.isfile(cfg_path):
        return {}
    try:
        import yaml  # type: ignore
    except ImportError:
        raise RuntimeError(
            f"cannot read {cfg_path}: pyyaml is not installed. "
            "Fix with: pip install pyyaml"
        ) from None
    with open(cfg_path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def resolve_model(name: str, cfg: dict | None = None) -> str:
    """Resolve a model alias (``models.aliases``) to a path or HF hub name.

    ``{whisper_model_dir}`` inside an alias is filled from env
    ``LECTURE_WHISPER_MODEL_DIR`` -> ``paths.whisper_model_dir`` -> ''. If an
    alias expands to a filesystem path that does not exist, exit 2 with a
    suggestion rather than letting faster-whisper fail deep in a load.
    """
    cfg = cfg if cfg is not None else {}
    aliases = ((cfg.get("models") or {}).get("aliases") or {})
    resolved = aliases.get(name, name)

    model_dir = (os.environ.get("LECTURE_WHISPER_MODEL_DIR")
                 or ((cfg.get("paths") or {}).get("whisper_model_dir") or ""))
    if "{whisper_model_dir}" in resolved:
        if not model_dir:
            _die(
                f"ERROR: model alias '{name}' needs a local model directory but none "
                "is set. Either use '--model large-v3' (auto-downloaded from the HF "
                "hub) or set paths.whisper_model_dir in config.yaml / the "
                "LECTURE_WHISPER_MODEL_DIR env var."
            )
        resolved = resolved.replace("{whisper_model_dir}", str(model_dir).rstrip("/\\"))

    looks_like_path = (os.path.isabs(resolved) or "/" in resolved or "\\" in resolved)
    if looks_like_path and not os.path.exists(resolved):
        _die(
            f"ERROR: model '{name}' resolves to '{resolved}', which does not exist. "
            "Either use '--model large-v3' (auto-downloaded from the HF hub) or point "
            "paths.whisper_model_dir / LECTURE_WHISPER_MODEL_DIR at the directory "
            "holding your converted models."
        )
    return resolved


# ----------------------------------------------------------------------
# Cooperative GPU pause flag
# ----------------------------------------------------------------------

def resolve_pause_flag(cfg: dict | None = None) -> str | None:
    """Path of the cooperative GPU pause flag, or None when the feature is off.

    Resolution order: env ``LECTURE_PAUSE_FLAG`` -> ``paths.pause_flag`` in
    config.yaml -> None. An empty/blank value at any level means "disabled",
    which is the right default on a machine that has no batch runner.
    """
    env_val = (os.environ.get("LECTURE_PAUSE_FLAG") or "").strip()
    if env_val:
        return env_val
    cfg_val = str(((cfg or {}).get("paths") or {}).get("pause_flag") or "").strip()
    return cfg_val or None


def wait_if_paused(flag_path: str | None,
                   log_fn: Callable[[str], None] | None = None,
                   max_wait_s: int = 3600,
                   poll_s: int = 10) -> None:
    """Block while ``flag_path`` exists, up to ``max_wait_s``, then continue.

    Used so two CUDA jobs never collide: whoever holds ``gpu_lease pause``
    creates the flag. The wait is BOUNDED — a stale flag left by a crashed job
    must not wedge the pipeline forever — and this never deletes the flag, since
    it does not own it. Callers set GPU_LEASE_BYPASS=1 to skip waiting on
    themselves.
    """
    if not flag_path or os.environ.get("GPU_LEASE_BYPASS"):
        return

    def _say(msg: str) -> None:
        if log_fn:
            try:
                log_fn(msg)
                return
            except Exception:
                pass
        print(msg, file=sys.stderr)

    waited = 0
    announced = False
    while os.path.exists(flag_path):
        if waited >= max_wait_s:
            _say(f"[pause] flag still present after {max_wait_s}s — treating "
                 f"{flag_path} as stale and continuing (flag NOT removed)")
            return
        if not announced:
            _say(f"[pause] waiting on GPU pause flag {flag_path}")
            announced = True
        elif waited % 60 == 0:
            _say(f"[pause] still waiting on {flag_path} ({waited}s)")
        time.sleep(poll_s)
        waited += poll_s


# ----------------------------------------------------------------------
# OCR (RapidOCR)
# ----------------------------------------------------------------------

_RAPIDOCR_ENGINE: Any = None
_RAPIDOCR_TRIED = False


def rapidocr_engine() -> Any | None:
    """Lazy singleton RapidOCR engine; None (with a one-time warning) if absent."""
    global _RAPIDOCR_ENGINE, _RAPIDOCR_TRIED
    if _RAPIDOCR_TRIED:
        return _RAPIDOCR_ENGINE
    _RAPIDOCR_TRIED = True
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
        _RAPIDOCR_ENGINE = RapidOCR()
    except Exception as e:  # ImportError, or onnxruntime provider failures
        print(f"WARNING: rapidocr_onnxruntime unavailable ({e}); OCR text will "
              "be empty for every image", file=sys.stderr)
        _RAPIDOCR_ENGINE = None
    return _RAPIDOCR_ENGINE


def _normalize_ocr_result(raw: Any) -> list[tuple]:
    """Normalize either RapidOCR return shape to ``[(bbox, text, conf), ...]``.

    v1 (installed here) returns ``(list_of_[bbox, text, conf], elapse_list)``.
    Newer releases return a single result object exposing ``.boxes/.txts/
    .scores``. Probed with hasattr rather than a version check so an upgrade
    does not silently produce empty text.
    """
    if raw is None:
        return []

    # New object API: .boxes / .txts / .scores
    if hasattr(raw, "txts") and not isinstance(raw, (list, tuple)):
        txts = list(getattr(raw, "txts", None) or [])
        boxes = list(getattr(raw, "boxes", None) or [])
        scores = list(getattr(raw, "scores", None) or [])
        out = []
        for i, t in enumerate(txts):
            bbox = boxes[i] if i < len(boxes) else []
            conf = scores[i] if i < len(scores) else 0.0
            out.append((bbox, t, float(conf or 0.0)))
        return out

    items: Any = raw
    # v1 tuple form: (result, elapse)
    if (isinstance(raw, tuple) and len(raw) == 2
            and (raw[0] is None or isinstance(raw[0], list))):
        items = raw[0]
    if not items:
        return []

    out = []
    for it in items:
        if isinstance(it, (list, tuple)) and len(it) >= 3:
            out.append((it[0], it[1], float(it[2] or 0.0)))
        elif isinstance(it, (list, tuple)) and len(it) == 2:
            out.append((it[0], it[1], 0.0))
    return out


def _bbox_top_y(bbox: Sequence) -> float:
    try:
        return float(min(pt[1] for pt in bbox))
    except (TypeError, ValueError, IndexError):
        return 0.0


def _bbox_left_x(bbox: Sequence) -> float:
    try:
        return float(min(pt[0] for pt in bbox))
    except (TypeError, ValueError, IndexError):
        return 0.0


def rapidocr_text(img_path: str, engine: Any = None) -> tuple[str, float, list]:
    """OCR one image -> ``(joined_text, mean_confidence, raw_items)``.

    Text is joined in reading order (top-to-bottom, then left-to-right), which
    is quick_ocr.py's canonical behavior — downstream title guessing and
    transcript grounding both assume it. ``raw_items`` is the normalized
    ``[(bbox, text, conf), ...]`` list so callers can still compute density or
    pick a title without re-running OCR.
    """
    engine = engine if engine is not None else rapidocr_engine()
    if engine is None:
        return "", 0.0, []
    try:
        items = _normalize_ocr_result(engine(img_path))
    except Exception as e:
        print(f"  WARN: rapidocr failed on {os.path.basename(img_path)}: {e}",
              file=sys.stderr)
        return "", 0.0, []
    if not items:
        return "", 0.0, []

    ordered = sorted(items, key=lambda r: (_bbox_top_y(r[0]), _bbox_left_x(r[0])))
    text = "\n".join(r[1] for r in ordered if r[1] and r[1].strip())
    confs = [r[2] for r in items if len(r) >= 3]
    mean_conf = float(sum(confs) / len(confs)) if confs else 0.0
    return text, mean_conf, items


def laplacian_variance(img_path: str, target_width: int = 512) -> float:
    """3x3 Laplacian variance on grayscale, resized to <= target_width.

    Low variance => few edges => likely decorative / single-tone background.
    Resizing first normalizes the metric across slide resolutions (1080p vs 4K
    change raw variance roughly linearly in pixel count). Pure NumPy so no
    scipy/cv2 dependency.
    """
    import numpy as np
    from PIL import Image
    try:
        with Image.open(img_path) as img:
            img = img.convert("L")
            w, h = img.size
            if w > target_width and w > 0:
                scale = target_width / w
                img = img.resize((target_width, max(1, int(h * scale))))
            arr = np.asarray(img, dtype=np.float32)
    except Exception:
        return 0.0
    if arr.shape[0] < 3 or arr.shape[1] < 3:
        return 0.0
    # 3x3 Laplacian via shifted-array trick (center=-4, edges=+1).
    center = arr[1:-1, 1:-1]
    lap = (arr[1:-1, :-2] + arr[1:-1, 2:]
           + arr[:-2, 1:-1] + arr[2:, 1:-1]
           - 4.0 * center)
    return round(float(lap.var()), 2)


# ----------------------------------------------------------------------
# Slide tier parsing
# ----------------------------------------------------------------------

def parse_tier(val: Any, default: int = 3) -> int:
    """Parse a Stage-F ``tier`` value into an int, robustly.

    Stage F is an LLM step, so the field arrives as 1, "1", "T1", "T1 核心",
    "tier 2" — anything that reads as a tier to a human. ``int(str(v).lstrip("Tt"))``
    handled only the first three and raised ValueError on the rest, which callers
    swallowed into tier 3 = "drop this slide": a labeling wobble silently deleted
    content.

    Absent/None -> ``default`` (3, the existing "untiered" convention).
    Present but unparseable -> 1 with a warning: the slide is KEPT, because the
    reviewable failure (an extra figure in the note) beats the invisible one.
    """
    if val is None:
        return default
    if isinstance(val, bool):
        pass  # bools are ints in Python; treat as unparseable
    elif isinstance(val, (int, float)):
        return int(val)
    else:
        # First 1-9 digit anywhere in the label ("T1 核心" -> 1). A scan rather
        # than a regex so this module's import list stays untouched.
        for ch in str(val):
            if ch.isdigit() and ch != "0":
                return int(ch)
    print(f"WARNING: cannot parse tier {val!r}; treating as tier 1 (keeping the "
          "slide) — check the Stage F output", file=sys.stderr)
    return 1


# ----------------------------------------------------------------------
# Stem matching (media index <-> ASR output dirs)
# ----------------------------------------------------------------------

def match_stem(sub: str, by_stem: dict) -> Any | None:
    """Resolve an ASR subdirectory name to its media-index row.

    Exact key match first; otherwise a substring match that must be UNIQUE.
    The previous ``next(r for k, r in ... if k in sub or sub in k)`` took the
    first arbitrary hit, so ``clip1`` happily matched ``clip10`` and inherited
    the wrong capture time — every wall-clock citation derived from it was then
    off by however far apart the two clips were recorded.

    Returns None (with a warning naming the candidates) when the match is
    ambiguous or absent, so the caller can skip rather than cite a wrong clock.
    """
    if sub in by_stem:
        return by_stem[sub]
    cands = [k for k in by_stem if k in sub or sub in k]
    if len(cands) == 1:
        return by_stem[cands[0]]
    if len(cands) > 1:
        print(f"WARNING: '{sub}' matches {len(cands)} index stems ambiguously "
              f"({', '.join(sorted(cands)[:6])}) — skipping rather than guessing. "
              "Rename the ASR dir to the media stem, or pass a name map.",
              file=sys.stderr)
    return None


# ----------------------------------------------------------------------
# OCR, error-visible variant (appended by WP2, 2026-08-02)
# ----------------------------------------------------------------------

RAPIDOCR_INSTALL_HINT = (
    "install it with: pip install rapidocr_onnxruntime\n"
    "  (Stage B needs it — without OCR text every slide looks decorative to the "
    "Stage D skip gate and the run finishes 'successfully' with an empty note.)"
)


def rapidocr_text_ex(img_path: str, engine: Any = None
                     ) -> tuple[str, float, list, str | None]:
    """Like :func:`rapidocr_text` but reports *why* the text is empty.

    Returns ``(text, mean_conf, raw_items, error)``. ``error`` is None when the
    call succeeded (an empty result then genuinely means "no text on this
    image") and a short ``Type: message`` string when the OCR call raised.
    Callers need the distinction because a broken engine and a blank slide used
    to be indistinguishable downstream — both wrote ``parse_valid: true`` with
    empty text.
    """
    engine = engine if engine is not None else rapidocr_engine()
    if engine is None:
        return "", 0.0, [], "engine_unavailable"
    try:
        items = _normalize_ocr_result(engine(img_path))
    except Exception as e:  # noqa: BLE001 — any engine failure must stay visible
        return "", 0.0, [], f"{type(e).__name__}: {str(e)[:200]}"
    if not items:
        return "", 0.0, [], None

    ordered = sorted(items, key=lambda r: (_bbox_top_y(r[0]), _bbox_left_x(r[0])))
    text = "\n".join(r[1] for r in ordered if r[1] and r[1].strip())
    confs = [r[2] for r in items if len(r) >= 3]
    mean_conf = float(sum(confs) / len(confs)) if confs else 0.0
    return text, mean_conf, items, None
