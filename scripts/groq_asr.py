"""Groq Whisper transcription helper.

Self-contained: this module has no dependency on any other skill or on the
local pipeline's config -- hand it a media file and a key and it transcribes.

Why this exists
---------------
Offload audio transcription to Groq's hosted `whisper-large-v3-turbo` to free the
local GPU (GPU contention is the real pain point). Output format is byte-for-byte
compatible with the local faster-whisper path: a list of ``{start, end, text}`` dicts.

Hard limits & caveats
---------------------
- **Groq has NO anti-collapse knobs** (no ``condition_on_previous_text``, no VAD params).
  Hosted API accepts only language / prompt / temperature / response_format. So on
  code-switched (CN<->EN) audio it can token-collapse more than the locally-tuned
  bilingual mode. Callers MUST run collapse detection on the result and fall back to
  local on failure.
- **25 MB per-request cap** (free tier). We compress to 16 kHz mono 64 kbps mp3 first
  (~7.7 MB/hour), then time-split into ~20-min chunks only if still over the cap.
- **PHI red line**: Groq's DPA is opaque, no HIPAA BAA. Caller is responsible for the
  PHI gate — this module just transcribes whatever it's handed.

Key handling
------------
``GROQ_API_KEY`` is read from the process environment and nowhere else. When it
is unset, :func:`transcribe_groq` raises an actionable RuntimeError telling you
where to get a key. The value is used internally and NEVER printed or echoed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

GROQ_ENDPOINT = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3-turbo"
SIZE_CAP_BYTES = 24 * 1024 * 1024  # stay under Groq's 25 MB with headroom
CHUNK_SECONDS = 20 * 60            # 20 min @ 64 kbps mono = 9.6 MB


def get_groq_key() -> str:
    """Resolve the Groq API key from the ``GROQ_API_KEY`` environment variable.

    Returns '' when the variable is unset or blank; transcribe_groq turns that
    into an actionable RuntimeError.
    """
    return (os.environ.get("GROQ_API_KEY") or "").strip()


def _lang_for_groq(language: str | None) -> str | None:
    """Map the local pipeline's --lang vocabulary to a Groq ISO code (or None=auto)."""
    if language in (None, "auto"):
        return None
    if language == "bilingual":
        # TW conferences are predominantly Mandarin with inline English terms.
        # Whisper's zh model handles that better than the reverse.
        return "zh"
    return language  # zh / en / ja / ...


def _compress(media_path: str, out_path: str) -> None:
    """ffmpeg -> 16 kHz mono 64 kbps mp3 (Whisper only needs 16 kHz)."""
    cmd = [
        "ffmpeg", "-y", "-i", media_path,
        "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k",
        out_path,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True,
                       encoding="utf-8", errors="replace")
    except subprocess.CalledProcessError:
        # Windows CJK-path corner case: retry without text-mode decoding of stderr.
        subprocess.run(cmd, check=True, capture_output=True)


def _split(audio_path: str, out_dir: str) -> list[tuple[str, float]]:
    """Time-split into CHUNK_SECONDS pieces. Returns [(chunk_path, offset_seconds)]."""
    pattern = os.path.join(out_dir, "groq_chunk_%03d.mp3")
    subprocess.run([
        "ffmpeg", "-y", "-i", audio_path,
        "-f", "segment", "-segment_time", str(CHUNK_SECONDS),
        "-c", "copy", pattern,
    ], check=True, capture_output=True)
    chunks = sorted(f for f in os.listdir(out_dir)
                    if f.startswith("groq_chunk_") and f.endswith(".mp3"))
    return [(os.path.join(out_dir, f), i * CHUNK_SECONDS)
            for i, f in enumerate(chunks)]


