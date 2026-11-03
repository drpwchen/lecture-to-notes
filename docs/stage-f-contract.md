# Stage F — the synthesis contract, for any LLM

Stages 0–E are scripts. Stage F, the write-up, is done by a language model, and
nothing in the pipeline cares which one: Claude, GPT, Gemini, or a local model
behind ollama all fit, through a coding agent or a plain script. This page is
the provider-neutral contract — what Stage F reads, what it must write, and
what the scripts after it check mechanically. `SKILL.md` is one adapter of it
(Claude Code: subagents, two passes); a Hermes skill or a CLI script would be
another. The detailed rules live in `reference/note-spec.md`; this page says
where each one is and which of them a script enforces.

## Two outputs, one per destination

| Destination | Stage F writes | Then | Spec |
|---|---|---|---|
| Markdown note (vault) | `slides_final.json` + `note_draft.md` | `render_embeds.py` → `finalize_to_vault.py` → `audit_note.py` | `reference/note-spec.md` |
| Synced HTML viewer | `L3/L3_segNN_<slug>.md`, one per segment, plus the overview | `export_web.py` | `reference/pipeline.md#web-export` |

A lecture can have both. The segment plan for the viewer (`plan.json` for
`build_single_talk_web.py`) is also Stage F's to write.

## Inputs (read-only)

| File | What it gives the writer |
|---|---|
| `slides_grounded.json` | per canonical slide: OCR text, VLM signals, transcript window, grounding signals — the source of truth for slides |
| `transcript.txt` | `[MM:SS] text` lines (format in [`transcript-contract.md`](transcript-contract.md)) |
| `asr_suspects.txt` | tokens that may be mis-heard, with look-alike slide terms — questions, not answers |
| `pdf_text.json` | the deck's own text, when there was a PDF |
| `transcript_anchor.txt` | on the Gemini path: the local second source for names and numbers |

Stage F never edits any of these. In particular it never rewrites the
transcript (HARD RULE 3): a mis-heard word is resolved in the note, and an
unresolved one keeps its raw form with a `⚠️`.

## `slides_final.json` — the tier decision

A JSON list with an entry for **every** `slide_id` in `slides_grounded.json`:

| Key | Type | Notes |
|---|---|---|
| `slide_id` | int | |
| `filename` | string | the source frame in `slides/` (`finalize_to_vault.py` copies it) |
| `tier` | **integer** 1/2/3 | 1 = in 總整理 and 逐投影片, 2 = 逐投影片 only, 3 = nowhere. Not `"T1"`. |
| `combined_score` | number | from the scoring rules |
| `tier_override_reason` | string or null | |
| `embed_width` | int or null | null for tier 3 |
| `embed_suppressed_reason` | string or null | non-null = never embedded |
| `attachment_name` | string | **one plain file name** — `render_embeds.py` and `finalize_to_vault.py` refuse a path (`/`, `\`, `..`, `:`) |
| `section_suggestion` | string | which 總整理 section the slide supports |

The scoring rules (weights, bonuses, penalties, thresholds, hard overrides,
width table) are in `reference/note-spec.md#tier-scoring` and `#widths`. A
model that cannot follow arithmetic reliably may compute them in code; the
contract is the output, not how it was reached.

## `note_draft.md` — the note

- Structure: exactly the template in `reference/note-spec.md#structure` (the
  Chinese headings are what `audit_note.py --mode lecture` checks).
- Figures: **placeholders only** — `[[EMBED sN]]` or `[[EMBED sN: intent]]`, each
  on its own line. No `![[…]]`, no widths, no paths: `render_embeds.py` expands
  them from `slides_final.json` and reports tier-3 / unknown references and
  tier-1/2 slides never placed.
- 逐投影片筆記 lists every tier-1/2 slide in `slide_id` order.
- Content rules (who the reader is, what to keep, uncertainty policy):
  `reference/note-spec.md#prompt-requirements`, Write-pass section.

## L3 files — the viewer's summary layer

- One `L3/L3_segNN_<slug>.md` per segment in `_intermediate/seg/segments.json`;
  `export_web.py` refuses while one is missing.
- Timecodes as `` `(V1 MM:SS)` `` — `V` for every source, audio included; the
  viewer parses nothing else (`reference/pipeline.md#vn-label-rule`).
- Image embeds by **basename only** (`![[frame_0012.jpg]]`). The exporter looks
  them up under the course's figure folders and refuses a path that leaves them.
- The overview segment's shape depends on one question — is this a single body
  of knowledge? (`reference/pipeline.md#web-export`). Never force a synthesis
  across unrelated talks.
- Slugs in `plan.json` are one plain name each; `build_single_talk_web.py`
  refuses a path.

## Rules no script can check

- A number that only the LLM's own source has (one ASR pass, no slide, no
  second transcript) stays unconfirmed in the note.
- `asr_suspects.txt` hints are look-alikes, not substitutions.
- On the Gemini path, check names and numbers against `transcript_anchor.txt`
  and the slides — Gemini is a different ASR source and an LLM.

## Gates after Stage F

| Script | Fails on |
|---|---|
| `render_embeds.py` | an `attachment_name` that is a path; reports (does not fail) unknown / tier-3 references and unplaced tier-1/2 slides |
| `finalize_to_vault.py` | zero attachment references (unless `--allow-no-refs`), a same-named note (unless `--force`), an `attachment_name` outside the attachment folder |
| `audit_note.py --mode lecture --grounding <dir>` | missing template sections, caption ↔ frame mismatches, coverage |
| `export_web.py` | a missing L3, an embed that resolves to no image (or to several different ones), an embed path outside the figure folders |

## Running it with a given provider

- **Two passes or one.** For long lectures and batches, a tier pass that writes
  only `slides_final.json`, then a fresh write pass that treats it as frozen
  (why: `reference/note-spec.md#prompt-requirements`). One pass is fine for a
  short talk.
- **Long transcripts.** Over ~25 k tokens, summarise in chunks with explicit
  coverage markers first (`reference/pipeline.md#chunked-summarization`) —
  whatever the model's context window, a single read drops the middle.
- **Privacy.** A hosted model means the transcript and slide text leave the
  machine. `LECTURE_LOCAL_ONLY=1` stops the pipeline's own cloud routes (Groq,
  Gemini); it cannot stop an agent you run yourself, so with confidential
  material use a local model for Stage F too.
- **Record what you used.** Stage F has no script to record it, so record it
  yourself — the next reader of `provenance.json` then sees which model wrote
  the note and from which inputs:

  ```bash
  python scripts/provenance.py record "$OUT_DIR" --stage synthesis \
      --output slides_final.json --output note_draft.md \
      --input slides_grounded.json --input transcript.txt --input asr_suspects.txt \
      --provider <vendor|local> --model <model id> --network <internet|loopback|none>
  ```

  `python scripts/provenance.py verify "$OUT_DIR"` later reports the note as
  `stale` if, say, the transcript was re-transcribed after it was written.
