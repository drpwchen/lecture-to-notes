# Note spec — quality bar, tier rules, synthesis prompts

Everything Stage F needs: what a finished note must contain, how each slide's
tier is decided, how wide each figure is embedded, and what the two synthesis
prompts must carry. Consolidated from the note quality spec (user-authored,
2026-05-24) and the Stage F sections of the old SKILL.md.

> **North star**: the user will in future read ONLY `# 總整理`. It must be
> ==self-contained== — complete content AND the figures needed to understand it.
> Clinical utility is the first priority for ordering, but nothing the speaker
> said may be silently discarded.

## Contents

- [Definition of Done](#done)
- [Good vs bad, concretely](#contrasts)
- [Rules A–G](#rules)
- [Tier scoring](#tier-scoring)
- [Width table](#widths)
- [Embed format and placement](#embed-format)
- [Note structure template](#structure)
- [Prompt requirements](#prompt-requirements)
- [Running the auditor](#auditor)

---

## Definition of Done {#done}

The note is done only when **every** box is true. ✅auto items are mechanically
enforced by `scripts/audit_note.py`; the rest are the Write-pass subagent's
self-audit.

1. **Structure present** ✅auto — frontmatter `tags`; a `> [!summary] Clinical
   Pearls` callout (3–5 bullets, the first thing the reader sees); `# 總整理`;
   `# 逐投影片筆記` (or `# 逐段筆記` for audio-only); `# Resource`.
2. **總整理 is self-contained** — a reader who reads only 總整理 grasps every
   clinical point, and the figures needed to understand it (==especially
   ultrasound and other imaging==) are embedded right there, each with a width.
3. **Nothing dropped** — every point the speaker made is in 總整理 or in a bottom
   `## 講者其他分享` section; speaker elaboration carries 🗣️ ✅auto-warn.
4. **Figures** — only cited Tier 1/2 slides embedded; ==every embed carries
   `|width`== ✅auto; fig callout = short title line + second-line legend ✅auto;
   no broken refs ✅auto; figures woven into the narrative, ==not dumped as a
   bottom gallery== ✅auto-warn.
5. **References** — every paper the lecture cited pulled into the reference
   library and reviewed; each mention carries an `[[link]]`; papers with no full
   text were ASKED about, not skipped; un-noted papers marked `⚠️ 待補`.
6. **Formatting clean** ✅auto — use "cadaver", never 屍體; no `[!todo]`; no
   source appended to a heading; escaped `\|` in table wikilinks; spaces around
   `==`.
7. **The auditor returns 0 FAIL**, and every WARN has been consciously accepted
   or fixed.

## Good vs bad, concretely {#contrasts}

| 面向 | ❌ 壞 | ✅ 好 |
|---|---|---|
| **總整理自含性** | 總整理寫「腋神經短軸見下圖」，圖只在 `逐投影片 s12` → 讀者要捲到下半部才看得懂 | 總整理該段正下方就放 `> [!figure]` + `![[…\|600]]`，原地看懂 |
| **內容流失** | 講者花 5 分鐘講自己門診 hydrodilation 失敗經驗，因「非考點」整段刪掉 | clinical-utility 排序在前，但該經驗收進底部 `## 講者其他分享`，用 🗣️ 標記，不刪 |
| **Embed width** | `![[99Attachment/x/s03.jpg]]` → 滿欄巨圖把上下文字淹沒 | `![[99Attachment/x/s03.jpg\|600]]`（US 用 600） |
| **縮寫** | 通篇 PHCA 不展開，讀者不知道是 posterior humeral circumflex artery | 首次出現 `PHCA (posterior humeral circumflex artery 後旋肱動脈)`，且縮寫表＋Clinical Pearls 都列 |
| **Fig callout** | `> [!figure] Fig 4.9 腋神經短軸長軸 AN=axillary nerve CHL=coracohumeral lig…`（長串擠第一行） | 第一行 `> [!figure] Fig 4.9 — 腋神經短軸/長軸`；第二行 `> AN=axillary nerve；CHL=…` |
| **空章節** | `## 信效度` 開了標題下面沒內容 → 半成品 | 有內容才開標題；無內容的模板段落刪掉或補滿 |

## Rules A–G {#rules}

### A. Abbreviations and terminology

- **A1 [review]** Uncommon abbreviations MUST be written in full on first use AND
  inside `> [!summary] Clinical Pearls` (the first thing the reader sees). Don't
  assume the reader knows lab-specific acronyms.
- **A1b [auto-warn]** Known lab-specific acronyms (seed list in `audit_note.py`
  `RARE_ACRONYMS`) that appear but are never expanded `ACRONYM (full…)` nor
  listed in an abbreviation table → WARN. Extend the seed list when a lecture
  introduces new jargon.
- **A2 [auto]** Include a `## 縮寫表` near the top (right after Clinical Pearls)
  listing every non-trivial abbreviation = full term (English) = 中文.
- **A3 [auto]** Use "cadaver", never 屍體.

### B. Headings and structure

- **B1 [auto]** ==No source or citation appended to a heading line.== Put the
  source on the line BELOW the heading.
- **B2 [review]** Related-note `[[links]]` for a section also go on the line
  below the heading, not jammed into it.
- **B3 [review]** `# 總整理` is self-contained: every teaching point that appears
  in `# 逐投影片筆記` is folded into the right 總整理 subsection. The reader never
  needs to scroll to 逐投影片 for content.
- **B4 [review]** Content outside your judged clinical utility is NOT discarded —
  it gets its own section at the ==bottom== of 總整理 (`## 講者其他分享`).
  Clinical-utility-first ordering ≠ deletion. A genuinely separate side-topic may
  instead become a dedicated cross-linked note.
- **B5 [auto-warn]** Speaker-only elaboration (not on the slide) is marked 🗣️. A
  note with zero 🗣️ markers usually means the transcript's value was dropped and
  only slide text survived → WARN. Legitimate only for slide-faithful talks.

### C. Figures — critical for imaging lectures

- **C1 [review]** 總整理 must contain the figures needed to understand it,
  ==especially ultrasound and other imaging==, where text without the image is
  not understandable. Put the figure UP in 總整理 next to its text, not only down
  in 逐投影片. No image-less imaging/technique subsection.
- **C2 [auto]** Fig callout format: **first line = short title**; the long legend
  and symbol explanation go on the **second line**.
  ```
  > [!figure] Fig 4.9 — 腋神經短軸/長軸
  > 完整圖說與符號：AN=axillary nerve…（第二行起）
  > ![[99Attachment/…|600]]
  ```
- **C3 [auto + review]** Every frame the narrative references is embedded inline
  and its content matches the caption — no wrong or swapped figures.
  ==Auto guardrail==: `--mode lecture --grounding <lecture_dir>` cross-checks each
  caption's vocabulary against that frame's OCR text and VLM labels; ==zero
  overlap → FAIL== (a gross swap), one-token overlap → WARN. ==Review still
  required==: a caption sharing incidental words while describing the wrong thing
  passes the script, so the Write-pass MUST Read each Tier-1/Tier-2 frame and
  confirm the caption describes the actual pixels. The failing case that
  motivated this: six captions written from the transcript timeline without ever
  looking at the frames.
- **C4 [review]** When the source is video and a cited paper has the same figure
  at higher quality, place BOTH ==side by side (並列)== so they can be compared.
  Don't silently replace.
- **C5 [auto]** No broken image refs, no leftover `[!todo]`. Figures woven into
  the narrative, not dumped as a bottom gallery (≥3 consecutive embeds with no
  intervening prose → WARN).
- **C6 [auto]** ==Every image embed carries `\|width`.== A widthless embed renders
  full-column and buries the text → FAIL. Width table below.
- **C7 [auto-warn]** `# 總整理` of an imaging lecture containing zero embeds →
  WARN (likely a C1 violation, figures stranded in 逐投影片). Legitimate only for
  pure-text or audio-only talks.

### D. References

- **D1 [review]** Pull every reference the lecture provided into the paper
  library and review each, unless a note already exists.
- **D2 [review]** Verify each paper's actual findings against what the speaker
  claimed; write a short summary; add an internal `[[link]]` at EVERY mention.
- **D3 [review]** If a paper's full text can't be obtained, ==ask the user==;
  don't silently skip it.
- **D4 [auto]** Any cited paper with no note yet is marked ` ⚠️ 待補`.
- **D5 [review]** `# Resource` lists ==ONLY sources the speaker actually named==.
  ==Never pad Resource with textbook references or topically-related papers the
  lecture did not mention== — that fabricates a citation trail. A genuinely useful
  external reference you add yourself is agent-inferred: omit it, or put it
  OUTSIDE Resource clearly marked `⚠️ agent-inferred`. Failing case: a Resource
  section that invented two textbooks and a meta-analysis the speaker never cited.

### E. Callouts and formatting

- **E1 [review]** Inside callouts, ==use indentation==: "X 有三個主要內容" must be
  followed by three indented lines, one per point, not a run-on.
- **E2 [auto]** `==highlight==` with spaces around the `==`; escape `<` `>` or use
  ≤ ≥; escape `|` inside `[[...]]` in table cells as `\|`; no code blocks for
  clinical content — use callouts. (`---` horizontal rules are allowed; just
  never put one directly after a table row, which breaks that table.)
- **E3 [auto]** ==Every markdown table needs a separator row== `|---|---|` between
  the header and the first data row. Without it pandoc renders the whole table as
  a run-on paragraph on HTML export.

### F. Process rules

- **F1** Ask the spoken language before transcribing (SKILL.md HARD RULE 1).
- **F2** In a batch, stop any pinned VLM and embedding models before each
  lecture's GPU check — residual VRAM otherwise makes the check chain-block and
  skip every remaining lecture.
- **F3** ==`slides_final.json` entries need BOTH `filename` and
  `attachment_name`.== `finalize_to_vault.py` reads `filename` to find the source
  frame; `render_embeds.py` reads `attachment_name` to write the embed path. A
  missing one gives a page of embeds pointing at files that were never copied,
  with a green audit.
- **F4** Live-demo footage: detect rotation (portrait container with landscape
  content) and rotation-correct frames BEFORE extraction and QC; treat the
  transcript as primary, hand-pick representative frames, cite timestamps for
  dynamic moments that can't be stilled.
- **F5** One talk split across multiple files → ONE note.
- **F6** Anti-hallucination: never invent venue names, speaker English names, or
  numbers that were not stated; mark any added textbook fact `⚠️ agent-inferred`.
- **F7** Respect a speaker's "please don't record" request — exclude that content.
- **F8** Don't drop off-topic-but-valued segments (tooling, methodology): keep
  them under B4 or split them into a dedicated note.

### G. Structural completeness [auto]

- **G1** Required headings all present: `# 總整理` AND (`# 逐投影片筆記` OR
  `# 逐段筆記`) AND `# Resource`. Missing any → FAIL.
- **G2** A `> [!summary]` Clinical Pearls callout exists before the first `#`
  heading. Missing → FAIL.
- **G3** Frontmatter present with a `tags:` key. Missing → FAIL.
- **G4 [warn]** No empty or near-empty section: a heading whose body until the
  next heading has zero non-blank lines and is not a parent of a deeper heading
  → WARN (half-finished template section; fill it or delete it).

## Tier scoring {#tier-scoring}

This is where the embed-or-not decision is made per canonical slide. The VLM and
grounding stages provide *signals*; this section combines them into a
`combined_score` and a `tier`. ==The VLM never makes the embed decision itself==
— that contains its hallucinations (a low score is soft evidence, not a hard no),
makes future model swaps schema-only changes, and lets "why was this slide
omitted?" be answered from the score breakdown.

**Step 1 — weights** (from `config.yaml` `scoring.weights`):

| Signal | Weight |
|---|---:|
| `vlm_signals.visual_complexity` | 0.15 |
| `vlm_signals.non_textual_information_density` | 0.25 |
| `transcript_signals.speaker_reference_density` | 0.30 |
| `transcript_signals.speaker_emphasis_score` | 0.15 |
| `min(time_spent_seconds / 120, 1.0)` | 0.15 |

**Step 2 — content-type bonuses.** Use `t in vlm_signals.content_type`, ==never
equality== — a slide can be `["title", "flowchart"]`.

| Type | Bonus |
|---|---:|
| flowchart, kaplan_meier | +0.15 |
| ultrasound, xray, mri | +0.10 |
| anatomy, scatter_plot | +0.08 |
| decorative | −0.40 |

**Step 3 — penalties.** `−0.30 × speaker_skip_score`; `−0.20 ×
speaker_confusion_score`; `−0.20` if `text_redundancy > 0.7`.

**Step 4 — clip to [0, 1], assign tier.** `≥ 0.65` → Tier 1; `≥ 0.40` → Tier 2;
else Tier 3.

**Step 5 — hard overrides** (these beat the thresholds):

- `apparent_educational_function` contains any of `{diagnostic_algorithm,
  treatment_protocol, staging_schema}` → force **Tier 1**, reason
  `"function includes <X>"`.
- `content_type` equals only `[decorative]`, `[title]`, or `[title, decorative]`
  → force **Tier 3**.
- ==P1 — text-mnemonic / pure-bullet slide==: `text_redundancy ≥ 0.7` AND
  `content_type ⊆ {text, title, decorative}` AND `contains_clinical_imaging=false`
  AND `contains_algorithm=false` AND no chart-class type → force **Tier 3**,
  reason `"text-redundant, no visual signal"`. ==Why hard, not soft==: the VLM
  over-rates `visual_complexity` on a colored mnemonic slide and pushes it to
  Tier 2, but the markdown bullets already carry the content. Embedding a
  decorative restatement adds noise without information.
- ==P1 synthesis-time check — markdown redundancy==: before embedding any Tier
  1/2 slide, compare its `ocr.quick_text` against the markdown bullets you just
  wrote. If >70 % of the slide's text appears as bullets in the same section,
  ==do not embed==, and record `embed_suppressed_reason = "markdown_overlap"`.
  ==EXEMPTION — visual-spatial content==: this check ==does not apply== to slides
  whose `content_type` contains flowchart / chart / kaplan_meier / scatter_plot /
  anatomy / xray / mri / ultrasound, or with `contains_clinical_imaging=true`, or
  `contains_algorithm=true`. These carry spatial, procedural or imaging
  information markdown cannot capture; text overlap is irrelevant. ==Always embed
  Tier 1 for these.== Failing case (2026-05-17): 11 flowchart/algorithm slides
  erroneously suppressed by a patch subagent applying this rule too broadly,
  caught only because the user checked by hand.
- ==IMG floor — a distinct imaging frame is never silently dropped==: if
  `dedup.is_canonical=true` AND (`contains_clinical_imaging=true` OR
  `content_type` contains any of `{ultrasound, xray, mri, anatomy, flowchart,
  chart, kaplan_meier, scatter_plot}`) AND the slide was NOT force-Tier-3 above →
  ==floor the tier at Tier 2== even when `combined_score < 0.40`, reason
  `"imaging floor (canonical <type>)"`. ==Why hard==: the score weights
  speaker-mention 0.30 + dwell 0.15, so a frame the speaker only glances at
  scores low even though its pixels carry information markdown cannot. Failing
  case (2026-05-21, a pediatric ultrasound lecture): 65 canonical frames, only 19
  embedded — low-mention ultrasound frames fell to Tier 3 and vanished. This
  floor does NOT force the slide into 總整理 (that stays score-driven); it only
  guarantees 逐投影片 coverage.

**Step 6 — embed by tier.** Tier 1 → embed in BOTH `# 總整理` (the relevant
section) AND `# 逐投影片筆記`. Tier 2 → `# 逐投影片筆記` only. Tier 3 → ==do not
embed and do not copy==. Only slides the note actually cites get copied; a Tier 3
slide needed later is still in the lecture dir's `slides/`, and pulling it in is a
manual one-off. (Copying everything used to bloat the attachment folder with
orphans.)

**Step 7 — write back the debug trail** to `slides_final.json`:
`combined_score`, `tier`, `tier_override_reason`, `embed_width` (or null if
suppressed), `embed_suppressed_reason`, and
`retrieval.summary_sentence` (one sentence, ≤25 chars).

## Width table {#widths}

==Stated once; every other document links here.== All embeds MUST carry a width —
Obsidian's default full-column width buries the surrounding text.

| 內容類型 | width | 對應 content_type / function |
|---|---:|---|
| ==Ultrasound / MRI / X-ray / clinical imaging== (cell-level detail needed) | **600** | `ultrasound`, `xray`, `mri`, `contains_clinical_imaging=true` |
| ==Anatomy diagram== (labels must stay readable) | **500** | `anatomy` |
| ==Mechanism / flowchart / algorithm== | **500** | `flowchart`, `contains_algorithm=true`, function = `diagnostic_algorithm`/`treatment_protocol`/`staging_schema` |
| ==Chart / scatter / KM curve / stress-strain== | **500** | `chart`, `kaplan_meier`, `scatter_plot` |
| ==Visual demonstration== (poses, manoeuvres) | **450** | mixed `anatomy + chart`, low complexity |
| Comparison table as image (==only if not also reproduced as a markdown table==) | **400** | `table` only |
| Pure text / mnemonic / bullet list | **don't embed** | Tier 3 by the P1 rule |

Synthesis must record the chosen width in `slides_final.json` under
`embed_width`, so a later run can audit sizing without re-reading the note.

## Embed format and placement {#embed-format}

```markdown
> [!figure] frame_0012.jpg — diagnostic algorithm (Tier 1, score 0.78)
> Speaker emphasized 93 s. Visible labels: supraspinatus, tear.
> ![[99Attachment/lecture_xxx/frame_0012.jpg|500]]
```

==Callout placement rule==: Obsidian renders `> [!figure]` correctly ==only at
column 0 with a blank line before AND after==. An indented `\t> [!figure]` does
not render. Place the callout where the bullet hierarchy returns to column 0 —
right after the nested group it relates to ends, before the next top-level
bullet. To reference a figure from inside nested bullets, write `- (見 sN 圖 ↓)`
as a normal bullet at the matching indent (no blank lines around it) and put the
real callout after the group ends.

The `[!figure]` callout type is not one of Obsidian's built-ins; without a CSS
snippet it renders as a default note callout. Cosmetic, not broken.

Attachment path — ==full slug, never truncated==:
`99Attachment/lecture_{full_lecture_slug}/{speaker}_{topic}_sNN.jpg`. This is
==a private vault convention==; `render_embeds.py --attach-root` /
`--attach-dir` make it configurable.

Note filename: `{date}_{speaker}_{topic}.md` in the inbox folder, no `lecture_`
prefix. Attachment renumbering is sequential among *cited* slides, not by
original `slide_id` — `s01..s30` reads better than `s03, s07, s12, …`. The
original→renumbered map lives in `slides_final.json` under `attachment_name`.

## Note structure template {#structure}

==The Chinese headings are a template contract, not decoration== — they are
exactly what `audit_note.py --mode lecture` requires, so changing them means
changing the mode you audit with. `--mode lecture` FAILs on a missing `# 總整理`
or `# 逐投影片筆記`/`# 逐段筆記`. ==`--mode generic` does not check either
heading==, so an English-language note can be audited with `--mode generic` and
still get every universal check (frontmatter `tags`, `> [!summary]` callout,
`# Resource`, widths, broken refs, formatting) — it just loses the two
Chinese-heading structural checks and the lecture-only WARNs (figures in 總整理,
gallery dump, 🗣️ markers) and the C3 caption↔frame guardrail, which is gated on
lecture mode. `--mode lecture-seg` is the segmented-course contract; see
`segmented-mode.md`.

```markdown
---
title: {Speaker} — {Topic title}
created: {YYYY-MM-DD}
tags:
  - source/{conference_tag}
  - med/{relevant_specialty}
aliases:
  - {speaker} {topic} 演講
speaker: {Name and affiliation}
---

# {Speaker} — {Topic title}
- Speaker / 座長 / Date / Duration

> [!summary] Clinical Pearls
> - 3–5 行 take-home（含展開的冷僻縮寫）
> - ==關鍵 cut-off / 決策點==

## 縮寫表
| 縮寫 | 全稱 (English) | 中文 |
|---|---|---|

# 總整理

## {Topic Section 1}
- Key points organized by topic, not by slide order
- ==highlighted critical values==
- [[Internal links]] to related notes

> [!figure] frame_0012.jpg — diagnostic algorithm (Tier 1)
> Visible labels: supraspinatus, tear.
> ![[99Attachment/lecture_{slug}/{speaker}_{topic}_s01.jpg|600]]

## 講者其他分享
- 非臨床效用但講者強調的內容放這（B4：排序在後，不刪）

# 逐投影片筆記

## Slide 1 — {Slide Title} `MM:SS`

![[99Attachment/lecture_{slug}/{speaker}_{topic}_s01.jpg|600]]

- Key point from slide
- 🗣️ 講者：「verbatim or paraphrased elaboration not on the slide」

# Resource
- 原始影片 / 逐字稿 / 投影片擷取 / OCR 結果 / 引用文獻
```

**Synthesis guidelines.** Follow presentation order in 逐投影片 (consecutive
slides on one topic may share a section). Slide content as bullets; `> 投影片：`
blockquote to describe the visual. Speaker elaboration marked 🗣️ 講者：, including
anecdotes and practical tips. Timestamps in backticks, `` `MM:SS` ``. Preserve
exact numbers. `==highlight==` for critical values, `**bold**` for key terms.
For slide text ==prefer `ocr.clean_text`== (Stage B2), falling back to
`quick_text` / `pdf_text`. Mark anything unclear `⚠️`. Nested bullets, no prose
paragraphs.

## Prompt requirements {#prompt-requirements}

Ported from the canonical Stage F prompt set. ==Provenance==: two prompt
documents existed side by side; the old SKILL.md named **v2** as canonical for
the write pass, so v2 is what is ported here, with the Tier-pass prompt from the
pre-v2 document that v2 explicitly leaves unchanged apart from dropping an
exam-framing slot. Two corrections were applied while porting, because the
prompts predate later decisions: they referenced a cleaned transcript file
(`transcript_clean.txt`) and a cleanup model in the Resource line — ==neither
exists; there is no cleaned transcript==. Read `transcript.txt` paired with
`asr_suspects.txt`.

### Tier-pass prompt — what it must contain

- **One job**: produce `slides_final.json`. ==Write no markdown note.==
- **Inputs**: `slides_grounded.json` (per-slide VLM + transcript signals + OCR
  text — the source of truth), `transcript.txt`, `pdf_text.json`. Sample slide
  images only when the signals for a specific slide are unclear.
- **The full scoring rules above**, verbatim: weights, bonuses, penalties,
  thresholds, all five hard overrides, the width table.
- **Output contract**: a JSON list with an entry for ==every== slide_id, each
  carrying `slide_id`, `filename`, `combined_score`, `tier` (==integer 1/2/3,
  never the string "T1"== — the Write-pass parser requires an int),
  `tier_override_reason` or null, `embed_width` (null if tier 3),
  `embed_suppressed_reason` or null, `attachment_name` (==populated for all
  tiers==, even tier 3, which simply won't be copied), and `section_suggestion`
  (one line: which 總整理 section this slide belongs to).
- **Failure handling**: slides whose VLM signals are null or failed get `tier=3`
  with `tier_override_reason="vlm_failed_conservative"`.
- **Deck-only note**: on a PDF/HTML path every slide has `timestamp_start=0` and
  `speaker_skip_score=0.9`, so the score baseline sits near zero and the decision
  rests on `content_type`, `apparent_educational_function` and the overrides.
- **Close with one line**: `TIER DONE: T1=N T2=N T3=N total=N anomalies=<text>`.
  Nothing else.

### Write-pass prompt — what it must contain

- **Reader and north star**: the reader is a ==practicing clinician deciding how
  to manage a real patient==, not an exam candidate. The one goal is to capture
  what CHANGES OR GUIDES clinical management. If a sentence would not change what
  a clinician does, writes, or orders, it probably does not belong in 總整理.
- **Keep**: decision points (indications, patient selection, X-vs-Y choices);
  actionable parameters (doses, settings, thresholds, cut-offs); safety
  (contraindications, red flags, monitoring, when to stop); local reimbursement
  and protocol reality; ==the speaker's real-world practice beyond textbook
  theory, marked 🗣️ — the highest-value content==. Compress basic definitions and
  theory that doesn't alter action.
- **Anti-checklist guard**: do NOT output a flat enumeration of facts. Organize
  總整理 around the clinical workflow — ==assessment → decision → intervention →
  monitoring → failure handling== — framed as "when you see patient X, here is
  what you do and why". Several slides may collapse into one decision rule.
- **Uncertainty policy**: unclear evidence → state ==what the clinician should
  check next== (the test, the guideline, the parameter). Multiple valid options →
  give a ==decision rule==, not a menu. Weak slide → demote to background, don't
  inflate it. Uncertain from OCR or transcript → mark `⚠️`, never invent.
- **Tier authority**: `slides_final.json` is ==frozen==. Embed only slides with
  `tier ∈ {1,2}` (integer comparison) and a null `embed_suppressed_reason`. Tier
  1 appears once in 總整理 and once in 逐投影片; Tier 2 in 逐投影片 only; Tier 3
  nowhere. Do not recompute tiers.
- **Embeds are placeholders only**: write `[[EMBED sN]]` or
  `[[EMBED sN: intent]]`, each ==on its own line==, exactly where the visual
  supports the clinical point. ==Do NOT write `![[...]]`, `> [!figure]`, widths,
  or attachment paths== — `render_embeds.py` does all of that deterministically
  and audits coverage. Place a figure only where it genuinely helps; never for
  decoration.
- **逐投影片筆記 is the complete record**: list ==every== Tier-1 and Tier-2 slide
  in slide_id order, none skipped. The ruthless clinical filtering applies to
  總整理 only; here completeness wins. Keep each entry brief — 1–3 bullets of the
  slide's clinical point plus a 🗣️ line when the speaker added something.
- **ASR suspects**: read `asr_suspects.txt` and resolve each flagged token from
  context. ==Never silently substitute a hint==; an unresolved token keeps its raw
  ASR form and a `⚠️`.
- **Output structure**: exactly the template in [Note structure](#structure).
- **Close with one line**: `NOTE DONE — 逐投影片 includes ALL Tier-1/2 slides:
  YES/NO`. ==Do not self-count placeholders== — LLM self-counts are unreliable and
  `render_embeds.py` is the authoritative counter and auditor.

**Why the split.** A single subagent doing both tends to simplify structure so
its own embed-count audit passes; freezing tiers first removes that incentive.
Cost is ~1.8× single-pass. For one short lecture, a single pass is fine.

## Running the auditor {#auditor}

```bash
python <skill-dir>/scripts/audit_note.py "<note path>" --mode lecture --grounding "<lecture_dir>"
```

The auditor is vendored into this skill (stdlib only) so it works on a machine
with no vault. ==Always pass `--grounding`== — the lecture directory holding
`slides_grounded.json` / `slides_final.json`. Without it the C3 caption↔frame
check cannot run and only WARNs. `--vault` sets the root used to resolve image
refs. Exit 0 when there are no FAILs.

**FAIL, blocks finalize** — missing required headings, missing `> [!summary]`
Clinical Pearls, missing frontmatter `tags`, any image embed without `|width`,
broken refs, leftover `[!todo]`, 屍體, source appended to a heading, unescaped `|`
in a table wikilink, ==C3 caption↔frame mismatch== (a caption sharing no
vocabulary with its frame's OCR or labels — a wrong or swapped figure).

**WARN, review each** — gallery dump, zero-figure 總整理, no 🗣️ marker, empty
headings, unexpanded rare acronyms, an over-long fig-callout first line, CJK
adjacent to `==`, weak one-token C3 overlap, ==transcript coverage below the
floor== (note payload chars ÷ transcript chars < `--min-coverage`, default 0.10 —
an anti-over-compression tripwire calibrated on 410 real segments where the
median ratio is 0.36 and only the bottom ~2% fall under 0.10; a legitimately
thin hands-on-demo segment is accepted consciously, a "90-min transcript,
one-screen note" collapse is rewritten. Idea borrowed from jieyu166's
rad-workflow Stage-1 coverage gate, recalibrated for synthesis), ==degenerate
repetition== (a content line repeated ≥4× after excluding table rows / callout
headers / citation lines, or char-8-gram diversity <0.70 — looping/filler
output, which would also defeat the coverage floor by padding; calibrated on
560 accepted notes: 1 line-dup hit, 0 diversity hits, while a looping paragraph
lands at diversity 0.03).

`[review]`-only items (B3/B4 self-containment, C1/C3/C4 figure placement, D1–D3
reference handling, E1 callout indentation) are never machine-checkable — they
need the Write-pass self-audit before finalize.
