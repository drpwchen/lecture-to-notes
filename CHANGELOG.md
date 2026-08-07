# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.0] — 2026-08-07

### Changed

- **Timecode citations are now parsed, not pattern-matched.** `export_web.py` used a
  single "one label + one time" regex, so every other way a writer naturally cites a
  moment fell through as **dead plain text — with no error anywhere**, because a
  non-match is indistinguishable from prose. Worse, the bullet's `data-time-source`
  silently degraded to `inherited`, meaning it reused the *previous* bullet's
  timestamp for scroll-sync. An audit of 79 delivered courses found 51 affected,
  8,086 citations, and **7,118 of 27,177 timed bullets (26%) mis-positioned**.
  The parser now accepts:
  - ranges — `(V1 04:22–04:31)` → one pill showing the range, jumping to its start
  - lists — `(V1 05:02, 06:13)` → one jumpable pill per time
  - cross-clip spans — `(clipA 29:50–clipB 01:25)` → each end keeps its own pill
  - three-digit minutes `(V1 100:43)` and second-only range ends `(V2 28:30-42)`
  - a leading note — `(practice V1 00:09)`, `(c00 14:17)` — kept verbatim
  - labels declared by the note's own `## 影片代號` table, so an audio-only source
    cited as `(A1 06:59)` resolves; this table now outranks the positional V-map,
    which was wrong for notes that continue numbering across segments
  - an unlabelled `(08:27–12:33)` **only** when the segment has exactly one media
    file. With more than one candidate it stays plain text: a pill pointing at the
    wrong video is worse than no pill.
  Seconds are validated as `00–59` so prose like `(120:80)` cannot match.

### Fixed

- **A citation naming a file the course does not have no longer becomes a link.**
  One `MEDIA_REL` check now gates every route that produces a filename (V-map,
  declared table, literal citation, single-clip fallback), so a parsing slip
  downgrades to plain text instead of minting a button that silently does nothing.
- **Filenames containing a comma resolve.** A real clip may be named
  `20221029_143246-ACL, PCL, menisc, TFCC.mp4`; the grammar would have read those
  commas as a list of times. A fast path matches the whole parenthesised run against
  the manifest before the general grammar runs.
- Placeholder names in two docstrings replaced with `<speaker>`/`<topic>`.

## [0.6.7] — 2026-08-05

### Fixed

Two silent failures found while repairing a reordered course. Both share a shape
this project keeps meeting: **a check that reports success about something it
never actually examined.**

- **`export_web.py` shipped 124 wrong-but-plausible figures under a green
  check.** The fast path for embedded images only ever looked in `<course>/figures/`,
  while `build_L1.py` writes to `<course>/_L1/figures/` — so on every course built
  by the batch pipeline it could not hit, and each embed fell through to a
  recursive walk that indexes the whole work dir **by basename**. After a manifest
  reorder there are two generations of `cNN_frame_XXXX.jpg` on disk (old numbering
  left in `_L1/_stale_figures/`, new in `_L1/figures/`), so one basename means two
  different pictures. The resolver picked one — silently when segment context
  happened to scope it — and `embed-check: all N image embeds mapped + copied ✓`
  reported success. Captions were right; 124 of 896 images were not.
  Now: figure roots are an ordered list (`figures/`, then `_L1/figures/`);
  candidates are collapsed by content hash, so identical bytes under several paths
  are not an ambiguity; a genuinely ambiguous basename is an **error that fails the
  export**, and one resolved by segment context prints a warning instead of
  resolving silently. Stage the intended generation in `<course>/figures/` to pin it.
- **`audit_segmentation.py` judged delivered courses by a superseded artifact.**
  It read only `proposal.md` — a planning note — and never `segments.json`, which
  is what actually ships. Every reordered course therefore returned `REGENERATE`,
  including one whose 20 segments each still mapped to a contiguous block of the
  corrected order and needed nothing but a `display_order` fix; acting on that
  verdict would have rewritten 41 L2/L3 notes for no reason. It now checks the
  delivered segmentation first — contiguity (is the grouping intact?), coverage
  (does any clip belong to no segment?), and whether playback order follows real
  time — and downgrades proposal staleness to INFO when that segmentation holds up.
  Overlapping source pairs already sitting in one segment are reported as intended,
  not as a defect to fix.

## [0.6.6] — 2026-08-05

### Documentation

The same-talk trap section shipped in 0.6.5 said what to look for but not what
should trip the alarm first — and its author then fell into the trap the same
day. Two additions to `multi-camera.md`:

