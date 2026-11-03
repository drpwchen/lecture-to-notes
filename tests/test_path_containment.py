"""Path containment (issue #1, point 5) and local-only mode (point 8).

Directories given on the command line are the approved roots. Names that come out
of data files — a plan slug, Stage F's attachment_name, an embed target, a slide
filename — must stay inside them. Valid names pass through unchanged (outputs stay
byte-identical); anything else is refused, never rewritten into a different name.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from _common import (UnsafePathError, contained_path, is_within, require_network_allowed,
                     safe_basename)
from conftest import SCRIPTS

PY = sys.executable


# ---------------------------------------------------------------- safe_basename

@pytest.mark.parametrize("name", ["slides_grounded.json", "L2_seg01_opening.md",
                                  "01_開場三個問題_01.jpg", "a b c.md", "x..y.jpg", ".hidden",
                                  "01 .. 逐字稿"])
def test_safe_basename_accepts_plain_names(name):
    assert safe_basename(name) == name


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a\\b", "../x", "C:x", "x.md:stream",
                                  "tab\tname", "nul\x00", "CON", "nul.md", "com1.txt", None, 3])
def test_safe_basename_refuses(name):
    with pytest.raises(UnsafePathError):
        safe_basename(name)


# ---------------------------------------------------------------- contained_path

def test_contained_path_returns_the_plain_join(tmp_path):
    root = str(tmp_path)
    assert contained_path(root, "slides_raw.json") == os.path.join(root, "slides_raw.json")
    assert contained_path(root, "L2", "x.md") == os.path.join(root, "L2", "x.md")
    assert contained_path(root, "slides/frame_0001.jpg") == os.path.join(root, "slides/frame_0001.jpg")


def test_contained_path_keeps_the_root_type(tmp_path):
    p = contained_path(tmp_path, "a.json")
    assert isinstance(p, Path) and p == tmp_path / "a.json"


@pytest.mark.parametrize("part", ["../x", "a/../../x", "..\\x", "a\\..\\..\\x", "/etc/passwd",
                                  "\\\\server\\share\\x", "C:\\Windows\\x", "C:x", "",
                                  "a/\x00b"])
def test_contained_path_refuses_escapes(tmp_path, part):
    with pytest.raises(UnsafePathError):
        contained_path(str(tmp_path), part)


def _dir_link(target, link):
    """A directory symlink, or on Windows without the privilege an NTFS junction."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        if sys.platform == "win32":
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                               capture_output=True)
            return r.returncode == 0
        return False


def test_contained_path_follows_symlinks(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    if not _dir_link(outside, root / "link"):
        pytest.skip("cannot create a directory link here")
    with pytest.raises(UnsafePathError):
        contained_path(root, "link", "evil.json")
    assert not is_within(root, root / "link" / "evil.json")


def test_is_within(tmp_path):
    assert is_within(tmp_path, tmp_path)
    assert is_within(tmp_path, tmp_path / "a" / "b")
    assert not is_within(tmp_path / "a", tmp_path / "b")
    assert not is_within(tmp_path / "a", tmp_path / "ab")   # prefix is not containment


# ---------------------------------------------------------------- stages refuse hostile data

def _run(args, **kw):
    return subprocess.run([PY] + args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120, **kw)


def _snapshot(path):
    return sorted(str(p.relative_to(path)) for p in Path(path).rglob("*"))


def test_single_talk_refuses_a_path_slug(tmp_path):
    lec = tmp_path / "lec"
    lec.mkdir()
    (lec / "transcript.json").write_text(json.dumps(
        [{"start": 0.0, "end": 5.0, "text": "hello"}, {"start": 70.0, "end": 80.0, "text": "bye"}]),
        encoding="utf-8")
    (lec / "talk.mp4").write_bytes(b"")
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps([{"seg": 1, "start": 0, "end": 90, "slug": "../../escaped",
                                 "title_zh": "x"}]), encoding="utf-8")
    before = _snapshot(tmp_path)
    p = _run([os.path.join(SCRIPTS, "build_single_talk_web.py"), str(lec), "--plan", str(plan)])
    assert p.returncode != 0 and "slug" in p.stderr
    assert _snapshot(tmp_path) == before          # nothing written anywhere


