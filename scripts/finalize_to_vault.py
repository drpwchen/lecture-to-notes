"""Finalize a synthesized lecture into the Obsidian vault.

1. Read slides_final.json (Stage F output: tier + attachment_name per slide).
2. Copy ONLY cited slides (tier in {1,2}, not embed_suppressed) from
   <lecture_dir>/slides/<filename> -> 99Attachment/lecture_<slug>/<attachment_name>.
3. Copy note_draft.md -> 00Inbox/<note_name>.
4. Audit: every ![[99Attachment/lecture_<slug>/...]] reference in the note must
   now resolve to a copied file; warn on any that don't (folds in the old
   _audit_and_fix_attachments check).

Slug/speaker/topic are derived from the dir name (YYYYMMDD_..._speaker_topic),
overridable via flags for non-standard names.

This is the one vault-coupled stage: it needs somewhere to put attachments and
notes. The vault root comes from _paths (CLAUDE_VAULT_ROOT env var) and can be
overridden per-run with --vault-root.

Usage:
    python finalize_to_vault.py <lecture_dir> \
        [--note-name 20250914_speaker_topic.md] \
        [--speaker NAME] [--topic TOPIC] [--date 20250914] \
        [--vault-root PATH] [--force] [--allow-no-refs] [--dry-run]
"""
import argparse, json, re, shutil, sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import parse_tier
from _paths import VAULT_ROOT as DEFAULT_VAULT

REF_RE = re.compile(r"!\[\[99Attachment/lecture_[^/]+/([^|\]]+)")


def derive(slug):
    """Best-effort (date, speaker, topic) from a dir name like
    20250914_L1_<speaker>_<topic> or <speaker>_<topic>."""
    parts = slug.split("_")
    date = parts[0] if parts and re.fullmatch(r"\d{8}", parts[0]) else ""
    rest = parts[1:] if date else parts
    rest = [p for p in rest if not re.fullmatch(r"L\d+", p)]  # drop L1.. tokens
    speaker = rest[0] if rest else "unknown"
    topic = "_".join(rest[1:]) if len(rest) > 1 else "lecture"
    return date, speaker, topic


def needs_refresh(src: Path, dst: Path) -> bool:
    """True when dst is absent or differs from src by size or mtime.

    Re-running extraction with a fixed crop used to leave the OLD frame in the
    vault forever (the copy was skipped whenever the destination existed), so
    the note kept showing a stale image with no sign anything was wrong.
    """
    if not dst.exists():
        return True
    try:
        s_st, d_st = src.stat(), dst.stat()
    except OSError:
        return True
    return (s_st.st_size != d_st.st_size
            or int(s_st.st_mtime) != int(d_st.st_mtime))


