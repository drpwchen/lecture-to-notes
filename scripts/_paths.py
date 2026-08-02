"""Vendored from ~/.claude/scripts/_paths.py (2026-08-02) so the skill is
self-contained; env overrides documented below still apply.

Shared path resolver for Claude Code skill/scheduled scripts.

Purpose: centralize filesystem roots so the same scripts run on any machine —
set the env vars below to relocate; with none set, every constant resolves to
a conventional HOME-relative default.

Env overrides (set in shell profile / Task Scheduler action if migrating):
    CLAUDE_DIR            -> ~/.claude
    CLAUDE_VAULT_ROOT      -> Obsidian vault root
    CLAUDE_PROJECTS        -> Documents/Projects root
    CLAUDE_TEXTBOOK_MD     -> textbook-md output root
    CLAUDE_VAULT_SEARCH    -> ~/.vault-search (lancedb + hash caches)
    CLAUDE_ONEDRIVE        -> OneDrive root

Usage (from a sibling script in this scripts/ directory):
    from _paths import VAULT_ROOT, TEXTBOOK_MD
"""

import os
from pathlib import Path

HOME = Path.home()

CLAUDE_DIR = Path(os.environ.get("CLAUDE_DIR") or (HOME / ".claude"))
VAULT_ROOT = Path(os.environ.get("CLAUDE_VAULT_ROOT") or (HOME / "Documents" / "Obsidian" / "Obsidian"))
PROJECTS = Path(os.environ.get("CLAUDE_PROJECTS") or (HOME / "Documents" / "Projects"))
TEXTBOOK_MD = Path(os.environ.get("CLAUDE_TEXTBOOK_MD") or (PROJECTS / "textbook-md"))
VAULT_SEARCH_DIR = Path(os.environ.get("CLAUDE_VAULT_SEARCH") or (HOME / ".vault-search"))
ONEDRIVE = Path(os.environ.get("CLAUDE_ONEDRIVE") or (HOME / "OneDrive"))

if __name__ == "__main__":
    for name in ("HOME", "CLAUDE_DIR", "VAULT_ROOT", "PROJECTS", "TEXTBOOK_MD",
                 "VAULT_SEARCH_DIR", "ONEDRIVE"):
        print(f"{name} = {globals()[name]}")
