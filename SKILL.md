---
name: lecture-to-notes
description: "Turn a lecture/conference recording (video or audio: MOV/MP4/M4A/MP3/WAV) into structured vault notes via local GPU transcription + slide extraction — 演講影片, 演講音檔, 上課錄影, '整理演講', '影片轉筆記', '音檔轉筆記', or a dropped media file. Handles batch runs."
allowed-tools: Read Write Edit Bash Glob Grep Agent
---

# Lecture-to-Notes

Turn a lecture recording (video or audio-only) into structured notes. Every heavy
stage runs locally at 0 Claude tokens; Claude only does the final synthesis. This
page is the map; detail lives in `reference/`, one topic per file.

| File | What is in it |
|---|---|
| `reference/pipeline.md` | Per-stage flags, thresholds, JSON schemas, timeouts, observability |
| `reference/note-spec.md` | Note quality spec, tier scoring, width table, synthesis prompt requirements |
| `reference/segmented-mode.md` | Multi-talk workshop folders → per-segment L2/L3 + Hub + web viewer |
| `reference/multi-camera.md` | One long recording + many phone clips/photos → one timeline |
| `reference/decisions.md` | Post-mortems, benchmarks, wrong turns, VRAM measurements |

## HARD RULES

1. ==ASK the user what language the speaker(s) used== (English / Mandarin /
   bilingual code-switching) before transcribing. There is no default and
   `transcribe_video.py` exits without `--lang`. A wrong guess makes Whisper
   hallucinate Chinese from accented English and the transcript is unusable.
2. ==Never skip Stage D (VLM) or Stage E (grounding)== for speed or for a
   deadline. ==The user has not set a deadline; do not invent one.== If a stage
   really is too slow (>2 h ETA), report the ETA and ask.
3. ==Never auto-correct the transcript.== Flag suspects, let synthesis resolve
   them. Both auto-correction passes ever built were measured and retired — see
   `reference/decisions.md#asr-auto-correction`. Do not add an auto-apply mode.
4. ==Do not bypass the collapse auto-retry.== `transcribe_video.py` auto-runs
   `retranscribe_segment.py --auto` on detected token-collapse. If collapses
   survive that, escalate (wider beam + `--no-repeat-ngram-size` + a glossary),
   never skip.
5. ==The VLM does not do OCR.== Stage D asks only for semantic signals. Text
   comes from Stage B `quick_text`, Stage B2 `clean_text`, or `pdf_text.json`.
   `ocr.vlm_text` is an always-empty compatibility field.
6. ==Serialize all GPU work.== Whisper and the VLM may not run concurrently on an
   8 GB card, and frame extraction must not run alongside transcription.
7. ==On an 8 GB card, never exceed `--batch-size 4` with `--beam-size 10`==, and
   never combine `--beam-size 15` with sequential mode — that crashes
   (`0xC0000005`). The measured sweet spot is `--batch-size 3 --beam-size 10`.
8. ==Director batch dispatch==: for a multi-lecture batch, dispatch Steps 1–9 as
   one subagent and Step 10 synthesis as a separate fresh subagent, spawned only
   after `slides_grounded.json` exists. A single subagent bounces during the long
   GPU waits and burns 30+ min of wall time per lecture.
9. ==🚫 PHI red line==: if the recording contains patient-identifiable content
   (case discussion, ward rounds, named patients), transcribe LOCAL ONLY — drop
   `--engine groq`. When unsure, ask; default to local.

## Input types and routing

==Start here. `route_inputs.py` is the front door== — it classifies a folder and
prints the ordered commands plus the questions a human must answer. It is
plan-only: it never runs anything and never writes a file.

```bash
python <skill-dir>/scripts/route_inputs.py <material_dir> [--recursive] [--out-dir DIR] [--json]
```

| What is in the folder | Slide source | Route |
|---|---|---|
| Video, no deck | frames from the video | Path A — Steps 5–7 |
| Audio/video **+ PDF deck** (==preferred==) | PDF text + page renders | Path B — `build_slides_from_pdf.py` |
| Audio/video **+ loose slide images** (≥3) | the images themselves | Path B-images — `build_slides_from_images.py` |
| Audio only, no deck | none | Path C — transcript-only note |
| N-up handout PDF | cropped tiles | Path B-multi — `crop_multiup_pdf.py` first |
| **Multi-talk workshop folder** | per segment | `reference/segmented-mode.md` |
| One long recording + many phone clips/photos | per source | `reference/multi-camera.md` |
| `.pptx` / `.docx` / `.key` | — | convert to PDF yourself first; there is no conversion step here |

