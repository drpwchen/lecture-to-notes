"""Stage E: transcript ↔ slide semantic grounding (0 LLM tokens).

Replaces naive "align by timestamp proximity". For each canonical slide,
computes positive signals (reference_density, emphasis_score, time_spent),
negative signals (skip_score, confusion_score), retrieval keywords, and
clinical_domain mapping by walking the transcript with a ±20s window.

Keyword classification (solves abbreviation false-positive problem):
  - abbreviation (≤4 chars ALL_CAPS or in whitelist) → exact word-boundary
  - drug (-mab/-inib/-statin/...)                     → fuzzy ≥90
  - medical_term (-itis/-oma/-pathy/...)             → fuzzy ≥85
  - general (≥5 chars)                                → fuzzy ≥88

Usage:
    python ground_slides.py <out_dir> [--config <path>] [--window-seconds 20]

Inputs:
    <out_dir>/slides_vlm.json
    <out_dir>/transcript.json  (authoritative; transcript_clean.json = legacy fallback)
    <skill_root>/config.yaml         (auto-detected sibling of scripts/)

Output:
    <out_dir>/slides_grounded.json   (pipeline_stage="grounded")
"""
import argparse
import json
import os
import re
import sys
import time

from _common import atomic_write_json, load_segments


# --------------------------- optional deps ------------------------------------

def try_import_rapidfuzz():
    try:
        from rapidfuzz import fuzz
        return fuzz
    except ImportError:
        print("WARNING: rapidfuzz not installed; fuzzy keyword match disabled "
              "(falls back to substring match).", file=sys.stderr)
        return None


def try_import_yaml():
    try:
        import yaml
        return yaml
    except ImportError:
        print("WARNING: pyyaml not installed; using built-in default config.",
              file=sys.stderr)
        return None


# --------------------------- config defaults ----------------------------------

DEFAULT_CONFIG = {
    "keyword_grounding": {
        "abbreviation_whitelist": [
            "ALS", "ADHD", "CPET", "RTC", "ROM", "DTR", "ABI", "CMAP",
            "NCS", "EMG", "MRI", "CT", "US", "PT", "OT", "ST",
            "ESWT", "TENS", "NMES", "FES", "FIM", "MMT", "COPD", "ILD",
            "HFrEF", "HFpEF", "VO2", "VE", "AT", "TNM", "DVT", "PE",
            "SCI", "TBI", "CP", "DMD",
        ],
        "drug_suffixes": [
            "mab", "inib", "statin", "pril", "prazole", "olol",
            "sartan", "pine", "gliflozin", "parin",
        ],
        "medical_term_suffixes": [
            "itis", "oma", "pathy", "osis", "ectomy", "plasty",
            "algia", "emia", "rhea", "centesis",
        ],
        "clinical_domain_map": {},
        "emphasis_cues_zh": ["重要", "最重要", "核心", "記住", "注意",
                              "千萬", "關鍵", "重點", "一定要"],
        "emphasis_cues_en": ["key point", "important", "remember", "critical",
                              "essential", "must", "absolutely", "takeaway",
                              "highlight", "decision making"],
        "confusion_cues": ["跳過", "skip", "let's move on", "不重要",
                            "next slide", "等一下回來", "back to",
                            "我們先看", "abandon", "忽略"],
    }
}


def deep_merge(base, override):
    """Recursive dict merge; override wins on leaves.

    An explicit ``null`` in YAML means "leave the default alone", not "set this
    key to None" — a commented-out-by-nulling config key used to replace a list
    with None and crash the consumer several stages later.
    """
    out = dict(base)
    for k, v in (override or {}).items():
        if v is None:
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(config_path):
    yaml = try_import_yaml()
    cfg = DEFAULT_CONFIG
    if yaml is None or not config_path or not os.path.isfile(config_path):
        return cfg
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            user_cfg = yaml.safe_load(f) or {}
        return deep_merge(cfg, user_cfg)
    except Exception as e:
        print(f"WARNING: failed to load config {config_path}: {e}; using defaults",
              file=sys.stderr)
        return cfg


# --------------------------- keyword extraction -------------------------------

TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-]{1,}|[一-鿿]{2,}")