- **Check the spread before the vote count.** Offsets recovered from two devices
  at one event agree within seconds; tens of seconds means different sittings of
  the same recurring course. The failure mode is comparing that spread against
  the offset being proposed — 45 seconds against a 4.5-year clock error reads as
  tight, and a wrong conclusion gets accepted. Compare it against seconds, not
  against the number you are trying to prove.
- **Settle "are these the same files?" with categorical evidence** — durations
  equal to the second, byte sizes equal — not similarity scores. The score
  ranges for "one file, two ASR runs" and "one script, two years" overlap
  completely (measured: 0.71–0.86 for both). Also noted: `difflib.quick_ratio()`
  compares character multisets and is an upper bound, not a comparison.

## [0.6.5] — 2026-08-05

### Documentation

The reasoning behind 0.6.1–0.6.4 now lives in the reference docs, not just in
the commits that fixed the code.

- `multi-camera.md` gains three sections on placing material whose clock lies:
  **serial-number bracketing** (a clip with no capture time at all can still be
  placed, because stills and video share one counter on the same card — and a
  below-threshold cross-correlation becomes usable once an independent line of
  evidence agrees with it); **the same-talk trap** (cross-correlation votes on
  shared N-grams, so a lecturer repeating a course scores as high as a second
  camera at one event — what separates them is speech that happens only once);
  and **phantom years** (a folder named from material timestamps inherits the
  camera's wrong clock, inventing a plausible year that every derived artifact
  then carries).
- `pipeline.md` states the label rule plainly: the viewer parses `Vn` and bare
  filenames only, so an `A1` invented for an audio source yields a note that
  reads perfectly and whose every timecode is dead.
- `decisions.md` gains three entries on the shape of silent failure — an empty
  scan reads like a clean one, an unmatched pattern reads like prose, and an
  empty comparison must never fall through to the success path.

## [0.6.4] — 2026-08-05

### Fixed

- Timecodes pointing into an audio file are now clickable in the exported
  viewer. The citation pattern listed only video extensions, so an audio-only
  session — a recorder file with photographed slides, which this pipeline
  supports as a first-class source — rendered every `(recording.mp3 03:23)` as
  dead plain text. Nothing errored, because a non-match looks exactly like
  prose. On a two-day conference with four audio-only afternoon talks, 312 of
  536 timecodes were dead this way.

## [0.6.3] — 2026-08-05

### Fixed

- `audit_note.py` exempts the overview segment from the transcript-coverage
  gate. That segment's transcript is every other segment's concatenated, so a
  one-page catalog scores about 0.01 against a 0.1 floor and can never pass —
  a WARN on every course with an overview, which is how readers learn to skim
  past WARNs. Per-segment L3s are still gated.

## [0.6.2] — 2026-08-05

### Fixed

- `audit_segmentation.py` compared timeline basenames against manifest `src`
  values that may carry a subdirectory prefix. For those courses the two lists
  never intersected, the order check found nothing, and the run fell through to
  an INFO line saying the clip order "was corrected by real time" — about a
  course it had not checked. Both sides are now compared as basenames. On a real
  course this turned a clean-looking INFO into a REGENERATE naming four
  genuinely out-of-order clips.

## [0.6.1] — 2026-08-05

### Fixed

- `course_timeline.py --reorder-manifest` crashed with a `KeyError` on any
  course whose manifest references clips inside a subdirectory, e.g.
  `Demo-Prac\00008.MTS`: the sort keyed on the clip's `src` against a dict
  built from bare filenames. The quieter half of the same bug was worse — two
  clips in different subdirectories that share a basename (what you get when a
  second camera card restarts its numbering) would both have resolved to one
  capture time, with no error. The sort now keys on the manifest index, which
  is unique by construction.

### Documentation

- README (both languages) now links back to the beginner series and to the post
  explaining this pipeline. No runtime change.

## [0.6.0] — 2026-08-05

Real capture time becomes the ordering authority. Segmentation used to be
ordered by the manifest's clip array — which is the filename sort — and on a
two-day, three-device conference that put the afternoon before the morning.

### Added

- **`scripts/batch/course_timeline.py`** — one real-time timeline per course
  across every capture device, with `--reorder-manifest` to fix clip order at
  the root, and photos mapped onto the recordings they were shot during. It
  refuses to reorder when any clip has no trustworthy time, rather than
  inventing a position for it.
- **`scripts/_mdpm.py`** — reads the recording clock out of AVCHD `.MTS`
  streams. **An AVCHD camcorder writes no container timestamp at all**, so
  `ffprobe` reports nothing and every `.MTS` used to read as "no capture time":
  the whole alignment layer was silently inert on conference footage, which is
  overwhelmingly AVCHD. Reads progressively (256 KB first), because originals
  often live on slow network storage.
