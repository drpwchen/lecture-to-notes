"""import_transcript.py (issue #1, point 1): an external transcript becomes the
contract in docs/transcript-contract.md, and the stages downstream accept it."""
import hashlib
import json
import os
import subprocess
import sys

import pytest

import import_transcript as it
from _common import load_segments
from conftest import SCRIPTS

PY = sys.executable

SRT = """1
00:00:01,000 --> 00:00:04,380
<i>Good morning,</i> everyone.

2
00:00:04,380 --> 00:00:09,100
{\\an8}Today: the brachial
plexus &amp; its branches.

3
01:00:00,500 --> 01:00:02,000
Past the hour.
"""

VTT = """WEBVTT
Kind: captions

NOTE this block is a comment
and spans two lines

STYLE
::cue { color: yellow }

intro
00:01.000 --> 00:04.000 align:start position:10%
<v Dr. Lee>Welcome <c.yellow>back</c></v>

00:04.000 --> 00:06.500
<00:04.200>word <00:05.000>timed
"""

WHISPER = {
    "text": "…", "language": "en",
    "segments": [
        {"id": 1, "start": 5.0, "end": 7.5, "text": " second", "avg_logprob": -0.2,
         "words": [{"start": 5.0, "end": 6.0, "word": " sec", "probability": 0.9},
                   {"start": 6.0, "end": 7.5, "word": "ond", "probability": 0.8}]},
        {"id": 0, "start": 0.0, "end": 4.38, "text": " first",
         "words": [{"start": 0.0, "end": 4.38, "word": " first"}]},
        {"id": 2, "start": 8.0, "end": 9.0, "text": " unaligned",
         "words": [{"word": " un"}, {"start": 8.5, "end": 9.0, "word": "aligned"}]},
    ],
}


def _sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def _import(tmp_path, name, content, *extra, out="lec"):
    src = tmp_path / name
    if isinstance(content, (dict, list)):
        src.write_text(json.dumps(content), encoding="utf-8")
    else:
        src.write_text(content, encoding="utf-8")
    out_dir = tmp_path / out
    rc = it.main([str(src), "--out-dir", str(out_dir), *extra])
    return rc, src, out_dir


def _segs(out_dir):
    with open(out_dir / "transcript.json", encoding="utf-8") as fh:
        return json.load(fh)


def test_srt(tmp_path):
    rc, src, out = _import(tmp_path, "cap.srt", SRT)
    assert rc == 0
    segs = _segs(out)
    assert isinstance(segs, list) and len(segs) == 3
    assert segs[0] == {"start": 1.0, "end": 4.38, "text": "Good morning, everyone."}
    assert segs[1]["text"] == "Today: the brachial plexus & its branches."
    assert segs[2]["start"] == 3600.5
    txt = (out / "transcript.txt").read_text(encoding="utf-8").splitlines()
    assert txt[0] == "[00:01] Good morning, everyone." and txt[2].startswith("[1:00:00] ")


def test_vtt_with_voice_notes_and_inline_timestamps(tmp_path):
    rc, _, out = _import(tmp_path, "cap.vtt", VTT)
    assert rc == 0
    segs = _segs(out)
    assert [s["text"] for s in segs] == ["Welcome back", "word timed"]
    assert segs[0]["speaker"] == "Dr. Lee" and segs[0]["start"] == 1.0 and segs[1]["end"] == 6.5


def test_whisper_json_sorted_and_words(tmp_path):
    rc, _, out = _import(tmp_path, "w.json", WHISPER, "--engine", "mlx-whisper", "--model", "large-v3")
    assert rc == 0
    segs = _segs(out)
    assert [s["text"] for s in segs] == [" first", " second", " unaligned"]
    assert segs[1]["words"] == [{"start": 5.0, "end": 6.0, "word": " sec"},
                                {"start": 6.0, "end": 7.5, "word": "ond"}]
    assert "words" not in segs[2]          # one word without times -> interpolated downstream
    assert set(segs[0]) == {"start", "end", "text", "words"}   # provider fields not carried
    meta = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
    st = meta["stages"]["transcribe"]
    assert st["engine"] == "mlx-whisper" and st["whisper_model"] == "large-v3" and st["n_segments"] == 3
    prov = json.loads((out / "provenance.json").read_text(encoding="utf-8"))
    ent = prov["artifacts"]["transcript.json"]
    assert ent["engine"] == "mlx-whisper" and ent["params"]["format"] == "json"
    assert ent["inputs"]["external:w.json"]["sha256"] == _sha(tmp_path / "w.json")


