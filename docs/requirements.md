# Requirements in full

Long version of the README's Requirements section: GPU and CPU paths, hosted-ASR offload, and Apple Silicon notes.

Honest version:

- **Tested on Windows 11** with an NVIDIA RTX 3070 Ti (8 GB). Nothing is
  Windows-specific by design, but Linux/macOS are untested.
- **Python 3.12**
- **An NVIDIA GPU (8 GB+) is the fast path, not a requirement.** Without one:
  - **CPU fallback is built in** — transcription drops to int8 on CPU
    automatically and prints an honest ETA first (a 60-minute lecture takes
    hours instead of minutes). The OCR triage stage is CPU-friendly already.
  - **A small GPU (2–4 GB) counts as no GPU.** `large-v3`-class models need
    ~4.5 GB; the pre-flight reports `GPU_TOO_SMALL` and lets the CPU path run.
    Make it bearable with a smaller model —
    `transcribe_video.py ... --device cpu --model small` (or `medium`) — or
    offload with `--engine groq` below. Skip Stage D (`minicpm-v:8b`) and
    Surya OCR on such a machine; RapidOCR is CPU-only anyway.
  - **Offload transcription to a hosted Whisper** — `scripts/groq_asr.py`
    sends compressed audio to Groq's `whisper-large-v3-turbo` (free tier
    works; the 25 MB request cap is handled by chunking) and returns the same
    segment format as the local path. Read its docstring first: hosted Whisper
    has no anti-collapse knobs for heavily code-switched audio, and anything
    you send leaves your machine — don't route confidential recordings there.
  - **Offload transcription to Gemini** — `scripts/asr_gemini_primary.py`
    makes Gemini the transcript and keeps a local breeze25 pass as the clock
    and as a second source to check names and numbers against (Gemini is an
    LLM: it swaps names and occasionally contradicts a slide). On 15 course
    clips Gemini hit 97–100 % of reference terms vs 64–85 % for the best local
    model per category; on far-field hall audio 82 % vs 54–60 %. Two routes, set by
    `gemini_asr.py --via`:
    - `api` (default) — REST API with `GEMINI_API_KEY`; usage is billed to
      that key.
    - `agy` — the Antigravity CLI in headless mode (Windows + WSL Ubuntu only),
      on the quota of the Google account it is signed into. **Read the terms-of-service note in
      `gemini_asr.py` first.** As relayed on the Google AI Developers Forum
      (2026-10-16), Google's FAQ says headless `agy` inside third-party coding
      agents is not supported on consumer plans (including Pro), even when
      the official binary is called as a local subprocess, and can lead to
      account suspension; direct scripts and CI are described as allowed.
      Whether a script that an AI agent launches counts as "direct" is not
      settled. Check the current FAQ; if in doubt, use `api`.
    Audio leaves your machine on both routes — non-confidential recordings
    only. `LECTURE_LOCAL_ONLY=1` (or `privacy.local_only: true` in
    `config.yaml`) makes the Groq and Gemini routes exit before anything is
    uploaded; no stage ever falls back from local to cloud on its own.
  - **Bring a transcript from elsewhere** — MLX Whisper, a hosted STT service,
    creator captions, a human transcript: `scripts/import_transcript.py`
    converts SRT, WebVTT or whisper-style JSON into the pipeline's
    `transcript.json` ([contract](docs/transcript-contract.md)) and Stage 0 is
    skipped.
  - **Apple Silicon** should work in CPU mode (faster-whisper on CPU, ollama
    is native on macOS), or with MLX Whisper's output imported as above —
    plausible but untested; there is no Apple Silicon resource profile in the
    GPU pre-flight yet. Reports welcome.
- **`ffmpeg` and `ffprobe` on PATH** — not optional.
- Optional: [ollama](https://ollama.com) with `minicpm-v:8b` for Stage D slide
  semantics; a separate venv with [Surya](https://github.com/VikParuchuri/surya)
  for high-quality OCR; `pandoc` for the web/PDF export.

On an 8 GB card, GPU stages must be serialized — Whisper and the VLM cannot run
concurrently, and frame extraction must not run during transcription. Run
transcription with its bare defaults (`--lang` only): an n=15 sweep found
`--beam-size 10` lost recall at 1.7× the time. Larger beams/batches are for the
anti-collapse retry only, and `--beam-size 15` with sequential mode crashes.