def classify_keyword(token, cfg):
    g = cfg["keyword_grounding"]
    if not token:
        return None
    t = token.strip()
    # Abbreviation: ≤4 chars and uppercase, OR in whitelist
    if (len(t) <= 4 and t.isupper() and t.isalpha()) or t.upper() in {
            x.upper() for x in g.get("abbreviation_whitelist", [])}:
        return "abbreviation"
    tl = t.lower()
    for suf in g.get("drug_suffixes", []):
        if tl.endswith(suf.lower()) and len(tl) > len(suf) + 2:
            return "drug"
    for suf in g.get("medical_term_suffixes", []):
        if tl.endswith(suf.lower()) and len(tl) > len(suf) + 2:
            return "medical_term"
    if len(t) >= 5:
        return "general"
    return None


def extract_keywords(slide, cfg):
    """Return list of (token, type) tuples deduplicated, lowercased except abbr."""
    ocr = slide.get("ocr") or {}
    vlm = slide.get("vlm_signals") or {}
    texts = [
        ocr.get("clean_text") or "",   # Stage B2 (Surya) high-quality OCR, preferred
        ocr.get("vlm_text") or "",
        ocr.get("quick_text") or "",
        ocr.get("quick_title_guess") or "",
    ]
    for lab in (vlm.get("visible_labels") or []):
        if isinstance(lab, str):
            texts.append(lab)
    blob = "\n".join(texts)

    seen = {}
    for m in TOKEN_RE.findall(blob):
        kt = classify_keyword(m, cfg)
        if kt is None:
            continue
        key = m if kt == "abbreviation" else m.lower()
        if key in seen:
            continue
        seen[key] = kt
    return list(seen.items())


# --------------------------- transcript matching ------------------------------

def window_segments(segments, start_sec, end_sec):
    """Return segments overlapping [start_sec, end_sec]; list of dicts + idx."""
    out = []
    for i, seg in enumerate(segments):
        s, e = seg.get("start", 0), seg.get("end", 0)
        if e < start_sec or s > end_sec:
            continue
        out.append((i, seg))
    return out


# Keywords at or below this length are matched EXACTLY, never fuzzily.
#
# partial_ratio scores the best-aligned substring of the haystack, and for a
# short needle a single lucky alignment anywhere in a ±20s window is enough.
# Measured 2026-08-02 on synthetic windows: the effect is NOT the blanket
# "everything scores >=88" the audit assumed (rapidfuzz normalizes by needle
# length, so an absent 4-6 char keyword scores ~50-75), but two real false
# positives remain and both are fixed here — a keyword found INSIDE a longer
# unrelated word ('motion' in 'emotional' scored 100), and an alignment that
# spans the join between two unrelated segments. One edit in a 6-char needle
# also lands at 83, uncomfortably close to the 88 line.
_EXACT_MATCH_MAX_LEN = 7

_LATIN_KW_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-]*$")


def _exact_hit(text_lower, kw_lower):
    """Substring match, anchored to a word START for latin keywords.

    CJK has no word boundaries, so plain substring is right there. For latin the
    match must begin a word — 'motion' should not be found inside 'emotional' —
    but the END is deliberately left free so ordinary inflection still counts
    ('tendon' matches 'tendons', 'atrophy' matches 'atrophied').
    """
    if _LATIN_KW_RE.match(kw_lower):
        return re.search(
            rf"(?<![a-z0-9]){re.escape(kw_lower)}", text_lower
        ) is not None
    return kw_lower in text_lower


def keyword_hit(seg_texts_lower, kw, kw_type, fuzz_lib):
    """Returns True iff this kw hits any ONE transcript segment per its type's rule.

    Two changes from the original whole-window scoring, both aimed at the same
    failure (every keyword hitting, so reference_density ~1.0 everywhere):

    1. Scoring is per SEGMENT (roughly a sentence) and OR-ed, not against the
       whole concatenated ±20s window. A fuzzy score against a 400-char blob is
       a "does this appear anywhere" test, not a similarity test.
    2. Keywords shorter than _EXACT_MATCH_MAX_LEN+1 chars must match exactly.

    Abbreviations are handled separately via abbreviation_hit (original-case
    word-boundary). This function only handles the fuzzy types.
    """
    kwl = kw.lower()
    if len(kwl) <= _EXACT_MATCH_MAX_LEN:
        return any(_exact_hit(t, kwl) for t in seg_texts_lower)
    if fuzz_lib is None:
        return any(kwl in t for t in seg_texts_lower)
    threshold = {"drug": 90, "medical_term": 85, "general": 88}[kw_type]
    return any(fuzz_lib.partial_ratio(kwl, t) >= threshold for t in seg_texts_lower)


