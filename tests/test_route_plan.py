"""route_inputs.py --json: the structured, shell-free plan (issue #1, point 4).

The router never runs anything; these tests check that what it hands an agent
or workflow engine is executable as an argv list without a shell, says up front
what it touches, and never claims to be runnable while a human still has to
fill in a placeholder.
"""
import contextlib
import io
import os

import pytest

import route_inputs as ri

SCENARIOS = {
    "video_only": ["talk.mp4"],
    "video_pdf": ["talk.mp4", "deck.pdf"],
    "audio_images": ["rec.m4a"] + [f"IMG_{i}.jpg" for i in range(5)],
    "multi": ["a.mp4", "b.MTS", "rec.mp3", "deck.pdf", "x.pptx"],
    "images_only": [f"s{i}.png" for i in range(4)],
}
NEW_KEYS = {"argv", "program", "script", "args", "network", "gpu", "reads", "writes",
            "placeholders", "runnable"}


def plan_for(tmp_path, files, dirname):
    d = tmp_path / dirname
    d.mkdir()
    for f in files:
        (d / f).write_bytes(b"")
    in_dir = str(d)
    out_dir = os.path.join(in_dir, "_lecture")
    return ri.build_plan(ri.classify(in_dir), in_dir, out_dir), in_dir, out_dir


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_step_is_structured(tmp_path, name):
    plan, _, _ = plan_for(tmp_path, SCENARIOS[name], name)
    assert plan["plan_schema"] == 2
    for s in plan["steps"]:
        assert NEW_KEYS <= set(s), s["what"]
        assert s["network"] in {"none", "loopback", None}
        if s["argv"]:
            assert s["argv"][0] == "python" == s["program"]
            assert s["script"] == s["argv"][1] and s["args"] == s["argv"][2:]
            assert os.path.isfile(s["script"]), s["script"]
            assert all(isinstance(a, str) for a in s["argv"])
        else:
            assert s["program"] is s["script"] is s["args"] is None
            assert not s["runnable"]


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_argv_is_the_same_command_as_cmd(tmp_path, name):
    plan, _, _ = plan_for(tmp_path, SCENARIOS[name], name)   # no spaces in the path
    for s in plan["steps"]:
        if s["argv"]:
            assert " ".join(s["argv"]) == s["cmd"]


def test_argv_needs_no_shell_quoting(tmp_path):
    plan, in_dir, _ = plan_for(tmp_path, ["talk.mp4", "deck.pdf"], "Lecture (2026) day 1")
    seen_raw = False
    for s in plan["steps"]:
        if s["argv"]:
            assert not any(a.startswith('"') for a in s["argv"]), s["argv"]
            seen_raw |= any(a.startswith(in_dir) for a in s["argv"])
    assert seen_raw
    # the copy-paste string still quotes that same path
    assert any(f'"{in_dir}' in (s["cmd"] or "") for s in plan["steps"])


def test_placeholders_block_runnable(tmp_path):
    plan, _, _ = plan_for(tmp_path, SCENARIOS["multi"], "multi")
    by_what = {s["what"]: s for s in plan["steps"]}
    tr = next(s for w, s in by_what.items() if w.startswith("transcribe "))
    assert tr["placeholders"] == ["<LANG>"] and not tr["runnable"]
    xc = by_what["measure the real offsets between the sources"]
    assert set(xc["placeholders"]) == {"<REC_NAME>", "<ASR dir of the full-room source>"}
    assert not xc["runnable"]
    gc = by_what["GPU pre-flight"]
    assert gc["placeholders"] == [] and gc["runnable"]


def test_resource_flags(tmp_path):
    plan, _, _ = plan_for(tmp_path, ["talk.mp4"], "video")
    by_what = {s["what"]: s for s in plan["steps"]}
    assert by_what["VLM signals (Stage D)"]["network"] == "loopback"
    assert by_what["VLM signals (Stage D)"]["gpu"] is True
    assert next(s for w, s in by_what.items() if w.startswith("transcribe "))["gpu"] is True
    assert by_what["quick OCR (Stage B)"]["gpu"] is False
    assert by_what["Stage F synthesis (Claude)"]["network"] is None   # agent's choice
    assert all(s["network"] != "none" or not s["what"].startswith("VLM")
               for s in plan["steps"])


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_reads_and_writes_stay_in_the_lecture_folders(tmp_path, name):
    # out-dir deliberately NOT inside the material folder
    _, in_dir, _ = plan_for(tmp_path, SCENARIOS[name], name)
    out_dir = str(tmp_path / "elsewhere" / "out")
    plan = ri.build_plan(ri.classify(in_dir), in_dir, out_dir)

    def under(p, root):
        return os.path.commonpath([p, root]) == root

    for s in plan["steps"]:
        for p in s["writes"]:
            assert under(p, out_dir), (s["what"], p)
        for p in s["reads"]:
            assert under(p, in_dir) or under(p, out_dir), (s["what"], p)
        if s["argv"]:
            assert s["writes"], s["what"]        # every command declares where it writes


def test_human_output_unchanged_by_the_new_fields(tmp_path):
    plan, _, _ = plan_for(tmp_path, ["talk.mp4"], "video")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ri.print_plan(plan)
    text = buf.getvalue()
    for key in ("argv", "runnable", "placeholders", "loopback"):
        assert key not in text
