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
``en``                  — English model
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

Tips for accented English / domain-heavy lectures
-------------------------------------------------
    --initial-prompt-file glossary.txt   bias decoder toward jargon
    --beam-size 10                       wider search; ~30% slower
    --no-condition                       disable previous-text conditioning
                                         (on by default; --lang bilingual too)
"""
from __future__ import annotations

import argparse
import gc
import os
import re
import subprocess
import sys
import time

from _common import (atomic_write_json, fmt_hms, load_config, require_binaries,
                     resolve_model, resolve_pause_flag, setup_nvidia_path,
                     wait_if_paused, write_transcript_lines)

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


def transcribe_with_fallback(audio_path: str, model_factory, decode_params: dict,
                             language: str | None, batch_sizes: list[int],
                             initial_prompt: str | None, log):
    """Try each batch size; on OOM, free + retry. Final fallback: sequential.

    Returns (segments_iter, info, used_batch_size, fallback_triggered, model).
    The caller iterates the (lazy) segments and is responsible for dropping its
    reference to ``model`` afterwards — that reference is what actually holds
    the VRAM, so it is returned rather than left dangling in a closure.
    """
    try:
        from faster_whisper import BatchedInferencePipeline
        batched_available = True
    except ImportError:
        batched_available = False
        if batch_sizes:
            log.retry(reason="BatchedInferencePipeline missing - using sequential",
                      batch_size=1)

    fallback_triggered = False
    last_err: Exception | None = None

    if batched_available and batch_sizes:
        for bs in batch_sizes:
            model = None
            pipeline = None
            try:
                model = model_factory()
                pipeline = BatchedInferencePipeline(model=model)
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
            except Exception as e:  # noqa: BLE001 — CT2 raises bare RuntimeError on OOM
                last_err = e
                # Release the model on EVERY exit from this attempt, not just the
                # OOM one: a re-raised non-OOM error used to leave several GB of
                # VRAM held by an unreachable model until process exit.
                pipeline = None
                if model is not None:
                    del model
                gc.collect()
                if not _is_oom(e):
                    raise
                log.retry(reason=f"batched OOM at batch_size={bs}",
                          batch_size=bs, error=str(e)[:200])
                fallback_triggered = True
                time.sleep(1.0)
                continue

    # Final fallback: sequential mode (model.transcribe directly).
    log.retry(reason="falling back to sequential mode",
              batch_size=1, prev_error=str(last_err)[:200] if last_err else None)
    model = model_factory()
    try:
        segments, info = model.transcribe(
            audio_path,
            language=language,
            initial_prompt=initial_prompt,
            **decode_params,
        )
    except Exception:
        del model
        gc.collect()
        raise
    return segments, info, 1, True, model


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

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
                        help="Beam size (default 5; try 10 for accented/technical speech)")
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
                        help="Disable condition_on_previous_text (--lang bilingual sets this automatically)")
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
    parser.add_argument("--engine", choices=["local", "groq"], default="local",
                        help="local = faster-whisper on GPU (default). groq = cloud "
                             "Whisper (frees the GPU); auto-falls-back to local on "
                             "failure OR token-collapse. NON-PHI audio only — Groq DPA "
                             "is opaque (see SKILL.md red line).")
    parser.add_argument("--retry-timeout", type=int, default=None,
                        help="Timeout (s) for the auto collapse-retry child process "
                             "(default: max(3600, 2x media duration))")
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
    if args.engine == "groq":
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
            log.close()
            return 0

    # ---- Audio extraction ----
    try:
        log.emit("audio_extract", status="running")
        extract_audio(media_path, audio_path, expected_duration_s=duration_s, log=log)
        log.emit("audio_extract", status="success")
    except subprocess.CalledProcessError as e:
        print(e.stderr or str(e), file=sys.stderr)
        log.stage_done(success=False,
                       error=f"audio_extract failed (exit {e.returncode})")
        log.close()
        return 1

    # ---- Cooperative GPU pause button ----
    # Yield the GPU while someone holds the pause flag (configured via
    # paths.pause_flag / LECTURE_PAUSE_FLAG; disabled when unset). Any GPU job
    # honors this so two CUDA jobs never collide. The holder of the pause runs
    # its own job with GPU_LEASE_BYPASS=1 so it doesn't wait on itself. The wait
    # is bounded inside wait_if_paused — a stale flag can't wedge the pipeline.
    # Best-effort: a config problem must never take down transcription.
    try:
        wait_if_paused(
            resolve_pause_flag(cfg),
            log_fn=lambda msg: log.emit("gpu_pause_wait", status="running", note=msg),
        )
    except Exception as e:  # noqa: BLE001
        print(f"[transcribe] pause check skipped ({e})", file=sys.stderr)

    # ---- Model setup ----
    setup_nvidia_path()

    def model_factory():
        from faster_whisper import WhisperModel
        try:
            return WhisperModel(model_ref, device="cuda",
                                compute_type=args.compute_type)
        except Exception as gpu_err:  # noqa: BLE001
            # Only a missing/broken CUDA runtime justifies the CPU path. A bad
            # model path or an unsupported compute type is a fixable mistake,
            # and silently answering it with a 10-20x slower run (one JSONL line
            # as the only evidence) has cost whole nights.
            msg = str(gpu_err).lower()
            cuda_missing = any(k in msg for k in (
                "cuda", "cudnn", "cublas", "no gpu", "libcuda", "nvidia", "driver"))
            if not cuda_missing:
                print(f"ERROR: model load failed for {model_ref!r} with "
                      f"compute_type={args.compute_type!r}: {gpu_err}",
                      file=sys.stderr)
                raise
            eta = ""
            if duration_s:
                eta = (f" This {fmt_hms(duration_s)} recording will take roughly "
                       f"{fmt_hms(duration_s * 1.5)}-{fmt_hms(duration_s * 3)} on CPU.")
            print("=" * 72, file=sys.stderr)
            print(f"WARNING: CUDA unavailable ({str(gpu_err)[:200]})", file=sys.stderr)
            print(f"WARNING: falling back to CPU int8 — 10-20x SLOWER.{eta}",
                  file=sys.stderr)
            print("WARNING: Ctrl-C now and fix CUDA if that is not acceptable.",
                  file=sys.stderr)
            print("=" * 72, file=sys.stderr)
            log.retry(reason="CUDA unavailable → CPU int8",
                      error=str(gpu_err)[:200])
            return WhisperModel(model_ref, device="cpu", compute_type="int8")

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
                )
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

    # Release the model before anything else runs: the collapse retry loads its
    # own model in a child process, and two of them on an 8 GB card crash it.
    # CTranslate2 frees VRAM in the model destructor, so dropping every Python
    # reference (model, the segment generator that closes over it, and info) is
    # the whole lever available to us — there is no explicit unload API.
    del segments_iter
    del info
    if model is not None:
        del model
    gc.collect()

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

    # ---- Auto-detect token collapse and retry ----
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
                rc = subprocess.run(cmd, timeout=timeout_s).returncode
            except subprocess.TimeoutExpired:
                rc = -1
                print(f"[transcribe] retranscribe child exceeded {timeout_s}s and "
                      "was killed; the transcript still has its original text for "
                      "those ranges", file=sys.stderr)
                log.emit("retranscribe_timeout", status="error",
                         timeout_s=timeout_s)
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

    log.close()
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