def abbreviation_hit(orig_text, kw):
    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(kw)}(?![A-Za-z0-9])")
    return pattern.search(orig_text) is not None


def cue_density(seg_texts, cues):
    """Fraction of segments containing any cue (case-insensitive substring)."""
    if not seg_texts:
        return 0.0
    cues_lc = [c.lower() for c in cues]
    hits = 0
    for t in seg_texts:
        tl = (t or "").lower()
        if any(c in tl for c in cues_lc):
            hits += 1
    return hits / len(seg_texts)


# --------------------------- per-slide grounding ------------------------------

def compute_signals(slide, segments, cfg, fuzz_lib, window_seconds):
    g = cfg["keyword_grounding"]

    ts_start = slide.get("timestamp_start", 0) or 0
    ts_end = slide.get("timestamp_end", ts_start) or ts_start
    w_start = max(0, ts_start - window_seconds)
    w_end = ts_end + window_seconds

    win = window_segments(segments, w_start, w_end)
    seg_texts = [s.get("text", "") or "" for _, s in win]
    seg_ids = [i for i, _ in win]
    combined = " ".join(seg_texts)
    seg_texts_lower = [t.lower() for t in seg_texts]
    word_count = len(re.findall(r"\S+", combined))

    keywords = extract_keywords(slide, cfg)
    if not keywords:
        speaker_reference_density = 0.0
        hit_keywords = []
    else:
        hits = []
        for kw, kt in keywords:
            if kt == "abbreviation":
                if abbreviation_hit(combined, kw):
                    hits.append(kw)
            else:
                if keyword_hit(seg_texts_lower, kw, kt, fuzz_lib):
                    hits.append(kw)
        speaker_reference_density = len(hits) / len(keywords)
        hit_keywords = hits

    emphasis_cues = g.get("emphasis_cues_zh", []) + g.get("emphasis_cues_en", [])
    speaker_emphasis_score = cue_density(seg_texts, emphasis_cues)
    speaker_confusion_score = cue_density(seg_texts, g.get("confusion_cues", []))

    time_spent = max(0, ts_end - ts_start)
    if time_spent < 10 and word_count < 30:
        speaker_skip_score = 0.9
    else:
        speaker_skip_score = max(0.0, min(1.0, 1.0 - speaker_reference_density))

    # clinical_domain mapping
    domain_map = g.get("clinical_domain_map", {}) or {}
    domains = []
    full_blob = (
        " ".join([(slide.get("ocr") or {}).get(k, "") or ""
                  for k in ("clean_text", "vlm_text", "quick_text", "quick_title_guess")])
        + " " + combined
    ).lower()
    for domain, hints in domain_map.items():
        # A single-hint domain is naturally written `msk: shoulder` in YAML,
        # which arrives as a str — iterating it would test each CHARACTER and
        # match nearly everything.
        if isinstance(hints, str):
            hints = [hints]
        elif not isinstance(hints, (list, tuple)):
            print(f"WARNING: clinical_domain_map['{domain}'] is "
                  f"{type(hints).__name__}, expected a list of strings; skipping",
                  file=sys.stderr)
            continue
        for h in hints:
            if not isinstance(h, str) or not h:
                continue
            if h.lower() in full_blob:
                domains.append(domain)
                break

    # Retrieval keywords: top-5 by hit, then any keyword if none hit
    retrieval = list(dict.fromkeys(hit_keywords))[:5]
    if len(retrieval) < 5:
        for kw, _ in keywords:
            if kw not in retrieval:
                retrieval.append(kw)
            if len(retrieval) >= 5:
                break

    return {
        "transcript_signals": {
            "speaker_reference_density": round(speaker_reference_density, 3),
            "speaker_emphasis_score": round(speaker_emphasis_score, 3),
            "speaker_skip_score": round(speaker_skip_score, 3),
            "speaker_confusion_score": round(speaker_confusion_score, 3),
            "time_spent_seconds": time_spent,
            "transcript_segment_ids": seg_ids,
        },
        "retrieval": {
            "retrieval_keywords": retrieval,
            "summary_sentence": None,
            "clinical_domain": list(dict.fromkeys(domains)),
        },
        "grounding_support": round(speaker_reference_density, 3),
    }