==Multi-source contract==: when two or more independent sources are present, run
`media_capture_index.py --emit-alignment alignment.json` first.
==Capture timestamps are HYPOTHESES; transcript cross-correlation
(`xcorr_media_offsets.py`) is EVIDENCE.== A source whose `reliable` flag is false
got its start from mtime or has none — it must not be aligned on. Nothing is ever
auto-corrected: a claimed-vs-measured disagreement >5 s is flagged
`"conflict": true` for a human to judge. Details in `reference/multi-camera.md`.

## Pipeline

One command plus its purpose per step; flags, thresholds and outputs are in
`reference/pipeline.md`.

### Step 1 — Ask the language (mandatory, no command)

English / Mandarin / bilingual? Accented speakers? Code-switching mid-sentence?
Use AskUserQuestion if the user has not said. HARD RULE 1.

### Step 2 — Set up the lecture directory

One directory per lecture holds every intermediate; name it
`{date}_{speaker}_{topic}`, the shape `finalize_to_vault.py` parses.

### Step 3 — GPU pre-flight

```bash
python <skill-dir>/scripts/gpu_check.py --out-dir "$OUT_DIR" --min-free-mb 6000
```
Gate before transcription and again before Stage D. Exit `0` proceed, `1` warn
and proceed, `2` blocked — surface it, ==do not retry in a loop==.
→ `reference/pipeline.md#gpu-check`

### Step 4 — Transcribe

```bash
python <skill-dir>/scripts/transcribe_video.py "<media>" \
    --output-dir "$OUT_DIR" --lang <zh|en|bilingual|auto> \
    --batch-size 3 --beam-size 10
```
Local faster-whisper by default; `--engine groq` is an optional offload (HARD
RULE 9). Default model alias is `breeze25` (needs a local model dir); on a
machine without one, pass `--model large-v3`, which faster-whisper downloads.
Recordings over ~30 min go through the chunked runner instead.
→ `reference/pipeline.md#transcription`

### Step 5 — Stage A: frame extraction (Path A only)

```bash
python <skill-dir>/scripts/extract_slides.py "<video>" --output-dir "$OUT_DIR" --interval 15
```
Writes `slides/frame_NNNN.jpg` + `slides/timestamps.json`, phash-deduping adjacent
near-identical frames. Path B/B-images skip this. → `reference/pipeline.md#stage-a`

### Step 6 — Stage B: quick OCR + entropy (Path A only)

```bash
python <skill-dir>/scripts/quick_ocr.py "$OUT_DIR"
```
RapidOCR on every frame → `slides_raw.json`. ==Required==: without it every slide
looks decorative to the Stage D gate. → `reference/pipeline.md#stage-b`

### Step 6-alt — Path B / B-images bridge

```bash
python <skill-dir>/scripts/build_slides_from_pdf.py    "$OUT_DIR"   [--audio-duration-sec N]
python <skill-dir>/scripts/build_slides_from_images.py "<img_dir>" -o "$OUT_DIR" [--audio-duration-sec N]
```
Either bridge emits `slides_raw.json` + `slides_dedup.json` directly, replacing
Steps 5–7. → `reference/pipeline.md#path-b`

### Step 7 — Stage C: semantic dedup (Path A only)

```bash
python <skill-dir>/scripts/dedup_semantic.py "$OUT_DIR"
```
Merges adjacent frames by text-subset or layout similarity, marks
`dedup.is_canonical`. Output `slides_dedup.json`.
→ `reference/pipeline.md#stage-c`

### Step 8 — Stage B2: high-quality OCR (Surya)

```bash
python <skill-dir>/scripts/ocr_surya.py "$OUT_DIR" [--resume]
```
Surya in its own venv on canonical text-bearing slides, RapidOCR as the shallow
fallback. Adds `ocr.clean_text` / `ocr_engine` / `ocr_confidence`. ==Updates
`slides_dedup.json` in place== (one-time backup `slides_dedup.pre_b2.json`) and
writes `slides_ocr.json`. Path B skips it — `pdf_text` is already clean. Without
a Surya venv it warns and routes everything to RapidOCR rather than failing.
→ `reference/pipeline.md#stage-b2`

### Step 9 — Stage D: VLM signals

```bash
python <skill-dir>/scripts/vlm_signals.py "$OUT_DIR" --model minicpm-v:8b --num-ctx 4096
```
Semantic signals per canonical slide, behind a 4-condition pre-skip gate for
decorative frames. Re-check the GPU first (Step 3). Output `slides_vlm.json`.
`scripts/ocr_slides.py` is a deprecated shim forwarding here, same argv and
outputs. → `reference/pipeline.md#stage-d`

### Step 10 — Stage E: transcript grounding

```bash
python <skill-dir>/scripts/ground_slides.py "$OUT_DIR"
```
Pure Python, 0 LLM calls. Ties each canonical slide to the words spoken over it.
Output `slides_grounded.json` — the input to synthesis.
→ `reference/pipeline.md#stage-e`