def main():
    ap = argparse.ArgumentParser(description="Finalize lecture -> vault (attachments + note + audit)")
    ap.add_argument("lecture_dir")
    ap.add_argument("--note-name", default=None)
    ap.add_argument("--speaker", default=None)
    ap.add_argument("--topic", default=None)
    ap.add_argument("--date", default=None)
    ap.add_argument("--vault-root", default=None,
                    help="Obsidian vault root (default: _paths.VAULT_ROOT / "
                         "$CLAUDE_VAULT_ROOT)")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite an existing note of the same name in 00Inbox")
    ap.add_argument("--allow-no-refs", action="store_true",
                    help="Treat a note with zero attachment references as OK "
                         "(default: that is an error)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    vault = Path(args.vault_root).expanduser() if args.vault_root else Path(DEFAULT_VAULT)
    if not vault.is_dir():
        print(f"ERROR: vault root not found: {vault}\n"
              "  Pass --vault-root <path> or set the CLAUDE_VAULT_ROOT env var.",
              file=sys.stderr)
        sys.exit(2)
    attach_root = vault / "99Attachment"
    inbox = vault / "00Inbox"

    lec = Path(args.lecture_dir).resolve()
    slug = lec.name
    d_date, d_speaker, d_topic = derive(slug)
    date = args.date or d_date
    speaker = args.speaker or d_speaker
    topic = args.topic or d_topic

    note_path = lec / "note_draft.md"
    sf_path = lec / "slides_final.json"
    for p in (note_path, sf_path):
        if not p.exists():
            print(f"MISSING: {p}", file=sys.stderr)
            sys.exit(1)

    note_name = args.note_name or (f"{date}_{speaker}_{topic}.md" if date else f"{speaker}_{topic}.md")
    out_note = inbox / note_name
    # Checked BEFORE any copying: two talks by the same speaker, or a dir name
    # without a date, collide on this filename, and the old code overwrote the
    # earlier note without a word.
    if out_note.exists() and not args.force:
        print(f"ERROR: a note of this name already exists:\n    {out_note}\n"
              "  Refusing to overwrite it. Either pass --note-name <other.md>, "
              "or --force to replace it.", file=sys.stderr)
        sys.exit(2)

    # Refs scan BEFORE any copying (WP-V finding C2: the old placement wrote
    # the note + attachments into the vault and only then exited 2 on 0 refs,
    # leaving the vault modified on a failed run). The scan needs only the
    # source note, so it can gate everything.
    refs = set(REF_RE.findall(note_path.read_text(encoding="utf-8")))
    if not refs and not args.allow_no_refs:
        # "0 of 0 references resolve" used to print a green tick, certifying
        # nothing — the usual cause is embeds that were never rendered.
        print("ERROR: no 99Attachment references found in the note — wrong note "
              "or embeds not rendered? Run render_embeds.py, check "
              f"{note_path}, or pass --allow-no-refs if the note really has no "
              "figures.", file=sys.stderr)
        sys.exit(2)

    attach_dir = attach_root / f"lecture_{slug}"
    if not args.dry_run:
        attach_dir.mkdir(parents=True, exist_ok=True)
        inbox.mkdir(parents=True, exist_ok=True)

    slides_final = json.loads(sf_path.read_text(encoding="utf-8"))

    copied, refreshed = {}, []
    for s in slides_final:
        if parse_tier(s.get("tier")) > 2 or s.get("embed_suppressed_reason"):
            continue
        fn = s.get("filename")
        if not fn:
            print(f"  SKIP: slide entry without 'filename': {str(s)[:120]}",
                  file=sys.stderr)
            continue
        src = lec / "slides" / fn
        if not src.exists():
            print(f"  MISSING slide source: {src.name}")
            continue
        dst_name = s.get("attachment_name")
        if not dst_name:
            sid = s.get("slide_id")
            if sid is None:
                print(f"  SKIP: {fn} has neither 'attachment_name' nor 'slide_id' "
                      "— cannot name the attachment", file=sys.stderr)
                continue
            try:
                dst_name = f"{speaker}_{topic}_s{int(sid):02d}.jpg"
            except (TypeError, ValueError):
                print(f"  SKIP: {fn} has a non-numeric slide_id {sid!r}",
                      file=sys.stderr)
                continue
        dst = attach_dir / dst_name
        stale = needs_refresh(src, dst)
        if stale and dst.exists():
            refreshed.append(dst_name)
        if not args.dry_run and stale:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        copied[dst_name] = True
    prefix = "[dry-run] " if args.dry_run else ""
    print(f"{prefix}Copied {len(copied)} cited slides -> {attach_dir}")
    if refreshed:
        print(f"{prefix}Refreshed {len(refreshed)} changed attachment(s):")
        for n in refreshed:
            print(f"     ↻ {n}")

    if not args.dry_run:
        shutil.copy2(note_path, out_note)
    print(f"{prefix}Note -> {out_note}")

    # Audit: note references must all resolve to copied attachments
    # (refs were scanned before any copying; see the pre-copy gate above)
    unresolved = [r for r in refs if r not in copied and not (attach_dir / r).exists()]
    if unresolved:
        print(f"  ⚠️ {len(unresolved)} note references have no attachment (tier-3 cited? rename mismatch?):")
        for r in sorted(unresolved)[:10]:
            print(f"     ✗ {r}")
    else:
        print(f"  ✓ audit: all {len(refs)} note references resolve")


if __name__ == "__main__":
    main()
