#!/usr/bin/env python3
"""Export the private lecture-to-notes skill tree into this public repo.

Direction is ONE WAY: skill dir -> repo. The skill is the source of truth;
this repo is a published projection of it. To change shipped code, edit the
skill and re-run this script, then review the diff and commit. The only files
you may edit here directly are the repo-only ones (README, LICENSE, CHANGELOG,
docs/, .gitignore, SYNC-LEDGER.md, tools/).

Design notes
------------
- **Manifest-driven, not "copy everything minus excludes"**: every shipped path
  is named explicitly below. A new file in the skill is NOT published until
  someone adds it here. That is the point -- an exclude-list export leaks the
  next private file somebody drops into the tree.
- **Transforms are declarative and fail loudly**: each transform asserts its
  anchor was found. If an upstream edit moves the anchor, the sync ABORTS
  instead of silently shipping the untransformed (private) text.
- **Verify mode**: ``--check`` reports what would change without writing, so CI
  or a pre-commit habit can catch "someone edited the repo copy directly".

Usage
-----
    python tools/sync_from_skill.py            # sync (writes)
    python tools/sync_from_skill.py --check    # dry run, exit 1 if out of date
    python tools/sync_from_skill.py --skill-dir <path>
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_SKILL_DIR = Path.home() / ".claude" / "skills" / "lecture-to-notes"


# --------------------------------------------------------------------------
# Transforms
# --------------------------------------------------------------------------
# Each takes the source text and returns the text to publish. Every one asserts
# its anchor matched -- an unmatched anchor means the upstream file changed
# shape and a human must re-check what is being published.

class TransformError(RuntimeError):
    pass


def _sub_once(pattern: re.Pattern, repl: str, text: str, what: str) -> str:
    out, n = pattern.subn(lambda _m: repl, text, count=1)
    if n != 1:
        raise TransformError(
            f"anchor not found for '{what}'. The upstream file changed shape; "
            f"re-check what this transform is supposed to strip before syncing."
        )
    return out


_GROQ_HEADER = re.compile(
    r"Vendored \d{4}-\d{2}-\d{2} from ~/\.claude/skills/transcribe/.*?"
    r"is not installed\.\n",
    re.S,
)
_GROQ_HEADER_NEW = (
    "Self-contained: this module has no dependency on any other skill or on the\n"
    "local pipeline's config -- hand it a media file and a key and it transcribes.\n"
)

_GROQ_KEYDOC = re.compile(r"Key handling\n-+\n.*?\n\"\"\"", re.S)
_GROQ_KEYDOC_NEW = (
    "Key handling\n"
    "------------\n"
    "``GROQ_API_KEY`` is read from the process environment and nowhere else. When it\n"
    "is unset, :func:`transcribe_groq` raises an actionable RuntimeError telling you\n"
    "where to get a key. The value is used internally and NEVER printed or echoed.\n"
    '"""'
)

# Strips the machine-local encrypted-secret-store fallback: everything from
# _key_from_local_store() through the end of get_groq_key(), replaced by an
# env-only resolver. The RuntimeError in transcribe_groq is untouched.
_GROQ_KEYFUNCS = re.compile(
    r"def _key_from_local_store\(\).*?(?=def _lang_for_groq)", re.S
)
_GROQ_KEYFUNCS_NEW = '''def get_groq_key() -> str:
    """Resolve the Groq API key from the ``GROQ_API_KEY`` environment variable.

    Returns '' when the variable is unset or blank; transcribe_groq turns that
    into an actionable RuntimeError.
    """
    return (os.environ.get("GROQ_API_KEY") or "").strip()


'''


def t_groq_asr(text: str) -> str:
    """Env-only key handling; drop the local encrypted-store fallback."""
    text = _sub_once(_GROQ_HEADER, _GROQ_HEADER_NEW, text, "groq_asr module header")
    text = _sub_once(_GROQ_KEYDOC, _GROQ_KEYDOC_NEW, text, "groq_asr key-handling docstring")
    text = _sub_once(_GROQ_KEYFUNCS, _GROQ_KEYFUNCS_NEW, text, "groq_asr local-store fallback")
    if "secret.ps1" in text or "_key_from_local_store" in text:
        raise TransformError("groq_asr: local secret store still referenced after transform")
    if "subprocess" not in text:  # _compress/_split still need it
        raise TransformError("groq_asr: subprocess import unexpectedly gone")
    return text


# (2026-08-02) Five transforms retired: the private traces they stripped
# (personal vault default in audit_note, machine-specific _paths docstring,
# private control-plane directory names in export_web/ocr_slides/SKILL.md)
# were fixed in the skill itself, so those files now sync byte-identical.


_DATA_GITIGNORE = """# The two wordlists here ARE tracked in this repo -- they are frequency lists of
# ordinary dictionary words and acronyms, used to decide whether an ASR token is
# a real word. Regenerate from your own corpus with scripts/build_real_words.py
# if you want lists tuned to your domain.
#
# (Upstream, these files are generated per-machine and deliberately untracked;
# the shipped copies are the deliberate exception. -- tools/sync_from_skill.py)
__pycache__/
*.pyc
"""


def t_data_gitignore(_text: str) -> str:
    """The private side ignores the generated wordlists; the OSS repo tracks them."""
    return _DATA_GITIGNORE


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------
# ("src relative to skill dir", "dst relative to repo", transform_or_None)
# A src ending in a glob is expanded; transforms only apply to explicit files.