### Step 11 — Flag suspect ASR tokens

```bash
python <skill-dir>/scripts/flag_asr_suspects.py --dir "$OUT_DIR"
```
Runs HERE, after Stage E: the slide glossary it needs comes from
`slides_grounded.json`. Writes `asr_suspects.txt`; ==the transcript is left
byte-identical==. Treat each line as a question, never a substitution.
→ `reference/pipeline.md#asr-suspects`

### Step 12 — Chunked pre-summarization (long lectures only)

Over ~30 min / 25 k tokens of transcript, offload chunk summaries to a Sonnet
subagent instead of reading the whole transcript into main context. Coverage
guards (`[CHUNK_END]`, `[CONTINUE_NEEDED]`, expected-chunk count) are mandatory.
→ `reference/pipeline.md#chunked-summarization`

### Step 13 — Stage F: synthesis (Claude)

Two passes for batches and long lectures — ==Tier-pass then Write-pass==:

- **Tier-pass subagent** reads `slides_grounded.json` + `transcript.txt` +
  `pdf_text.json`, applies the tier scoring rules, writes **only**
  `slides_final.json` (integer `tier`, `attachment_name`, `embed_width`,
  `section_suggestion`). This file is the frozen tier authority.
- **Write-pass subagent** reads the frozen `slides_final.json` + transcript +
  slide text, writes `note_draft.md` with `[[EMBED sN]]` placeholders only — no
  paths, widths or callouts.

One pass is fine for one short lecture; splitting them stops the writer from
simplifying structure to make its own embed audit pass. → `reference/note-spec.md`
(mandatory: quality spec, tier rules, prompt requirements)

### Step 14 — Render, finalize, audit

```bash
python <skill-dir>/scripts/render_embeds.py    "$OUT_DIR" --note note_draft.md --in-place
python <skill-dir>/scripts/finalize_to_vault.py "$OUT_DIR" [--vault-root PATH]
python <skill-dir>/scripts/audit_note.py "<note path>" --mode lecture --grounding "$OUT_DIR"
```
`render_embeds.py` expands placeholders to col-0 callouts with path + width and
audits Tier-1/2 coverage; `finalize_to_vault.py` copies cited slides + the note
into the vault; the auditor is the gate. ==Always pass `--grounding`== — without
it the caption↔frame check only warns. → `reference/note-spec.md`
**Draft review exemption**: this output is machine-transcribed and synthesized —
write to the inbox without showing a draft; the user reviews in Obsidian.

### Step 15 — Web viewer export (default whenever there is video)

