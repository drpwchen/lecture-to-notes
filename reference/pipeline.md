# Pipeline reference — flags, thresholds, schemas, timeouts

Per-stage detail for `SKILL.md`. Everything here was read off the scripts'
current argparse and docstrings; where a flag is not listed, it does not exist.
Rationale and post-mortems live in `decisions.md`.

## Contents

- [Routing — route_inputs.py](#routing)
- [GPU pre-flight](#gpu-check)
- [Transcription](#transcription)
- [Collapse rescue](#collapse-rescue)
- [Stage A — frame extraction](#stage-a)
- [Stage B — quick OCR](#stage-b)
- [Path B / B-images / B-multi bridges](#path-b)
- [Stage C — semantic dedup](#stage-c)
- [Stage B2 — Surya OCR](#stage-b2)
- [Stage D — VLM signals](#stage-d)
- [Stage E — transcript grounding](#stage-e)
- [ASR suspects](#asr-suspects)
- [Chunked pre-summarization](#chunked-summarization)
- [Stage F post-processing](#stage-f-post)
- [Multi-source alignment](#alignment)
- [JSON schemas](#schemas)
- [Timeouts](#timeouts)
- [Observability](#observability)

---

## Routing {#routing}

`route_inputs.py <dir> [--recursive] [--out-dir DIR] [--json]`

Classifies files by extension into video / audio / pdf / image / convert-first /
other, then prints an ordered plan: the required questions, one numbered step per
command with its rationale, and warnings. ==Plan only — it never executes and
never writes.== Loose images count as a deck only at ≥3 of them.

Two things it deliberately refuses to default: the transcription language, and
the capture-time alignment whenever two or more independent sources are present.

- Multiple speech sources → it emits one ASR sub-directory per source
  (`<out>/asr/<stem>/`), because they all write `transcript.json` into their
  output dir and a shared dir means each run silently overwrites the last.
- It prints `flag_asr_suspects` right after transcription; the glossary that step
  wants comes from Stage E, so in practice run it at Step 11 (it uses whichever
  of `slides_grounded.json` / `slides_vlm.json` / `slides_final.json` /
  `pdf_text.json` already exist).

## GPU pre-flight {#gpu-check}

`gpu_check.py [--out-dir DIR] [--min-free-mb 5000] [--util-threshold 40]
[--polls 3] [--poll-interval 2.0] [--alloc-fraction 0.8] [--alloc-cap-mb 0]
[--skip-alloc-test] [--quiet] [--json]`

Blocks (exit 2) when any of these holds across ==all== polls:

| Check | Default |
|---|---|
| free VRAM below threshold | 5000 MB (`--min-free-mb`); use 6000 before batched Whisper |
| another process holds >1 GB **and** GPU util above threshold | 40 % (`--util-threshold`) |
| CUDA allocation test fails | `cuMemAlloc` via ctypes/nvcuda, torch fallback; catches fragmentation that free-MB numbers miss |

Grace period: 3 polls at 2 s (`--polls`, `--poll-interval`); a transient util
spike does not kill the run. Exit `0` ok, `1` warning (another process owns >1 GB
but is idle — proceed and log), `2` blocked. Emits
`logs/progress_gpu_check.jsonl` when `--out-dir` is set.

## Transcription {#transcription}

`transcribe_video.py <media_path> --lang {zh,en,bilingual,auto} [options]`

| Flag | Default | Notes |
|---|---|---|
| `--lang` | ==none — required== | no default on purpose; wrong language = catastrophic collapse |
| `--language` / `-l` | none | legacy alias kept for old invocations |
| `--output-dir` / `-o` | the media file's own directory | |
| `--model` | `breeze25` | alias from `config.yaml models.aliases`, or any path / HF hub name |
| `--compute-type` | `float16` | |
| `--beam-size` | `5` | 10 for accented / domain-heavy speech (~30 % slower) |
| `--batched` / `--no-batched` | batched ON | ==but batching only engages when `--vad` is also passed==, because `BatchedInferencePipeline` requires `vad_filter=True` and VAD has been off by default since 2026-07-12. With default flags this runs SEQUENTIAL. |
| `--batch-size` | `4` | safe on 8 GB with beam 10; on CUDA OOM it retries once at half, then falls back to sequential |
| `--vad` | off | VAD was eating quiet speech |
| `--condition` / `--no-condition` | conditioning ON | `--lang bilingual` forces `--no-condition` |
| `--initial-prompt` / `--initial-prompt-file` | none | glossary to bias decoding (≤~150 words) |
| `--keep-audio`, `--word-timestamps` | off | |
| `--engine` | `local` | `groq` is an ==optional== hosted offload, not the default |
| `--retry-timeout` | none | seconds for the auto-retry child process |

**Models.** `breeze25` is the default alias and resolves through
`models.aliases` in `config.yaml`, which expands `{whisper_model_dir}` from
`paths.whisper_model_dir` or `LECTURE_WHISPER_MODEL_DIR`. On a machine with no
local model directory that alias is unusable — pass `--model large-v3`, which
faster-whisper downloads from the hub. `config.example.yaml` ships
`whisper_model_dir: ''`; a concrete path is one machine's local value, never a
shipped default.

**Language modes.** `zh` Chinese model, accepts inline English domain terms · `en`
English model · `bilingual` Chinese model with deterministic decode
(`condition_on_previous_text=False`, `temperature=[0.0, 0.2]`, VAD
`min_silence_ms=800`) for code-switching · `auto` experimental, unstable on mixed
audio, logs the probability vector to `runs.jsonl`, not for production.

**Groq offload.** `--engine groq` sends audio to a hosted `whisper-large-v3-turbo`
and frees the local GPU. It auto-runs collapse detection on the result and falls
back to local faster-whisper on collapse or on any Groq failure. ==PHI red line —
local only for patient-identifiable content.== Absent API key falls back to local
automatically. The `--initial-prompt-file` glossary must stay under ~224 tokens
(~150 CJK chars) or every chunk returns 400 and it silently falls back; keep a
separate trimmed prompt file for Groq.

**Long recordings.** Over ~30 min, do not call this script directly — use the
chunked runner, which cuts the audio into ~10 min pieces, transcribes each in its
own process, and merges with time offsets into the same
`transcript.json`/`transcript.txt` schema. Re-running resumes: chunks that
already have a `transcript.json` are skipped.

**`transcript.partial.json` checkpoint (do not remove).** `transcribe_video.py`
checkpoints results every 50 segments via atomic temp+replace. ==This file has an
external consumer==: the chunked runner promotes the checkpoint after an abort,
cuts the audio from the last decoded timestamp, and re-runs until the chunk is
covered. Deleting the write breaks crash-resume. Background:
`decisions.md#ctranslate2-abort`.

**`transcript.txt` format.** One line per segment, `[MM:SS] text`, widening to
`[H:MM:SS]` past one hour. The text is stripped — a single shared writer, so a
retry no longer makes the whole file diff-noisy.

**VAD parameters** (when enabled): `min_speech=3000 ms`, `min_silence=800 ms`,
`speech_pad=400 ms`. ==Don't push `min_speech` to 4000== — short Q&A utterances
over-merge.

## Collapse rescue {#collapse-rescue}

`retranscribe_segment.py --dir DIR --language LANG [options]`

| Flag | Default | Notes |
|---|---|---|
| `--dir` | required | lecture output dir |
| `--language` | ==required== | no default; a missed flag used to decode Chinese audio as English |
| `--audio` | `<dir>/audio.wav` | |
| `--model` | none → config/parent-run model | keeps the patch on the same model as the parent transcription |
| `--auto` | off | detect collapse ranges automatically |
| `--dry-run` | off | report ranges, change nothing |
| `--start` / `--end` | none | manual range, `MM:SS` or seconds |
| `--slides` | none | e.g. `35-43`, pulls the glossary from that slide range |
| `--glossary` | none | manual glossary string |
| `--beam-size` | `15` | |
| `--repetition-penalty` | `1.5` | |
| `--no-repeat-ngram-size` | `3` | |

The anti-collapse recipe is all six knobs together — `condition_on_previous_text=False`,
repetition penalty, n-gram ban, temperature fallback `[0.0, 0.2, 0.4, 0.6, 0.8]`,
tighter VAD, wide beam. When the model is *locked into* a loop rather than
propagating one, `--no-condition` alone reproduces the same collapse.

Collapse symptoms in a raw transcript: ≥10 consecutive ~1.0 s segments of 1–3
words; a phrase looping; long runs of bare punctuation; a whole topic missing
that the slides clearly cover. This is distinct from code-switching damage —
collapse also happens on pure accented English.

Timestamped backups protect `transcript.json`; an empty replacement list no
longer deletes the range it was meant to repair, and merging is idempotent.

## Stage A — frame extraction {#stage-a}

`extract_slides.py <video_path> [--output-dir DIR] [--interval 15]
[--hash-threshold 40] [--group-drift-threshold N] [--ffmpeg-timeout 600]`

Samples at `fps=1/interval` and phash-dedupes adjacent near-identical frames on a
center crop (ignores letterbox / projector borders). Outputs
`slides/frame_NNNN.jpg` + `slides/timestamps.json`.

==Frame timestamps start at t=0, not at t=interval== — ffmpeg's first sampled
frame is the one at zero. The old assumption shifted every slide +15 s and
mis-grounded every Path A run; see `decisions.md#frame-timestamp-offset`.

`--group-drift-threshold` bounds chained dedup so an animated build-up sequence
cannot collapse dozens of genuinely different frames into one. `--ffmpeg-timeout`
is enforced and reported rather than raising a bare traceback.

## Stage B — quick OCR {#stage-b}

`quick_ocr.py <out_dir> [--force]`

RapidOCR (CPU, no CUDA) on every frame, ~0.5 s each. Writes into
`slides_raw.json`:

| Block | Field | Meaning |
|---|---|---|
| `ocr` | `quick_text` | full visible text, top→bottom reading order |
| `ocr` | `quick_title_guess` | largest-font top-band line, ≤60 chars |
| `ocr` | `ocr_text_density` | ==bbox area / image area== |
| `ocr` | `model_confidence` | mean RapidOCR confidence |
| `entropy` | `file_size_kb` | text slides compress, noise does not |
| `entropy` | `laplacian_variance` | 3×3 Laplacian on 512 px-wide grayscale, so 1080p and 4K compare |
| `entropy` | `quick_text_len` | convenience for the Stage D gate |

==Entropy metrics are computed for every slide==, regardless of any later skip
decision — keeping the raw numbers lets the Stage D thresholds be re-tuned
without re-running OCR.

RapidOCR missing is ==not== a silent success: the stage reports it and gates,
because an empty `quick_text` everywhere makes Stage D pre-skip nearly every
slide as decorative and produce a confident, empty note.

## Path B / B-images / B-multi bridges {#path-b}

**PDF deck.** `build_slides_from_pdf.py <lecture_dir> [--audio-duration-sec N]
[--density-mode ocr|none] [--density-budget-sec 240]`

Consumes `pdf_text.json` (both historical schemas) and emits `slides_raw.json` +
`slides_dedup.json` with every page canonical, so Stage D/E run unchanged.

**Loose slide images.** `build_slides_from_images.py <img_dir> -o <out_dir>
[--audio-duration-sec N] [--ext .jpg,.png] [--density-mode ocr|none]
[--density-budget-sec 240] [--utc-offset 8]`

Same schema, plus a superset of fields: `order_source` (which signal ordered this
image), `capture_time`, and `ocr_error` when density measurement failed. Images
are COPIED into `<out_dir>/slides/` under their ==original filenames== — no
`frame_NNNN` renaming, so a human can match a note's slide back to the photo they
took. Nothing is moved or deleted.

Ordering is EXIF `DateTimeOriginal` first (the same probe
`media_capture_index.py` uses, imported from it), then file mtime, then
natural-sorted filename. ==mtime is a hypothesis, not evidence== — a zip / Drive /
photo-library round-trip rewrites it, and falling back to it prints a warning to
verify the order visually. When the chosen order disagrees with plain filename
order on more than 20 % of pairs you also get a warning: that disagreement is
exactly the case where one of the two orderings is wrong.

Three invariants both bridges share, each learned from a silent failure:

1. ==`slide_id` is 1-based==, matching Path A. A 0-based bridge made every
   `[[EMBED sN]]`, attachment name and human-facing `sN` off by one.
2. ==No `--audio-duration-sec` gives `timestamp_start: 0` plus `no_audio: true`,
   never `None`.== Stage D does `int(timestamp_start)` and `int(None)` killed the
   run on slide 1.
3. ==`ocr_text_density` is MEASURED== (RapidOCR bbox area / image area), never a
   character-count proxy: the Stage D gate is calibrated on the bbox scale, so a
   fabricated scale pre-skips normal slides as decorative. With no RapidOCR the
   density is `null`, which every downstream gate reads as "unknown, therefore has
   text" — never as a reason to skip.

**N-up handout PDFs.** `crop_multiup_pdf.py <pdf_path> <out_dir> [--zoom 3.0]
[--detect-zoom 1.0] [--expected-rows N] [--expected-cols N] [--pad 0.02]
[--debug]`

==Render one mid page and look at it before deciding the grid.== Page-AR and
text-block-count heuristics are unreliable on slide-heavy PDFs, where each slide
is one image so block counts stay low even at 6-up.

- `--expected-rows` / `--expected-cols`: give them when visual inspection tells
  you the grid; detection retries thresholds until the count matches. Without
  hints it picks the most plausible grid from 1–4 × 1–4. A hinted grid that
  detection cannot find is reported, not crashed on.
- `--pad` fractional outer padding per cell; ==bump to 0.03+ if slide-bottom
  citations get clipped==.
- `--zoom 3.0` gives ~600 px tall per 6-up cell, enough to read most text. Don't
  lower it.
- Pages that are genuinely 1-up (title pages) are handled as one slide rather
  than forced through the consensus grid — that mis-slice used to inject four
  fake slides per title page into the whole pipeline.

Output: `slide_NN.jpg` (1-indexed across all pages) + `_crop_meta.json` mapping
each slide to (page, row, col, `page_text_full`). Very sparse citations isolated
in whitespace can still fall under the ink-band threshold — re-crop that page
with a larger `--pad`, or accept the loss.

## Stage C — semantic dedup {#stage-c}

`dedup_semantic.py <out_dir> [--force] [--text-threshold 88]
[--layout-threshold 0.85] [--max-gap-seconds 60] [--max-span-seconds N]
[--config PATH]`

Merges an adjacent frame into the next when its `quick_text` is a subset of the
next's (overlap ≥ `--text-threshold`) OR layout similarity (fused SSIM +
histogram) exceeds `--layout-threshold`, within `--max-gap-seconds`.
`--max-span-seconds` caps how long a single merged group may run — use it on
static-screen footage where a group would otherwise swallow minutes.

Per-slide `dedup` block: `is_canonical` (only canonical slides reach Stage D),
`superseded_by`, `semantic_group`, `ssim_to_prev`, `histogram_similarity`,
`ocr_overlap_ratio`, `layout_similarity`. Output `slides_dedup.json`.

`dedup.ui_chrome_tokens` in `config.yaml` lists screen-share interface text (and
its common OCR misreadings) that pollutes `quick_text` on conference recordings;
any `quick_text` line containing one is dropped before line length picks a
group's canonical frame, so the frame with the most UI noise doesn't win over the
one with the most slide content. Add Teams/Meet/WebEx chrome there rather than in
code.

==scikit-image is a hard requirement for correctness, not an optimization.==
Without it the layout comparison used to degrade to a plain grayscale histogram,
which scores same-template white slides at ~0.95 and collapsed 120 slides into
~5. The missing dependency is now reported and gated.

**Open item (2026-08-02, unresolved).** Even with scikit-image installed, the
merge condition `text_subset OR layout > 0.85` still over-merges synthetic decks
that are mostly whitespace on one template. Synthetic fixtures are not enough to
convict real slides; tightening the threshold or switching to AND needs an A/B on
real course data first.

## Stage B2 — Surya OCR {#stage-b2}

`ocr_surya.py <out_dir> [--config PATH] [--surya-python PATH]
[--conf-threshold F] [--timeout SECONDS] [--resume]`

Runs after Stage C (which marks canonical) and before grounding. Routing uses
Stage-B signals only — no VLM needed:

| Slide signal | Route |
|---|---|
| `ocr.source == "pdf"` | keep the PDF text, skip OCR (Path B, no rework) |
| `quick_text_len < 12` AND `density < 0.03` | skip OCR — pure imaging / photo / decorative |
| has text | **Surya** — dense text, tables, algorithms |
| Surya error OR `page_confidence < 0.60` | **RapidOCR fallback** — handwritten, low quality |

Thresholds come from `config.yaml ocr_engine`: `skip_no_text.quick_text_len_max`
(12), `skip_no_text.density_max` (0.03), `confidence_threshold` (0.60).
==The fallback tree is deliberately shallow (Surya → RapidOCR)== — no ensemble,
voting, or multi-layer retry.

Interpreter resolution: `--surya-python` → env `LECTURE_SURYA_PYTHON` →
`config.yaml ocr_engine.surya_python`. ==A missing or non-existent interpreter is
not fatal==: the stage warns loudly and routes every slide to RapidOCR, so a
machine without Surya still gets Stage B2 text. The production adapter is
`scripts/adapters/surya_adapter.py` — production must not depend on the benchmark
tree.

Adds three minimal fields per canonical slide's `ocr` block:
`clean_text`, `ocr_engine` (`surya | rapidocr_fallback | pdf_text |
skipped_no_text | quick_text_fallback`), `ocr_confidence`.

Outputs:

| File | Role |
|---|---|
| `slides_dedup.json` | ==updated in place, atomically== — this is what Stage D/E read |
| `slides_dedup.pre_b2.json` | one-time snapshot of the pre-B2 file, so a bad OCR run has a resume point |
| `slides_ocr.json` | this stage's own result, for inspection |

`--resume` skips slides already done.

## Stage D — VLM signals {#stage-d}

`vlm_signals.py <out_dir> [options]` (`ocr_slides.py` is a deprecated shim
forwarding here with the same argv, exit code and outputs — mentioned once,
because live callers still use the old path.)

| Flag | Default |
|---|---|
| `--model` / `-m` | `minicpm-v:8b` |
| `--num-ctx` | `4096` |
| `--connect-timeout` | `10` s |
| `--inference-timeout` | `120` s |
| `--no-skip-gate` | off — disables the pre-skip gate (recovery option) |
| `--max-retries` | `2` |
| `--force` | off |
| `--allow-empty` | off — see the guard below |
| `--save-every` | `20` slides |
| `--resume` | off |
| `--verbose` | off |
| `--temperature` / `--top-p` / `--top-k` / `--repeat-penalty` | `0` / `0.8` / `20` / `1.1` |
| `--num-predict` | `600` (slim prompt — no `vlm_text`) |
| `--seed` | `42` (deterministic) |

Runs on `is_canonical=true` slides, filtered further by a ==4-condition AND-gate==
that pre-skips decorative/blank slides. All four must hold to skip; any signal of
real content keeps the slide in.

| Condition | Threshold |
|---|---|
| `ocr.ocr_text_density` | < 0.05 |
| `entropy.quick_text_len` | < 20 |
| `entropy.file_size_kb` | < 80 |
| `entropy.laplacian_variance` | < 100 |

Skipped slides get `vlm_skip=true`, `skip_reason="decorative_low_entropy"`,
`vlm_signals.content_type=["decorative"]` so tier scoring demotes them naturally.
Raw `skip_metrics` are written alongside for debugging.

**Signals returned per slide** — `content_type` (multi-label list from
flowchart / ultrasound / xray / mri / anatomy / table / chart / scatter_plot /
kaplan_meier / title / text / decorative), `apparent_educational_function`
(diagnostic_algorithm / staging_schema / treatment_protocol /
anatomical_relationship / summary / decorative / evidence_table — ==inferred only
from visible structure, never guessed intent==), `visible_labels`,
`contains_measurement` / `contains_algorithm` / `contains_clinical_imaging`
(bools), `text_redundancy` / `visual_complexity` /
`non_textual_information_density` (0–1 floats), `model_confidence`.

`ocr.vlm_text` is written as `""` for every slide. ==It is an always-empty
compatibility field==, kept so older readers do not KeyError; the slim prompt
stopped asking for text. ==`vlm_error` is the diagnostic field== — read that, not
`vlm_text`, when a slide's signals look wrong.

==Clinical image rule==: for X-ray / ultrasound / MRI the prompt forbids naming
pathology. The model transcribes labels, arrows and measurements only; the
diagnosis comes from the speaker.

JSON parse failures are non-fatal — those slides get `parse_valid=false`,
`source=vlm_failed`, and fall back to Stage B text downstream.

**Empty-canonical guard.** If the canonical set is empty, the fail-ratio guard
would be vacuously satisfied (zero calls, zero failures) and the stage would
write a signal-free `slides_vlm.json` and exit 0 — exactly the silent content
loss the guard exists to prevent. An empty canonical set now stops the stage
unless you pass `--allow-empty` deliberately.

Output `slides_vlm.json`.

## Stage E — transcript grounding {#stage-e}

`ground_slides.py <out_dir> [--config PATH] [--window-seconds 20] [--force]`

Pure Python, 0 LLM calls. For each canonical slide it walks transcript segments
in a ±`--window-seconds` window and computes:

**Positive** — `speaker_reference_density` (fraction of slide keywords the
speaker said), `speaker_emphasis_score` (density of emphasis cues),
`time_spent_seconds` (dwell across the semantic group).
**Negative** — `speaker_skip_score` (high when dwell <10 s AND transcript words
<30, else `1 − reference_density`), `speaker_confusion_score` (density of skip
cues).

Keyword classification, so `ALS` does not match `all`:

| Class | Test | Match rule |
|---|---|---|
| abbreviation | ≤4 chars ALL_CAPS, or in `keyword_grounding.abbreviation_whitelist` | exact, word-boundary only |
| drug | suffix `-mab`/`-inib`/`-statin`/… | fuzzy ≥ 90 |
| medical term | suffix `-itis`/`-oma`/`-pathy`/… | fuzzy ≥ 85 |
| general | ≥5 chars | fuzzy ≥ 88 |

Short words are matched at word-start with an exact comparison and scored
sentence by sentence, rather than fuzzy-matching against the whole window — the
window-wide partial match let a substring like `motion` hit `emotional`.

Emphasis / confusion cue lists and the keyword→`clinical_domain` map are all in
`config.yaml keyword_grounding`. Output `slides_grounded.json` — the input to
synthesis. A missing transcript is an error, not a warning that returns an
empty-but-confident grounding.

Both `ground_slides.py` and `flag_asr_suspects.py` load transcripts through one
shared `load_segments()`, so a Whisper-native `{"segments": [...]}` dict and a
bare list both work.

## ASR suspects {#asr-suspects}

`flag_asr_suspects.py --dir DIR [--min-len 4] [--top 3] [--wordlist PATH]
[--acronyms PATH]`

Writes `asr_suspects.txt`: every transcript token that is neither a known-real
term nor present on the lecture's slides, with occurrence count, first segment,
and the closest slide terms as hints. ==The transcript is left byte-identical.==

Glossary sources, in order, whichever exist in the directory:
`slides_grounded.json`, `slides_vlm.json`, `slides_final.json`, `pdf_text.json`.
That is why this runs after Stage E — before Stage D only the near-empty
`pdf_text.json` exists, and for image-based decks fitz extracts almost nothing.

**How synthesis must use it**: ==treat every line as a question, not an answer.==
The hints are look-alikes and the correct term is frequently absent from the
slides, so a garbled token may resolve to a word no hint mentions. Resolve from
transcript context and domain knowledge; if a token stays unresolved, keep the
raw ASR form and mark it `⚠️`. ==Never silently substitute a hint.==

**Word lists.** `build_real_words.py [--corpus DIR] [--min-books 3]
[--files-per-book 15] [--max-bytes 300000]` builds `data/real_words.txt` +
`data/real_acronyms.txt` from a local markdown textbook corpus: a token is "real"
if it appears in at least `--min-books` distinct books. That cross-book test is
what separates a garble from a genuine term. `--corpus` is required on any
machine that does not have the builder's default corpus; without the word lists
the flagging script refuses to run rather than flagging everything.

## Chunked pre-summarization {#chunked-summarization}

For transcripts over ~30 min / ~25 k tokens. Reading an 88 k-token transcript
inflates main context and forces auto-compaction mid-synthesis. Offload chunk
summaries to a Sonnet subagent that reads `transcript.txt` for one time range
plus the matching slice of `slides_grounded.json`, and returns markdown bullets
as a string; main Claude appends them under `# 總整理`.

Per 30-min chunk the subagent returns, for each 5-min sub-chapter, 3–5 bullets of
what the speaker said, 1–2 verbatim quotes where the speaker emphasized, and the
`retrieval_keywords` anchoring each bullet to slide content.

==Triple anti-truncation guard, all three required==:

1. `[CONTINUE_NEEDED]` — the subagent self-reports running out of output budget;
   main Claude spawns a follow-up on the narrowed range.
2. `[CHUNK_END start=HH:MM:SS end=HH:MM:SS]` at the end of every chunk. After all
   chunks return, ==verify coverage==: ranges must be contiguous and span the full
   duration, no gaps, no overlaps.
3. ==Chunk-count check first==: `expected = ceil(duration_s / 1800)`. A single
   chunk claiming a huge `end=` can pass contiguity while skipping the middle 30
   minutes, so check the count before trusting the ranges.

Chapter-boundary-aware chunking is not implemented; some 5-min chapters straddle
a cut, and the `[CHUNK_END]` ranges document where.

==Reconciling this with `# 總整理`==: these chunk summaries are chronological, but
`# 總整理` is organized by clinical workflow, not by time. The chunk output is
==input material for the Write-pass, not a section of the note==. Re-order and
merge it; do not paste the chronological chapters in as-is. Saving each return to
`chapter_NN.md` in the lecture dir is useful while iterating.

## Stage F post-processing {#stage-f-post}

`render_embeds.py <lecture_dir> [--note note_draft.md] [--slug SLUG]
[--attach-root PATTERN] [--attach-dir DIR] [--in-place] [--dry-run]`

Expands `[[EMBED sN]]` / `[[EMBED sN: intent]]` placeholders into col-0 Obsidian
callouts with path, width and caption from `slides_final.json`, and audits:
a Tier-3 / suppressed / unknown slide referenced is removed and warned; a
Tier-1/2 slide never referenced is warned as a missing figure; an inline
placeholder gets a reference marker plus the callout after the line. Without
`--in-place` it writes `<note>.rendered.md`.

`--attach-root` defaults to `99Attachment/lecture_{slug}` (==this pipeline's
private vault convention==) or `paths.vault_attach_pattern` in `config.yaml`;
`{slug}` is substituted. Outside that vault layout, set it, or every embed is a
dead link. Tier parsing accepts the malformed forms that used to silently demote
a slide to tier 3 (and thus delete its figure) — `"T1 核心"`, floats, `None`.

`finalize_to_vault.py <lecture_dir> [--note-name NAME] [--speaker NAME]
[--topic TOPIC] [--date YYYYMMDD] [--vault-root PATH] [--force]
[--allow-no-refs] [--dry-run]`

Copies cited slides (tier ∈ {1,2}, not suppressed) from
`<lecture_dir>/slides/<filename>` to the attachment folder as
`<attachment_name>`, copies the note into the inbox, then audits that every
attachment reference in the note resolves to a copied file. Slug / speaker /
topic are derived from the directory name (`YYYYMMDD_..._speaker_topic`) and
overridable. Vault root comes from `--vault-root` or the `CLAUDE_VAULT_ROOT`
environment variable. `--force` allows overwriting an existing note of the same
name; ==without it, a same-named note is refused rather than silently replaced==
(two talks by the same speaker really do collide). `--allow-no-refs` is needed to
pass an audit with zero references, so an empty reference set can no longer show
up as a green light proving nothing.

==Both files matter==: `slides_final.json` entries need BOTH `filename` (the
source frame) and `attachment_name` (the copied name). `finalize_to_vault.py`
reads `filename`; `render_embeds.py` reads `attachment_name`. A missing one gives
a page of embeds pointing at files that were never copied, with a green audit.

`audit_note.py <note> [--mode generic|textbook|lecture|lecture-seg]
[--template ...] [--vault ROOT] [--grounding DIR]` — the vendored auditor. See
`note-spec.md` for what each mode enforces.

## Multi-source alignment {#alignment}

`media_capture_index.py <out.json> <DIR|LABEL=DIR> [...] [--utc-offset 8]
[--emit-alignment PATH] [--xcorr-results PATH]`

Capture-time source of truth, in order: video `com.apple.quicktime.creationdate`
(carries its own offset) → video `creation_time` (UTC; `--utc-offset` hours are
added) → photo EXIF `DateTimeOriginal`, else `DateTime`. ==Never file mtime.==

Directories are scanned with `scandir`, not `glob` — a folder named
`2026-07 [workshop]` is a character class to glob and silently matched nothing.

`--emit-alignment alignment.json` writes the hypothesis set: one row per source
with `capture_start`, `start_source` (`exif` / `ffprobe` / `mtime` / `none`),
`duration_s`, and `reliable`, which is false whenever the start came from mtime
or is absent. ==An unreliable start must never be used to align material.== It is
recorded so a human can see what is missing, not so a script can use it. The
command also prints the exact `xcorr_media_offsets.py` invocation and does not
run it — cross-correlation needs every source transcribed first, so the user
decides when to spend that.

`--xcorr-results xcorr.json` (only meaningful together with `--emit-alignment`)
compares the claimed offset against the measured one and flags any pair
disagreeing by more than 5 s as `"conflict": true` on both sources, loudly.
==Nothing is ever auto-corrected== — a conflict means one clock is lying and
which one is a judgement call. Conflicts warn; the run still exits 0.

`xcorr_media_offsets.py <out> --clip-asr DIR --recording NAME=DIR [--index PATH]
[--name-map JSON] [--n 6] [--bin 5] [--min-votes 40] [--max-gram-freq 4]` —
the measurement itself; see `multi-camera.md`.

`query_near_field.py [day] [start] [end] --asr DIR --index PATH [--map JSON]
[--grep TERM] [--ctx 2]` — look up what a near-field clip captured at a given
wall-clock time or around a keyword.

## JSON schemas {#schemas}

==Single home for every schema.== Each stage output is a superset of the
previous, so `slides_final.json` below carries every field.

```jsonc
{
  "slide_id": 12,                       // 1-based on every path
  "pipeline_stage": "final",
  "filename": "frame_0012.jpg",         // source frame — finalize_to_vault reads this
  "attachment_name": "s01.jpg",         // copied name — render_embeds reads this
  "timestamp_start": 752, "timestamp_end": 845, "time_str": "12:32-14:05",
  "no_audio": false,                    // true when built from a deck with no --audio-duration-sec
  "dedup":   { "phash_group", "semantic_group", "superseded_by", "is_canonical",
               "ssim_to_prev", "histogram_similarity", "ocr_overlap_ratio",
               "layout_similarity" },
  "ocr":     { "quick_text", "quick_title_guess",
               "vlm_text": "",          // ALWAYS EMPTY — compat field only
               "clean_text",            // Stage B2, preferred text for synthesis
               "ocr_engine": "surya|rapidocr_fallback|pdf_text|skipped_no_text|quick_text_fallback",
               "ocr_confidence",
               "source": "vlm|vlm_failed|pdf|quick|skipped|none",
               "model_confidence", "parse_valid",
               "ocr_text_density",      // bbox area / image area; null = unknown, NOT zero
               "grounding_support" },
  "entropy": { "file_size_kb", "laplacian_variance", "quick_text_len" },
  "vlm_skip": false,
  "skip_reason": "decorative_low_entropy | null",
  "skip_metrics": { "ocr_text_density", "quick_text_len",
                    "file_size_kb", "laplacian_variance" },
  "vlm_error": null,                    // the diagnostic field when Stage D failed
  "vlm_signals": { "content_type": [], "apparent_educational_function": [],
                   "visible_labels": [], "contains_measurement",
                   "contains_algorithm", "contains_clinical_imaging",
                   "text_redundancy", "visual_complexity",
                   "non_textual_information_density" },
  "transcript_signals": { "speaker_reference_density", "speaker_emphasis_score",
                          "speaker_skip_score", "speaker_confusion_score",
                          "time_spent_seconds", "transcript_segment_ids": [] },
  "retrieval": { "retrieval_keywords": [], "summary_sentence", "clinical_domain": [] },
  "combined_score": 0.78, "tier": 1,    // tier is an INTEGER, never "T1"
  "embed_width": 600,
  "embed_suppressed_reason": null,
  "tier_override_reason": "function includes diagnostic_algorithm",
  "section_suggestion": "…"             // Tier-pass only: which 總整理 section
}
```

`build_slides_from_images.py` adds a superset of three fields, in this order,
right after `filename`: `order_source` (`exif` / `mtime` / `filename`),
`capture_time` (ISO string or null), `ocr_error` (null unless density
measurement failed).

`slides_ocr.json` — Stage B2's own record, one entry per slide processed:
`slide_id`, `filename`, `clean_text`, `ocr_engine`, `ocr_confidence`, and the
error string when Surya failed. `slides_dedup.pre_b2.json` is a byte copy of
`slides_dedup.json` from before the first Stage B2 run.

`alignment.json`:

```jsonc
{
  "sources": [
    { "file", "path", "kind": "video|audio|image",
      "capture_start": "2026-07-26 07:12:30",
      "start_source": "ffprobe|exif|mtime|none",
      "duration_s": 963,
      "reliable": true,                 // false => MUST NOT be aligned on
      "conflict": false }               // true when claimed vs measured differ > 5 s
  ],
  "notes": [ "capture timestamps are HYPOTHESES; …" ]
}
```

`metadata.json` — per-lecture identity plus at-a-glance stage status: `run_id`
(UUID, bootstrapped by the first stage and reused by all others), `lecture`,
`started_at`, `hostname`, `pipeline_version`, `media` (`path`, `size_bytes`,
`mtime`, `fp_sha256_8` = first+last 64 KB hash as a cheap stable id), and a
`stages` map keyed by stage name with `started_at` / `ended_at` / `elapsed_s` /
`success` plus stage-specific counters. Writes are atomic; a stage that dies
without `stage_done` gets `aborted: true`, distinguishing "in progress" from
"died silently".

`runs.jsonl` — one line per stage run, one level up from the lecture dir, joined
to `metadata.json` by `run_id`. Grep it for trends: rerun rate, OOM fallback
frequency, retry frequency, detected-language probability drift. ==These are the
signals that justify further work; do not optimize ahead of them.==

## Timeouts {#timeouts}

Compute from duration or slide count. Round up generously.

| Stage | Formula | Example: 40 min video, ~60 frames → ~40 canonical |
|---|---|---|
| Transcription | `duration_s × 0.15 + 120` | 480 s |
| Stage A frame extract | fixed `600` (also `--ffmpeg-timeout`) | 600 s |
| Stage B quick OCR | `frames × 1 + 30` | 90 s |
| Stage B2 Surya | `canonical × 3 + 120` | 240 s |
| Stage C dedup | `frames × 2 + 30` | 150 s |
| Stage D VLM | `canonical × 8 + 60` | 380 s |
| Stage E grounding | fixed `60` (pure Python) | 60 s |
| Step 11 ASR suspects | fixed `120` (seconds in practice) | 120 s |
| Step 12 chunk summaries | per subagent call, not a shell timeout | — |

Long chunked transcription runs need a much larger budget — ~7200 s for a 2-hour
recording, because each chunk is a fresh process and an aborted chunk is retried.

## Observability {#observability}

Three layers: `<lecture>/metadata.json` (identity + stage status, one per
lecture, atomically updated), `<lecture>/logs/progress_*.jsonl` (full event
stream per stage, append-only, line-buffered for `tail -F`), and
`<lecture-parent>/runs.jsonl` (one summary line per run). Every event and summary
line carries the same `run_id`.

==Not every stage emits a progress JSONL.== Verified by which scripts import
`_log`:

| Emits `logs/progress_*.jsonl` | Does NOT |
|---|---|
| `gpu_check` → `progress_gpu_check.jsonl` | `extract_slides` (Stage A) |
| `transcribe_video` → `progress_transcribe.jsonl` | `ocr_surya` (Stage B2) |
| `quick_ocr` → `progress_quick_ocr.jsonl` | `ground_slides` (Stage E) |
| `dedup_semantic` → `progress_dedup.jsonl` | `flag_asr_suspects` |
| `vlm_signals` → `progress_vlm.jsonl` | the Path B / B-images bridges |
| `retranscribe_segment` → `progress_retranscribe.jsonl` | `render_embeds`, `finalize_to_vault`, `export_web` |

==Do not poll for a progress file a stage never writes.== For the stages in the
right column, watch the process and its output files instead.

Each event carries `ts`, `run_id`, `stage`, `event`, `status`,
`elapsed_monotonic_s`, plus stage-specific fields. `status` is one of
`running | success | error | retry`, so monitors filter without parsing event
types. Run two monitors in parallel — errors and heartbeats have very different
cadences:

```bash
tail -F $OUT_DIR/logs/progress_*.jsonl | grep --line-buffered '"status":"error"'
tail -F $OUT_DIR/logs/progress_vlm.jsonl | grep --line-buffered '"event":"heartbeat"'
```

Diagnosing a stall:

- ==No heartbeat for >90 s== → main thread deadlocked, or the heartbeat thread
  died (look for `"event":"heartbeat_died"`).
- ==Heartbeat continues but no `slide_done`== → upstream queue stall (the Ollama
  HTTP queue); the main thread is alive.
- ==`status:"retry"` repeating on one `slide_id`== → transient retry loop; after
  `--max-retries` that slide errors out and the pipeline continues.
- ==`status:"retry" reason:"batched OOM"` thrashing== → the batched fallback chain
  is still failing; drop `--batch-size` or force sequential.

VLM heartbeats carry `avg_slide_s` (rolling mean), `queue_remaining`, and
`eta_s` (their product) for ETA reporting.
