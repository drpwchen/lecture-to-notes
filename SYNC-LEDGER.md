# Sync ledger

## The standing rule

**The upstream skill tree is the source of truth. This repo is a published
projection of it.**

- To change shipped code or `reference/` docs: **edit upstream, then re-run the
  sync**, review the diff, and commit.
- Editing a synced file directly in this repo is a bug — the next sync silently
  reverts it. `python tools/sync_from_skill.py --check` exits 1 when that has
  happened.
- Repo-only files may be edited here freely: `README.md`, `LICENSE`,
  `CHANGELOG.md`, `SYNC-LEDGER.md`, `.gitignore`, `requirements*.txt`, `docs/`,
  `tools/` (including `tools/templates/`), `tests/`, `pyproject.toml`,
  `.github/`.

## Source

| | |
|---|---|
| Source tree | the private `lecture-to-notes` Claude Code skill directory (default `~/.claude/skills/lecture-to-notes/`) |
| Sync tool | `tools/sync_from_skill.py` (manifest-driven, one-way, rerunnable) |
| Direction | source → repo, never the reverse. The tool never writes to the source. |

```bash
python tools/sync_from_skill.py            # sync
python tools/sync_from_skill.py --check    # dry run; exit 1 if out of date
```

The manifest is an explicit **include list**, not "copy everything minus
excludes". A new file upstream is not published until someone adds it to
`FILES`/`GLOBS` in the sync tool. That is the point: an exclude-list export
publishes the next private file somebody drops into the tree.

## Not published

| Path | Why |
|---|---|
| `config.yaml` | author name + machine-specific absolute paths |
| `ocr_bench/fixtures/`, `fixtures_manifest.json` | real lecture slides — third-party content, not ours to distribute |
| `ocr_bench/results/`, `ocr_bench/report.md` | benchmark numbers from one machine; also embed fixture text |
| `scripts/.gitignore` | merged into the root `.gitignore` instead |
| `publish-denylist.txt` | name patterns for the leak scan — publishing them would publish the names |
| `reference/decisions-private.md` | run-by-run evidence behind `decisions.md#far-field-hall` / `#gemini-primary`: recording names and local paths. The public `decisions.md` carries the neutral summary. |
| `__pycache__/`, `*.pyc` | build artifacts |

## Transforms applied at export

Each transform asserts its anchor matched. If an upstream edit moves an anchor,
the sync **aborts** rather than silently shipping the untransformed text — the
private version must never reach this repo by accident.

| File | Transform | Why |
|---|---|---|
| `scripts/groq_asr.py` | Local encrypted secret-store fallback removed; `GROQ_API_KEY` read from the environment only. The actionable `RuntimeError` is kept. Module header reworded. | The fallback shells out to a helper that exists on exactly one machine. Env-only is the portable contract. |
| `scripts/gemini_asr.py` | `api_key()` reads `GEMINI_API_KEY` from the environment only (upstream falls back to a machine-local secret store). Everything else, including the `agy` route and its terms-of-service note, ships unchanged; the transform aborts if that note is missing upstream. | Same portability contract as `groq_asr.py`. |
| `data/real_words.txt`, `data/real_acronyms.txt` | Entries matching the private denylist are dropped. | The lists are built from a local corpus; a page watermark once became a "word". |
| *(every file)* | Leak scan after transforms: absolute user paths, WSL/NAS paths, `_platform`, `.claude/hooks`, `secret("…")`, plus the denylist (`publish-denylist.txt` in the skill dir, itself never published). A hit aborts the sync with nothing written. | Byte-identical copies are only as clean as upstream; this is the backstop. |
| `data/.gitignore` | Replaced. Upstream ignores the generated wordlists; this repo **tracks** them. | Deliberate call: they are dictionary-word frequency lists compiled from a local reference corpus, and `flag_asr_suspects.py` is unusable without them. |
| `ocr_bench/README.md` | Fully replaced by `tools/templates/ocr_bench_README.md`. | The upstream README documents the private fixture set and machine-specific venv paths. The replacement documents the fixture format so users can bring their own. |

Everything else is copied **byte-identical**. That is a deliberate constraint:
the smaller the delta between the private tree and the public one, the less
likely a fix lands in only one of them.

## Judgment calls worth revisiting

- `ocr_bench/adapters/*.py` (RapidOCR + PaddleOCR-VL) are published even though
  the harness spec listed only `schema.py` / `run_bench.py` / `report.py`. They
  contain no identifying content, and without them the harness cannot run any
  engine.
- `run_bench.py` reads `fixtures_manifest.json`, which is not shipped. With no
  fixtures and no manifest it exits with a `FileNotFoundError`. This is
  documented in `ocr_bench/README.md` rather than patched, to keep the file
  byte-identical with upstream.

## History