# --------------------------- main ---------------------------------------------

def load_transcript(out_dir):
    """Load the lecture transcript, or exit 2 if there isn't one.

    transcript.json is authoritative (2026-07-26): the auto-cleanup pass that
    produced transcript_clean.json was retired for rewriting garbles into
    confident wrong terms — see HARD RULE #5 in SKILL.md. The clean file is
    still accepted as a fallback so pre-2026-07-26 lecture dirs keep working.

    A missing transcript is fatal, not a warning: every transcript_signal would
    be 0, which downstream tiering reads as "the speaker skipped this slide" —
    a confident wrong answer indistinguishable from a real one.
    """
    plain = os.path.join(out_dir, "transcript.json")
    clean = os.path.join(out_dir, "transcript_clean.json")
    path = plain if os.path.isfile(plain) else clean
    if not os.path.isfile(path):
        print(f"ERROR: no transcript found in {out_dir} (looked for "
              "transcript.json, then transcript_clean.json). Grounding without a "
              "transcript would mark every slide as skipped. Run "
              "transcribe_video.py for this lecture first.", file=sys.stderr)
        sys.exit(2)
    return load_segments(path)


def main():
    parser = argparse.ArgumentParser(description="Stage E: transcript grounding")
    parser.add_argument("out_dir", help="Lecture output directory")
    parser.add_argument("--config", default=None,
                        help="Path to config.yaml (default: skill_root/config.yaml)")
    parser.add_argument("--window-seconds", type=int, default=20,
                        help="Transcript window padding around slide timestamp (default 20s)")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite output (default behavior)")
    args = parser.parse_args()

    vlm_path = os.path.join(args.out_dir, "slides_vlm.json")
    out_path = os.path.join(args.out_dir, "slides_grounded.json")

    if not os.path.isfile(vlm_path):
        print(f"ERROR: {vlm_path} not found. Run ocr_slides.py first.", file=sys.stderr)
        sys.exit(2)

    if args.config:
        config_path = args.config
    else:
        # skill_root = parent of scripts/
        config_path = os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "config.yaml"
        ))

    cfg = load_config(config_path)
    fuzz_lib = try_import_rapidfuzz()

    with open(vlm_path, "r", encoding="utf-8") as f:
        slides = json.load(f)
    segments = load_transcript(args.out_dir)

    start = time.time()
    grounded = []
    for s in slides:
        if not s.get("dedup", {}).get("is_canonical"):
            s["pipeline_stage"] = "grounded"
            grounded.append(s)
            continue
        sig = compute_signals(s, segments, cfg, fuzz_lib, args.window_seconds)
        s["transcript_signals"] = sig["transcript_signals"]
        s["retrieval"] = sig["retrieval"]
        s.setdefault("ocr", {})["grounding_support"] = sig["grounding_support"]
        s["pipeline_stage"] = "grounded"
        grounded.append(s)

        ts = sig["transcript_signals"]
        print(f"  Slide {s['slide_id']:3d}: "
              f"ref={ts['speaker_reference_density']:.2f} "
              f"emph={ts['speaker_emphasis_score']:.2f} "
              f"skip={ts['speaker_skip_score']:.2f} "
              f"conf={ts['speaker_confusion_score']:.2f} "
              f"time={ts['time_spent_seconds']}s "
              f"dom={sig['retrieval']['clinical_domain']}")

    atomic_write_json(out_path, grounded)

    elapsed = time.time() - start
    canonical_count = sum(1 for s in grounded if s.get("dedup", {}).get("is_canonical"))
    print(f"\nDone: {canonical_count} canonical slides grounded in {elapsed:.1f}s "
          f"→ {out_path}")


if __name__ == "__main__":
    main()
