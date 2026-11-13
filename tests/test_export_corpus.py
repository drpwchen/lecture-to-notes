"""export_corpus.py: finished notes -> a `t2n-corpus/1` lecture corpus folder.

Synthetic fixtures only (tests/fixtures/corpus_export/); no real lecture text.
The Source-line check below restates the contract the indexer parses; set
LTN_VAULT_SEARCH_DIR to a vault-search checkout to also run its own parser
(server/lecture_meta.py) over the output.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import _common
import export_corpus as ec
from conftest import FIXTURES

FX = Path(FIXTURES) / "corpus_export"
NOTE1 = FX / "20250301_Example_Shoulder ultrasound basics.md"
NOTE2 = FX / "20250302_Example_Day 2 hands-on.md"

# date first, then speaker, then 《title》, then `· course_dir`
SOURCE_RE = re.compile(r"^Source: (\d{4}-\d{2}-\d{2}) ([^《]*)《([^》]+)》· (\S[^·]*?)(?: · (c\d{2}(?:, c\d{2})*))?$")


@pytest.fixture(autouse=True)
def _no_user_config(monkeypatch):
    """A developer's own config.yaml / env must not switch the export on or off here."""
    monkeypatch.delenv("LECTURE_CORPUS_ROOT", raising=False)
    monkeypatch.setattr(_common, "load_config", lambda *a, **k: {})


def run(tmp_path, *extra, notes=(NOTE1, NOTE2), course="example_course"):
    root = tmp_path / "corpus"
    argv = [str(n) for n in notes] + ["--corpus-root", str(root)]
    if course:
        argv += ["--course-dir", course]
    rc = ec.main(argv + list(extra))
    return rc, root, root / (course or "")


def entries(cdir):
    return sorted(p for p in cdir.glob("*.md") if p.name != "_meta.md")


def read(p):
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------- contract

def test_corpus_json_and_layout(tmp_path):
    rc, root, cdir = run(tmp_path)
    assert rc == 0
    assert json.loads(read(root / "corpus.json")) == {"format": "t2n-corpus/1", "kind": "lecture"}
    assert (cdir / "_meta.md").is_file()
    assert len(entries(cdir)) >= 2
    assert sorted(p.name for p in root.iterdir()) == ["corpus.json", "example_course"]


def test_every_entry_opens_with_h1_then_source_line(tmp_path):
    _rc, _root, cdir = run(tmp_path)
    for p in entries(cdir):
        lines = read(p).split("\n")
        assert lines[0].startswith("# ") and len(lines[0]) > 2, p.name
        assert lines[1] == ""
        m = SOURCE_RE.match(lines[2])
        assert m, (p.name, lines[2])
        assert m.group(4) == "example_course"


def test_source_fields_per_note(tmp_path):
    _rc, _root, cdir = run(tmp_path)
    srcs = {read(p).split("\n")[2] for p in entries(cdir)}
    parsed = {SOURCE_RE.match(s).group(1, 2, 3) for s in srcs}
    # speaker from frontmatter without the affiliation; title = H1 minus the speaker
    assert ("2025-03-01", "Dr. Example", "Shoulder ultrasound basics") in parsed
    # second note: its own date (frontmatter `date`) and speaker
    assert ("2025-03-02", "Dr. Sample", "Day 2 hands-on") in parsed


@pytest.mark.skipif(not os.environ.get("LTN_VAULT_SEARCH_DIR"), reason="LTN_VAULT_SEARCH_DIR not set")
def test_vault_search_parser_reads_every_source_line(tmp_path):
    sys.path.insert(0, os.path.join(os.environ["LTN_VAULT_SEARCH_DIR"], "server"))
    import lecture_meta as lm
    _rc, _root, cdir = run(tmp_path)
    for p in entries(cdir):
        r = lm.parse_source(lm.find_source_line(read(p)))
        assert r["date"] and r["speaker"] and r["course"], p.name
    assert lm.is_meta_file("example_course/_meta.md")