```bash
python <skill-dir>/scripts/build_single_talk_web.py "$OUT_DIR" --plan seg_plan.json
# … Stage F writes the L3/ files … then:
python <skill-dir>/scripts/build_single_talk_web.py "$OUT_DIR" --plan seg_plan.json --export
```
A single talk gets the synced HTML viewer by default, not just workshops. Write
a segment plan (JSON list: seg/start/end/slug/title_zh) — ==content sections =
more segments, not more headings==, the viewer builds exactly two chapters per
segment. The script assembles manifest + `segments.json` + L2 slices + HUB
skeleton from `transcript.json`; Stage F then writes one `L3_segNN_<slug>.md`
per segment (==image embeds by basename only, timecodes `` `(V1 MM:SS)` ``==);
`--export` runs `export_web.py`. Compression default is ==H.265 CRF 24==
(self-use; `--codec h264` when sharing to machines you can't verify).
Multi-talk workshops keep their own flow → `reference/segmented-mode.md`.
→ `reference/pipeline.md#web-export`

## Edge cases

- **Audio only, no deck** → transcript-only note using `# 逐段筆記` instead of
  `# 逐投影片筆記`.
- **N-up handout PDF** → render one mid page and ==look at it== before deciding
  the grid; heuristics are unreliable on slide-heavy PDFs. Then
  `crop_multiup_pdf.py <pdf> <out_dir> --expected-rows R --expected-cols C`.
  Pages that are genuinely 1-up (title pages) are handled per page, not forced
  into the consensus grid.
- **Very long lecture (>90 min)** → chunked runner for transcription, Step 12 for
  reading it. **Batch of recordings** → transcribe strictly sequentially; ~4.5 GB
  RAM per faster-whisper instance.
- **One talk split across several files** → one note, not several.
- **Slides English, speaker Mandarin** → keep both; the deck gives terms, the
  transcript gives the explanation.
- **Speaker asked not to be recorded** → exclude that content.
- **Dense text handout, not a slide deck** → primary source, but drop the
  slide-by-slide structure.
- **CJK path failures** (exit 127 / 3221226505) → extract audio with ffmpeg
  separately first; the script reuses a validated `audio.wav`.

## Optional infrastructure

Everything below is ==this machine's setup, not a requirement== — nothing in the
pipeline depends on any of it, and the generic alternative is inline.

| Used for | Generic alternative |
|---|---|
| `job_runner.py` wrapping long GPU jobs (tree-kills children on timeout) | plain `timeout <n> <cmd>`, or run in the foreground |
| `gpu_lease.py` / a pause-flag file between concurrent batches | run GPU stages one at a time; leave `paths.pause_flag` empty in config |
| `vault-search` / OpenEvidence / Zotero lookups during synthesis | skip; cite only what the lecture itself provided |
| `ntfy` completion pings | skip |
| external batch control plane (`run_queue`, `rerun_batch`, `clip_order`, dashboard) | course-specific, not shipped — see `reference/segmented-mode.md` |

Vault paths (`99Attachment/lecture_{slug}`, the inbox folder) are ==a private vault
convention== and configurable: `render_embeds.py --attach-root` / `--attach-dir`,
`finalize_to_vault.py --vault-root`.

## Dependencies

```bash
ollama pull minicpm-v:8b               # ~5.5 GB Q4_K_M, Stage D
pip install rapidfuzz rapidocr-onnxruntime scikit-image pyyaml json_repair pillow numpy
```
`ffmpeg` + `ffprobe` on PATH. Surya (Stage B2) lives in its own venv; point
`ocr_engine.surya_python` at it in `config.yaml`, or leave it blank to fall back
to RapidOCR. Copy `config.example.yaml` → `config.yaml` on a new machine; every
machine-specific value there is blank by default and env-overridable.
==Optional dependencies degrade loudly, not silently== — a missing scikit-image or
RapidOCR is reported and gated, because a silent degrade produced *wrong* output
rather than less output (`reference/decisions.md#optional-dependency-degradation`).

## File locations

```
<skill-dir>/
├── SKILL.md, config.yaml, config.example.yaml
├── reference/  pipeline.md · note-spec.md · segmented-mode.md · multi-camera.md · decisions.md
├── data/       real_words.txt, real_acronyms.txt   (regenerated, not committed)
├── ocr_bench/  engine benchmark harness (bring your own fixtures)
└── scripts/
    ├── route_inputs.py           front door — classifies material, prints the plan
    ├── transcribe_video.py  retranscribe_segment.py  gpu_check.py  groq_asr.py
    ├── extract_slides.py  quick_ocr.py  dedup_semantic.py  ocr_surya.py
    ├── vlm_signals.py            (ocr_slides.py = deprecated shim → here)
    ├── build_slides_from_pdf.py  build_slides_from_images.py  crop_multiup_pdf.py
    ├── ground_slides.py  flag_asr_suspects.py  make_glossary.py  build_real_words.py
    ├── render_embeds.py  finalize_to_vault.py  audit_note.py  export_web.py
    ├── media_capture_index.py  xcorr_media_offsets.py  query_near_field.py
    ├── adapters/    surya_adapter.py        (production OCR adapters)
    ├── batch/       build_L1 · split_segments · split_L1_by_segment · add_dhash ·
    │                vlm_cache · detect_language · detect_language_audio ·
    │                phi_mask · process_slide_deck     (generic batch layer)
    ├── layout2/     viewer.css, viewer.js   (web viewer assets, edited verbatim)
    └── _common.py  _log.py  _paths.py
```

Per-lecture output directory:

```
{lecture}/
├── metadata.json          run_id + media fingerprint + per-stage status
├── transcript.json/.txt   timestamped segments; .txt is [MM:SS] text, H:MM:SS past an hour
├── asr_suspects.txt       flagged tokens — flags only, never a rewrite
├── alignment.json         multi-source capture-start hypotheses (when applicable)
├── slides/                frame_NNNN.jpg | page_NN.jpg | original photo names
├── slides_raw.json        Stage B    quick text + density + entropy
├── slides_dedup.json      Stage C    canonical markers (Stage B2 updates in place)
├── slides_dedup.pre_b2.json          one-time pre-Stage-B2 snapshot
├── slides_ocr.json        Stage B2   Surya result, for inspection
├── slides_vlm.json        Stage D    VLM signals + vlm_skip + skip_metrics
├── slides_grounded.json   Stage E    transcript grounding + retrieval fields
├── slides_final.json      Stage F    tier + score + attachment_name + width
└── logs/progress_*.jsonl  per-stage event streams (not every stage emits one)
```

Each stage output is a superset of the previous, so you can re-run one stage
without redoing transcription or frame extraction. `runs.jsonl` one level up
carries one summary line per stage run, joined by `run_id`. Keep the intermediates
— they are how tier decisions get debugged.