- **`scripts/batch/audit_segmentation.py`** — after the order is fixed, decides
  mechanically whether an existing segmentation proposal is still usable:
  `OK` / `REVIEW` / `REGENERATE`, from staleness, source coverage, split
  recordings vs real breaks, parallel tracks, and photo availability.
- **`media_capture_index.py --clock-offset SELECTOR=SECONDS`** — apply a
  *measured* per-device clock correction; the device's own claim is preserved
  in `capture_raw_start`. Recorder filenames (`…240526_1119.mp3`) now count as
  a capture-time source, and `mtime` may refine their seconds only when it
  falls inside the minute the device itself named.
- **`build_slides_from_images.py --capture-clock` / `--between`** — place each
  photo at its own EXIF time minus the recording start instead of spreading
  images evenly across the audio, and split one day-long photo folder across
  sessions without copying it once per session.

### Changed

- `build_L1.py` prints each clip's absolute capture clock in its section
  heading, and no longer asserts in a comment that array position equals
  chronological order.
- `reference/multi-camera.md` gains a device-clock-calibration section: every
  device has its own clock and any of them can be wrong. Offsets must be
  *measured* — transcript cross-correlation for anything with audio, OCR
  slide-text matching for photos — and validated against sources that took no
  part in the calibration.
- `reference/segmented-mode.md` gains Step 0a: build the timeline before L1.
  It also records that a printed agenda is not a clock (one conference ran
  ~25 min early on day one and ~35 min late on day two, while its break gaps
  matched to the minute).
- Documented that **AVCHD `mtime` is the recording END**, not the start —
  treating it as a start is wrong by the clip's entire duration.

## [0.5.2] — 2026-08-04

### Fixed

- **Media existence is now verified, not assumed.** `export_web.py` wrote
  `exists: true` into every `media_parts` entry without ever checking the
  disk. Web-native files (mp3 / already-web-playable mp4) are served in
  place — no copy is made into the support folder — so if their source files
  are later moved or deleted, the delivered page keeps referencing them and
  the player just fails silently, with no warning at export time either.
  (Real case: a course's 錄音 recorder-backup mp3s were cleaned up along
  with the raw camera files after delivery; the page carried 11 dead audio
  references for a month with zero alarm.) The exporter now resolves every
  part against the disk, marks missing ones `exists: false`, and prints a
  loud per-file warning; the viewer shows a "來源檔已遺失，無法播放" notice
  instead of a dead player, and sequential autoplay skips missing parts.
- **Previous delivered artifacts are kept as `.bak` before overwrite.** A
  re-export now saves the existing `影片筆記整合.html`, `_timeline.json`
  and `_media_map.json` as one-generation `.bak` copies first — a buggy
  re-export once clobbered a course's only good timeline/media map in
  place, leaving nothing to diagnose or roll back to.

## [0.5.1] — 2026-08-04

### Fixed

- **Overview segment no longer hijacks friendly video-filename ownership.**
  `export_web.py` names each remuxed clip after whichever segment first
  references it; since the 全場總整理 overview sorts first (`display_order`
  0) and its `files[]` is the union of every clip in the course, it was
  claiming ownership of EVERY video, renaming all of them to
  `NN_全場總整理.mp4`. On a re-export where the raw source no longer exists
  to re-encode under the new name (common for older courses whose staging
  directory was cleaned up after original delivery), this broke the
  resumable-cache match, the re-encode failed, and the exported page's video
  links went dead — orphaning correctly-named `.mp4` files still sitting on
  disk. Content segments now claim naming ownership first; the overview only
  claims a file none of them reference. Found and fixed 2026-08-04 while
  retrofitting the overview segment onto already-delivered courses — 3 of a
  5-course pilot batch broke this way and were re-exported clean after the fix.

## [0.5.0] — 2026-08-03

### Added

- **Whole-talk overview segment (全場總整理)**, auto-appended by
  `build_single_talk_web.py`: however many segments a talk is split into, the
  viewer still needs one place listing everything — and the `_HUB` note never
  renders in the viewer (markdown export only), so the overview is its own
  **L3-only segment**: `display_order` 0 (sorts first in the drawer), time
  range = the whole talk, no L2 file (the segment card falls back to the L3
  section when L2 is absent). `--no-overview` opts out; `--export` counts the
  overview note as a required L3.
