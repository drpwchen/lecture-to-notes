"""tools/sync_from_skill.py leak scan: the backstop that aborts a sync when a published
payload carries a machine-local path or helper."""
import importlib.util
import os

import pytest

_spec = importlib.util.spec_from_file_location(
    "sync_from_skill", os.path.join(os.path.dirname(__file__), "..", "tools", "sync_from_skill.py"))
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)


@pytest.mark.parametrize("text", [
    'p = r"C:\\Users\\someone\\x.mp4"',
    "D:/Users/a/b",
    "cd /mnt/c/Users/x/proj",
    "open('/home/alice/data')",
    "see /Users/bob/Documents",
    "from _platform import secret",
    'key = secret("GEMINI_API_KEY")',
    'sys.path.insert(0, "~/.claude/hooks")',
    r"\\nas\home\x",
    r"C:\Users\someone\x.mp4",
    "//nas/home/x",
])
def test_leaks_are_caught(text):
    assert S.leak_scan("f.py", text.encode())


@pytest.mark.parametrize("text", [
    "Drop the tree into `~/.claude/skills/lecture-to-notes/`",
    "subprocess.run(['wsl.exe', '-d', 'Ubuntu'])",
    'timeout 900 $HOME/.local/bin/agy -p "x"',
    "return f'/mnt/{p[0].lower()}{p[2:]}'",
])
def test_generic_text_passes(text):
    assert S.leak_scan("f.py", text.encode()) == []


def test_current_tree_is_clean():
    skill = os.path.expanduser("~/.claude/skills/lecture-to-notes")
    if not os.path.isdir(skill):
        pytest.skip("no upstream skill tree on this machine")
    from pathlib import Path
    deny = S.load_denylist(Path(skill))
    S._DENY[:] = deny
    hits = []
    for src, dst, fn in S.collect(Path(skill)):
        hits += S.leak_scan(dst.relative_to(S.REPO).as_posix(), S.render(src, fn), deny)
    assert hits == []
