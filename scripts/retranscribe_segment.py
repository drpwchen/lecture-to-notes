"""Detect token-collapse hallucination and re-transcribe failed segments.

Whisper sometimes falls into a repeating-token loop on dense / hard-to-recognize
audio (esp. heavily accented English with technical jargon and long pauses).
Once the collapse starts, `condition_on_previous_text=True` carries the bad
state forward — entire minutes of transcript become useless.

This tool:
  1. Scans transcript.json for collapse signatures
  2. For each collapse range, cuts that segment from audio with ffmpeg
  3. Re-transcribes with --no-condition + per-slide glossary as initial_prompt
  4. Merges corrected segments back into the master transcript

Detection heuristics for "collapse":
  - >=3 consecutive segments with no word characters (empty / punctuation only)
  - Repeated identical text across >=3 consecutive segments
  - >=10 consecutive ultra-short segments (token-by-token fragmentation)
  - Mean char/sec across a 30s window drops below 30% of the median for the
    same script (CJK vs Latin measured separately — see detect_collapses)

Usage:
    # Auto-detect and re-transcribe all collapses
    python retranscribe_segment.py --dir <out_dir> --audio <path> --auto \
           --language zh

    # Manual: re-transcribe a specific time range
    python retranscribe_segment.py --dir <out_dir> --audio <path> --language en \
           --start 36:00 --end 53:16 --glossary "VE VCO2 OUES Wasserman"

    # Slide-aware: pass slide PDF text per range as glossary
    python retranscribe_segment.py --dir <out_dir> --audio <path> --language zh \
           --slides 35-43  # use pdf_text.json pages 35-43 as glossary

``--language`` is REQUIRED: decoding Mandarin audio as English (the old default)
produces exactly the token-collapse this script exists to repair.

The transcript that was READ is the transcript that gets rewritten (normally
transcript.json; transcript_clean.json only if that is the only file present).
The first run saves ``<transcript>.bak``; later runs save timestamped copies, so
the original decode is never lost.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from _common import (atomic_write_json, load_config, resolve_model,
                     setup_nvidia_path, write_transcript_lines)

try:
    from _log import StageLogger, read_metadata
except Exception as _log_err:  # noqa: BLE001
    print(f"WARNING: _log unavailable ({_log_err}); no progress_retranscribe.jsonl "
          "will be written", file=sys.stderr)
    StageLogger = None  # type: ignore
    read_metadata = None  # type: ignore

DEFAULT_MODEL_ALIAS = "breeze25"


def parse_time(s):
    """Parse '36:00' or '2160' or '36:00.5' to seconds (float).

    Raises ValueError on anything else — returning None here used to surface as
    a TypeError several frames away, inside the arithmetic that computes the cut.
    """
    raw = str(s).strip()
    try:
        if ":" in raw:
            parts = raw.split(":")
            if len(parts) == 2:
                return int(parts[0]) * 60 + float(parts[1])
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            raise ValueError("too many ':' groups")
        return float(raw)
    except ValueError as e:
        raise ValueError(
            f"cannot parse time {s!r} ({e}) — use seconds (2160), MM:SS (36:00) "
            "or H:MM:SS (1:36:00)") from None


def fmt_time(t):
    m = int(t // 60)
    s = t - m * 60
    return f"{m:02d}:{s:05.2f}"


_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def _script_class(text: str) -> str:
    """'cjk' or 'latin' — which yardstick this segment's char rate belongs to.

    Mandarin packs far more meaning per character than English, so a mixed
    CN/EN lecture has a bimodal char/sec distribution. A single global median
    sits between the two modes and marks every Mandarin stretch as "low rate",
    i.e. collapsed — on precisely the code-switched audio this pipeline targets.
    """
    stripped = "".join(text.split())
    if not stripped:
        return "latin"
    return "cjk" if len(_CJK_RE.findall(stripped)) / len(stripped) >= 0.30 else "latin"


_WORD_RE = re.compile(r"[^\W_]", re.UNICODE)


def _is_wordless(text: str) -> bool:
    """True for empty / whitespace / punctuation-only text (``\\w`` covers CJK)."""
    return not _WORD_RE.search(text or "")


def _median(values):
    ordered = sorted(values)
    return ordered[len(ordered) // 2] if ordered else 0.0


def detect_collapses(segments, min_run=3, min_window_sec=30):
    """Find time ranges where transcript has collapsed.

    Returns list of (start_sec, end_sec, reason) tuples.
    """
    collapses = []
    if len(segments) < min_run:
        return collapses

    def _text(seg):
        return (seg.get("text") or "").strip()

    # Signature 1a: a run of >=min_run segments carrying no words at all —
    # empty, whitespace, or pure punctuation ("。。。"), which is what a decoder
    # that has stopped producing text looks like.
    #
    # The old rule entered on len<2 and extended on len<5, so three legitimate
    # short replies ("好" / "OK." / "對") formed a 3-run and triggered the
    # destructive replacement path. Length is the wrong signal: Mandarin
    # backchannels are genuinely one character. Word-content is the right one.
    i = 0
    while i < len(segments):
        text = _text(segments[i])
        if _is_wordless(text):
            j = i
            while j < len(segments) and _is_wordless(_text(segments[j])):
                j += 1
            if j - i >= min_run:
                collapses.append((segments[i]["start"], segments[j - 1]["end"],
                                  f"empty/wordless run x{j-i}"))
            i = j if j > i else i + 1
            continue

        j = i + 1
        while j < len(segments) and _text(segments[j]) == text:
            j += 1
        if j - i >= min_run:
            collapses.append((segments[i]["start"], segments[j - 1]["end"],
                              f"repeated text x{j-i}: {text[:40]!r}"))
            i = j
        else:
            i += 1

    # Signature 2: token-by-token fragmentation — many consecutive ultra-short
    # segments. Whisper collapse often produces ~1s-per-token output.
    i = 0
    while i < len(segments):
        j = i
        while j < len(segments):
            seg = segments[j]
            d = seg["end"] - seg["start"]
            c = len(_text(seg))
            if d < 1.2 and c < 20:
                j += 1
            else:
                break
        if j - i >= 10:
            span = segments[j - 1]["end"] - segments[i]["start"]
            collapses.append((segments[i]["start"], segments[j - 1]["end"],
                              f"token-by-token fragmentation x{j-i} "
                              f"(avg {span/(j-i):.2f}s/seg)"))
            i = j
        else:
            i += 1

    # Signature 3: char/sec drops to <30% of the median for the same script.
    rates_by_class = {"cjk": [], "latin": []}
    for s in segments:
        d = s["end"] - s["start"]
        if d > 0.5:
            t = _text(s)
            if t:
                rates_by_class[_script_class(t)].append(len(t) / d)
    all_rates = rates_by_class["cjk"] + rates_by_class["latin"]
    if all_rates:
        global_median = _median(all_rates)
        # A class needs enough samples to trust its own median; below that,
        # borrow the global one rather than threshold off two segments.
        medians = {k: (_median(v) if len(v) >= 5 else global_median)
                   for k, v in rates_by_class.items()}
        i = 0
        while i < len(segments):
            if segments[i]["end"] - segments[i]["start"] < 0.5:
                i += 1
                continue
            window_start = segments[i]["start"]
            j = i
            while j < len(segments) and segments[j]["end"] - window_start < min_window_sec:
                j += 1
            window_segs = segments[i:j]
            if not window_segs:
                i += 1
                continue
            tot_chars = sum(len(_text(s)) for s in window_segs)
            tot_dur = window_segs[-1]["end"] - window_segs[0]["start"]
            if tot_dur >= min_window_sec * 0.8:
                # Threshold from the script the window is actually mostly in.
                n_cjk = sum(1 for s in window_segs if _script_class(_text(s)) == "cjk")
                cls = "cjk" if n_cjk * 2 >= len(window_segs) else "latin"
                threshold = medians[cls] * 0.30
                rate = tot_chars / tot_dur
                if rate < threshold:
                    collapses.append((window_segs[0]["start"], window_segs[-1]["end"],
                                      f"low char rate {rate:.1f} (<{threshold:.1f}, "
                                      f"{cls} median)"))
                    i = j
                    continue
            i += 1

    # Merge overlapping collapse ranges
    if not collapses:
        return []
    collapses.sort(key=lambda x: x[0])
    merged = [list(collapses[0])]
    for start, end, reason in collapses[1:]:
        if start <= merged[-1][1] + 5:  # within 5s = merge
            merged[-1][1] = max(merged[-1][1], end)
            merged[-1][2] = merged[-1][2] + " | " + reason
        else:
            merged.append([start, end, reason])
    return [tuple(m) for m in merged]


def cut_audio(audio_path, start_sec, duration_sec, out_path):
    """ffmpeg-cut [start, start+duration) to a 16k mono wav.

    Validates the range first: a non-positive duration produced an empty wav,
    which decoded to zero segments and (before B1) deleted the master segments
    for that range.
    """
    if duration_sec is None or duration_sec <= 0:
        raise ValueError(f"refusing to cut a {duration_sec}s range at "
                         f"{start_sec}s — start must be strictly before end")
    if start_sec < 0:
        raise ValueError(f"negative start time {start_sec}s")
    cmd = [
        "ffmpeg", "-y", "-ss", str(start_sec), "-i", audio_path,
        "-t", str(duration_sec),
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        out_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-30:])
        raise RuntimeError(f"ffmpeg cut failed (exit {proc.returncode}) for "
                           f"{fmt_time(start_sec)}+{duration_sec:.1f}s:\n{tail}")


def load_model(model_ref):
    """Load faster-whisper on CUDA, falling back to CPU only when CUDA is absent.

    A wrong model path or a bad compute type is a bug to fix, not a reason to
    spend hours on CPU int8. Only a genuinely unavailable CUDA runtime falls
    back, and it says so loudly (an unnoticed CPU fallback turned a 20-minute
    repair into an overnight one, with a single JSONL line as the only clue).
    """
    from faster_whisper import WhisperModel
    try:
        return WhisperModel(model_ref, device="cuda", compute_type="float16")
    except Exception as gpu_err:  # noqa: BLE001 — ct2 raises bare RuntimeError
        msg = str(gpu_err).lower()
        cuda_missing = any(k in msg for k in (
            "cuda", "cudnn", "cublas", "no gpu", "libcuda", "nvidia", "driver"))
        if not cuda_missing:
            print(f"ERROR: could not load model {model_ref!r}: {gpu_err}",
                  file=sys.stderr)
            raise
        print(f"WARNING: CUDA unavailable ({str(gpu_err)[:200]}) — falling back to "
              "CPU int8. Expect ROUGHLY 10-20x slower decoding; Ctrl-C and fix CUDA "
              "if that is not acceptable.", file=sys.stderr)
        return WhisperModel(model_ref, device="cpu", compute_type="int8")


def transcribe_chunk(audio_path, language, glossary, beam_size=15,
                     repetition_penalty=1.5, no_repeat_ngram_size=3,
                     model_ref=None):
    """Re-transcribe with anti-collapse recipe.

    Recipe (validated 2026-05-16 on a Zoom-recorded lecture whose speaker
    disfluency + echo caused token-repeat hallucination):
      - condition_on_previous_text=False : stop collapse propagation
      - repetition_penalty=1.5            : penalize repeating tokens
      - no_repeat_ngram_size=3            : ban 3-gram repeats outright
      - temperature fallback              : escape low-confidence loops
      - vad_filter + min_silence=300ms    : tighter silence cuts on disfluency
      - beam_size=15                      : wider search for accented/dense speech

    These knobs together recovered ranges where --no-condition alone produced
    identical collapse output (i.e. when the model is locked into the loop, not
    just propagating it forward).
    """
    setup_nvidia_path()
    model = load_model(model_ref or DEFAULT_MODEL_ALIAS)
    try:
        segments, info = model.transcribe(
            audio_path,
            language=language,
            beam_size=beam_size,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 300},
            initial_prompt=glossary,
            condition_on_previous_text=False,
            temperature=[0.0, 0.2, 0.4, 0.6, 0.8],
            repetition_penalty=repetition_penalty,
            no_repeat_ngram_size=no_repeat_ngram_size,
        )
        out = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text}
               for s in segments]
    finally:
        # Best effort: drop our reference so the next chunk's model does not
        # meet this one on an 8GB card. CTranslate2 frees VRAM in the model's
        # destructor, so this is as much control as Python gives us.
        del model
        import gc
        gc.collect()
    return out


def build_glossary_from_pdf(pdf_text_path, slide_range=None, max_chars=600):
    """Pull keywords from pdf_text.json for relevant slide range."""
    if not os.path.isfile(pdf_text_path):
        return ""
    try:
        with open(pdf_text_path, encoding="utf-8") as f:
            pages = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"WARNING: cannot read {pdf_text_path} ({e}); continuing without a "
              "glossary", file=sys.stderr)
        return ""
    if not isinstance(pages, list):
        print(f"WARNING: {pdf_text_path} is not a list of pages; continuing "
              "without a glossary", file=sys.stderr)
        return ""
    # Pages written by other tools may lack 'page' or 'text' — a KeyError here
    # used to abort a repair run over a cosmetic schema difference.
    if slide_range:
        lo, hi = slide_range
        pages = [p for p in pages
                 if isinstance(p, dict) and isinstance(p.get("page"), int)
                 and lo <= p["page"] <= hi]
    text = "\n".join(str(p.get("text") or "") for p in pages
                     if isinstance(p, dict))
    if not text.strip():
        return ""
    # Pull capitalized terms, abbreviations, numeric units
    terms = set()
    for m in re.finditer(r"\b[A-Z][A-Za-z0-9\u2082\u2083]{1,}(?:[-_/][A-Za-z0-9]+)*\b", text):
        terms.add(m.group(0))
    glossary = " ".join(sorted(terms)[:50]) + ". " + re.sub(r"\s+", " ", text)[:max_chars]
    return glossary[:max_chars + 200]


def write_transcript_files(segments, out_dir, basename="transcript"):
    """Write ``<basename>.json`` + ``.txt`` (canonical [MM:SS] writer)."""
    atomic_write_json(os.path.join(out_dir, f"{basename}.json"), segments)
    write_transcript_lines(segments, os.path.join(out_dir, f"{basename}.txt"))


def merge_segments(master, replacements):
    """Replace master segments overlapping each (start, end, new_segs) tuple.

    Contract, learned the hard way:
      - An EMPTY new_segs never deletes anything. A failed re-transcription
        used to wipe every master segment in the range, turning "repair" into
        "delete several minutes of transcript" — on a path the main script
        triggers automatically.
      - Neither `master` nor `replacements` is mutated, so calling this twice
        with the same arguments yields the same result (the old ``_inserted``
        marker was written INTO the caller's dicts, so the second call inserted
        nothing and dropped the range).
      - A master segment is replaced when the MAJORITY of its span lies in the
        range. A segment straddling the boundary is otherwise both kept and
        re-transcribed, duplicating that sentence.
      - Output is sorted by start time.
    """
    out = []
    active = [(rs, re_, list(segs)) for rs, re_, segs in replacements if segs]
    inserted = [False] * len(active)

    for seg in master:
        s, e = seg["start"], seg["end"]
        dur = max(e - s, 1e-6)
        hit = None
        for idx, (rep_start, rep_end, _segs) in enumerate(active):
            overlap = min(e, rep_end) - max(s, rep_start)
            if overlap > 0 and overlap / dur >= 0.5:
                hit = idx
                break
        if hit is None:
            out.append(seg)
            continue
        if not inserted[hit]:
            inserted[hit] = True
            out.extend(dict(r) for r in active[hit][2])

    # A replacement whose range matched no master segment (e.g. the master was
    # a single long segment that only 40% overlapped) still belongs in the
    # output — otherwise the re-transcribed audio is silently discarded.
    for idx, (_rs, _re, segs) in enumerate(active):
        if not inserted[idx]:
            out.extend(dict(r) for r in segs)

    out.sort(key=lambda r: (r.get("start", 0.0), r.get("end", 0.0)))
    return out


def backup_path_for(transcript_path):
    """First run -> ``<file>.bak``; later runs -> ``<file>.bak.YYYYMMDD-HHMMSS``.

    The old code copied over ``.bak`` every run, so a second pass destroyed the
    only pre-repair copy — the exact thing needed when a repair goes wrong.
    """
    plain = transcript_path + ".bak"
    if not os.path.exists(plain):
        return plain
    return f"{plain}.{time.strftime('%Y%m%d-%H%M%S')}"


def resolve_model_ref(explicit, out_dir, cfg):
    """--model > parent run's model in metadata.json > breeze25 alias.

    Inheriting the parent's model matters: patching a large-v3 transcript with
    breeze25 output (or vice versa) splices two different vocabularies and
    romanization habits into one transcript.
    """
    if explicit:
        return resolve_model(explicit, cfg), f"--model {explicit}"
    if read_metadata is not None:
        meta = read_metadata(out_dir) or {}
        parent = (((meta.get("stages") or {}).get("transcribe") or {})
                  .get("whisper_model") or meta.get("whisper_model"))
        if parent:
            return resolve_model(parent, cfg), f"metadata.json ({parent})"
    return resolve_model(DEFAULT_MODEL_ALIAS, cfg), f"default ({DEFAULT_MODEL_ALIAS})"


def main():
    parser = argparse.ArgumentParser(
        description="Re-transcribe collapsed ranges and merge them back in.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", required=True, help="lecture-notes output dir")
    parser.add_argument("--audio", help="audio path (defaults to <dir>/audio.wav)")
    parser.add_argument("--language", required=True,
                        help="REQUIRED decode language: zh / en / bilingual / auto "
                             "(bilingual and auto are mapped for faster-whisper). "
                             "Guessing wrong is what causes collapse in the first "
                             "place — if unknown, ASK before running.")
    parser.add_argument("--model", default=None,
                        help="Model alias or path (default: the model recorded for "
                             "this run in metadata.json, else 'breeze25')")
    parser.add_argument("--start", help="manual start time (MM:SS or seconds)")
    parser.add_argument("--end", help="manual end time")
    parser.add_argument("--glossary", help="manual glossary string")
    parser.add_argument("--slides", help="slide range e.g. '35-43' to pull glossary "
                                         "from pdf_text.json")
    parser.add_argument("--auto", action="store_true", help="auto-detect collapses")
    parser.add_argument("--dry-run", action="store_true",
                        help="report only, don't re-transcribe")
    parser.add_argument("--beam-size", type=int, default=15)
    parser.add_argument("--repetition-penalty", type=float, default=1.5,
                        help="Penalty for repeating tokens (default: 1.5)")
    parser.add_argument("--no-repeat-ngram-size", type=int, default=3,
                        help="Ban n-gram repeats (default: 3)")
    args = parser.parse_args()

    out_dir = args.dir
    if not os.path.isdir(out_dir):
        parser.error(f"--dir {out_dir!r} is not a directory")

    # All usage validation happens BEFORE the logger is built: constructing a
    # StageLogger bootstraps metadata.json and a logs/ dir, so a plain typo
    # would otherwise leave a "stage_aborted" record in a lecture dir where
    # nothing was ever attempted.
    slide_range = None
    if args.slides:
        m = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", args.slides)
        if not m:
            parser.error(f"--slides must look like '35-43' (got {args.slides!r})")
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo > hi:
            parser.error(f"--slides start {lo} is after end {hi}")
        slide_range = (lo, hi)

    manual_range = None
    if args.start and args.end:
        try:
            manual_range = (parse_time(args.start), parse_time(args.end))
        except ValueError as e:
            parser.error(str(e))
        if manual_range[1] <= manual_range[0]:
            parser.error(f"--end ({args.end}) must be after --start ({args.start})")
    elif args.start or args.end:
        parser.error("--start and --end must be given together")
    elif not args.auto:
        parser.error("use --auto, or --start/--end together")

    # faster-whisper takes ISO codes; 'bilingual' and 'auto' are this pipeline's
    # own vocabulary. bilingual pins zh (the Chinese model handles inline English
    # better than the reverse); auto means "let the model detect" = None.
    lang_arg = args.language
    if lang_arg == "bilingual":
        decode_language = "zh"
    elif lang_arg in ("auto", ""):
        decode_language = None
    else:
        decode_language = lang_arg

    # transcript.json is authoritative since 2026-07-26 (transcript_clean.json
    # came from the retired auto-cleanup pass; kept only as legacy fallback).
    # Whichever file we READ is the one we rewrite — validating one file and
    # overwriting a different one was how a clean transcript got clobbered.
    transcript_path = os.path.join(out_dir, "transcript.json")
    if not os.path.isfile(transcript_path):
        transcript_path = os.path.join(out_dir, "transcript_clean.json")
    if not os.path.isfile(transcript_path):
        print(f"ERROR: no transcript.json or transcript_clean.json in {out_dir}",
              file=sys.stderr)
        sys.exit(1)
    basename = os.path.splitext(os.path.basename(transcript_path))[0]

    try:
        with open(transcript_path, encoding="utf-8") as f:
            master = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"ERROR: cannot read {transcript_path}: {e}", file=sys.stderr)
        sys.exit(1)
    if isinstance(master, dict) and isinstance(master.get("segments"), list):
        master = master["segments"]
    if not isinstance(master, list):
        print(f"ERROR: {transcript_path} is not a segment list", file=sys.stderr)
        sys.exit(1)

    cfg = {}
    try:
        cfg = load_config()
    except Exception as e:  # noqa: BLE001 — missing pyyaml must not block a repair
        print(f"WARNING: config not loaded ({e}); using built-in defaults",
              file=sys.stderr)

    if StageLogger is None:
        class _Null:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def __getattr__(self, _): return lambda *a, **k: None
            def close(self): return None
        log = _Null()
    else:
        log = StageLogger("retranscribe", out_dir,
                          extra={"lang": lang_arg, "source": basename})
    log.start(mode="auto" if args.auto else "manual", dry_run=args.dry_run,
              n_master_segments=len(master))

    pdf_text_path = os.path.join(out_dir, "pdf_text.json")

    # Determine audio source
    audio_path = args.audio
    if not audio_path or not os.path.isfile(audio_path):
        cand = os.path.join(out_dir, "audio.wav")
        if os.path.isfile(cand):
            audio_path = cand
        else:
            print("ERROR: --audio not given and no audio.wav in dir. The main "
                  "transcribe run deletes audio.wav unless --keep-audio was passed; "
                  "re-extract it from the source media to repair this transcript.",
                  file=sys.stderr)
            log.stage_done(success=False, error="no audio source")
            log.close()
            sys.exit(1)

    # Glossary is identical for every range in --auto mode (there is no
    # range->slide mapping yet), so build it ONCE instead of re-parsing
    # pdf_text.json per range.
    if args.glossary is not None:
        shared_glossary = args.glossary
    else:
        shared_glossary = build_glossary_from_pdf(pdf_text_path, slide_range) or ""

    # Build target ranges
    targets = []  # list of (start, end, glossary)
    if manual_range:
        targets.append((manual_range[0], manual_range[1], shared_glossary))
    elif args.auto:
        collapses = detect_collapses(master)
        print(f"Detected {len(collapses)} collapse range(s):")
        for s, e, reason in collapses:
            print(f"  [{fmt_time(s)} - {fmt_time(e)}] {reason}")
        log.emit("collapses_detected", status="running", n=len(collapses))
        if args.dry_run:
            log.stage_done(success=True, n_collapses=len(collapses), dry_run=True)
            log.close()
            return
        for s, e, _ in collapses:
            targets.append((s, e, shared_glossary))
    else:
        parser.error("use --auto, or --start/--end together")

    if args.dry_run:
        for s, e, g in targets:
            print(f"  range {fmt_time(s)}-{fmt_time(e)}, glossary {len(g)} chars")
        log.stage_done(success=True, n_targets=len(targets), dry_run=True)
        log.close()
        return

    if not targets:
        print("Nothing to re-transcribe.")
        log.stage_done(success=True, n_targets=0)
        log.close()
        return

    model_ref, model_source = resolve_model_ref(args.model, out_dir, cfg)
    print(f"Model: {model_ref}  (from {model_source})")
    log.emit("model_resolved", status="running", model=model_ref,
             source=model_source)

    # Re-transcribe each range
    replacements = []
    failed = []  # (start, end, why) — ranges left untouched in the master
    with tempfile.TemporaryDirectory() as tmp:
        for idx, (s, e, glossary) in enumerate(targets):
            chunk_audio = os.path.join(tmp, f"chunk_{idx:02d}.wav")
            duration = e - s
            print(f"\n[{idx+1}/{len(targets)}] Cutting {fmt_time(s)}-{fmt_time(e)} "
                  f"({duration:.1f}s)")
            try:
                cut_audio(audio_path, s, duration, chunk_audio)
            except (ValueError, RuntimeError, OSError) as err:
                print(f"  SKIP: {err}", file=sys.stderr)
                log.item_error(str(err)[:400], range_start=s, range_end=e)
                failed.append((s, e, f"cut failed: {err}"))
                continue
            print(f"  Re-transcribing with --no-condition + glossary "
                  f"({len(glossary)} chars)")
            t0 = time.time()
            try:
                new_segs = transcribe_chunk(
                    chunk_audio, decode_language, glossary, args.beam_size,
                    args.repetition_penalty, args.no_repeat_ngram_size,
                    model_ref=model_ref)
            except Exception as err:  # noqa: BLE001
                print(f"  SKIP: re-transcription failed: {err}", file=sys.stderr)
                log.item_error(str(err)[:400], range_start=s, range_end=e)
                failed.append((s, e, f"decode failed: {err}"))
                continue
            for seg in new_segs:
                seg["start"] = round(seg["start"] + s, 2)
                seg["end"] = round(seg["end"] + s, 2)
            print(f"  Got {len(new_segs)} new segments in {time.time()-t0:.1f}s")
            if not new_segs:
                # Empty result = we learned nothing about this range. Keeping the
                # (bad) original is strictly better than deleting it.
                print("  WARNING: 0 segments returned — KEEPING the original "
                      "transcript for this range (nothing was deleted). Check the "
                      "audio and the --language choice.", file=sys.stderr)
                log.item_error("empty replacement", range_start=s, range_end=e)
                failed.append((s, e, "re-transcription returned 0 segments"))
                continue
            log.item_done(range_start=s, range_end=e, n_new=len(new_segs),
                          elapsed_s=round(time.time() - t0, 1))
            replacements.append((s, e, new_segs))

    if not replacements:
        print("\nNo range produced usable output; transcript left unchanged.",
              file=sys.stderr)
        for s, e, why in failed:
            print(f"  [{fmt_time(s)}-{fmt_time(e)}] {why}", file=sys.stderr)
        log.stage_done(success=False, n_failed=len(failed),
                       error="no usable replacements")
        log.close()
        return 1

    # Backup + merge
    bak = backup_path_for(transcript_path)
    shutil.copy(transcript_path, bak)
    print(f"\nBacked up to {bak}")
    merged = merge_segments(master, replacements)
    write_transcript_files(merged, out_dir, basename)
    print(f"Wrote merged transcript: {len(master)} -> {len(merged)} segments "
          f"({basename}.json / .txt)")
    if failed:
        print(f"{len(failed)} range(s) NOT repaired (original text kept):")
        for s, e, why in failed:
            print(f"  [{fmt_time(s)}-{fmt_time(e)}] {why}")
    print("Re-run flag_asr_suspects.py to refresh asr_suspects.txt for the new "
          "segments.")
    log.stage_done(success=True, n_replaced=len(replacements),
                   n_failed=len(failed), n_segments_before=len(master),
                   n_segments_after=len(merged), backup=bak)
    log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