- **Overview content spec, typed by one question — "is this a single body of
  knowledge?"** (`reference/pipeline.md#web-export`): single talk → 5–8
  whole-talk pearls with clickable timecodes + one line per segment;
  same-lecturer series with a thematic arc → thematic reorganization
  (assessment→intervention tables, workflows) with per-claim source tags and
  an "editor's ordering" disclaimer; multi-speaker multi-topic workshop →
  **catalog only, never forced cross-talk synthesis**.

### Changed

- SKILL.md Step 15: the segment plan is authored by the synthesis stage itself
  (derived from transcript + slide topics) — single-talk HTML export is fully
  automatic, no human segmentation step.

## [0.4.0] — 2026-08-03

### Added

- **`build_single_talk_web.py` — the synced web viewer for a single talk**
  (`export_web.py` was written for multi-talk workshops, but a single talk
  exports fine: every segment's `files[]` points at the same video with
  absolute timestamps, and `media_parts` dedups by filename — the video is
  processed once, never split). Feed it the lecture dir plus a segment plan
  (JSON list: seg/start/end/slug/title_zh) and it assembles the manifest,
  `segments.json`, per-segment L2 transcript slices (30-second bullet buckets,
  `` `(V1 MM:SS)` `` timecodes) and a HUB skeleton; synthesis writes the L3
  notes; `--export` then runs `export_web.py` (and refuses while L3 files are
  missing). Guards: overlapping segments / duplicate slugs are refused, and a
  plan that stops >60 s before the transcript ends warns about the dropped
  tail. SKILL.md gains Step 15 — with a video present, the web viewer is now a
  default pipeline output, and `route_inputs.py` prints the step.

### Changed

- **`export_web.py` compresses by default, and the codec is now H.265** (x265
  CRF 24 `-preset medium -tag:v hvc1`). On 2170×1220 screen-recording samples
  H.265 CRF 24 matched x264 CRF 20 on static segments and came out 43–51%
  smaller on motion segments (measured 2026-08-03). Note HEVC playback needs
  decode support on the viewing machine (Windows: HEVC Video Extensions +
  hardware decode) — the right default for keeping your own archives small;
  when sharing to machines you can't verify, `--codec h264` keeps the old
  universal x264 CRF 18 output, and `--no-compress` restores the pre-0.4
  copy-stream behavior. The `--compress` flag is accepted as a no-op for
  backward compatibility.

## [0.3.0] — 2026-08-03

### Added

