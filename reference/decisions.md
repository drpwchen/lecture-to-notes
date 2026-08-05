# Decisions, benchmarks and wrong turns

Why the pipeline is shaped the way it is. Every entry here is a measurement or a
post-mortem, kept because the reasoning is what stops the mistake being repeated;
dates and numbers are preserved deliberately. Lectures are referred to by their
batch label (L1…L8, Lecture-A) and machines generically.

Nothing here is required reading to run the pipeline — `SKILL.md` and
`pipeline.md` carry the rules. Read a section when you are about to change the
thing it is about, or when you want to relitigate a decision.

## Contents

- [ASR auto-correction — both passes retired](#asr-auto-correction)
- [The CTranslate2 abort, and two wrong turns](#ctranslate2-abort)
- [Crash reality — the audio, not the VRAM](#crash-reality)
- [8 GB VRAM budget](#vram)
- [Batch size and beam width](#batch-beam)
- [Dual-model transcription](#dual-model)
- [Frame timestamp off-by-one](#frame-timestamp-offset)
- [OCR engine benchmark — Surya chosen](#ocr-benchmark)
- [The slim VLM prompt](#slim-vlm-prompt)
- [Three "OCR" scripts, one misnomer](#three-ocr-scripts)
- [Optional-dependency degradation](#optional-dependency-degradation)
- [Two-pass synthesis](#two-pass-synthesis)
- [Tier floors and suppression](#tier-floors)
- [L2 model choice](#l2-model-choice)
- [Browser playback and AVCHD](#browser-playback-and-avchd)
- [Filename order is not time](#filename-order-is-not-time)
- [Director batch dispatch](#batch-dispatch)
- [Measurement discipline](#measurement-discipline)

---

## ASR auto-correction — both passes retired {#asr-auto-correction}

**2026-07-26.** Two automatic transcript-correction passes were built and both
were retired after measurement on the same 8-lecture batch.

- **Free-form LLM proofreading** (a small local model) produced
  `pneumomagination` → `pneumomeningocele` when the context said *hypomyelination*,
  and `adjustation` → `adjustment` when the context said *gestation*.
- **Glossary-grounded mechanical replacement** still made ~20 wrong edits out of
  78, including `blader` → `header` (bladder absent from the slides, header
  present), `clotinic` → `clinic` (it was *clonic*), and `percentileDQ73` →
  `Percentiles`, which destroyed the DQ 73 datum.

==The correct term is frequently absent from the slides==, so no grounding rule
saves either approach. The asymmetry is the whole argument: a raw garble makes
the synthesizer stop and verify; a confident wrong term does not. Leaving the
error visible strictly dominates.

The replacement is `flag_asr_suspects.py`, which never rewrites. ==Do not add an
auto-apply mode.== The retired cleanup script is kept for reference only; nothing
calls it. Consequence for every downstream step: ==there is no
`transcript_clean.txt`== — read `transcript.txt` paired with `asr_suspects.txt`.

## The CTranslate2 abort, and two wrong turns {#ctranslate2-abort}

**Diagnosed 2026-07-25.** Long local transcriptions were dying with
`exit=3221226505` (`0xC0000409`).

**What actually happens.** `transcribe_video.py` checkpoints results to
`transcript.partial.json` every 50 segments (atomic temp+replace). The chunked
runner promotes that checkpoint on abort, cuts the audio from the last decoded
timestamp, and runs again until the chunk is covered. Verified on a chunk that
had aborted 3/3 times before: ==2 rounds, 62 s, 279 segments, full 600 s covered,
2480 chars== (recursive halving got 2475 chars but with collapse garbage; VAD got
996).

**Why the abort cannot be caught.** `0xC0000409` is Windows killing the process
for a corrupted stack — ==no exception, no `finally`, no `atexit`==, nothing
flushed. The Python-level traceback appears only with `PYTHONFAULTHANDLER=1` or
`faulthandler.enable()`. Upstream: faster-whisper issues #1293 and #71 —
reported, ==unfixed in any release==; this was fw 1.2.1 + ct2 4.7.1.

**What it is NOT** (each was tested, each cost a rerun): not the audio (a
"cursed" chunk decodes fine — it aborts while *finishing*); not VRAM (aborted at
2.9–5.8 GB free of 8 GB); not precision, not chunk length, not glossary length,
not the heartbeat thread, not the VAD parameters, not batched-vs-sequential, not
`import torch`. ==The abort point drifts== — segment 100 on one run, 250 on the
next — which is why "this specific audio is cursed" looked plausible for hours.

**Wrong turn #1 — "it's the audio".** Concluded from chunks failing at similar
positions in two files, without ever checking whether the decode had *finished*.

**Wrong turn #2 — "`--vad` is the fix".** VAD does stop every abort and runs 3×
faster, but measured over all 13 crashed chunks it kept only **0.57 / 0.92** of
the characters (worst case ==83 chars for a 10-min chunk vs 2258==) with a
==HIGHER== 6-gram repeat rate (0.12–0.39 vs 0.00–0.07). It was declared the fix
from ONE sample, then "confirmed" by an A/B run only on chunks that never crashed
— structurally blind to the loss it caused. ==Measure the disputed property on
the population the claim is about.==

Legacy fallbacks kept but off the default path: recursive-halving rescue, gap
filling, and the VAD upgrade path.

==This is why the `transcript.partial.json` write must not be removed== — the
chunked runner is its external consumer, and deleting it breaks crash-resume.

## Crash reality — the audio, not the VRAM {#crash-reality}

Measured on a two-day workshop batch (2026-07-25): two independent 2-hour
recordings both crashed in their 30–40 min span — hands-on practice segments with
overlapping speech and room noise.

- ==Precision and model swaps do not help==: `int8_float16` and `large-v3` crash
  on the same audio (one apparent recovery was a fluke).
- VRAM at crash was 2.9–5.8 GB free of 8 — ==not exhaustion==.
- **Decisive test**: a chunk that crashed re-ran fine at chunk 1, and chunk 1
  re-ran fine AFTER the crashes → the driver and environment are not degraded.
  ==Run this test before blaming the environment.==
- Rescue = recursive halving down to ~30 s, transcribing everything around the
  toxic seconds and dropping only the smallest failing slice. Typical loss: ~19 s
  per bad 10-min chunk.

==Serialize GPU work — and verify it, don't assume it.== Stopping a background
shell does not kill a job-runner child still holding the GPU. Before starting any
GPU job, check for surviving processes and confirm free VRAM. Three concurrent
transcribe processes once drove free VRAM to 53 MiB and made every chunk crash —
a false signal that cost two full reruns.

## 8 GB VRAM budget {#vram}

Measured on an RTX 3070 Ti (8 GB) class card. ==Do not issue a command whose
worst-case VRAM exceeds the budget== — the failure mode is either a CUDA
`0xC0000005` access violation (process crash, exit 3221226505) or hours of
shared-memory swap thrash.

| Component | Model measured on | Typical VRAM | Notes |
|---|---|---:|---|
| faster-whisper, sequential, beam 5 | `large-v3` | ~4.5 GB | safe baseline |
| faster-whisper, sequential, beam 10 | `large-v3` | ~5.0 GB | safe |
| faster-whisper, ==sequential, beam 15== | `large-v3` | ==~5.5 GB + workspace== | ==often crashes on 8 GB== |
| faster-whisper, batched, batch 8, beam 5 | `large-v3` | ~6.0 GB | safe only if nothing else is loaded |
| faster-whisper, batched, batch 4, beam 10 | `large-v3` | ~5.5 GB | preferred for anti-collapse retry |
| Stage D VLM | `minicpm-v:8b` | ~4.7 GB | leaves ~2.5 GB |
| embedding model | `bge-m3` | ~1.2 GB | ==often loaded by a semantic-search tool==; stop it before the VLM |

The `breeze25` default alias is a converted `large-v3`-class model; its footprint
tracks the rows above, but the numbers were taken on `large-v3` and have not been
re-measured per model.

Before launching GPU work, check what is loaded (`ollama ps`, `nvidia-smi
--query-gpu=memory.free`) and stop any pinned embedding or VLM model first.

- ==Whisper and the VLM may NOT run concurrently on 8 GB.== Serialize everything;
  run multiple lectures' Stage D sequentially in one wrapper rather than spawning
  parallel subagents that all hit the GPU.
- ==Do not run frame extraction concurrently with transcription== (2026-05-21,
  Path A lesson). ffmpeg is CPU-bound, but on HEVC/10-bit/4K source it spikes CPU
  and memory bandwidth hard enough to starve the Whisper pipeline; VRAM pressure
  rises and it OOMs. L6 died from this; the serial run worked first try. For Path
  A: transcribe → wait → extract frames → then Stages B–F.
- ==Shared GPU memory warning sign==: if Task Manager's "Shared GPU memory"
  climbs above ~1 GB during transcribe or VLM, the job has spilled into system
  RAM and will run 5–20× slower. Abort and shrink batch/beam.
- ==Anti-collapse retry recipe==: `--no-condition --beam-size 10 --batch-size 4
  --initial-prompt-file <glossary>`. ==NOT== `--beam-size 15 --no-batched` —
  higher beam plus sequential mode multiplies activation memory and has been
  observed to crash on 8 GB.

## Batch size and beam width {#batch-beam}

**Validated 2026-05-22** on an 8 GB card: batch 3 / beam 10 (207 s) is 17 %
faster than batch 4 / beam 10 (250 s) at the same quality. Batch 3 / beam 5
(114 s) loses ~5 % of transcript detail. Batch 3 / beam 15 (418 s) is 2× slower
and ==introduces typos== (因據 for 依據) — not worth it.

So `--batch-size 3 --beam-size 10` is the recommended production invocation.
==The script's own defaults are different and that is deliberate==: argparse
defaults to `--batch-size 4 --beam-size 5`, and batching only engages when
`--vad` is also passed (the batched pipeline requires `vad_filter=True`, and VAD
has been off by default since 2026-07-12 because it was eating quiet speech). So
a bare invocation runs sequential at beam 5; pass the flags explicitly for the
measured configuration.

Batch size was lowered from 8 to 4 on 2026-05-21 after three lectures in one
conference batch showed transcribe peaks above 7900 MiB on English audio. On CUDA
OOM the script retries once at half the batch size, then falls back to sequential;
the chosen size and whether the fallback fired land in `runs.jsonl`, so flapping
is visible over time.

## Dual-model transcription {#dual-model}

**Benchmarked 2026-07-12 on 5 clips.** Neither `large-v3` nor `breeze25`
dominates — ==their errors are complementary==. On one real clinical clip:
`large-v3` got 3/10 key clinical terms, `breeze25` got 6/10; `large-v3` heard
*piriformis* as "performance", `breeze25` hallucinated 「椎間盤突出」into
「最艱難突出」. They agree on ~88–91 % of characters.

For quality-first bilingual medical lectures, run BOTH and reconcile, using slide
OCR as the disambiguating vocabulary — it is the best disambiguator available.
Cost: about +3 min of GPU per hour of audio; ==`breeze25` is the same speed as
`large-v3`, not slower==.

- ==Dual-model agreement below 75 % means the audio is unintelligible and BOTH
  models are hallucinating== — flag that span for human review instead of writing
  it into the note.
- ==One model per process.== Loading `large-v3` and a Breeze model in the same
  process crashes an 8 GB card (`0xC0000005`, exit 3221226505).
- A Taiwanese-Hokkien-tuned Breeze variant is a bad fit here: it segments 10 min
  into ~16 chunks instead of ~223, which wrecks the timestamp↔slide alignment this
  pipeline depends on.

Merging two transcripts is not implemented inside this skill; the reconciliation
tooling lives with the general transcription workflow.

## Frame timestamp off-by-one {#frame-timestamp-offset}

ffmpeg's `fps=1/N` filter emits its **first** frame at t=0, not at t=N. The
original code assumed the first frame was at t=interval, which shifted every
slide timestamp by +15 s (at the default interval) and made Stage E ground each
slide against the wrong 15 seconds of speech. ==This hit every Path A run==, and
it was invisible: grounding scores stayed plausible, they were just anchored to
the neighbouring slide's narration. Fixed in the extractor; the lesson is that a
systematic offset does not look like a bug in aggregate statistics.

## OCR engine benchmark — Surya chosen {#ocr-benchmark}

**2026-05-22, 28 fixtures × 3 runs, single consumer GPU.**

| engine | json_valid | max_rep ⭐ | stability_cv | chars | blocks | conf | latency_s |
|---|---|---|---|---|---|---|---|
| **surya** ⭐ | 1.0 | **0.167** | 0.0 | **320.5** | 19.3 | 0.854 | 1.39 (GPU) |
| rapidocr | 1.0 | 0.20 | 0.0 | 276.1 | 17.0 | 0.954 | 1.22 (CPU) |
| paddleocr_vl | 1.0 | **0.601** ⚠️ | 0.0 | 283.0 | 7.9 | n/a | 4.73 (GPU) |

**Verdict — Surya is the Stage B2 default**: deterministic, lowest repetition
risk, extracts the most text (+16 % chars vs RapidOCR), runs at GPU speed, and
exposes confidence. **RapidOCR is the zero-dependency fallback.**
**PaddleOCR-VL was rejected as default** — the benchmark reproduced the predicted
generative runaway on a dense table (1924 chars, 0.601 repeated-3gram), and it is
3.4× slower with no confidence signal. Keep it out of the production path.

The metric that mattered was ==content-dependent `max_repetition_score`==, not
run-to-run variance: a greedy generative model can be perfectly *reproducible*
while being reproducibly wrong. Stability CV alone would have passed
PaddleOCR-VL.

Design philosophy behind the redesign, still binding: schema-first
`ocr_output {blocks, labels, reading_order, confidence, engine}`;
engine-as-adapter with each GPU engine in its own venv invoked as a subprocess;
==stable low-entropy structured extraction, NOT generative clean-markdown==.

Validation of the production wiring (same date): 28 fixtures → 27 Surya + 1
skipped as decorative; Surya `clean_text` +16 % chars vs RapidOCR.

An earlier evaluation of classic PaddleOCR was abandoned for a different reason:
3.5.0 has dependency conflicts with Python 3.12 (oneDNN backend
`NotImplementedError`, torch shm.dll load failure through the modelscope chain).

**The four hard slide categories**, and what each layer does with them:

| Category | OCR adapter | VLM | Embed |
|---|---|---|---|
| algorithm / flowchart | node and edge-label text as blocks | `contains_algorithm=true`, labels — ==not== decision-tree reconstruction (that is diagram-graph parsing, a separate research problem) | embed the image; markdown can't hold the spatial info |
| pure clinical photo | usually empty, or caption only | describe pose/demonstration, ==no diagnosis== | embed; bullets come from the transcript |
| MRI | labels, measurements, annotations only | modality + region, ==no diagnosis== | embed at width 600 |
| ultrasound | on-image measurements and labels | modality + region, ==no diagnosis== | embed at width 600 |

## The slim VLM prompt {#slim-vlm-prompt}

**Patched 2026-05-22 after an external architecture review.** The Stage D prompt
dropped its `vlm_text` field; the VLM now returns only semantic signals
(content_type, function, labels, booleans, floats).

Production A/B: ==0/25 failures with the slim prompt vs 2–3/25 with full OCR==,
and 38 % faster (258 s vs 416 s). The hallucination loops — a slide's text
repeating a phrase a hundred times — were driven by `vlm_text` autoregressive
OCR; removing the field eliminated the root cause rather than damping it.

Text now comes from Stage B `quick_text` (RapidOCR), Stage B2 `clean_text`
(Surya), or `pdf_text.json`. ==Re-add `vlm_text` only if a video-frame path has no
external OCR engine available at all.== The field itself survives as an
always-empty string for schema compatibility; `vlm_error` is the diagnostic field.

## Three "OCR" scripts, one misnomer {#three-ocr-scripts}

The recurring "we have three duplicate OCR scripts" impression came from one bad
name. What they actually are:

| Script | Real role | Engine | Scope |
|---|---|---|---|
| `quick_ocr.py` | Stage B cheap triage (quick_text + entropy, feeds dedup and the skip gate) | RapidOCR, CPU | every frame |
| `ocr_surya.py` | Stage B2 high-quality text (feeds grounding and synthesis) | Surya, GPU, own venv subprocess | canonical, text-bearing |
| `vlm_signals.py` | ==not OCR== — Stage D VLM semantic classifier | a VLM via ollama | canonical minus pre-skipped |

**Verdict: do not merge them** — three engines, three runtimes, three scopes. The
fix was to rename the misnomer (`ocr_slides.py` → `vlm_signals.py`, shim kept for
live callers), pull the genuinely duplicated helpers into `_common.py` (the
RapidOCR wrapper, `laplacian_variance`, `load_segments`, the transcript writer,
the NVIDIA path setup), and move the Surya adapter out of the benchmark tree into
`scripts/adapters/` — ==production must not depend on the benchmark tree==.

The duplicated helpers were not merely redundant, they had *diverged*: two
RapidOCR wrappers with different reading-order and confidence handling, and two
`laplacian_variance` implementations with different resample filters and variance
ranges while a docstring claimed they matched. Two implementations of one
threshold's input is two rulers for one number.

## Optional-dependency degradation {#optional-dependency-degradation}

A recurring failure class worth naming: ==an optional dependency that degrades
into *wrong output* rather than *less output* is not optional.==

- **scikit-image missing** made Stage C fall back to plain grayscale histogram
  similarity, which scores same-template white slides at ~0.95 and collapsed 120
  slides into ~5. The pipeline reported success.
- **RapidOCR missing** made Stage B write `quick_text=""` everywhere and exit 0;
  Stage D's skip gate then classified nearly every slide as decorative, and a
  fresh install produced a confident, empty note.
- **A mid-run Surya OOM** used to be entirely silent: everything after it fell to
  the fallback engine while the report looked normal.

All three are now reported and gated. The rule going forward: a degraded path may
produce *less*, never *differently wrong*, and it must say so.

**Open item (2026-08-02, not resolved).** Even with scikit-image installed,
Stage C's merge condition `text_subset OR layout > 0.85` still over-merges
synthetic decks that are mostly whitespace on a shared template — the SSIM side of
the same failure shape the histogram fallback showed. Synthetic fixtures are not
enough to convict real slides; tightening the threshold or switching to AND needs
an A/B on real course data first.

## Two-pass synthesis {#two-pass-synthesis}

**Validated on the L1–L8 batch, 2026-05.** Splitting Stage F into a Tier-pass
(writes only `slides_final.json`) and a Write-pass (reads the frozen tiers,
writes the note) exists because ==a single subagent doing both tends to simplify
structure so that its own embed-count audit passes==. That is what caused the
tier divergence seen on L3/L5/L6.

Cost is about 1.8× a single pass and it eliminates over- and under-embedding. For
one short lecture, a single pass is fine.

The same split logic drove moving mechanical formatting out of the prompt
entirely: the earlier write prompt was ~60 % embed/callout/path/audit rules and
only ~10 % guidance on what clinical content to capture, so subagents optimized
for format compliance over usefulness. Formatting is now deterministic
(`render_embeds.py`), and the prompt is about clinical reasoning.

## Tier floors and suppression {#tier-floors}

Two hard rules exist because scoring alone lost content in opposite directions.

**The markdown-overlap exemption.** Suppressing an embed whose text is already in
the bullets is right for a mnemonic slide and wrong for anything spatial. On
2026-05-17 a patch subagent applied the suppression rule broadly and erased 11
flowchart and algorithm slides from one lecture — caught only because the user
checked by hand. Hence the explicit exemption list for imaging, flowcharts,
charts and anatomy.

**The imaging floor.** The score weights speaker-mention at 0.30 and dwell at
0.15, so a frame the speaker only glances at scores low even when its pixels
carry information markdown cannot. On a pediatric ultrasound lecture (run
2026-05-21) that produced 65 canonical frames but only 19 embedded: low-mention
ultrasound frames fell to Tier 3 and vanished. The floor guarantees 逐投影片
coverage only; it does not force the slide into 總整理.

**Tier parsing.** A tier value that fails to parse used to silently become tier 3
— which deletes the figure while the audit message still reads plausibly. Forms
like `"T1 核心"`, floats and `None` are now handled explicitly. ==A parse failure
that resolves to a valid-looking default is worse than a crash.==

## L2 model choice {#l2-model-choice}

**User ruling 2026-07-30: sonnet for all L2 generation, both languages. Do not
downgrade to a cheaper model to save cost.**

Measured that day (n=1, an English-spoken segment): the cheaper model obeyed the
flag-only rule perfectly — 0 ghost frames, every garble preserved with ⚠️, 95/115
slides vs sonnet's 92/115 — but emitted near-verbatim English bullets (0 % CJK vs
sonnet's 20 %) despite the 繁中 instruction. ==It followed every rule and still
produced a transcript copy instead of an index==, so the failure is the
deliverable itself, not a fixable slip. Rule compliance is not a proxy for
deliverable quality.

## Browser playback and AVCHD {#browser-playback-and-avchd}

==A `.MTS` course cannot be shared as-is.== Browsers cannot decode AVCHD's AC-3
audio (Chrome plays the video *silently*) or its MPEG-TS container.

Everything else was evaluated and rejected against the bar "the recipient just
opens it": `ffmpeg.wasm` can't fit a 2 GB clip in WASM memory; `mpegts.js` and
enable-AC3 hacks need recipient-side setup and still can't decode AC-3 in
`<video>`; on-the-fly transcoding, a media server, or a remote-controlled player
all require a running server or a per-recipient install.

==Since AC-3 must be transcoded anyway, one ffmpeg pass to a browser-native mp4 is
the simplest universal artifact.== Originals stay untouched, and `.mp4` sources
need no conversion at all.

Measured on lecture AVCHD at ~9 Mbps: x264 CRF 18 ≈ **−45 % size** at ~3× realtime;
CRF 20 ≈ −62 %. ==NVENC was rejected==: at matching quality its files came out
*larger* than the source, useless for a small share. x264 on CPU is the size
champion. ==Use H.264, not HEVC== — Firefox has no HEVC support and Chrome on
Windows needs a paid codec extension.

## Filename order is not time {#filename-order-is-not-time}

**2026-08-04, 2024-05 Conference-Y (2 days, 20 AVCHD clips + 5 mp3 + 289 photos).**

Segmentation ordered courses by the `manifest.json` clips array, which is the
filename sort. Nobody had written down that this was an assumption; `build_L1.py`
even stated it as fact in a comment — *"clip 編號 = manifest 陣列位置 = AVCHD 流水
號時序"*. On a single-camera one-day lecture it happens to hold. Here it did not:

- the five `.mp3` sorted between `00011.MTS` and `00012.MTS`, so **day-2
  afternoon audio sat in the middle of day-2 morning video** inside
  `L1_coarse.md` — 9 of 25 clips out of place
- `<speaker>-1-` was written on the file that came SECOND
- a two-day shoot means the camcorder counter restarts, so file numbers repeat

The earlier repair attempt reached the right ORDER by reasoning from the printed
agenda plus timestamps embedded in the mp3 filenames — but the agenda turned out
to be no clock at all (day 1 ran ~25 min early, day 2 morning ~35 min late), so
that was a lucky landing, not a method, and it still left three ⚠️ items open.

**What actually fixed it — read every device's own clock:**

1. ==AVCHD carries no container timestamp==, which is why this went unnoticed:
   ffprobe showed nothing, so `.MTS` read as "no capture time" and the alignment
   layer silently did not apply to conference footage at all. The clock is in
   the stream's MDPM pack (`_mdpm.py`). Corroboration: `MDPM start + duration ==
   mtime` on all 20 files (0.2–3.2 s) — which also proves ==AVCHD mtime is the
   recording END==.
2. ==Every device's clock was wrong differently==: camcorder +1 day +5m30s
   (measured by transcript cross-correlation, 3 clips agreeing to ±3 s), Canon
   stills +72m30s (measured by OCR-matching photos to slide text, 31 matches,
   σ=30 s), recorder correct. Recipe:
   `multi-camera.md#device-clock-calibration`.
3. The date-off-by-one is the nastiest failure mode: hours and minutes stay
   right, so every file looks individually plausible and only cross-device
   comparison exposes it.

**Payoffs beyond the ordering.** Two of the three open ⚠️ items were answered by
measurement rather than argument (a 62-min audio stretch was proven to be the
parallel track of a talk already on video — cross-correlation put the three video
files at 495/2320/4145 s into it), and 289 photos nobody had placed turned out to
be full slide decks for four audio-only talks.

**Rules that came out of it** — SKILL.md HARD RULE 9, `course_timeline.py`, and
`segmented-mode.md` Step 0a. ==Filenames are labels; agendas are intentions; only
capture clocks are time — and clocks must be calibrated before they are trusted.==

## Director batch dispatch {#batch-dispatch}

**Added 2026-05-21 after an 8-lecture batch.** When running many lectures,
dispatch the pipeline stages as one subagent and synthesis as a separate fresh
subagent.

Reason: a single subagent doing the full pipeline "bounces" — it exits during the
long Whisper and VLM background waits, because it has no immediate tool calls to
make, then the harness re-fires it on background events, repeatedly. That wastes
30+ minutes of wall time per lecture. The director should poll the progress logs
itself (or schedule wakeups) and only spawn the synthesis subagent ==after
`slides_grounded.json` exists==. L8 used this pattern: 25 min for synthesis,
against L1's 73 min for a full-pipeline subagent.

## Measurement discipline {#measurement-discipline}

The recurring meta-lesson across the entries above, stated once:

1. ==Measure the disputed property on the population the claim is about.== The
   VAD "fix" was A/B-tested only on chunks that never crashed — structurally
   blind to the loss it caused on the chunks in question.
2. ==n=1 is not a benchmark.== Both the VAD conclusion and the "cursed audio"
   theory came from a single sample that happened to fit.
3. ==Reproducibility is not correctness.== PaddleOCR-VL had perfect run-to-run
   stability while being reproducibly wrong; the metric that caught it was
   content-dependent repetition.
4. ==A silent degrade is worse than a crash.== Every entry in
   [optional-dependency degradation](#optional-dependency-degradation) exited 0.
5. ==A parse failure that lands on a valid-looking default is invisible.== Tier
   parsing, `int(None)` on timestamps, and 0-vs-1-based slide ids all failed this
   way.
6. ==Grep the whole system, not one directory.== A checkpoint file was nearly
   deleted as dead code because the search for consumers covered only the skill
   directory; the consumer was one directory up.
7. ==Machine-green is not correct.== In multi-source alignment, every mechanical
   check passed while the material was 44 minutes out of place; a human looking at
   one clip's actual content was what caught it.
8. ==A clean report and an empty scan look identical.== A cleanup script printed
   "0 stale figures" for a course that had 5,762 of them: it gated on the previous
   tool generation's backup filename and ignored its course argument, so it had
   scanned nothing. Before believing any audit's all-clear, check that it matched
   something — hits > 0, targets scanned > 0. Tools that can select nothing should
   exit non-zero when a named target matches nothing, rather than reporting a
   reassuring total.
9. ==A pattern that fails to match is indistinguishable from prose.== The viewer's
   timecode regex listed only video extensions, so every citation into an audio
   file rendered as dead plain text — no error, no warning, and the export summary
   counts blocks rather than resolutions. On one two-day course 312 of 536
   timecodes were dead this way. Anything that silently degrades to "render as-is"
   needs a positive check downstream: count how many citations became links and
   compare against how many were written.
10. ==Four silent-failure bugs surfaced in a single session, all the same shape:==
   reporting "checked, nothing wrong" about something never checked. Three came
   from one root cause — a manifest `src` carrying a subdirectory prefix compared
   against a bare basename, so the lookup found nothing and the "nothing" was read
   as "nothing wrong". When a comparison can come up empty, the empty case needs
   its own branch; falling through to the success path is how a checker becomes
   decorative.