@pytest.mark.skipif(not os.environ.get("LTN_VAULT_SEARCH_DIR"), reason="LTN_VAULT_SEARCH_DIR not set")
def test_vault_search_checker_passes_strict(tmp_path):
    checker = os.path.join(os.environ["LTN_VAULT_SEARCH_DIR"], "server", "corpus.py")
    if not os.path.isfile(checker):
        pytest.skip("this vault-search checkout has no server/corpus.py")
    _rc, root, _cdir = run(tmp_path)
    r = subprocess.run([sys.executable, checker, "check", str(root), "--strict"],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr


# ---------------------------------------------------------------- cleaning

def test_obsidian_syntax_is_gone(tmp_path):
    _rc, _root, cdir = run(tmp_path)
    text = "\n".join(read(p) for p in entries(cdir))
    for bad in ("![[", "[[", "> [!", "[[EMBED", "%%", "\n---\n", "tags:", "`(V1"):
        assert bad not in text, bad
    assert "See the cuff note and Other note" in text          # wikilink -> alias / name
    assert "==loss of the convex contour==" in text           # highlights kept
    assert "Labels: GT = greater tuberosity" in text          # figure legend kept as text


def test_timecodes_become_clip_codes(tmp_path):
    _rc, _root, cdir = run(tmp_path)
    text = "\n".join(read(p) for p in entries(cdir))
    assert "[c01 03:21]" in text and "[c01 03:40]" in text
    assert "[c01 01:02:03]" in text                           # H:MM:SS in brackets
    assert "[c02 10:05]" in text                              # V2 of note 1
    assert "[c03 05:00]" in text                              # V1 of note 2
    meta = read(cdir / "_meta.md")
    assert "| c01 | 20250301_Example_Shoulder ultrasound basics.md | V1 |" in meta
    assert "| c03 | 20250302_Example_Day 2 hands-on.md | V1 |" in meta


def test_untimed_lines_get_no_invented_timecode(tmp_path):
    _rc, _root, cdir = run(tmp_path)
    text = "\n".join(read(p) for p in entries(cdir))
    line = next(ln for ln in text.split("\n") if "Relative rest for two days" in ln)
    assert "[c" not in line


def test_bookkeeping_goes_to_meta_not_entries(tmp_path):
    _rc, _root, cdir = run(tmp_path)
    text = "\n".join(read(p) for p in entries(cdir))
    meta = read(cdir / "_meta.md")
    assert "PEARL-ONLY-SENTINEL" not in text and "PEARL-ONLY-SENTINEL" not in meta
    assert "RESOURCE-SENTINEL" not in text
    assert "long head of biceps" not in text and "| LHB | long head of biceps |" in meta
    assert "講者：Dr. Example" in meta and "講者：Dr. Example" not in text


# ---------------------------------------------------------------- slide notes

def test_slide_notes_exported_without_repeating_summary(tmp_path):
    _rc, _root, cdir = run(tmp_path)
    text = "\n".join(read(p) for p in entries(cdir))
    assert "SLIDE-ONLY-SENTINEL" in text
    assert text.count("Start in short axis over the bicipital groove") == 1
    assert text.count("sits between the greater and lesser tuberosity") == 1
    assert "Slide 2 — Biceps groove" not in text      # nothing left after de-duplication
    assert "slide-note line(s) already in 總整理 were not repeated" in read(cdir / "_meta.md")


def test_summary_only_skips_slide_notes(tmp_path):
    _rc, _root, cdir = run(tmp_path, "--summary-only")
    text = "\n".join(read(p) for p in entries(cdir))
    assert "SLIDE-ONLY-SENTINEL" not in text
    assert "--summary-only" in read(cdir / "_meta.md")


# ---------------------------------------------------------------- sizing

def test_small_sections_merge_and_a_short_tail_joins_the_previous(tmp_path):
    _rc, _root, cdir = run(tmp_path, "--summary-only", notes=(NOTE1,))
    es = entries(cdir)
    assert len(es) == 1
    body = read(es[0])
    assert "## Biceps groove landmark" in body and "### Pitfalls" in body
    assert "SHORT-TAIL-SENTINEL" in body


def test_one_entry_per_section_when_nothing_is_small(tmp_path):
    _rc, _root, cdir = run(tmp_path, "--summary-only", "--min-chars", "0", notes=(NOTE1,))
    titles = [read(p).split("\n")[0] for p in entries(cdir)]
    assert titles == ["# Biceps groove landmark", "# Supraspinatus long axis", "# 講者其他分享"]
    first = read(entries(cdir)[0])
    assert "## Pitfalls" in first                    # ### under a section -> ##


def test_large_section_splits_at_its_subheadings(tmp_path):
    _rc, _root, cdir = run(tmp_path, "--summary-only", "--min-chars", "0", "--max-chars", "200",
                           notes=(NOTE1,))
    titles = [read(p).split("\n")[0] for p in entries(cdir)]
    assert "# Biceps groove landmark — Pitfalls" in titles


# ---------------------------------------------------------------- behaviour

def test_byte_identical_reruns(tmp_path):
    _rc, _root, a = run(tmp_path / "a")
    _rc, _root, b = run(tmp_path / "b")
    fa = {p.name: p.read_bytes() for p in a.iterdir()}
    fb = {p.name: p.read_bytes() for p in b.iterdir()}
    assert fa == fb


def test_export_is_off_without_a_root(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = ec.main([str(NOTE1)])
    assert rc == 0
    assert "RAG export is off" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_env_root_turns_it_on(tmp_path, monkeypatch):
    monkeypatch.setenv("LECTURE_CORPUS_ROOT", str(tmp_path / "envroot"))
    assert ec.main([str(NOTE1), "--course-dir", "c"]) == 0
    assert (tmp_path / "envroot" / "corpus.json").is_file()


def test_config_root_turns_it_on(tmp_path, monkeypatch):
    monkeypatch.setattr(_common, "load_config",
                        lambda *a, **k: {"rag": {"corpus_root": str(tmp_path / "cfgroot")}})
    assert ec.main([str(NOTE1), "--course-dir", "c"]) == 0
    assert (tmp_path / "cfgroot" / "c" / "_meta.md").is_file()


def test_existing_course_needs_replace(tmp_path):
    rc, _root, cdir = run(tmp_path)
    assert rc == 0
    stale = cdir / "99_stale.md"
    stale.write_text("# old\n", encoding="utf-8")
    keep = cdir / "notes.txt"
    keep.write_text("not ours", encoding="utf-8")
    rc, _root, _ = run(tmp_path)
    assert rc == 2 and stale.exists()
    rc, _root, _ = run(tmp_path, "--replace")
    assert rc == 0
    assert not stale.exists() and keep.exists()


def test_refuses_a_corpus_of_another_kind(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "corpus.json").write_text('{"format": "t2n-corpus/1", "kind": "textbook"}', encoding="utf-8")
    rc, _root, cdir = run(tmp_path)
    assert rc == 2 and not cdir.exists()


@pytest.mark.parametrize("bad", ["../escape", "a/b", "C:x", "_meta.md", ".hidden"])
def test_refuses_unsafe_course_dir(tmp_path, bad):
    rc, root, _ = run(tmp_path, course=bad)
    assert rc == 2
    assert not (root / "corpus.json").exists()
    assert not (tmp_path / "escape").exists()


def test_default_course_dir_is_the_first_note_name(tmp_path):
    rc, root, _ = run(tmp_path, notes=(NOTE1,), course=None)
    assert rc == 0
    assert (root / "20250301_Example_Shoulder_ultrasound_basics" / "_meta.md").is_file()


def test_dry_run_writes_nothing(tmp_path):
    rc, root, _ = run(tmp_path, "--dry-run")
    assert rc == 0 and not root.exists()


def test_note_without_sections_is_reported(tmp_path, capsys):
    bare = tmp_path / "bare.md"
    bare.write_text("# Only a title\n\n", encoding="utf-8")
    rc, root, _ = run(tmp_path, notes=(bare,))
    assert rc == 1 and not root.exists()