def _post_chunk(chunk_path: str, key: str, language: str | None,
                prompt: str | None) -> list[dict]:
    """Send one chunk to Groq; return its segments (timestamps relative to chunk)."""
    import requests

    data = {
        "model": GROQ_MODEL,
        "response_format": "verbose_json",
        "temperature": "0",
    }
    if language:
        data["language"] = language
    if prompt:
        data["prompt"] = prompt[:1000]  # Groq prompt is a short biasing hint

    with open(chunk_path, "rb") as fh:
        resp = requests.post(
            GROQ_ENDPOINT,
            headers={"Authorization": f"Bearer {key}"},
            data=data,
            files={"file": (os.path.basename(chunk_path), fh,
                            "application/octet-stream")},
            timeout=600,
        )
    resp.raise_for_status()
    body = resp.json()
    segs = body.get("segments") or []
    out = []
    for s in segs:
        out.append({
            "start": round(float(s.get("start", 0.0)), 2),
            "end": round(float(s.get("end", 0.0)), 2),
            "text": s.get("text", ""),
        })
    # Fallback: no segment array (some response shapes) -> single text blob.
    if not out and body.get("text"):
        out.append({"start": 0.0, "end": 0.0, "text": body["text"]})
    return out


def transcribe_groq(media_path: str, language: str | None = "zh",
                    prompt: str | None = None,
                    work_dir: str | None = None) -> list[dict]:
    """Transcribe via Groq. Returns [{start, end, text}, ...].

    Raises RuntimeError if the key is missing/invalid; lets requests exceptions
    (network/429/5xx) propagate so the caller can fall back to local.
    """
    key = get_groq_key()
    if not key.startswith("gsk_"):
        raise RuntimeError(
            "No usable Groq API key. Set the GROQ_API_KEY environment variable "
            "(get one at https://console.groq.com/keys), or use the local "
            "faster-whisper engine instead of --engine groq."
        )

    work_dir = work_dir or os.path.dirname(os.path.abspath(media_path))
    os.makedirs(work_dir, exist_ok=True)
    compressed = os.path.join(work_dir, "groq_audio.mp3")
    glang = _lang_for_groq(language)

    print(f"[groq] compressing -> 16k mono 64k mp3", file=sys.stderr)
    _compress(media_path, compressed)
    size = os.path.getsize(compressed)
    print(f"[groq] compressed size: {size/1024/1024:.1f} MB", file=sys.stderr)

    results: list[dict] = []
    chunk_files: list[str] = []
    try:
        if size <= SIZE_CAP_BYTES:
            parts = [(compressed, 0.0)]
        else:
            print(f"[groq] over {SIZE_CAP_BYTES/1024/1024:.0f}MB -> splitting into "
                  f"{CHUNK_SECONDS//60}-min chunks", file=sys.stderr)
            parts = _split(compressed, work_dir)
            chunk_files = [p for p, _ in parts]

        for idx, (part_path, offset) in enumerate(parts):
            print(f"[groq] chunk {idx+1}/{len(parts)} (offset {offset/60:.0f}m) -> {GROQ_MODEL}",
                  file=sys.stderr)
            segs = _post_chunk(part_path, key, glang, prompt)
            for s in segs:
                results.append({
                    "start": round(s["start"] + offset, 2),
                    "end": round(s["end"] + offset, 2),
                    "text": s["text"],
                })
    finally:
        # Regenerable scratch audio this function created in work_dir.
        for f in [compressed, *chunk_files]:
            try:
                os.remove(f)
            except OSError:
                pass

    print(f"[groq] done: {len(results)} segments", file=sys.stderr)
    return results


def write_outputs(results: list[dict], json_path: str, txt_path: str,
                  timestamped_txt: bool = False) -> None:
    """Write transcript.json + transcript.txt in the local-pipeline format."""
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    with open(txt_path, "w", encoding="utf-8") as f:
        for r in results:
            if timestamped_txt:
                mins, secs = divmod(int(r["start"]), 60)
                f.write(f"[{mins:02d}:{secs:02d}] {r['text']}\n")
            else:
                f.write(r["text"] + "\n")


if __name__ == "__main__":
    # Minimal CLI for ad-hoc testing: python groq_asr.py <audio> [lang]
    import argparse
    ap = argparse.ArgumentParser(description="Groq Whisper transcription (standalone)")
    ap.add_argument("media")
    ap.add_argument("--lang", default="zh")
    ap.add_argument("--prompt", default=None)
    args = ap.parse_args()
    res = transcribe_groq(args.media, language=args.lang, prompt=args.prompt)
    base = os.path.splitext(os.path.abspath(args.media))[0]
    write_outputs(res, base + "_transcript.json", base + "_transcript.txt")
    print(f"Saved: {base}_transcript.txt ({len(res)} segments)", file=sys.stderr)
