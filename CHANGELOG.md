# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