FILES: list[tuple[str, str, object]] = [
    # 2026-08-02: the SKILL.md/audit_note/_paths/export_web/ocr_slides transforms
    # were retired — the private traces they stripped are now fixed in the skill
    # itself, so those files sync byte-identical. Only groq_asr (secret-store
    # fallback removal) and data/.gitignore (track shipped wordlists) remain.
    ("SKILL.md", "SKILL.md", None),
    ("config.example.yaml", "config.example.yaml", None),
    ("data/real_words.txt", "data/real_words.txt", None),
    ("data/real_acronyms.txt", "data/real_acronyms.txt", None),
    ("data/.gitignore", "data/.gitignore", t_data_gitignore),
    # ocr_bench: harness only. fixtures/, results/, report.md and
    # fixtures_manifest.json are NOT published (they are real lecture slides and
    # this machine's numbers). README is repo-authored -- see OVERRIDES.
    ("ocr_bench/schema.py", "ocr_bench/schema.py", None),
    ("ocr_bench/run_bench.py", "ocr_bench/run_bench.py", None),
    ("ocr_bench/report.py", "ocr_bench/report.py", None),
    ("ocr_bench/adapters/rapidocr_adapter.py", "ocr_bench/adapters/rapidocr_adapter.py", None),
    ("ocr_bench/adapters/paddleocr_vl_adapter.py", "ocr_bench/adapters/paddleocr_vl_adapter.py", None),
    # Transformed scripts.
    ("scripts/groq_asr.py", "scripts/groq_asr.py", t_groq_asr),
    ("scripts/audit_note.py", "scripts/audit_note.py", None),
    ("scripts/_paths.py", "scripts/_paths.py", None),
    ("scripts/export_web.py", "scripts/export_web.py", None),
    ("scripts/ocr_slides.py", "scripts/ocr_slides.py", None),
]

GLOBS: list[tuple[str, str, str]] = [
    # (src dir, glob, dst dir)
    ("reference", "*.md", "reference"),
    ("scripts", "*.py", "scripts"),
    ("scripts/adapters", "*.py", "scripts/adapters"),
    ("scripts/batch", "*.py", "scripts/batch"),
    ("scripts/layout2", "*", "scripts/layout2"),
]

# Never published, whatever a glob says.
NEVER = {
    "config.yaml",              # author name + machine paths
    "scripts/.gitignore",       # merged into the root .gitignore instead
}

# Repo-authored files that shadow a skill file of the same path. The skill copy
# is never published; the template here is.
OVERRIDES = {
    "ocr_bench/README.md": "tools/templates/ocr_bench_README.md",
}


def collect(skill_dir: Path) -> list[tuple[Path, Path, object]]:
    """Resolve the manifest into concrete (src, dst, transform) triples."""
    explicit = {dst for _s, dst, _t in FILES}
    jobs: list[tuple[Path, Path, object]] = []

    for src_rel, dst_rel, fn in FILES:
        jobs.append((skill_dir / src_rel, REPO / dst_rel, fn))

    for src_dir, pattern, dst_dir in GLOBS:
        base = skill_dir / src_dir
        if not base.is_dir():
            raise SystemExit(f"missing source dir: {base}")
        for p in sorted(base.glob(pattern)):
            if not p.is_file():
                continue
            rel = f"{dst_dir}/{p.name}"
            if rel in explicit or rel in NEVER or rel in OVERRIDES:
                continue  # explicit entry (possibly transformed) wins
            if p.name.endswith(".pyc") or "__pycache__" in p.parts:
                continue
            jobs.append((p, REPO / rel, None))

    for dst_rel, tpl_rel in OVERRIDES.items():
        tpl = REPO / tpl_rel
        if not tpl.is_file():
            raise SystemExit(f"missing override template: {tpl}")
        jobs.append((tpl, REPO / dst_rel, None))

    return jobs


def render(src: Path, fn) -> bytes:
    if fn is None:
        return src.read_bytes()
    text = src.read_text(encoding="utf-8")
    try:
        return fn(text).encode("utf-8")
    except TransformError as e:
        raise SystemExit(f"TRANSFORM FAILED on {src}: {e}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--skill-dir", default=str(DEFAULT_SKILL_DIR),
                    help="source skill directory (read-only; never written to)")
    ap.add_argument("--check", action="store_true",
                    help="report differences without writing; exit 1 if any")
    args = ap.parse_args()

    skill_dir = Path(args.skill_dir).resolve()
    if not skill_dir.is_dir():
        raise SystemExit(f"skill dir not found: {skill_dir}")
    if REPO in skill_dir.parents or skill_dir == REPO:
        raise SystemExit("refusing to sync: skill dir is inside the repo")

    jobs = collect(skill_dir)
    changed, created, same = [], [], 0

    for src, dst, fn in jobs:
        if not src.is_file():
            raise SystemExit(f"manifest names a file that does not exist: {src}")
        payload = render(src, fn)
        rel = dst.relative_to(REPO).as_posix()
        if not dst.exists():
            created.append(rel)
        elif dst.read_bytes() != payload:
            changed.append(rel)
        else:
            same += 1
            continue
        if not args.check:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(payload)

    verb = "would create" if args.check else "created"
    print(f"{len(jobs)} manifest entries: {same} unchanged, "
          f"{len(changed)} {'differ' if args.check else 'updated'}, "
          f"{len(created)} {verb}")
    for r in created:
        print(f"  + {r}")
    for r in changed:
        print(f"  ~ {r}")

    if args.check and (changed or created):
        print("\nrepo is OUT OF DATE with the skill (or was edited directly).")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