| Date | Event |
|---|---|
| 2026-08-02 | Initial export. 56 manifest entries; 6 transforms + 1 override. |
| 2026-08-05 | v0.6.0 real-timeline pass. 60 entries: 3 created (`_mdpm.py`, `batch/course_timeline.py`, `batch/audit_segmentation.py`), 7 updated. ==De-identification was done UPSTREAM this time==: the new docs and code comments originally named a real conference and four real speakers, and were rewritten in the skill itself to `Conference-Y` / `<speaker>` before syncing, so no new transform was needed. That is the direction this ledger recommends above — keep doing it that way. |
| 2026-09-01 | v0.7.1 small-GPU handling. 60 entries, 9 updated. Trigger: a user with a 2 GB card read `gpu_check` "blocked" as a broken machine. Edited upstream (`gpu_check.py`, `transcribe_video.py`, `retranscribe_segment.py`, `SKILL.md`, `reference/pipeline.md`) then synced; the sync also carried the 2026-08-10 `gpu_lease_ctx` refactor (no-op off this machine) and the 2026-08-25 `build_L1.py` deck-text change, both logged in the CHANGELOG. Name sweep of the full diff: clean. |
| 2026-10-13 | v0.8.0 viewer hardening (issue #1). ==Edited in this repo first, against the standing rule==, because the work was scoped to the public repo: `scripts/export_web.py`, `scripts/layout2/viewer.js`, the new `scripts/_html_safety.py`, and `scripts/route_inputs.py` (structured plan). Same situation for the 2026-10-07 `scripts/vlm_signals.py` change (ollama `localhost` → `127.0.0.1`), also made here only; the port patch covers it. The same change is prepared as a patch against the upstream skill; ==apply it upstream before the next sync==, or the sync silently reverts the hardening (`_html_safety.py` would survive — the glob never deletes — but `export_web.py`/`viewer.js` would not). Upstream had also drifted ahead on 11 synced files before this change; those are not part of 0.8.0. |
| 2026-11-02 | v0.9.0 Gemini transcription. 65 entries: 4 created (`gemini_asr.py`, `asr_gemini_primary.py`, `ts_anchor.py`, `test_ct2_teardown.py`), the rest updated. Private evidence split upstream into `reference/decisions-private.md` (NEVER); new `gemini_asr` transform (env-only key); the 0.8.0 port patch was applied upstream first. New: leak scan on every payload + private `publish-denylist.txt`; it caught a watermark token in `data/real_words.txt` shipped since 0.1.0 (now filtered). Reviewed by three independent model reviews before release. |
| 2026-08-07 | v0.7.0 timecode parser. 60 entries, 2 updated (`export_web.py`, `batch/split_L1_by_segment.py`). De-identification upstream again, and this time the sweep also caught a **pre-existing** leak that predates this change: a real speaker name sitting in `split_L1_by_segment.py`'s module docstring since 2026-07-24, already public. ==Grep the whole repo for real names on every sync, not just the files you touched== — the per-change check would never have found it. |
| 2026-11-13 | v0.11.0 (unreleased; v0.10.0 was already published 2026-11-03) gains the optional corpus export. ==Edited in this repo first, against the standing rule==, same situation as 0.8.0: new `scripts/export_corpus.py` (the `scripts/*.py` glob never deletes, so it survives a sync, but upstream has no copy yet), plus the "Last step — Searchable corpus" section in `SKILL.md`, the `rag.corpus_root` block in `config.example.yaml`, and the file-tree line in `SKILL.md`. Those two files were already out of date with upstream before this change (`--check` listed both). ==Port all three to the upstream skill before the next sync==, or the sync reverts that section and the config block. (Unnumbered on purpose: upstream already has its own private `Step 16 — Retrieval-corpus handoff`.) ==Ported upstream the same day== (see the next row). Repo-only additions: `tests/test_export_corpus.py`, `tests/fixtures/corpus_export/` (synthetic, no lecture content), README sections. Leak scan + private denylist run by hand over every new/changed file: clean. |
| 2026-11-13 | Corpus export ported upstream (no sync run): `scripts/export_corpus.py` copied byte-identical, the "Last step — Searchable corpus" section inserted after the private Step 16, the file-tree line and the `rag:` block added to `config.example.yaml`. Private `config.yaml` has no `rag` key, so the export stays off there. `--check` before: 67 entries, 2 differ (SKILL.md, config.example.yaml); after: 68 entries, the same 2 differ, and the remaining diff is exactly the pre-existing private drift — `Step 16 — Retrieval-corpus handoff` in `SKILL.md` and `paths.retrieval_handoff` in `config.example.yaml` — not resolved here (it describes a machine-local procedure; publishing it is a separate decision). |
| 2026-11-14 | v0.11.0: `scripts/export_corpus.py` renamed vault-search → ask-your-library in its help text and printed next-step lines (format URL + `<ask-your-library>/server/corpus.py`). Same edit applied upstream byte-identical (copied); `--check` after: 68 entries, 66 unchanged, only the pre-existing SKILL.md / config.example.yaml private drift differs. |
| 2026-11-14 | v0.11.0 (unreleased): vault-search renamed ask-your-library in the docs. One synced line changed — `SKILL.md` "Last step — Searchable corpus" (`vault-search's job` → `ask-your-library's job`) — ==edited upstream and here identically== (no sync run), so `--check` still lists only the pre-existing 2 differing files (SKILL.md, config.example.yaml; private Step 16 / `retrieval_handoff` drift). The `vault-search` row in SKILL.md's private-tools table is the author's MCP server name and was left as is. Repo-only: README.md, README.zh-TW.md, CHANGELOG.md. No new names or paths introduced. |
