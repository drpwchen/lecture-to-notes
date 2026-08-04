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
  `tools/` (including `tools/templates/`).

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
| `__pycache__/`, `*.pyc` | build artifacts |

## Transforms applied at export

Each transform asserts its anchor matched. If an upstream edit moves an anchor,
the sync **aborts** rather than silently shipping the untransformed text — the
private version must never reach this repo by accident.

| File | Transform | Why |
|---|---|---|
| `SKILL.md` | The optional-infrastructure table row naming a private course control-plane directory reworded to describe it generically. | De-identification. The name survives in `reference/segmented-mode.md`, where the whole point of the passage is documenting that external control plane. |
| `scripts/groq_asr.py` | Local encrypted secret-store fallback removed; `GROQ_API_KEY` read from the environment only. The actionable `RuntimeError` is kept. Module header reworded. | The fallback shells out to a helper that exists on exactly one machine. Env-only is the portable contract. |
| `scripts/audit_note.py` | `--vault` default changed from a hardcoded personal vault path to `$CLAUDE_VAULT_ROOT` (empty when unset). | Absolute-path de-identification. |
| `scripts/_paths.py` | Module docstring rewritten. | The original described the private machine it was extracted from, by name, plus an unrelated private hook config. |
| `scripts/export_web.py` | Comment naming a private course control plane reworded to describe the manifest field generically. | De-identification; the field itself is generic. |
| `scripts/ocr_slides.py` | Same — the deprecation shim's comment named private callers. | De-identification. |
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
- The five transforms that exist purely for de-identification
  (`SKILL.md`, `audit_note.py`, `_paths.py`, `export_web.py`, `ocr_slides.py`)
  would be better fixed upstream — then they could be dropped here and those
  files would be byte-identical too. Worth doing on the next upstream pass.

## History

| Date | Event |
|---|---|
| 2026-08-02 | Initial export. 56 manifest entries; 6 transforms + 1 override. |
| 2026-08-05 | v0.6.0 real-timeline pass. 60 entries: 3 created (`_mdpm.py`, `batch/course_timeline.py`, `batch/audit_segmentation.py`), 7 updated. ==De-identification was done UPSTREAM this time==: the new docs and code comments originally named a real conference and four real speakers, and were rewritten in the skill itself to `Conference-Y` / `<speaker>` before syncing, so no new transform was needed. That is the direction this ledger recommends above — keep doing it that way. |