def test_single_talk_still_works_with_a_normal_plan(tmp_path):
    lec = tmp_path / "lec"
    lec.mkdir()
    (lec / "transcript.json").write_text(json.dumps(
        [{"start": 0.0, "end": 5.0, "text": "hello"}, {"start": 40.0, "end": 50.0, "text": "bye"}]),
        encoding="utf-8")
    (lec / "talk.mp4").write_bytes(b"")
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps([{"seg": 1, "start": 0, "end": 30, "slug": "intro", "title_zh": "開場"},
                                {"seg": 2, "start": 30, "end": 60, "slug": "end", "title_zh": "結尾"}]),
                    encoding="utf-8")
    p = _run([os.path.join(SCRIPTS, "build_single_talk_web.py"), str(lec), "--plan", str(plan)])
    assert p.returncode == 0, p.stderr
    assert (lec / "L2" / "L2_seg01_intro.md").is_file()
    prov = json.loads((lec / "provenance.json").read_text(encoding="utf-8"))
    assert "L2/L2_seg01_intro.md" in prov["artifacts"]
    assert prov["artifacts"]["L2/L2_seg01_intro.md"]["inputs"]["transcript.json"]["sha256"]


def _final_slides(lec, attachment):
    (lec / "slides").mkdir(parents=True)
    (lec / "slides" / "frame_0001.jpg").write_bytes(b"\xff\xd8fake")
    (lec / "slides_final.json").write_text(json.dumps([
        {"slide_id": 1, "tier": 1, "filename": "frame_0001.jpg", "attachment_name": attachment}]),
        encoding="utf-8")


def test_finalize_refuses_an_attachment_name_that_leaves_the_vault(tmp_path):
    vault, lec = tmp_path / "vault", tmp_path / "20250101_spk_topic"
    vault.mkdir()
    _final_slides(lec, "../../../escaped.jpg")
    (lec / "note_draft.md").write_text("![[99Attachment/lecture_x/a.jpg]]\n", encoding="utf-8")
    p = _run([os.path.join(SCRIPTS, "finalize_to_vault.py"), str(lec), "--vault-root", str(vault)])
    assert p.returncode == 2 and "attachment_name" in p.stderr
    assert not list(tmp_path.rglob("escaped.jpg"))


def test_render_embeds_refuses_a_path_attachment_name(tmp_path):
    lec = tmp_path / "lec"
    _final_slides(lec, "..\\..\\escaped.jpg")
    (lec / "note_draft.md").write_text("# n\n\n[[EMBED s1]]\n", encoding="utf-8")
    p = _run([os.path.join(SCRIPTS, "render_embeds.py"), str(lec)])
    assert p.returncode == 2 and "attachment name" in p.stderr
    assert not (lec / "note_draft.rendered.md").exists()


def test_render_embeds_normal_name_unchanged(tmp_path):
    lec = tmp_path / "lec"
    _final_slides(lec, "spk_topic_s01.jpg")
    (lec / "note_draft.md").write_text("# n\n\n[[EMBED s1]]\n", encoding="utf-8")
    p = _run([os.path.join(SCRIPTS, "render_embeds.py"), str(lec), "--attach-root", "att"])
    assert p.returncode == 0, p.stderr
    out = (lec / "note_draft.rendered.md").read_text(encoding="utf-8")
    assert "![[att/spk_topic_s01.jpg|500]]" in out


def test_ground_slides_refuses_a_symlinked_output(tmp_path):
    lec, outside = tmp_path / "lec", tmp_path / "outside.json"
    lec.mkdir()
    outside.write_text("[]", encoding="utf-8")
    (lec / "slides_vlm.json").write_text("[]", encoding="utf-8")
    (lec / "transcript.json").write_text("[]", encoding="utf-8")
    try:
        os.symlink(outside, lec / "slides_grounded.json")
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here (Windows without developer mode)")
    p = _run([os.path.join(SCRIPTS, "ground_slides.py"), str(lec)])
    assert p.returncode == 2 and "outside" in p.stderr
    assert outside.read_text(encoding="utf-8") == "[]"


