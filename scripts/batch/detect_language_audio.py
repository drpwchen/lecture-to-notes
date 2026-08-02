# -*- coding: utf-8 -*-
"""detect_language_audio.py <media> [--json] [--windows 3] [--seconds 45]

Detect spoken language from the AUDIO itself (not from a possibly-garbage prior
transcript). The text-based detect_language is unreliable: an ENGLISH lecture
force-transcribed with --lang zh produces hallucinated/translated Chinese (high
CJK) → text detection says zh → re-transcribe zh → garbage again. Whisper detects
language from audio in one shot, before decoding.

Samples several evenly-spaced windows (default 3 × 45s) so an English intro to a
Mandarin talk (or vice-versa) doesn't bias the verdict; majority vote weighted by
probability. Maps to our transcription buckets: **en** or **zh** (Whisper's zh
model handles inline English medical terms fine, so a Mandarin-with-English-terms
clip stays zh; only a genuinely English-spoken clip becomes en).

Output: prints `lang prob` (or full JSON with --json). lang ∈ {en, zh, <other>}.
"""
import json
import os
import subprocess
import sys
import tempfile

setup_done = False


def setup_nvidia():
    global setup_done
    if setup_done:
        return
    try:
        import nvidia.cublas.lib, nvidia.cudnn.lib  # noqa
        for mod in (nvidia.cublas.lib, nvidia.cudnn.lib):
            os.add_dll_directory(os.path.dirname(mod.__file__))
    except Exception:
        pass
    setup_done = True


def sample_wav(media, start, dur, out):
    subprocess.run(["ffmpeg", "-v", "error", "-ss", str(start), "-t", str(dur),
                    "-i", media, "-vn", "-ac", "1", "-ar", "16000", "-y", out],
                   capture_output=True, timeout=120)
    return os.path.exists(out) and os.path.getsize(out) > 1000


def media_duration(media):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                              "format=duration", "-of", "csv=p=0", media],
                             capture_output=True, text=True, timeout=60).stdout
        return float(out.strip())
    except Exception:
        return 0.0


def main():
    argv = sys.argv[1:]
    want_json = "--json" in argv
    windows = int(argv[argv.index("--windows") + 1]) if "--windows" in argv else 3
    secs = int(argv[argv.index("--seconds") + 1]) if "--seconds" in argv else 45
    media = [a for a in argv if not a.startswith("--")][0]

    dur = media_duration(media)
    setup_nvidia()
    from faster_whisper import WhisperModel
    try:
        model = WhisperModel("large-v3", device="cuda", compute_type="float16")
    except Exception:
        model = WhisperModel("large-v3", device="cpu", compute_type="int8")

    # evenly spaced windows, skipping the very start/end
    if dur <= secs or windows <= 1:
        offsets = [max(0, dur / 2 - secs / 2)]
    else:
        span = dur * 0.8
        step = span / windows
        offsets = [dur * 0.1 + i * step for i in range(windows)]

    votes = {}  # lang -> summed probability
    detail = []
    with tempfile.TemporaryDirectory() as td:
        for i, off in enumerate(offsets):
            wav = os.path.join(td, f"s{i}.wav")
            if not sample_wav(media, off, secs, wav):
                continue
            _segs, info = model.transcribe(wav, language=None, vad_filter=True,
                                           beam_size=1)
            lang = getattr(info, "language", None)
            prob = float(getattr(info, "language_probability", 0) or 0)
            if lang:
                votes[lang] = votes.get(lang, 0.0) + prob
                detail.append({"offset": round(off), "lang": lang, "prob": round(prob, 3)})

    if not votes:
        out = {"media": os.path.basename(media), "lang": "zh", "prob": 0.0,
               "reason": "no_detection_fallback_zh", "detail": detail}
    else:
        best = max(votes, key=votes.get)
        # collapse any non-en detection that isn't zh into its code; keep en/zh
        total = sum(votes.values())
        out = {"media": os.path.basename(media), "lang": best,
               "prob": round(votes[best] / total, 3), "votes": votes, "detail": detail}

    if want_json:
        print(json.dumps(out, ensure_ascii=False))
    else:
        print(f"{out['lang']} {out['prob']}")


if __name__ == "__main__":
    main()