def test_generic_json_ms_with_offset(tmp_path):
    data = {"utterances": [{"begin": 1500, "finish": 2500, "words": "hi"},
                           {"begin": 0, "finish": 1000, "words": "first"}]}
    rc, _, out = _import(tmp_path, "x.json", data, "--list-key", "utterances", "--start-key", "begin",
                         "--end-key", "finish", "--text-key", "words", "--time-unit", "ms",
                         "--offset", "600")
    assert rc == 0
    assert _segs(out) == [{"start": 600.0, "end": 601.0, "text": "first"},
                          {"start": 601.5, "end": 602.5, "text": "hi"}]


def test_bare_list_json(tmp_path):
    rc, _, out = _import(tmp_path, "t.json", [{"start": 0, "end": 1, "text": None}])
    assert rc == 0 and _segs(out) == [{"start": 0.0, "end": 1.0, "text": ""}]


@pytest.mark.parametrize("content", [
    [{"start": 5, "end": 4, "text": "end before start"}],
    [{"start": "abc", "end": 4, "text": "x"}],
    [{"end": 4, "text": "x"}],
    [{"start": 0, "end": 1, "text": 3}],
    {"no": "segments"},
])
def test_bad_json_is_refused_and_nothing_written(tmp_path, content):
    rc, _, out = _import(tmp_path, "bad.json", content)
    assert rc == 2 and not (out / "transcript.json").exists()


def test_bad_srt_is_refused(tmp_path):
    rc, _, out = _import(tmp_path, "bad.srt", "1\n00:00:01 --> 00:00:02\nno millis\n")
    assert rc == 2 and not (out / "transcript.json").exists()


def test_source_untouched_overwrite_refused_backup_kept(tmp_path):
    rc, src, out = _import(tmp_path, "cap.srt", SRT)
    h = _sha(src)
    first = _sha(out / "transcript.json")
    rc2, _, _ = _import(tmp_path, "cap2.srt", "1\n00:00:00,000 --> 00:00:01,000\nnew\n")
    assert rc2 == 2 and _sha(out / "transcript.json") == first          # no --force
    rc3, _, _ = _import(tmp_path, "cap2.srt", "1\n00:00:00,000 --> 00:00:01,000\nnew\n",
                        "--force", "--backup")
    assert rc3 == 0 and _sha(src) == h
    baks = sorted(p.name for p in out.iterdir() if ".bak-import-" in p.name)
    assert len(baks) == 2 and _sha(out / baks[0]) == first


def test_importing_onto_itself_is_refused(tmp_path):
    out = tmp_path / "lec"
    out.mkdir()
    tj = out / "transcript.json"
    tj.write_text(json.dumps([{"start": 0, "end": 1, "text": "x"}]), encoding="utf-8")
    h = _sha(tj)
    assert it.main([str(tj), "--out-dir", str(out), "--force"]) == 2
    assert _sha(tj) == h


def test_format_detection():
    assert it.detect_format("x.srt", "") == "srt"
    assert it.detect_format("x.txt", "WEBVTT\n") == "vtt"
    assert it.detect_format("x.txt", '{"segments": []}') == "json"
    assert it.detect_format("x.txt", "1\n00:00:01,000 --> 00:00:02,000\n") == "srt"
    with pytest.raises(it.ImportError_):
        it.detect_format("x.txt", "just words")


def test_downstream_readers_accept_the_import(tmp_path):
    """The contract's readers: the shared loader, Step 11 and the single-talk assembly."""
    rc, _, out = _import(tmp_path, "w.json", WHISPER)
    assert rc == 0
    assert len(load_segments(str(out / "transcript.json"))) == 3
    r = subprocess.run([PY, os.path.join(SCRIPTS, "flag_asr_suspects.py"), "--dir", str(out)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    (out / "talk.mp4").write_bytes(b"")
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps([{"seg": 1, "start": 0, "end": 10, "slug": "all", "title_zh": "全"}]),
                    encoding="utf-8")
    r = subprocess.run([PY, os.path.join(SCRIPTS, "build_single_talk_web.py"), str(out),
                        "--plan", str(plan)], capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    assert r.returncode == 0, r.stderr
    l2 = (out / "L2" / "L2_seg01_all.md").read_text(encoding="utf-8")
    assert "- `(V1 00:00)` first second unaligned" in l2      # sorted by start, one 30 s bucket
