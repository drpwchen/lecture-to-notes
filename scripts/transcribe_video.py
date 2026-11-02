"""Extract audio from video / audio file and transcribe with faster-whisper.

Usage:
    python transcribe_video.py <media_path> --lang zh [options]

Outputs (in --output-dir, default: the media file's own directory):
    transcript.json  — [{start, end, text}, ...]
    transcript.txt   — [MM:SS] text (one line per segment; H:MM:SS past an hour)
    logs/progress_transcribe.jsonl — pipeline event log
    runs.jsonl (parent dir) — one-line summary

VRAM is NOT checked here. ``gpu_check.py`` is a separate, manual pre-flight —
run it first (or from your batch runner) if the GPU may be busy.

Speed
-----
``--batched`` (default ON) would use faster-whisper's BatchedInferencePipeline,
but that pipeline REQUIRES ``vad_filter=True`` and VAD has been OFF by default
since 2026-07-12 (it was eating quiet speech). So with default flags this script
runs SEQUENTIAL and batching only engages when you also pass ``--vad``.

When batching does engage it starts at ``--batch-size`` (default 4 — safe on an
RTX 3070 Ti 8GB with beam=10), and on OOM retries once at half that before
falling back to sequential. The batch size used and whether the fallback fired
are recorded in runs.jsonl.

Language (`--lang`, REQUIRED)
-----------------------------
``zh``                  — Chinese model, accepts inline English domain words
``en``                  — English decoding. SAME model (breeze25): on English audio
                          it ties large-v3 (1535 vs 1537 real word-types,
                          precision 98.6% vs 97.8%; asr-benchmark RESULTS.md).
                          Do NOT switch to large-v3 for English.
``bilingual``           — Chinese model with deterministic decode params
                          (``condition_on_previous_text=False``,
                          ``temperature=[0.0, 0.2]``,
                          ``vad min_silence_ms=800``). Tuned for TW conferences
                          where speakers code-switch CN <-> EN mid-sentence.
``auto``     (experimental) — let Whisper detect; logs the detected language +
                              probability into runs.jsonl. Detection is unstable
                              on code-switched audio; not for production.

There is no default: decoding English audio as Chinese (or the reverse) causes
catastrophic token-collapse. The legacy ``--language`` flag still works as an
alias for backward compatibility with older SKILL.md invocations.

Models
------
``--model`` takes an alias from config.yaml (``models.aliases``) or any path /
Hugging Face hub name. Default ``breeze25`` (a local converted model; set
``paths.whisper_model_dir`` or ``LECTURE_WHISPER_MODEL_DIR``). Without a local
model directory, use ``--model large-v3``, which faster-whisper downloads.

Production invocation = the BARE defaults (2026-10-20)
------------------------------------------------------
``--lang <x>`` and nothing else. The defaults ARE the configuration adopted by the
2026-07-12 n=15 sweep (asr-benchmark, ``breeze25 / novad_nocond``): breeze25,
beam 5, VAD off, condition_on_previous_text off. Each "tuning" flag measured worse
on a FIRST pass:
    --beam-size 10        604 vs 649 real terms (loses recall), 1.7x GPU time
    --initial-prompt(-file)  a prompt is a pull, not a dictionary: it invents
                          near-miss words (paravertibone, sacroilar) and did not
                          rescue the terms it spelled out
    --model large-v3      ties on English; breeze25 captures +49% terms on zh/en
                          medical lectures (649 vs 435)
    --condition           propagates drift / repetition loops
These belong ONLY in the anti-collapse retry recipe (reference/decisions.md).
The script prints a WARNING when a first pass deviates.
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import subprocess
import sys
import time

# Written by the decode child after stage_done; read + removed by _run_parent.
HANDOFF_NAME = ".transcribe_handoff.json"
# CTranslate2 objects parked so they are never destructed in-process.
_KEEPALIVE: list = []

from _common import (atomic_write_json, fmt_hms, gpu_lease_ctx, load_config,
                     require_binaries, resolve_model, resolve_pause_flag,
                     setup_nvidia_path, wait_if_paused, write_transcript_lines)

try:
    from _log import StageLogger, append_run_summary, git_hash, lecture_name
except Exception as _log_err:  # noqa: BLE001
    # Losing the logger silently meant losing every JSONL event, metadata.json
    # and runs.jsonl row with no hint why. Degrade, but say it once.
    print(f"WARNING: _log unavailable ({_log_err}); this run will produce no "
          "progress JSONL, no metadata.json and no runs.jsonl entry",
          file=sys.stderr)
    StageLogger = None  # type: ignore
    append_run_summary = None  # type: ignore
    git_hash = None  # type: ignore
    lecture_name = None  # type: ignore


# ----------------------------------------------------------------------
# Audio
# ----------------------------------------------------------------------

def _ffmpeg_extract(media_path: str, audio_path: str) -> None:
    """ffmpeg -> 16k mono PCM, overwriting whatever is at audio_path."""
    proc = subprocess.run([
        "ffmpeg", "-y", "-i", media_path,
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        audio_path,
    ], capture_output=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        # The old code retried the byte-identical command (only Python-side
        # decoding differed) and never showed ffmpeg's own words, so the actual
        # cause — bad codec, unreadable path, no disk — stayed invisible.
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-30:])
        raise subprocess.CalledProcessError(
            proc.returncode, proc.args,
            output=proc.stdout, stderr=f"ffmpeg failed:\n{tail}")


def extract_audio(media_path: str, audio_path: str,
                  expected_duration_s: float | None = None,
                  log=None) -> None:
    """Extract audio, reusing an existing audio.wav only if it looks complete.

    Reuse is what makes a rerun cheap, but an unvalidated reuse silently
    transcribes the WRONG or a TRUNCATED audio track — e.g. an ffmpeg killed
    mid-write, or a second lecture pointed at an output dir that already has an
    audio.wav from the first. Compare against the source duration and re-extract
    on any mismatch beyond max(5s, 5%).
    """
    if os.path.isfile(audio_path) and os.path.getsize(audio_path) > 0:
        existing = probe_duration_seconds(audio_path)
        if expected_duration_s is None or existing is None:
            reason = ("cannot verify existing audio.wav (no duration from ffprobe) "
                      "— re-extracting to be safe")
        else:
            tol = max(5.0, expected_duration_s * 0.05)
            if abs(existing - expected_duration_s) <= tol:
                if log:
                    log.emit("audio_reused", status="success",
                             audio_s=round(existing, 1),
                             media_s=round(expected_duration_s, 1))
                return
            reason = (f"existing audio.wav is {existing:.1f}s but the media is "
                      f"{expected_duration_s:.1f}s (tolerance {tol:.1f}s) — "
                      "truncated or from a different file; re-extracting")
        print(f"[transcribe] {reason}", file=sys.stderr)
        if log:
            log.emit("audio_reextract", status="running", reason=reason)
    _ffmpeg_extract(media_path, audio_path)


def probe_duration_seconds(path: str) -> float | None:
    """ffprobe -> duration in seconds. None on failure."""
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            stderr=subprocess.DEVNULL, text=True, timeout=30,
        ).strip()
        return float(out)
    except (subprocess.SubprocessError, FileNotFoundError, ValueError, OSError):
        return None


# ----------------------------------------------------------------------
# Decode params
# ----------------------------------------------------------------------

DEFAULT_VAD = {
    # 3000ms = a sentence-ish chunk. 4000 over-merges TW Q&A short utterances.
    "min_speech_duration_ms": 3000,
    "min_silence_duration_ms": 800,
    "speech_pad_ms": 400,
}

LANG_CHOICES = ("zh", "en", "bilingual", "auto")


def build_decode_params(args) -> tuple[dict, str | None]:
    """Returns (kwargs, language) for model.transcribe / pipeline.transcribe.

    language=None means "let model detect" (only --lang auto).
    """
    # ==vad_filter is OFF by default as of 2026-07-12.== An n=15 sweep (13 variants,
    # harness in transcribe/eval/) found VAD was silently EATING quiet speech:
    # turning it off captured +26 distinct real medical terms with FEWER invented
    # ones. Verified it does not reintroduce Whisper's silence hallucination
    # ("謝謝觀看"/subtitle spam) or repetition loops — 0 of each across 2,236 lines —
    # and segment granularity stays fine (18.8 segs/min, median gap 3s), which is
    # what slide alignment depends on. Pass --vad to restore the old behaviour.

    # Bilingual: deterministic, anti-collapse settings. Pin language=zh because
    # Whisper's Chinese model handles inline English domain words better than
    # the reverse (the English model loops on Chinese audio).
    if args.lang == "bilingual":
        return ({
            "beam_size": args.beam_size,
            "best_of": 5,
            "condition_on_previous_text": False,
            "temperature": (0.0, 0.2),
            "vad_filter": args.vad,
            "vad_parameters": DEFAULT_VAD,
        }, "zh")

    if args.lang == "auto":
        # Experimental; rely on Whisper's language detection.
        return ({
            "beam_size": args.beam_size,
            "vad_filter": args.vad,
            "vad_parameters": DEFAULT_VAD,
            "condition_on_previous_text": not args.no_condition,
        }, None)

    # zh / en: respect existing knobs (--no-condition, --beam-size).
    return ({
        "beam_size": args.beam_size,
        "vad_filter": args.vad,
        "vad_parameters": DEFAULT_VAD,
        "condition_on_previous_text": not args.no_condition,
    }, args.lang)


# ----------------------------------------------------------------------
# OOM-aware batched transcription
# ----------------------------------------------------------------------

OOM_PATTERNS = re.compile(
    r"out of memory|CUDA failed|cudaMalloc|cudaErrorMemoryAllocation"
    r"|insufficient memory|CUBLAS_STATUS_ALLOC_FAILED",
    re.IGNORECASE,
)


def _is_oom(exc: BaseException) -> bool:
    return bool(OOM_PATTERNS.search(str(exc)))


def _write_partial(path: str, results: list[dict]) -> None:
    """Checkpoint results so a process abort cannot destroy a finished decode.

    NOT dead code, despite nothing in this repo reading it: the resume driver
    (chunked_local_transcribe.py, outside the skill dir) promotes this file when
    a run dies, cuts the audio from where decoding stopped, and continues. It is
    the whole reason a 2-hour recording survives a CTranslate2 abort. Deleting
    the writes silently turns those aborts back into total losses — check that
    driver before touching this.

    Atomic (temp + replace) so a kill mid-write leaves the previous checkpoint
    intact rather than a truncated file.
    """
    try:
        atomic_write_json(path, results, indent=None)
    except OSError:
        pass  # checkpointing must never break the run


class OomRetry(Exception):
    """This attempt OOMed and a smaller one remains: the decode child must exit
    (code OOM_RETRY_RC) so the parent re-launches it with ``--attempt next``."""

    def __init__(self, next_attempt: int, err: BaseException):
        super().__init__(str(err))
        self.next_attempt = next_attempt


OOM_RETRY_RC = 75


def attempt_plan(batch_sizes: list[int], batched_available: bool) -> list[int]:
    """Batch sizes to try in order; the final 1 = sequential model.transcribe."""
    return ([b for b in batch_sizes] if batched_available else []) + [1]


def transcribe_with_fallback(audio_path: str, model_factory, decode_params: dict,
                             language: str | None, batch_sizes: list[int],
                             initial_prompt: str | None, log, attempt: int = 0):
    """Run ONE attempt of the plan (batched sizes…, then sequential).

    Returns (segments_iter, info, used_batch_size, fallback_triggered, model).

    ==No model is ever destroyed in this process (2026-10-05)==: the
    old in-process `del model; gc.collect()` between OOM retries ran
    CTranslate2's destructor — the 0xC0000409 abort (decisions.md
    §ctranslate2-abort). A failed attempt's model is parked in _KEEPALIVE and
    the error raised: OOM with a smaller attempt left → OomRetry, which makes
    the decode child os._exit(OOM_RETRY_RC); the parent (_run_parent) then
    starts a fresh child with ``--attempt next`` — VRAM is freed by the OS at
    process exit, never by the destructor.
    """
    try:
        from faster_whisper import BatchedInferencePipeline
        batched_available = True
    except ImportError:
        batched_available = False
        if batch_sizes:
            log.retry(reason="BatchedInferencePipeline missing - using sequential",
                      batch_size=1)

    plan = attempt_plan(batch_sizes, batched_available)
    attempt = max(0, min(attempt, len(plan) - 1))
    bs = plan[attempt]
    is_last = attempt == len(plan) - 1
    fallback_triggered = attempt > 0
    if fallback_triggered:
        log.retry(reason=(f"retrying at batch_size={bs}" if bs > 1
                          else "falling back to sequential mode"),
                  batch_size=bs, attempt=attempt)

    model = None
    try:
        model = model_factory()
        if bs > 1:
            pipeline = BatchedInferencePipeline(model=model)
            _KEEPALIVE.append(pipeline)
            segments, info = pipeline.transcribe(
                audio_path,
                language=language,
                initial_prompt=initial_prompt,
                batch_size=bs,
                **decode_params,
            )
            # The pipeline returns lazily; trigger first item to surface
            # an immediate OOM before we hand the iterator back.
            segments = iter(segments)
            first = next(segments, None)

            def _chained(first_seg, rest):
                if first_seg is not None:
                    yield first_seg
                yield from rest

            return _chained(first, segments), info, bs, fallback_triggered, model
        segments, info = model.transcribe(
            audio_path,
            language=language,
            initial_prompt=initial_prompt,
            **decode_params,
        )
        return segments, info, 1, fallback_triggered, model
    except Exception as e:  # noqa: BLE001 — CT2 raises bare RuntimeError on OOM
        _KEEPALIVE.append(model)   # never destroy in-process; OS frees at exit
        if _is_oom(e) and not is_last:
            log.retry(reason=f"OOM at batch_size={bs} → new decode process",
                      batch_size=bs, error=str(e)[:200])
            raise OomRetry(attempt + 1, e) from e
        raise


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def _warn_off_default(args) -> None:
    """Loudly flag a first pass that deviates from the adopted config.

    2026-10-20: a batch copied ``--beam-size 10 --initial-prompt`` from an older
    run script and switched English to large-v3 — all three measured worse in the
    n=15 sweep. Warn (not fail): the anti-collapse retry legitimately uses them.
    """
    off = []
    if args.beam_size != 5:
        off.append(f"--beam-size {args.beam_size} (adopted: 5; beam 10 loses recall)")
    if args.initial_prompt or args.initial_prompt_file:
        off.append("--initial-prompt (invents near-miss words on a first pass)")
    if args.model != "breeze25":
        off.append(f"--model {args.model} (adopted: breeze25; ties large-v3 on English)")
    if not args.no_condition:
        off.append("--condition (propagates drift / repetition)")
    if off:
        print("WARNING: off the adopted config (asr-benchmark 2026-07-12; see "
              "docstring 'Production invocation'):" +
              "".join("\n  - " + o for o in off) +
              "\n  OK only for the anti-collapse retry or a CPU/no-local-model machine.",
              file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Transcribe video/audio with faster-whisper (batched, OOM-aware)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("media_path", help="Path to video or audio file")
    # --lang is the new primary flag; keep --language as backward-compat alias.
    parser.add_argument("--lang", choices=list(LANG_CHOICES),
                        default=None,
                        help="Language mode (REQUIRED). bilingual = CN+EN code-switching, deterministic. auto = experimental detection.")
    parser.add_argument("--language", "-l", default=None,
                        help="DEPRECATED alias for --lang (same values)")
    parser.add_argument("--output-dir", "-o", default=None,
                        help="Output directory (default: same as media)")
    parser.add_argument("--initial-prompt", default=None,
                        help="Domain glossary to bias decoding (<= ~224 tokens)")
    parser.add_argument("--initial-prompt-file", default=None,
                        help="Read initial prompt from a text file")
    parser.add_argument("--beam-size", type=int, default=5,
                        help="Beam size (default 5 = adopted config; 10 loses recall on a "
                             "first pass — anti-collapse retry only)")
    parser.add_argument("--batched", dest="batched", action="store_true",
                        default=True,
                        help="Use BatchedInferencePipeline (default ON, but only "
                             "effective together with --vad)")
    parser.add_argument("--no-batched", dest="batched", action="store_false",
                        help="Disable batched mode (use sequential model.transcribe)")
    parser.add_argument("--batch-size", type=int, default=4,
                        help="Initial batch size for batched mode (default 4 — safe on RTX 3070 Ti 8GB with beam=10). On OOM, retries once at half this, then sequential.")
    parser.add_argument("--condition", dest="no_condition", action="store_false",
                        help="Re-enable condition_on_previous_text (old default; the "
                             "2026-07-12 sweep found conditioning propagates 簡體 drift "
                             "and repetition once started, for no capture gain)")
    parser.add_argument("--no-condition", dest="no_condition", action="store_true",
                        default=True,
                        help="Disable condition_on_previous_text (DEFAULT; flag kept for "
                             "backward compatibility)")
    parser.add_argument("--vad", dest="vad", action="store_true", default=False,
                        help="Re-enable VAD filtering. OFF by default since 2026-07-12: "
                             "the n=15 sweep showed VAD silently eats quiet speech "
                             "(-26 real medical terms) without preventing any hallucination. "
                             "Only pass this if you have evidence VAD helps on your audio.")
    parser.add_argument("--keep-audio", action="store_true",
                        help="Keep audio.wav after transcription (default: delete)")
    parser.add_argument("--word-timestamps", action="store_true",
                        help="Emit per-word timings into transcript.json (enables "
                             "sentence-level re-segmentation via split_segments.py; "
                             "conference speech yields 20-30s segments that span "
                             "multiple slides, blurring text<->slide alignment)")
    parser.add_argument("--model", default="breeze25",
                        help="Whisper model alias (config.yaml models.aliases), path, "
                             "or HF hub name. Default breeze25 — won the 2026-07-12 "
                             "n=15 sweep: +49%% medical-term capture vs large-v3, no "
                             "簡體 drift. No local model dir? use --model large-v3.")
    parser.add_argument("--compute-type", default="float16",
                        help="CTranslate2 compute type (default float16; try int8_float16 for low VRAM)")
    parser.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto",
                        help="auto (default) = try CUDA, fall back to CPU int8 when "
                             "CUDA is missing or the card is too small; cpu = skip the "
                             "CUDA attempt (2-4 GB cards: pair with --model small/medium); "
                             "cuda = never fall back, fail loudly.")
    parser.add_argument("--engine", choices=["local", "groq"], default="local",
                        help="local = faster-whisper on GPU (default). groq = cloud "
                             "Whisper (frees the GPU); auto-falls-back to local on "
                             "failure OR token-collapse. NON-PHI audio only — Groq DPA "
                             "is opaque (see SKILL.md red line).")
    parser.add_argument("--retry-timeout", type=int, default=None,
                        help="Timeout (s) for the auto collapse-retry child process "
                             "(default: max(3600, 2x media duration))")
    # Internal: set by the thin parent (see _run_parent) on the decode child.
    parser.add_argument("--decode-only", action="store_true",
                        help=argparse.SUPPRESS)
    # Internal: index into attempt_plan(); the parent bumps it after an OOM child.
    parser.add_argument("--attempt", type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()

    # Reconcile --lang vs --language. Language is REQUIRED — guessing wrong
    # (e.g. running --lang zh on English audio) produces catastrophic
    # token-collapse where Whisper hallucinates Chinese characters from
    # accented English. Caller must specify; if unknown, ASK THE USER.
    lang_help = (
        "Pick one of: zh, en, bilingual, auto.\n"
        "  zh         : Mandarin (may contain inline EN domain terms)\n"
        "  en         : pure English (default for international conferences)\n"
        "  bilingual  : TW conferences with deliberate CN<->EN code-switching\n"
        "  auto       : experimental detection (not recommended)\n"
        "If you don't know what the speaker(s) used, ASK the user before re-running.")
    if args.lang is None:
        if args.language:
            args.lang = args.language
        else:
            print(f"ERROR: --lang is required. {lang_help}", file=sys.stderr)
            return 2
    # --lang is constrained by argparse choices; --language was not validated at
    # all, so a typo there used to sail through to faster-whisper.
    if args.lang not in LANG_CHOICES:
        print(f"ERROR: unknown language {args.lang!r}. {lang_help}", file=sys.stderr)
        return 2

    media_path = args.media_path
    if not os.path.isfile(media_path):
        print(f"ERROR: File not found: {media_path}", file=sys.stderr)
        return 1

    # ffmpeg/ffprobe are needed before anything expensive happens; discovering
    # they are missing after a model load (or not at all, as a bare
    # FileNotFoundError) wastes minutes and reads as a Python bug.
    require_binaries("ffmpeg", "ffprobe")

    # dirname() of a bare filename is "", and os.makedirs("") raises
    # FileNotFoundError — so `transcribe_video.py lecture.mp4` used to die
    # before doing anything. abspath() first.
    output_dir = args.output_dir or os.path.dirname(os.path.abspath(media_path))
    os.makedirs(output_dir, exist_ok=True)
    audio_path = os.path.join(output_dir, "audio.wav")
    transcript_json = os.path.join(output_dir, "transcript.json")
    transcript_txt = os.path.join(output_dir, "transcript.txt")
    transcript_partial = os.path.join(output_dir, "transcript.partial.json")
    handoff_path = os.path.join(output_dir, HANDOFF_NAME)

    if not args.decode_only:
        _warn_off_default(args)
        return _run_parent(args, output_dir, audio_path, transcript_json,
                           handoff_path, media_path)

    cfg = {}
    try:
        cfg = load_config()
    except Exception as e:  # noqa: BLE001 — a missing pyyaml must not block a run
        print(f"WARNING: config not loaded ({e}); using built-in defaults",
              file=sys.stderr)
    # Aliases live in config.yaml; this exits 2 with a fix-it message when an
    # alias points at a model directory that is not on this machine.
    model_ref = resolve_model(args.model, cfg)

    initial_prompt = args.initial_prompt
    if args.initial_prompt_file:
        if not os.path.isfile(args.initial_prompt_file):
            print(f"ERROR: --initial-prompt-file not found: "
                  f"{args.initial_prompt_file}", file=sys.stderr)
            return 2
        with open(args.initial_prompt_file, encoding="utf-8") as f:
            initial_prompt = f.read().strip()

    # ---- Logger ----
    if StageLogger is None:
        # Dummy logger so .progress / .retry / .stage_done / .heartbeat work.
        class _Null:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def __getattr__(self, _): return lambda *a, **k: None
            def close(self): return None
            def heartbeat(self, *a, **k): return self
        log = _Null()
        lecture = os.path.basename(os.path.abspath(output_dir))
    else:
        log = StageLogger(
            "transcribe", output_dir,
            extra={"lang": args.lang, "model": model_ref,
                   "batched": args.batched, "batch_size": args.batch_size},
            media_path=media_path,
        )
        lecture = lecture_name(output_dir)

    wall_start = time.time()
    log.start(media=os.path.basename(media_path),
              beam_size=args.beam_size,
              compute_type=args.compute_type)

    # ---- Probe duration ----
    duration_s = probe_duration_seconds(media_path)
    if duration_s:
        log.emit("media_probe", status="running",
                 duration_s=round(duration_s, 1))

    def _prob(p):
        # 0.0 is a real probability; `if p` turned it into null.
        return round(p, 3) if p is not None else None

    # ---- Groq cloud path (frees the local GPU) ----
    # Try Groq first when requested. Verify with the same collapse detector used
    # for local output; on collapse OR any Groq failure, fall through to the local
    # faster-whisper path below. Groq has no anti-collapse knobs, so this guard
    # is load-bearing for bilingual/code-switched audio.
    # groq_asr._lang_for_groq maps this pipeline's vocabulary to what the API
    # accepts: bilingual -> "zh", auto -> None (omit, let it detect); zh/en and
    # any other ISO-639-1 code pass through untouched.
    # attempt > 0 means an earlier child already fell back to local and OOMed:
    # Groq was tried (and rejected) then, so a relaunch goes straight to local
    # instead of paying for the same API call again.
    if args.engine == "groq" and args.attempt > 0:
        log.emit("groq_skipped", status="running", attempt=args.attempt,
                 reason="OOM relaunch — groq already tried by attempt 0")
    if args.engine == "groq" and args.attempt == 0:
        gres: list | None = None
        try:
            from groq_asr import transcribe_groq
            from retranscribe_segment import detect_collapses

            log.emit("groq_transcribe", status="running")
            gres = transcribe_groq(media_path, language=args.lang,
                                   prompt=initial_prompt, work_dir=output_dir)
            collapses = detect_collapses(gres) if gres else [("", "", "empty")]
            if collapses:
                log.emit("groq_collapse_fallback", status="running",
                         n_collapses=len(collapses), n_segments=len(gres or []))
                print(f"[transcribe] groq result has {len(collapses)} collapse "
                      f"range(s) → falling back to local faster-whisper",
                      file=sys.stderr)
                gres = None
        except Exception as e:  # noqa: BLE001 — any Groq failure → local fallback
            gres = None
            log.emit("groq_fallback", status="running", error=str(e)[:200])
            print(f"[transcribe] groq failed ({e}) → falling back to local "
                  f"faster-whisper", file=sys.stderr)

        # Finalizing lives OUTSIDE that try: once a good Groq transcript is on
        # disk, a later error (a bad runs.jsonl write, say) must not send us
        # down the local path to overwrite it.
        if gres:
            atomic_write_json(transcript_json, gres)
            write_transcript_lines(gres, transcript_txt)
            wall_s = time.time() - wall_start
            log.stage_done(success=True, n_segments=len(gres),
                           engine="groq", whisper_model="whisper-large-v3-turbo",
                           total_wall_s=round(wall_s, 1))
            if append_run_summary is not None:
                try:
                    append_run_summary(output_dir, {
                        "date": time.strftime("%Y-%m-%d"),
                        "lecture": lecture,
                        "stage": "transcribe",
                        "video_duration_s": round(duration_s, 1) if duration_s else None,
                        "transcribe_s": round(wall_s, 1),
                        "n_segments": len(gres),
                        "lang": args.lang,
                        "engine": "groq",
                        "whisper_model": "whisper-large-v3-turbo",
                        "success": True,
                        "pipeline_version": git_hash() if git_hash else "unknown",
                    })
                except Exception as e:
                    log.emit("runs_jsonl_write_failed", status="error", error=str(e))
            # Same contract as the local path: written after stage_done +
            # runs.jsonl. Without it _run_parent reads "no handoff" as a failed
            # child and exits 1 on a finished transcript.
            atomic_write_json(handoff_path, {
                "n_segments": len(gres),
                "device_cpu": False,
                "duration_s": duration_s,
            })
            log.close()
            return 0

    # ---- Audio extraction ----
    try:
        if args.attempt > 0 and os.path.isfile(audio_path) and os.path.getsize(audio_path) > 0:
            # OOM relaunch: attempt 0 of this same parent run extracted (and
            # validated) this audio.wav moments ago — don't ffprobe/re-extract.
            log.emit("audio_reused", status="success", attempt=args.attempt)
        else:
            log.emit("audio_extract", status="running")
            extract_audio(media_path, audio_path, expected_duration_s=duration_s, log=log)
            log.emit("audio_extract", status="success")
    except subprocess.CalledProcessError as e:
        print(e.stderr or str(e), file=sys.stderr)
        log.stage_done(success=False,
                       error=f"audio_extract failed (exit {e.returncode})")
        log.close()
        return 1

    # ---- Machine-wide GPU lease (2026-08-10, replaces the pause-flag wait) ----
    # The old wait_if_paused here was best-effort AND bounded: after 3600 s it
    # printed "treating as stale and continuing" and ran the GPU anyway — while
    # a legitimate holder (e.g. the LanceDB indexer, acquire timeout 6 h) still
    # had the card. Now we take a real FIFO lease for the rest of the process:
    # timeout = ABORT (never run unleased), release via atexit on every exit
    # path, hard crashes self-heal through the lease's reap_on_pid_death. Under
    # a lease-holding parent (GPU_LEASE_BYPASS=1) the acquire short-circuits,
    # and on boxes without the lease script it is a no-op.
    _lease_cm = gpu_lease_ctx("transcribe_video", min_free_mb=4000, timeout=7200)
    try:
        _lease_cm.__enter__()
    except RuntimeError as e:
        print(f"[transcribe] GPU lease unavailable — aborting: {e}", file=sys.stderr)
        log.stage_done(success=False, error=f"gpu_lease acquire failed: {e}")
        log.close()
        return 1
    atexit.register(lambda: _lease_cm.__exit__(None, None, None))

    # ---- Model setup ----
    setup_nvidia_path()

    def _cpu_eta() -> str:
        if not duration_s:
            return ""
        return (f" This {fmt_hms(duration_s)} recording will take roughly "
                f"{fmt_hms(duration_s * 1.5)}-{fmt_hms(duration_s * 3)} on CPU.")

    def _cpu_model():
        from faster_whisper import WhisperModel
        # The collapse-retry child (retranscribe_segment.py) loads its own
        # model; tell it not to re-attempt CUDA and OOM/fall back all over again.
        os.environ["LECTURE_ASR_DEVICE"] = "cpu"
        return WhisperModel(model_ref, device="cpu", compute_type="int8")

    def model_factory():
        from faster_whisper import WhisperModel
        if args.device == "cpu":
            print(f"[transcribe] --device cpu: loading {model_ref!r} as CPU int8."
                  f"{_cpu_eta()}", file=sys.stderr)
            log.emit("device", status="running", device="cpu", compute_type="int8",
                     reason="--device cpu")
            return _cpu_model()
        try:
            return WhisperModel(model_ref, device="cuda",
                                compute_type=args.compute_type)
        except Exception as gpu_err:  # noqa: BLE001
            # Only a missing/broken CUDA runtime — or a card too small to hold
            # the model — justifies the CPU path. A bad model path or an
            # unsupported compute type is a fixable mistake, and silently
            # answering it with a 10-20x slower run (one JSONL line as the only
            # evidence) has cost whole nights.
            msg = str(gpu_err).lower()
            gpu_oom = _is_oom(gpu_err)
            cuda_missing = any(k in msg for k in (
                "cuda", "cudnn", "cublas", "no gpu", "libcuda", "nvidia", "driver"))
            if args.device == "cuda" or not (cuda_missing or gpu_oom):
                print(f"ERROR: model load failed for {model_ref!r} with "
                      f"compute_type={args.compute_type!r}: {gpu_err}",
                      file=sys.stderr)
                raise
            why = ("GPU out of memory loading the model — the card is too small "
                   "for it (2-4 GB cards: try --model small or --engine groq)"
                   if gpu_oom else "CUDA unavailable")
            print("=" * 72, file=sys.stderr)
            print(f"WARNING: {why} ({str(gpu_err)[:200]})", file=sys.stderr)
            print(f"WARNING: falling back to CPU int8 — 10-20x SLOWER.{_cpu_eta()}",
                  file=sys.stderr)
            print("WARNING: Ctrl-C now and fix CUDA if that is not acceptable.",
                  file=sys.stderr)
            print("=" * 72, file=sys.stderr)
            log.retry(reason=("GPU too small → CPU int8" if gpu_oom
                              else "CUDA unavailable → CPU int8"),
                      error=str(gpu_err)[:200])
            return _cpu_model()

    decode_params, language = build_decode_params(args)
    if args.word_timestamps:
        decode_params["word_timestamps"] = True
    log.emit("decode_params", status="running",
             language=language, decode_params=decode_params,
             model=model_ref,
             initial_prompt_len=len(initial_prompt) if initial_prompt else 0)

    # ---- Transcription ----
    # An explicit --batch-size 1 means the user WANTS sequential mode. The old
    # `b > 1` filter silently emptied the list so it reached the sequential path
    # via the OOM-"fallback" branch, mislabeling an intentional choice as a
    # failure recovery. Honor 1 explicitly and say so in the log.
    # BatchedInferencePipeline REQUIRES vad_filter=True (it chunks audio by VAD;
    # vad_filter=False raises "No clip timestamps found"). With VAD off now the
    # default, batched mode is only reachable via an explicit --vad.
    if args.batched and not args.vad:
        log.emit("batched_incompatible_novad", status="running",
                 note="vad_filter=False cannot run under BatchedInferencePipeline "
                      "→ sequential model.transcribe (pass --vad to re-enable "
                      "batching at the cost of VAD eating quiet speech)")
        args.batched = False
    if args.batched and args.batch_size <= 1:
        log.emit("batch_size_1_requested", status="running",
                 note="explicit --batch-size 1 → sequential model.transcribe "
                      "(intentional, not an OOM fallback)")
        batch_sizes_to_try: list[int] = []
    elif args.batched:
        # Try the requested size, then half of it; dedupe; drop anything <=1
        # (sequential is the final fallback inside transcribe_with_fallback).
        batch_sizes_to_try = []
        for b in (args.batch_size, max(1, args.batch_size // 2)):
            if b > 1 and b not in batch_sizes_to_try:
                batch_sizes_to_try.append(b)
    else:
        batch_sizes_to_try = []

    try:
        hb_interval = float(os.environ.get("TRANSCRIBE_HB_S", "30"))
        if hb_interval <= 0:
            raise ValueError("must be > 0")
    except ValueError as e:
        print(f"WARNING: ignoring TRANSCRIBE_HB_S="
              f"{os.environ.get('TRANSCRIBE_HB_S')!r} ({e}); using 30s",
              file=sys.stderr)
        hb_interval = 30.0

    model = None
    detected_lang = None
    detected_prob = None
    with log.heartbeat(interval_s=hb_interval):
        try:
            segments_iter, info, used_bs, fallback_triggered, model = \
                transcribe_with_fallback(
                    audio_path, model_factory, decode_params,
                    language, batch_sizes_to_try, initial_prompt, log,
                    attempt=args.attempt,
                )
        except OomRetry as e:
            # Not a stage failure: the parent re-launches at e.next_attempt.
            atomic_write_json(handoff_path + ".oom",
                              {"next_attempt": e.next_attempt})
            log.close()
            return OOM_RETRY_RC
        except Exception as e:  # noqa: BLE001
            log.stage_done(success=False, error=str(e)[:500])
            log.close()
            return 2

        detected_lang = getattr(info, "language", None)
        detected_prob = getattr(info, "language_probability", None)
        log.emit("transcription_started", status="running",
                 detected_language=detected_lang,
                 detected_language_probability=_prob(detected_prob),
                 used_batch_size=used_bs,
                 batch_fallback_triggered=fallback_triggered)

        results: list[dict] = []
        try:
            for seg in segments_iter:
                rec = {
                    "start": round(seg.start, 2),
                    "end": round(seg.end, 2),
                    "text": seg.text,
                }
                segwords = getattr(seg, "words", None)
                if args.word_timestamps and segwords:
                    rec["words"] = [
                        {"start": round(w.start, 2), "end": round(w.end, 2),
                         "word": w.word}
                        for w in segwords if w.start is not None and w.end is not None
                    ]
                results.append(rec)
                if len(results) % 50 == 0:
                    log.progress(n_segments=len(results),
                                 last_t=round(seg.end, 1))
                    # ==Crash-proofing (2026-07-25)==: CTranslate2 can abort the
                    # whole process (0xC0000409) while the segment generator
                    # winds down — upstream faster-whisper#1293/#71, unfixed.
                    # An abort is a hard kill: no exception, no finally, no
                    # atexit. Checkpoint so a killed run costs <=50 segments
                    # instead of the entire transcript (see _write_partial).
                    _write_partial(transcript_partial, results)
        except Exception as e:  # noqa: BLE001
            log.emit("segment_iteration_error", status="error",
                     error=str(e)[:500], n_segments_so_far=len(results))
            # Continue with whatever we got — partial is better than zero.

        # ---- Write outputs (INSIDE the heartbeat block — see below) ----
        # faster-whisper 1.2.1 + ctranslate2 4.7.1 on Windows/CUDA abort the
        # process (0xC0000409 STATUS_STACK_BUFFER_OVERRUN) during teardown after
        # a successful transcription — upstream SYSTRAN/faster-whisper#1293 and
        # #71, unfixed in any release. Writing here means a teardown abort can no
        # longer destroy a completed transcript; `os._exit` at __main__ keeps the
        # exit code honest. Verified 2026-07-25 on a chunk that aborted 3/3 times:
        # 273 segments written, exit 0.
        #
        # Nothing is written for an empty result: every batch runner treats an
        # existing transcript.txt as "this clip is done", so an empty one would
        # make the failure permanent and invisible on the next pass.
        if results:
            atomic_write_json(transcript_json, results)
            write_transcript_lines(results, transcript_txt)
            # Full transcript landed — the checkpoint would only confuse the
            # resume driver into thinking this run died.
            try:
                os.remove(transcript_partial)
            except OSError:
                pass

    # ==Never destroy the model in-process (2026-10-05)==. The
    # 2026-08-02 `del model; gc.collect()` here ran CTranslate2's destructor
    # inside main(), i.e. BEFORE the os._exit guard, and that destructor is the
    # 0xC0000409 abort (decisions.md §ctranslate2-abort): 3 of 4 jobs on
    # 2026-10-03 died here with the transcript written but no stage_done. The
    # references are parked at module level so even main()'s return does not
    # drop them; the OS reclaims process memory + VRAM at os._exit, and the
    # collapse retry runs in _run_parent only after this process has exited.
    _KEEPALIVE.extend([segments_iter, info, model])

    wall_s = time.time() - wall_start

    # A transcription that produced nothing is a FAILURE. Reporting success here
    # sent empty transcripts downstream, where "no content" is indistinguishable
    # from a quiet lecture until someone reads the finished note.
    if not results:
        msg = (f"ERROR: transcription produced 0 segments from {media_path}. "
               f"Likely causes: wrong --lang (ran {args.lang!r}), a silent or "
               "music-only audio track, or a corrupt media file. Listen to "
               f"{audio_path} and check the language before re-running.")
        print(msg, file=sys.stderr)
        log.stage_done(success=False, n_segments=0, error="0 segments",
                       lang=args.lang, total_wall_s=round(wall_s, 1))
        if append_run_summary is not None:
            try:
                append_run_summary(output_dir, {
                    "date": time.strftime("%Y-%m-%d"),
                    "lecture": lecture,
                    "stage": "transcribe",
                    "video_duration_s": round(duration_s, 1) if duration_s else None,
                    "transcribe_s": round(wall_s, 1),
                    "n_segments": 0,
                    "lang": args.lang,
                    "whisper_model": model_ref,
                    "success": False,
                    "pipeline_version": git_hash() if git_hash else "unknown",
                })
            except Exception as e:
                log.emit("runs_jsonl_write_failed", status="error", error=str(e))
        if not args.keep_audio:
            print(f"[transcribe] keeping {audio_path} for diagnosis",
                  file=sys.stderr)
        log.close()
        return 3

    log.stage_done(success=True,
                   n_segments=len(results),
                   used_batch_size=used_bs,
                   batch_fallback_triggered=fallback_triggered,
                   detected_language=detected_lang,
                   detected_language_probability=_prob(detected_prob),
                   whisper_model=model_ref,
                   lang=args.lang,
                   total_wall_s=round(wall_s, 1))

    # ---- runs.jsonl summary ----
    if append_run_summary is not None:
        try:
            append_run_summary(output_dir, {
                "date": time.strftime("%Y-%m-%d"),
                "lecture": lecture,
                "stage": "transcribe",
                "video_duration_s": round(duration_s, 1) if duration_s else None,
                "transcribe_s": round(wall_s, 1),
                "n_segments": len(results),
                "lang": args.lang,
                "detected_language": detected_lang,
                "detected_language_probability": _prob(detected_prob),
                "whisper_model": model_ref,
                "batch_size": used_bs,
                "batch_fallback_triggered": fallback_triggered,
                "success": True,
                "pipeline_version": git_hash() if git_hash else "unknown",
            })
        except Exception as e:
            log.emit("runs_jsonl_write_failed", status="error", error=str(e))

    # Handoff to the parent: written only after stage_done + runs.jsonl, so its
    # presence means "transcript complete and recorded" even if this process
    # still aborts on the way out.
    atomic_write_json(handoff_path, {
        "n_segments": len(results),
        "device_cpu": os.environ.get("LECTURE_ASR_DEVICE", "").lower() == "cpu",
        "duration_s": duration_s,
    })
    log.close()
    return 0


def _run_parent(args, output_dir, audio_path, transcript_json, handoff_path,
                media_path) -> int:
    """Thin parent: never imports CTranslate2.

    Runs the decode in a child (`--decode-only`), which writes the transcript,
    stage_done and runs.jsonl and then os._exit()s WITHOUT destroying the model.
    Only after that child is gone — and its VRAM with it — does the collapse
    retry start, so two Whisper models never share the card.
    """
    try:
        os.remove(handoff_path)   # stale marker from an earlier run
    except OSError:
        pass
    oom_path = handoff_path + ".oom"
    attempt = 0
    while True:
        try:
            os.remove(oom_path)
        except OSError:
            pass
        cmd = [sys.executable, os.path.abspath(__file__), *sys.argv[1:],
               "--decode-only", "--keep-audio", "--attempt", str(attempt)]
        rc = subprocess.run(cmd).returncode
        nxt = None
        if rc == OOM_RETRY_RC and os.path.isfile(oom_path):
            try:
                with open(oom_path, encoding="utf-8") as f:
                    nxt = int(json.load(f)["next_attempt"])
            except (OSError, ValueError, KeyError, TypeError):
                nxt = None
            try:
                os.remove(oom_path)
            except OSError:
                pass
        if nxt is None or nxt <= attempt:
            break
        # The OOMed child has exited, so its VRAM is back before the retry.
        print(f"[transcribe] decode OOM at attempt {attempt}; relaunching "
              f"decode process at attempt {nxt}", file=sys.stderr)
        attempt = nxt
    handoff = None
    if os.path.isfile(handoff_path):
        try:
            with open(handoff_path, encoding="utf-8") as f:
                handoff = json.load(f)
        except (OSError, ValueError):
            handoff = None
        try:
            os.remove(handoff_path)
        except OSError:
            pass
    if handoff is None:
        # Failure before the transcript was recorded: the child already wrote
        # stage_done(success=False) where it could. Keep audio for diagnosis.
        return rc if rc != 0 else 1
    if rc != 0:
        # Transcript + stage_done are on disk; only the exit path died.
        print(f"[transcribe] decode child exit {rc} AFTER recording the "
              f"transcript ({handoff.get('n_segments')} segments) — treating "
              "as success", file=sys.stderr)

    # ---- Auto-detect token collapse and retry ----
    duration_s = handoff.get("duration_s")
    try:
        with open(transcript_json, encoding="utf-8") as f:
            results = json.load(f)
    except (OSError, ValueError) as e:
        print(f"[transcribe] cannot re-read {transcript_json}: {e}",
              file=sys.stderr)
        results = []
    if not results or not os.path.isfile(audio_path):
        # Groq path (no local audio) or nothing to check.
        print("[transcribe] collapse retry skipped (no transcript or no audio)",
              file=sys.stderr)
        return 0
    model_ref = args.model
    retry_env = dict(os.environ)
    if handoff.get("device_cpu"):
        retry_env["LECTURE_ASR_DEVICE"] = "cpu"
    # Whisper sometimes falls into 1-token-per-segment loops on accented or
    # mixed audio. Detect + auto-retranscribe so the pipeline doesn't silently
    # ship a broken transcript.
    try:
        from retranscribe_segment import detect_collapses
        collapses = detect_collapses(results)
        if collapses:
            print(f"\n[transcribe] detected {len(collapses)} collapse range(s); "
                  f"auto-running retranscribe_segment.py --auto",
                  file=sys.stderr)
            for s, e, reason in collapses:
                print(f"  [{fmt_hms(s)}-{fmt_hms(e)}] {reason}", file=sys.stderr)
            script_dir = os.path.dirname(os.path.abspath(__file__))
            retry_script = os.path.join(script_dir, "retranscribe_segment.py")
            # bilingual decodes better as English on a short collapsed chunk;
            # 'auto' has no ISO code — faster-whisper rejects language="auto",
            # so the child must be told to detect (it maps 'auto' -> None).
            retry_lang = "en" if args.lang == "bilingual" else args.lang
            cmd = [sys.executable, retry_script, "--dir", output_dir,
                   "--audio", audio_path, "--auto", "--language", retry_lang,
                   "--model", model_ref]
            # A wedged CUDA child would otherwise hold the GPU forever while the
            # parent waits with no timeout.
            timeout_s = args.retry_timeout or int(max(3600, (duration_s or 0) * 2))
            try:
                rc = subprocess.run(cmd, timeout=timeout_s,
                                    env=retry_env).returncode
            except subprocess.TimeoutExpired:
                rc = -1
                print(f"[transcribe] retranscribe child exceeded {timeout_s}s and "
                      "was killed; the transcript still has its original text for "
                      "those ranges", file=sys.stderr)
            if rc != 0:
                print(f"[transcribe] retranscribe exit {rc}; transcript may still "
                      "have collapses", file=sys.stderr)
        else:
            print("[transcribe] no token-collapse detected", file=sys.stderr)
    except Exception as e:
        print(f"[transcribe] collapse auto-detect failed: {e}", file=sys.stderr)

    # ---- Cleanup audio (now safe — retranscribe done) ----
    if not args.keep_audio:
        try:
            os.remove(audio_path)   # regenerable intermediate this run created
        except OSError:
            pass

    return 0


if __name__ == "__main__":
    rc = main()
    # Skip interpreter teardown: CTranslate2's CUDA cleanup aborts the process
    # with 0xC0000409 on Windows (upstream #1293/#71, no released fix), turning a
    # finished transcription into a "failed" job. Verified 2026-07-25 on a chunk
    # that aborted 3/3 times with a clean exit. That is still load-bearing, so
    # os._exit stays.
    #
    # What it costs: atexit never runs, so _log's stage_aborted safety net is
    # dead on this path. main() therefore calls log.close() (which also
    # unregisters that handler) on EVERY return path, and an exception escaping
    # main() skips this block entirely and gets the normal teardown + atexit.
    # Set TRANSCRIBE_CLEAN_EXIT=1 to force a normal exit when debugging.
    sys.stdout.flush()
    sys.stderr.flush()
    if os.environ.get("TRANSCRIBE_CLEAN_EXIT") == "1":
        sys.exit(rc)
    os._exit(rc)
