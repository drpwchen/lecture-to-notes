# Segmented mode — one course folder → per-segment L2/L3 + Hub + web viewer

For a **multi-talk workshop folder**: several back-to-back talks and hands-on
demos in one directory, often AVCHD `.MTS`. The deliverable is not one note but
a set — ==per-segment **L2** (逐字稿索引) + **L3** (總整理) + a Hub note + PDFs +
an interactive web viewer==.

This file merges the former `segmented_workflow.md` (the steps) and
`l2_l3_segmented_spec.md` (the rules) into one document. Where those two
disagreed, the resolution is stated inline and marked ==裁決==.

## Contents

- [Pipeline shape](#shape)
- [Tooling — what ships and what does not](#tooling)
- [Course-type taxonomy](#taxonomy)
- [Step 0 — L1 prerequisite](#step0)
- [Step 1 — Segmentation (propose → user confirms)](#step1)
- [Step 2 — Per-segment L1 extraction](#step2)
- [Step 3 — L2 rules](#step3)
- [Step 4 — L3 rules](#step4)
- [Step 5 — L3 image cross-check](#step5)
- [Step 6 — Hub note](#step6)
- [Step 7 — PDF export](#step7)
- [Step 8 — Folder layout](#step8)
- [Step 9 — Web export](#step9)
- [File template](#template)
- [Gotchas](#gotchas)

---

## Pipeline shape {#shape}

```
L1 (transcript ↔ slide interleave, per-clip language)
  → Segmentation (propose → USER CONFIRMS)
    → per-segment L2  +  per-segment L3
      → Hub note  →  PDF export  →  web export
```

L2 and L3 are separate deliverables with different jobs: L2 is a condensed,
searchable **index of what was said**; L3 is the **clinical synthesis**. A
segment that is pure student practice gets L2 only.

## Tooling — what ships and what does not {#tooling}

The generic batch layer now lives in this skill at ==`scripts/batch/`==:

| Script | Job |
|---|---|
| `build_L1.py <course_dir> [--granularity coarse\|fine\|both]` | the L1 note: every slide frame + every timestamped transcript sentence interleaved by time, per clip. 0 tokens, zero reordering, zero loss. `coarse` = canonical frames only. |
| `split_segments.py <clip_dir> [--max-dur 8] [--soft-dur 4] [--backup]` | re-segment a word-timestamped `transcript.json` into sentence units, so L1's interleave binds text to the slide actually on screen. Conference Mandarin yields 20–30 s Whisper segments that span several slide changes. |
| `split_L1_by_segment.py <course_dir> [--granularity coarse\|fine]` | split `L1_coarse.md` into `_seg/L1_segNN.md` per the confirmed segment table. |
| `add_dhash.py <dir> [--hash-size 8]` | difference-hash on the ORIGINAL frame content (not the center-cropped phash dedup uses), so hashes survive a re-cut at a different interval. |
| `vlm_cache.py` | map a stable content dhash → prior VLM signals, so a re-cut only pays for genuinely new frames. |
| `detect_language.py <course_dir> [--json]` | text-based language verdict across clips. |
| `detect_language_audio.py <media> [--json] [--windows 3] [--seconds 45]` | ==the reliable one== — detects from the audio itself. Text detection is circular: an English lecture force-transcribed as `zh` produces hallucinated Chinese, text detection says zh, and it re-transcribes as garbage again. |
| `phi_mask.py <image\|dir> [--out PATH] [--band 0.06] [--mode blur\|black] [--inplace]` | redact the video-conference top UI band (participant sign-in names are PHI) before any frame is shared. |
| `process_slide_deck.py <course_dir> <deck_path>` | standardized PPTX/PDF deck processing. It does ==not== try to time-align deck pages to video frames — a pilot found real multi-to-one mismatch risk. |

The old `_xlf_batch/` paths for these nine are ==shims== that forward here with
the same argv and exit code, kept because live callers still use them.

==Not shipped==: the course **control plane** — `run_queue.py`, `rerun_batch.py`,
`clip_order.py` and the batch dashboard — stays `_xlf_batch`-only. It encodes one
specific course library's queue, naming and re-run bookkeeping, so it is
course-specific infrastructure, not a general tool. Segmented mode is fully
runnable without it: you drive the steps below by hand, or with your own runner.

`export_web.py` (Step 9) and `obsidian-to-pdf` (Step 7) live outside the batch
layer; `export_web.py` ships with the skill, the PDF renderer does not.

## Course-type taxonomy {#taxonomy}

Course-level `course_type` lives in `segments.json` (and the manifest);
segment-level `seg_type` stays `static` / `dynamic` / `mixed`. The Step-1
proposal agent assigns `course_type` from the folder name plus the segment table;
the L3 agent reads it and picks the matching template.

| course_type | Detection cue | Extraction params | L2 rule | L3 emphasis |
|---|---|---|---|---|
| `didactic` | slide/PDF deck present, single speaker | interval 15 s, max-span off | every slide, chronological | topic synthesis + Pearls + 縮寫表 (the default template) |
| `us-demo` | ultrasound/injection keywords, dynamic footage dominant | interval 3 s, max-span 90 | no images | technique steps + probe/needle checklists + timestamped key moments; a high `vlm_skip` rate is NORMAL |
| `workshop-paired` | workshop / 工作坊 | as `us-demo` | teaching segments like didactic or us-demo per content; practice segments brief | pair teach ↔ practice links; sub-number the pair (11a/11b) to keep display order monotonic |
| `case-discussion` | case keywords, PDF imaging pages, PHI risk | ==local-only transcription, never a hosted engine== | per-case structure; PDF-page images allowed | one section per case: presentation → imaging → discussion → verdict; ==PHI statement mandatory== |
| `conference` | 學會 / 年會 folder, multi-talk | (usually already L1-done) | per-talk segmentation, strict speaker table | per-talk mini-L3 under the Hub as the primary artifact; never fabricate affiliation |

`slide_interval_sec` feeds `extract_slides --interval`; `max_span_seconds` feeds
`dedup_semantic --max-span-seconds` (null = off) and caps over-merging of static
screens. ==These params apply to FUTURE extractions only== — changing them does
not retro-apply, and courses already extracted are never re-extracted for a
parameter change.

**PHI statement** (case-discussion, and any segment with live-patient footage): a
plain sentence in the L3 stating the note and its embeds exclude PHI, e.g.
`本段包含對 live patient 的臨床示範；PHI（姓名/病歷號/可識別影像）已全數排除。`

**Per-type L3 emphasis** (the canonical file template shape still holds):
*didactic* → 影片代號 · 縮寫表 · synthesis sections woven with figures · Pearls.
*us-demo* → 影片代號 · 縮寫表 · `## 掃描/操作步驟` (ordered, each with a
timestamp) · `## 探頭/針具 checklist` · `## 關鍵時刻` · Pearls; no 逐投影片,
images only if a genuine teaching slide appears. *workshop-paired* → per-segment,
teach and practice cross-linked `↔ [[L3_segNNx_…]]`. *case-discussion* → one
`## 個案 N` block each: `### 病史/PE` → `### 影像` → `### 討論` → `### 結論`.
*conference* → per-talk mini-L3 under the Hub, strict speaker table, ⚠️ any
unconfirmed affiliation.

## Step 0 — L1 prerequisite {#step0}

Needs `<COURSE>/_L1/L1_coarse.md` and a state file recording that L1 was built
with ==per-clip audio language detection==. The chain is: detect language per
clip from the audio → transcribe → `split_segments` → extract slides → `add_dhash`
→ seed `vlm_cache` → `build_L1`.

Sanity-check before going further: open `L1_coarse.md` and confirm the
transcripts are coherent in the language actually spoken — ==not a Chinese
hallucination of English audio==. If L1 is stale or mis-languaged, redo it; every
downstream step inherits the damage.

## Step 1 — Segmentation (propose → user confirms) {#step1}

Spawn one analysis agent (sonnet) to read `L1_coarse.md`, the manifest
(clip → source video filename), and any existing L2 for speaker hints, then
propose a table:

`段# | 來源影片(檔名)+時間範圍 | 講者(⚠️ if unsure, NEVER fabricate) | 主題 |
類型(static slide-driven / dynamic demo-live-scan)`

plus a one-line boundary rationale per cut and a course-level `course_type`.
==Classify static vs dynamic PER SEGMENT== — one course holds both. A Q&A or
verbal-discussion segment where the camera sits on a talking head is still
**dynamic**; mark static only when there is a genuine chronological slide deck.
==Separate student practice from teaching==: a teaching clip followed by hands-on
practice must be cut into a teaching segment (L2 + L3) and a separate practice
segment (L2 only — index the instructor's corrections; its synthesis would
duplicate the teaching segment). ==Do not generate L2 or L3 yet.==

==STOP and present the table to the user; generate nothing until they confirm.==
Confirm at the same time: (1) the cuts, (2) the ==official course name==,
(3) speaker names and whether to print any affiliation — default `<name>醫師`
with no affiliation. Cutting wrong means a whole re-do. Reuse the confirmed name
and speakers verbatim across every segment and the Hub.

Persist the confirmed result to `segments.json` as a list of
`{seg, clips:[ints], time, speaker, topic, type, make_l3}` plus the course-level
`course_type`. `clips` are the manifest clip indices the segment spans; a
boundary clip may appear in two adjacent segments. `make_l3` is true for teaching
and procedure segments, false for pure practice. Also capture `title_zh` (中文短
標題), `region` (sidebar grouping) and `display_order` here — Step 9 reads them
for the friendly names and the viewer.

### segments.json contract {#segments-contract}

==裁決 — normalize_segments does not touch your input.== The two old specs
disagreed about whether the adapter rewrites `segments.json` in place. The
resolution: `export_web.py`'s `normalize_segments()` reads the input
`segments.json`, backfills only MISSING keys (never clobbering hand-tuned
values), and writes the ==normalized copy to
`_intermediate/segments.normalized.json`==. ==The input file is left
untouched.== Backfill sources: `files` from `manifest.clips`, `slug` from the L2
filenames, `make_l3` from whether an L3 exists, `title_zh` from the Hub table or
the topic, `region` → empty, `display_order` → `seg`.

Per-segment fields the exporter needs: `seg`, `files[]` (source video filenames
in V-order), `slug`, `make_l3`, `title_zh`, `region`, `display_order`.

## Step 2 — Per-segment L1 extraction {#step2}

```bash
python <skill-dir>/scripts/batch/split_L1_by_segment.py "<COURSE>" --granularity coarse
```

Splits `L1_coarse.md` (sections headed `## clip NN — <clipname>`) into
`_seg/L1_segNN.md`, one file per segment in segment order. 0 tokens.

## Step 3 — L2 rules {#step3}

One sonnet agent per segment. ==Sonnet for all lectures in both languages==
(user ruling 2026-07-30; do not downgrade to a cheaper model to save cost — the
measurement behind this is in `decisions.md#l2-model-choice`).

**L2 = 精簡條列式逐字稿索引 (a searchable index, not a summary).**

- ==Primary goal: the reader sees what the video covered and can jump to it
  fast.== Condense the transcript so it stays ==faithful to coverage== — trim
  filler, keep what was actually said. This is not aggressive key-point
  distillation.
- ==Put the source and timestamp at the END of each bullet==, in backticks:
  `… go lateral, scan up and down `` `(V1 03:12)` ``. Not at the front.
- **Images by segment type**: ==dynamic/demo → NO screenshots== (frames can't
  align to a sentence and mislead). ==static/slide → embed EVERY slide== at its
  chronological position, each with a `|width` and a two-line caption built from
  the frame's on-screen text label plus the transcript spoken while it showed.
  Use the Obsidian embed `![[cNN_frame_XXXX.jpg|600]]`, ==not== a markdown
  `![](figures/…|width=…)` link — the markdown form breaks both Obsidian widths
  and the PDF rewrite.
- ==L2 needs no vision QC==: slides are placed in time order, not curated, so
  there is no selection error to catch.
- ==ASR suspects: flag with ⚠️, never rewrite.== Keep the original token; a
  visible garble beats a plausible wrong term, because the correct word is
  frequently absent from the slides. Adopt a look-alike candidate only when the
  surrounding transcript also supports it. Variants of one term may be unified —
  note the original ASR form at first occurrence.

## Step 4 — L3 rules {#step4}

One sonnet agent per segment with `make_l3=true`.

**L3 = 總整理 synthesis only.**

- ==No `逐投影片筆記` section.== L3 is the clinical/topic synthesis, organized
  clinically (by structure or topic: anatomy → scan → pathology → intervention),
  not by slide order.
- Each synthesized item carries the source video filename + timestamp for
  traceback, at the end of the bullet.
- ==Curated== key images only — 1–2 per topic — as `![[cNN_frame_XXXX.jpg|width]]`
  with a two-line caption, following the static/dynamic rule (a dynamic segment
  usually has none).
- Keep the `> [!summary] Clinical Pearls` callout, the abbreviation table, and
  `# Resource`.

**Caption sourcing — TEXT-FIRST; vision is the exception.** ==The most accurate
description of a slide is (the slide's on-screen display window) ∩ (the
transcript spoken during that window) + the slide's own OCR text label, all
already captured in L1.== All three are plain text, cost no vision, and are more
reliable than asking a vision model to read a grayscale sonogram — which it
usually cannot. Build every caption from those signals. ==Vision is not a
mandatory step.== Escalate to an actual image Read ONLY for a curated L3 frame
whose text label is empty or ambiguous and that sits in a key slot.

**Anatomy naming (both L2 and L3)**: ==English primary. First mention =
`Full name (ABBR)`; afterwards `ABBR`.== Do not print the Chinese name of the
structure — Chinese still carries the explanation and clinical reasoning, just
not the structure's label.

**Accuracy guards**: never fabricate an affiliation or title; state only what the
source supports, ⚠️ otherwise. Verify anatomy claims (course, depth, relations)
and mark ⚠️ when unsure.

## Step 5 — L3 image cross-check {#step5}

Vision's one real catch is a ==wrong-frame selection== in L3's curated images —
a title or logo slide picked instead of the anatomy slide — and that is
detectable by a cheap text cross-check.

For each L3 that embeds curated images, spawn one check agent (sonnet, no vision
unless needed): for each embedded `![[cNN_frame_XXXX.jpg]]`, look up that frame's
block in `_seg/L1_segNN.md` (its on-screen text label plus the transcript spoken
in its display window) and verify it matches the section it sits under. A
mismatch → pick a better frame from the same display window and replace the
embed, or remove it. Fix captions; a no-needle injection frame becomes
「注射前定位」.

==裁決 — QC logs go in `_intermediate/`, never in `L3/`.== The two old specs
contradicted each other here (one listed `_vision_qc_segNN.md` under `L3/`, one
under `_intermediate/`). QC logs are working files, not deliverables, so they
belong in `_intermediate/_vision_qc_segNN.md`. `export_web.py` ignores stray
`_vision_qc_seg*.md` files left in `L3/` — it reads only exact
`L3_seg{NN}_{slug}.md` — but that tolerance is a safety net, not a licence.

Log format: one row per image, columns `# | Filename | Section | content |
Verdict | Action`. ==The filename cell is an EMBEDDED link
`![[cNN_frame_XXXX.jpg]]`== so the image renders inline for at-a-glance checking;
on a swap, the Action cell names the replacement the same way. A segment with no
embedded images gets no QC log.

## Step 6 — Hub note {#step6}

==裁決 — one Hub spec.== Both old files described the Hub; this is the merged,
single version. One `_HUB_<course>.md` per course:

1. **Course metadata**: the official course name, folder, source videos, date,
   all speakers (note where an affiliation came from).
2. **The confirmed segmentation table** — the same seg# / video+time / speaker /
   topic / type table the user approved.
3. ==ONE merged `## 各段 Segments` list== — fuse links and summary; do not keep
   them as two separate sections.
   - **Level 1, one line per segment**: `**segNN · 講者** —
     [[L3_segNN_<slug>|主題]] · `` `來源影片 時間` `` · 類型 ·
     [[L2_segNN_<slug>|逐字稿]]`. The 主題 text is the wikilink to L3 (the
     synthesis is the course content); the L2 link rides along as 逐字稿.
   - **Level 2, sub-bullets**: 2–4 key points of that talk (a third level is
     fine) so a reader grasps each talk at a glance.

## Step 7 — PDF export {#step7}

==裁決 — one PDF section.== `obsidian-to-pdf` renders `![[img]]` embeds as
*text*, so rewrite them for the PDF pass ONLY: copy each note to a temp file
turning `![[cNN_frame_XXXX.jpg|600]]` into
`![](<abs>/figures/cNN_frame_XXXX.jpg){width=520px}`, then run the renderer on
the temp copy into `pdf/`. ==The vault notes keep the clean `![[…]]` form.==

QC each PDF with fitz: sane page count, embedded-image count ≈ slide count, no
literal `[[` or `==` left in the text. Generate a Hub PDF too — Step 9 requires
it.

## Step 8 — Folder layout {#step8}

Deliverable-first. Only these are top-level deliverables; everything else is
demoted:

```
<COURSE>/
  .obsidian/
  _HUB_<course>.md                     # index
  L2/   L2_segNN_<slug>.md             # deliverable
  L3/   L3_segNN_<slug>.md             # deliverable (teaching segments only)
  figures/                             # ONLY frames actually embedded by L2+L3
  pdf/  L2_segNN.pdf, L3_segNN.pdf, hub pdf
  _raw/           clips/, rerun_backup/, figures-unused/, manifest.json, metadata.json
  _intermediate/  seg/ (incl. segments.normalized.json), L1_coarse.md, L1_fine.md,
                  *cache*.json, logs/, _vision_qc_segNN.md, make_pdfs helper
```

Compute the used-figure set from the L2/L3 markdown, move that subset into
`figures/`, send the rest of the frame pool to `_raw/figures-unused/`. Embeds are
filename-resolved `![[name]]`, so the move never breaks a link. Delete superseded
drafts (`L2_coarse`, `L2_part_*`, `L3.md`, `_L1_part_*`) — they are not
deliverables.

==This layout keeps STABLE machine names== (`cNN_frame_XXXX.jpg`,
`L2_segNN_slug.md`, the original video filenames) and is the ==source of truth==.
The friendly-named web deliverable is a regenerable VIEW. ==Do not rename frames
or notes here== — the rename map lives in the export, not the work dir.

## Step 9 — Web export {#step9}

```bash
python <skill-dir>/scripts/export_web.py "<course dir>" [--name "課程全名"] \
    [--out PATH] [--date DATE] [--author NAME] [--compress] [--crf 18] \
    [--remux | --no-remux]
```

| Flag | Effect |
|---|---|
| `--name` / `--date` | course title and date printed on the page |
| `--out` | override the support-folder path (the HTML is written beside it as `<name>.html`) |
| `--author` | name in the page footer; ==blank by default, which omits the footer entirely== |
| `--remux` | force-process ALL clips to mp4 (default is auto — only non-web-playable formats) |
| `--no-remux` | never process, even `.MTS`; deploy original filenames as-is |
| `--compress` | re-encode video to H.264 for a small shareable set; default just copies the video stream (fast, full size) |
| `--crf` | x264 CRF for `--compress`; 18 = visually lossless, 20 ≈ transparent and smaller. Default 18 |

**Output**: ==ONE webpage at the course root plus a same-named support folder==
beside it. No wrapper folder, no portable package.

```
<course root>/
  <原始影片…>                        ← untouched
  影片筆記整合.html                  ← the ONLY webpage (outermost)
  影片筆記整合/                       ← support folder (opens as an Obsidian vault)
    媒體/圖片/  NN_<中文主題>_<seq>.jpg     ← only the embedded frames, friendly-renamed
    媒體/影片/  NN_<中文主題>.mp4           ← browser-playable clips, ONE copy
    markdown/  00 目錄 / NN <中文> 逐字稿|整理稿   ← link-rewritten, indexes in Obsidian
    pdf/       同名 pdf
    _media_map.json    the rename map (debug / re-run)
    _timeline.json     the embedded TIMELINE, dumped for debugging
```

**Viewer.** Video on the left (resizable) + SUMMARY (L3) / REPLAY (L2 index)
panes on the right + a segment drawer. ==Video playing → the matching note block
auto-highlights and auto-scrolls==; clicking `(Vn MM:SS)` seeks; clicking a
segment card jumps both. Three read modes (both / summary only / index only) and
smart auto-scroll that suspends for 5 s after a manual wheel or touch. It is
self-contained: the per-course `TIMELINE` is embedded inline, no external fetch.

**Search covers the slide layer too** (schema v3, 2026-08-03; idea borrowed from
jieyu166's rad-workflow course hub). Besides every note bullet, the search box
indexes `slide_blocks` — per-canonical-slide OCR text + VLM summaries pulled from
`clips/*/slides_grounded.json` at export time — so ==a term that appears only ON
a slide and was never spoken is still findable==. A slide hit jumps the player to
that slide's own display window (the slide is on screen in the video; no image is
copied). Filters keep hands-on-demo noise out: OCR text must read like language
(≥14 wordy chars, not a machine-UI overlay, frame not typed `ultrasound`/
`decorative`); VLM summaries only for information graphics (table/flowchart/
diagram). Courses without grounding files export exactly as before.

**Deep links + copy-link.** `影片筆記整合.html?f=<原始影片檔名>&t=<秒>` opens
straight at that spot (works from `file://` too); the 🔗 header button copies the
current playing position as such a link. No autoplay — a shared link lands
quietly on the right segment with the video cued.

**TIMELINE build.** Each L2/L3 bullet becomes one `note_block` carrying
`media_file`, `start_sec`/`end_sec`, `section_kind` (`transcript_index` = L2,
`summary_note` = L3) and `segment_id`. `start_sec` is parsed from the bullet's
`` `(Vn MM:SS)` `` via the segment V-map; a bullet with no timestamp inherits the
previous block (`time_source=inherited`, rendered with a dotted underline).
`end_sec` is the next block's start in the same media file within the section
(the last block gets +45 s). The viewer matches `start-0.15 ≤ t < end+0.15`.

**Link rewriting.** Every copied note has its internal refs translated to the
friendly names — `![[cNN_frame…|W]]` → `![[NN_<中文>_<seq>.jpg|W]]`, inline
`` `cNN_frame…` `` prose mentions too, and `[[L2/L3_segNN_slug]]` / `[[_HUB_…]]`
→ the friendly note name, preserving `|alias` and `#section`. ==Video filename
mentions are left untouched== — they name the real source recordings and stay the
sync key. The result opens cleanly as a vault with no dead links.

**Timestamp formats parsed** in bullets: `` `(V1 00:01)` `` (V-map) or
`` `(00004.MTS 03:12)` `` (raw filename, legacy). Both work.

**Auto-selective conversion.** A clip gets an ffmpeg pass when its container is
not web-playable, OR ==its audio is not browser-decodable==, OR `--compress` /
`--remux` is set. ==AVCHD `.MTS` carry AC-3 audio, which no browser decodes —
Chrome plays the video silently== (verified via ffprobe). Web-native `.mp4`+AAC
clips are used as-is and ==are not copied or renamed== (avoids duplicating
gigabytes); only transcoded clips land in `媒體/影片`.

- Video: default `-c:v copy`; `--compress` → ==H.264 x264 `-crf 18 -preset
  slow`==. ==H.264, NOT HEVC== — Firefox doesn't support HEVC and Chrome on
  Windows needs a paid codec extension. No downscaling.
- Audio: AC-3 / DTS / … → `-c:a aac -b:a 192k`; already AAC or MP3 → copy. This
  is the fix for the silent-in-Chrome bug. Subtitles dropped.
- Resumable: a valid prior output (AAC audio, and actually smaller under
  `--compress`) is skipped. Originals are never touched.

Full rationale for choosing pre-conversion over browser-side or server playback,
plus the measured compression numbers, is in
`decisions.md#browser-playback-and-avchd`.

**Versioning.** The manifest carries `schema_version` and `viewer_version`; the
schema is ==additive only== and the viewer tolerates missing optional fields, so
any future viewer reads any past manifest and ==every deployed HTML keeps working
forever==. ==The standardized course dir is the source of truth; the deployed
HTML is a disposable, regenerable view.== To roll out a UI improvement, edit
`scripts/layout2/{viewer.css,viewer.js}` and re-run the export — remux is cached,
so it is near-instant. Protect the course dir, not the HTML.

## File template {#template}

Follow this exactly in every segment — inconsistent per-file structure is a bug
(each agent improvising produces an unusable deliverable).

**Frontmatter** (valid YAML — ==never put `·` or two quoted strings in one
value==; multi-file fields are a YAML list):

```yaml
---
tags: [med/MSK-US, med/<region>, source/<course-slug>]
course: <official course name>
date: 2018-10-28
speaker: <name>醫師
segment: seg11
type: dynamic
---
```

**Body, L2**:

```
# L2 逐字稿索引 — seg11 <topic>

> **課程** … / **日期** … / **講者** … / **類型** dynamic（無截圖） or static（投影片）

## 影片代號
| 代號 | 原始檔案 |
|---|---|
| V1 | … |

## <bullets, (Vn MM:SS) at the END>   (static → every slide; dynamic → no images)

## 課程 Hub
[[_HUB_<course-slug>]]
```

**Body, L3** (==the course-info block goes ABOVE Clinical Pearls==):

```
# L3 總整理 — seg11 <topic>

> **課程** … / **日期** … / **講者** … / **類型** …

## 影片代號
| 代號 | 原始檔案 |
|---|---|

> [!summary] Clinical Pearls
> - 3–5 bullets

## 縮寫表
| 縮寫 | Full |
|---|---|

## <synthesis sections, (Vn MM:SS) at the END>

## 課程 Hub
[[_HUB_<course-slug>]]
```

==Every L2 and L3 ends with a `## 課程 Hub` link to the EXACT hub filename.== Get
the slug right — a mismatched `[[_HUB_…]]` is a dead link.

**Video naming**: reference the ORIGINAL source filename (e.g. `00004.MTS`), not
an internal label like "clip04". When filenames are long, put a 影片代號 table at
the top (`V1 = <full filename>`) and use the alias in bullets.

Metadata header on every L2 and L3: source video file(s), lecture title, speaker,
date. ==Print an affiliation only if the user or source confirms one==; otherwise
just `<name>醫師`. Do not write ⚠️ or "unconfirmed" into the deliverable, do not
assume an institution, do not fabricate. Any summary or 回報 block goes at the
TOP, never as a trailing section.

Example bullets:

```
Static L2 (slide image + timestamp at END):
- Suprascapular nerve (SSN) 從上幹第一分支發出，掃描自 supraclavicular fossa 往後追 `(V1 03:12)`
![[c04_frame_0123.jpg|600]]
> SSN at suprascapular notch
> 橫切面，notch 內見 SSN 與 suprascapular artery 伴行

Dynamic L2 (NO image, timestamp at END):
- 旋轉探頭看清 transverse process (TP)，找到 C5 後往下追 C6、C7 `(V1 00:22)`

L3 synthesis item (curated image, traceback at END):
- **Median nerve (MN) at carpal tunnel**：腕隧道入口見 MN 扁平化，CSA 增大為 CTS 指標 `(V3 12:40)`
```

## Gotchas {#gotchas}

- **Output language**: per-clip language is already handled at L1 (English clips
  in English, Chinese in Chinese). ==L2/L3 OUTPUT is 繁中 + medical English== —
  summarize English talks into Chinese notes, keeping English anatomy terms per
  the naming rule.
- **Boundary clips** shared by two segments: include them in both `L1_segNN`
  extracts, but each L2/L3 covers only its own time range; note the cut time in
  the metadata.
- **Large segments**: L2 can be chunked by clip across two agents and
  concatenated.
- **Draft mistakes to avoid**, all seen in early drafts and since fixed: the
  timestamp at the FRONT of a bullet (it must be at the END, in backticks);
  internal `clipNN` labels instead of the real source filename; markdown
  `![](figures/x.jpg|width=500)` instead of the Obsidian `![[x.jpg|600]]` form;
  asserting an affiliation with no slide or transcript source.
- **Auditing a segmented note**: use `audit_note.py --mode lecture-seg`, which
  knows this contract — L3 needs a `# L3 …` or `# 總整理` heading but ==no
  `# 逐投影片筆記`==; `# Resource` on an L3 is a WARN, not a FAIL; L2 and Hub
  legitimately carry no `# Resource`, no Pearls callout, and (on early courses)
  no frontmatter tags, so those are not FAILs for them. Images resolve against
  the course dir.
