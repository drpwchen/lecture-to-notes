"""provenance.json (issue #1, point 8): what produced each artifact, and whether the
chain still holds — an artifact edited after its stage wrote it is `modified`, one
whose input changed since is `stale`."""
import json
import os
import subprocess
import sys

import pytest

import provenance as pv
from conftest import SCRIPTS

PY = sys.executable


def _w(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _load(d):
    return json.loads((d / "provenance.json").read_text(encoding="utf-8"))


def test_record_and_verify_ok(tmp_path):
    inp = _w(tmp_path / "transcript.json", "[]")
    out = _w(tmp_path / "slides_grounded.json", "[1]")
    assert pv.record(tmp_path, "ground", [out], [inp], tool="ground_slides.py", engine="python",
                     params={"window_seconds": 20})
    ent = _load(tmp_path)["artifacts"]["slides_grounded.json"]
    assert ent["stage"] == "ground" and ent["provider"] == "local" and ent["network"] == "none"
    assert ent["params"] == {"window_seconds": 20}
    assert ent["digest"]["sha256"] and ent["inputs"]["transcript.json"]["sha256"]
    assert [r["status"] for r in pv.verify(tmp_path)] == ["ok"]


def test_modified_and_stale_and_missing(tmp_path):
    a = _w(tmp_path / "a.json", "1")
    b = _w(tmp_path / "b.json", "2")
    c = _w(tmp_path / "c.json", "3")
    pv.record(tmp_path, "s1", [b], [a])
    pv.record(tmp_path, "s2", [c], [b])
    _w(tmp_path / "a.json", "1 changed")        # upstream re-run -> b is stale
    _w(tmp_path / "c.json", "3 hand-edited")    # c no longer what s2 wrote
    st = {r["artifact"]: r["status"] for r in pv.verify(tmp_path)}
    assert st == {"b.json": "stale", "c.json": "modified"}
    os.remove(b)
    st = {r["artifact"]: r["status"] for r in pv.verify(tmp_path)}
    assert st["b.json"] == "missing"


def test_latest_record_wins_and_other_artifacts_survive(tmp_path):
    a = _w(tmp_path / "a.json", "1")
    b = _w(tmp_path / "b.json", "2")
    pv.record(tmp_path, "first", [a])
    pv.record(tmp_path, "second", [b])
    pv.record(tmp_path, "third", [a])
    arts = _load(tmp_path)["artifacts"]
    assert arts["a.json"]["stage"] == "third" and arts["b.json"]["stage"] == "second"


def test_in_place_rewrite_uses_the_snapshot(tmp_path):
    """Stage B2 rewrites slides_dedup.json in place: its pre-image is the input."""
    f = _w(tmp_path / "slides_dedup.json", "before")
    snap = pv.snapshot([f], tmp_path)
    _w(f, "after")
    pv.record(tmp_path, "ocr_b2", [f], snap)
    ent = _load(tmp_path)["artifacts"]["slides_dedup.json"]
    assert ent["inputs"]["slides_dedup.json"]["sha256"] != ent["digest"]["sha256"]
    assert [r["status"] for r in pv.verify(tmp_path)] == ["ok"]   # own pre-image is not "stale"


def test_paths_outside_the_root_are_never_stored(tmp_path):
    lec = tmp_path / "lecture"
    media = _w(tmp_path / "private folder" / "talk.mp4", "media bytes")
    out = _w(lec / "transcript.json", "[]")
    pv.record(lec, "transcribe", [out], [media])
    raw = (lec / "provenance.json").read_text(encoding="utf-8")
    assert "external:talk.mp4" in raw
    assert "private folder" not in raw and str(tmp_path) not in raw


def test_directory_digest_and_sampled_large_files(tmp_path, monkeypatch):
    _w(tmp_path / "slides" / "frame_0001.jpg", "x")
    _w(tmp_path / "slides" / "frame_0002.jpg", "y")
    d1 = pv.digest(tmp_path / "slides")
    assert d1["kind"] == "dir" and d1["files"] == 2
    _w(tmp_path / "slides" / "frame_0002.jpg", "z")
    assert pv.digest(tmp_path / "slides")["sha256"] != d1["sha256"]
    monkeypatch.setattr(pv, "HASH_FULL_MAX", 10)
    monkeypatch.setattr(pv, "_SAMPLE", 4)
    big = _w(tmp_path / "big.bin", "0123456789abcdefghij")
    d = pv.digest(big)
    assert "sample_sha256" in d and "sha256" not in d and d["bytes"] == 20
    assert pv.digest(tmp_path / "nope") is None


def test_export_style_manifest_dir(tmp_path):
    """export_web keeps provenance.json in the support folder, keys relative to the HTML's folder."""
    html = _w(tmp_path / "影片筆記整合.html", "<html>")
    tl = _w(tmp_path / "影片筆記整合" / "_timeline.json", "{}")
    pv.record(tmp_path, "export_web", [html, tl], [], manifest_dir=tmp_path / "影片筆記整合")
    data = json.loads((tmp_path / "影片筆記整合" / "provenance.json").read_text(encoding="utf-8"))
    assert data["root"] == ".."
    assert set(data["artifacts"]) == {"影片筆記整合.html", "影片筆記整合/_timeline.json"}
    assert [r["status"] for r in pv.verify(tmp_path / "影片筆記整合")] == ["ok", "ok"]


def test_record_never_raises(tmp_path, capsys):
    out = _w(tmp_path / "a.json", "1")
    assert pv.record(tmp_path / "does" / "not" / "exist", "x", [out]) is False
    assert "provenance not recorded" in capsys.readouterr().err


def test_cli_record_and_verify(tmp_path):
    _w(tmp_path / "slides_grounded.json", "[]")
    _w(tmp_path / "slides_final.json", "[]")
    _w(tmp_path / "note_draft.md", "# n")
    cli = os.path.join(SCRIPTS, "provenance.py")
    r = subprocess.run([PY, cli, "record", str(tmp_path), "--stage", "synthesis",
                        "--output", "slides_final.json", "--output", "note_draft.md",
                        "--input", "slides_grounded.json", "--provider", "some-llm-vendor",
                        "--model", "model-x", "--network", "internet", "--param", "pass=tier"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    ent = _load(tmp_path)["artifacts"]["note_draft.md"]
    assert ent["tool"] == "manual" and ent["provider"] == "some-llm-vendor"
    assert ent["network"] == "internet" and ent["params"] == {"pass": "tier"}
    v = subprocess.run([PY, cli, "verify", str(tmp_path)], capture_output=True, text=True)
    assert v.returncode == 0 and "ok=2" in v.stdout
    _w(tmp_path / "slides_grounded.json", "[1]")
    v = subprocess.run([PY, cli, "verify", str(tmp_path), "--json"], capture_output=True, text=True)
    assert v.returncode == 1
    assert {r["status"] for r in json.loads(v.stdout)} == {"stale"}


@pytest.mark.parametrize("stage_script,args,outputs", [
    ("flag_asr_suspects.py", ["--dir", "{d}"], ["asr_suspects.txt"]),
])
def test_a_real_stage_records(tmp_path, stage_script, args, outputs):
    _w(tmp_path / "transcript.json", json.dumps([{"start": 0, "end": 2, "text": "Zzyzxq term"}]))
    r = subprocess.run([PY, os.path.join(SCRIPTS, stage_script)] + [a.format(d=tmp_path) for a in args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    arts = _load(tmp_path)["artifacts"]
    for o in outputs:
        assert arts[o]["tool"] == stage_script and "transcript.json" in arts[o]["inputs"]
    assert {r["status"] for r in pv.verify(tmp_path)} == {"ok"}