# ---------------------------------------------------------------- local-only mode

def test_require_network_allowed(monkeypatch):
    monkeypatch.delenv("LECTURE_LOCAL_ONLY", raising=False)
    require_network_allowed("test")          # off: returns
    for off in ("0", "false", "no", "off"):
        monkeypatch.setenv("LECTURE_LOCAL_ONLY", off)
        require_network_allowed("test")
    monkeypatch.setenv("LECTURE_LOCAL_ONLY", "1")
    with pytest.raises(SystemExit) as e:
        require_network_allowed("test")
    assert e.value.code == 2


def test_gemini_routes_fail_closed_before_any_work(tmp_path):
    env = dict(os.environ, LECTURE_LOCAL_ONLY="1")
    env.pop("GEMINI_API_KEY", None)
    media = tmp_path / "a.mp3"
    media.write_bytes(b"")
    for script, args in (("gemini_asr.py", ["--output-dir", str(tmp_path / "g")]),
                         ("asr_gemini_primary.py", ["-o", str(tmp_path / "p"), "--lang", "en"])):
        p = _run([os.path.join(SCRIPTS, script), str(media)] + args, env=env)
        assert p.returncode == 2 and "local-only" in p.stderr, script
    assert not (tmp_path / "g").exists() and not (tmp_path / "p").exists()


def test_groq_refuses_in_local_only_mode(monkeypatch, tmp_path):
    import groq_asr
    monkeypatch.setenv("LECTURE_LOCAL_ONLY", "1")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_not_a_real_key")
    with pytest.raises(RuntimeError, match="local-only"):
        groq_asr.transcribe_groq(str(tmp_path / "x.mp3"))


def test_export_refuses_an_embed_outside_the_figure_folders(tmp_path, monkeypatch, capsys):
    """0.9.0 resolved ![[../../x.jpg]] against figures/ and copied the file it found
    into the shareable support folder."""
    import export_web as ew
    course = tmp_path / "course"
    (course / "figures").mkdir(parents=True)
    (course / "figures" / "ok.jpg").write_bytes(b"x")
    (tmp_path / "secret.jpg").write_bytes(b"x")
    monkeypatch.setattr(ew, "FIGURE_ROOTS", [str(course / "figures")], raising=False)
    monkeypatch.setattr(ew, "COURSE", str(course), raising=False)
    monkeypatch.setattr(ew, "_IMG_INDEX", None, raising=False)
    assert ew.resolve_image_src("../secret.jpg") is None
    assert ew.resolve_image_src("../../secret.jpg") is None
    assert "refusing embed" in capsys.readouterr().err
    assert ew.resolve_image_src("ok.jpg") == os.path.join(str(course / "figures"), "ok.jpg")


def test_export_moves_only_clips_inside_the_course_root(tmp_path, monkeypatch):
    import types
    import export_web as ew
    root = tmp_path / "course"
    sup = root / "影片筆記整合"
    vid = sup / "媒體" / "影片"
    root.mkdir()
    (root / "inside.mp4").write_bytes(b"in")
    (tmp_path / "outside.mp4").write_bytes(b"out")
    man = root / "manifest.json"
    man.write_text("{}", encoding="utf-8")
    for k, v in {"A": types.SimpleNamespace(media_in_place=False, no_remux=False),
                 "SUPPORT": str(sup), "COURSE_ROOT": str(root), "COURSE": str(root),
                 "MEDIA_VID": str(vid), "VID_URL_PREFIX": "影片筆記整合/媒體/影片/",
                 "MEDIA_REL": {"inside.mp4": "inside.mp4", "outside.mp4": os.path.join("..", "outside.mp4")},
                 "MANIFEST": {"clips": []}, "MANIFEST_PATH": str(man)}.items():
        monkeypatch.setattr(ew, k, v, raising=False)
    moved = ew.move_media_into_support(["inside.mp4", "outside.mp4"], set())
    assert set(moved) == {"inside.mp4"}
    assert (vid / "inside.mp4").is_file() and not (root / "inside.mp4").exists()
    assert (tmp_path / "outside.mp4").read_bytes() == b"out"     # left where it was
