# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Documentation

- README (both languages) now links back to the beginner series and to the post
  explaining this pipeline. No runtime change.

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