- **Degenerate-repetition tripwire in `audit_note.py`** (all modes): WARN when a
  normalized content line repeats ≥4× (table rows, callout headers and citation
  lines are excluded first — those repeat legitimately) or when char-8-gram
  diversity drops below 0.70. Catches looping/filler output, which would also
  defeat the 0.2.0 coverage floor by padding. Calibrated on 560 accepted notes
  (1 line-dup hit, 0 diversity hits; a genuinely looping paragraph lands at
  diversity 0.03). *Failure mode reported from batch runs by
  [jieyu166/rad-workflow](https://github.com/jieyu166/rad-workflow)'s author
  (「重複無意義的文字」) — thanks again!*

## [0.2.0] — 2026-08-03

### Added

- **Slide-layer search in the web viewer** (TIMELINE schema v3, additive). The
  search box now indexes `slide_blocks` — per-canonical-slide OCR text and VLM
  summaries pulled from `clips/*/slides_grounded.json` at export time — so a term
  that appears only ON a slide and was never spoken is still findable. A slide
  hit jumps the player to that slide's own display window; no image is copied.
  Calibrated filters keep hands-on-demo noise out (OCR must read like language,
  not a device-UI overlay; VLM summaries only for information graphics). Courses
  without grounding files export exactly as before. *Idea borrowed from
  [jieyu166/rad-workflow](https://github.com/jieyu166/rad-workflow)'s course-hub
  cross-lecture search — thanks!*
- **Deep links + copy-link button in the viewer**: `<course>.html?f=<media>&t=<sec>`
  opens straight at that spot (works from `file://` too), and a 🔗 header button
  copies the current playing position as such a link.
- **Transcript-coverage gate in `audit_note.py`** (`--min-coverage`, default
  0.10): note payload chars ÷ transcript chars below the floor → WARN, catching
  the "90-min transcript, one-screen note" over-compression collapse. Calibrated
  on 410 real segments (median 0.36, p10 0.17 — the default floor trips only the
  bottom ~2%). Runs in `--mode lecture` (via `--grounding`) and per-segment on
  `--mode lecture-seg` L3 notes. *Adapted from rad-workflow's Stage-1 coverage
  ratio, recalibrated for synthesis notes.*
- CI: gitleaks secret-scan workflow over full history (`.github/workflows/secret-scan.yml`) + README badges.
- README: real viewer screenshots (synced view / course hub / summary view), a "Bring whatever you captured" section, and an honest no-GPU ladder: built-in CPU fallback → hosted Groq Whisper (`scripts/groq_asr.py`) → untested Apple Silicon.

## [0.1.0] — 2026-08-02

Initial public release.

### Added

- **Local-first lecture pipeline**: faster-whisper transcription, frame
  extraction with perceptual dedup, two-tier OCR (RapidOCR triage → Surya
  high-quality), local VLM slide semantics via ollama, and transcript↔slide
  grounding. Stages 0–E run entirely on your machine at zero LLM cost.
- **`route_inputs.py` as a single front door** — classifies a material folder
  (video, audio, PDF deck, loose slide images, multi-talk workshop) and prints
  the ordered commands for the right path. Plan-only: never executes, never
  writes.
- **Multi-source alignment** treating capture timestamps as hypotheses and
  transcript cross-correlation as evidence, with conflicts flagged for a human
  rather than auto-reconciled.
- **Segmented mode** for multi-talk workshop folders: per-segment notes, a hub
  note, and a static web viewer export.
- **`ocr_bench/`** — an OCR engine A/B/C harness with a shared output schema,
  so engine choice is decided on your own slide distribution instead of on
  published benchmarks. Fixtures are bring-your-own.
- MIT license, `requirements.txt` / `requirements-optional.txt`, and
  `config.example.yaml` with every machine-specific value blank and
  environment-overridable.

### Notes on this being a 0.1.0 and not a 1.0

The code is a year old and has processed a lot of real lectures, but this is the
first release outside a single machine. It has been hardened for that by a full
pre-release audit — four independent reviewers over ~6,800 lines, ~130 findings,
with every release blocker and every silent-wrong-output defect fixed before this
repo existed. The audit story, including two findings the audit itself got wrong,
is in [docs/AUDIT_SUMMARY.md](docs/AUDIT_SUMMARY.md).

What is not proven yet is portability: only Windows 11 + an 8 GB NVIDIA card has
been exercised. Bug reports from other platforms are the most useful thing you
can send.

---

## [Unreleased]

### 新增

- CI：gitleaks 全 history 密鑰掃描 workflow ＋ README 徽章。
- README：實際檢視頁截圖（同步視圖／課程首頁／總整理頁）、「手上有什麼就帶什麼來」一節、無 GPU 退階說明（內建 CPU fallback → Groq 雲端 Whisper → Apple Silicon 未實測）；中文版換中文 banner。

## [0.1.0] — 2026-08-02（繁體中文）

首次公開發佈。

### 新增

- **本機優先的演講管線**：faster-whisper 轉錄、抽幀＋感知雜湊去重、兩層 OCR
  （RapidOCR 快篩 → Surya 精修）、ollama 本地視覺模型判讀投影片語意、逐字稿與
  投影片對位。Stage 0–E 全在本機跑，零 LLM 成本。
- **`route_inputs.py` 統一入口**：判斷素材資料夾型態（影片／音檔／PDF 講義／
  散圖／多場工作坊），印出對應路線的完整指令。==只規劃、不執行、不寫檔==。
- **多來源時間軸對齊**：拍攝時間只當「宣稱」，逐字稿交叉相關才是「證據」，兩者
  衝突時標記給人判斷而非自動修正。
- **分段模式**：多場次工作坊資料夾 → 每場一篇筆記＋總覽 Hub＋靜態網頁檢視器。
- **`ocr_bench/`**：OCR 引擎 A/B/C 比較框架，共用同一份輸出 schema，讓引擎選擇
  建立在自己的投影片分布上而不是論文數字上。測試圖自備。
- MIT 授權、`requirements.txt` / `requirements-optional.txt`、
  `config.example.yaml`（所有機器相關值預設留空，可用環境變數覆蓋）。

### 為什麼是 0.1.0 而不是 1.0

程式碼跑了一年、處理過大量真實課程，但這是第一次離開單一機器。發佈前做了完整
體檢：四位獨立審查者、約 6,800 行、約 130 項發現，所有「跑不起來」和「靜默產出
錯誤」等級的缺陷都在這個 repo 存在之前就修完了。體檢過程（包含審查本身出錯的
兩個案例）記在 [docs/AUDIT_SUMMARY.md](docs/AUDIT_SUMMARY.md)。

還沒被驗證的是可攜性：目前只在 Windows 11 + 8GB NVIDIA 顯卡上實際跑過。其他平台
的 bug 回報是最有價值的貢獻。
