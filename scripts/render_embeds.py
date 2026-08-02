"""Stage F post-processor: expand semantic embed placeholders into Obsidian
callouts, deterministically. This is the "layout is not reasoning" split — the
synthesis subagent only writes WHERE a figure helps the clinical narrative;
this script handles all the mechanical formatting it used to carry.

The subagent writes a placeholder on its own line:
    [[EMBED s12]]
    [[EMBED s12: decision_support]]      # optional semantic intent (density control)

This script, using slides_final.json, expands each to a col-0 callout with the
correct attachment path + width + caption, and AUDITS:
  - Tier-3 / suppressed / unknown slide referenced  -> removed + warned
  - Tier-1/2 slide never referenced                 -> warned (missing figure)
  - inline placeholder (not on its own line)        -> ref marker + callout after line

Attachment path: `--attach-root` (default `99Attachment/lecture_{slug}`, the
Obsidian vault convention this pipeline was built for) or `paths.vault_attach_pattern`
in config.yaml. `{slug}` is substituted. Outside that vault layout, set it —
otherwise every embed is a dead link.

The `[!figure]` callout type is NOT one of Obsidian's built-ins; without a CSS
snippet defining it, it renders as a default note callout. That is cosmetic, not
broken.

Pairs with finalize_to_vault.py (which copies the cited images + note to vault).
Pipeline order: subagent -> render_embeds.py -> finalize_to_vault.py.

Usage:
    python render_embeds.py <lecture_dir> [--note note_draft.md]
                            [--slug <attach_slug>] [--attach-root PATTERN]
                            [--attach-dir DIR] [--in-place] [--dry-run]
"""
import argparse, json, os, re, sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from _common import load_config, parse_tier  # noqa: E402

PLACEHOLDER = re.compile(r"\[\[EMBED\s+s(\d+)(?:\s*:\s*([^\]]+?))?\s*\]\]")
DEFAULT_ATTACH_ROOT = "99Attachment/lecture_{slug}"


def load_final(lec_dir):
    p = os.path.join(lec_dir, "slides_final.json")
    if not os.path.isfile(p):
        print(f"ERROR: {p} not found", file=sys.stderr)
        sys.exit(2)
    by_id = {}
    with open(p, encoding="utf-8") as fh:
        for s in json.load(fh):
            try:
                by_id[int(s["slide_id"])] = s
            except (KeyError, ValueError, TypeError):
                pass
    return by_id


def tier_of(s):
    """Tier as an int, failing OPEN (keep the slide) on anything unparseable.

    Delegates to _common.parse_tier: Stage F is an LLM step, so `tier` arrives
    as 1, "1", "T1", "T1 核心", 1.0. The old `int(str(v).lstrip("Tt"))` raised on
    most of those and the caller swallowed it into tier 3 — which here means
    "drop the figure". A labeling wobble silently deleted content while the
    audit line still looked green.
    """
    return parse_tier(s.get("tier"))


def resolve_attach_root(arg_value, lec_dir):
    """--attach-root > config paths.vault_attach_pattern > vault default."""
    if arg_value:
        return arg_value
    try:
        cfg = load_config()
    except RuntimeError as e:
        print(f"  [config] {e}; using the built-in attachment pattern",
              file=sys.stderr)
        return DEFAULT_ATTACH_ROOT
    pat = ((cfg.get("paths") or {}).get("vault_attach_pattern") or "").strip()
    return pat or DEFAULT_ATTACH_ROOT


def attachment_name(s, missing_names):
    """Filename for a slide's attachment, reading either key.

    finalize_to_vault.py writes/reads `attachment_name`; other stages carry
    `filename`. Reading only one produced pages full of embeds pointing at
    fabricated names — with a green audit, because the audit only counted
    placeholders.
    """
    name = s.get("attachment_name") or s.get("filename")
    if not name:
        name = f"s{int(s['slide_id']):02d}.jpg"
        missing_names.append(int(s["slide_id"]))
    return name


def callout(attach_root, s, intent, missing_names):
    name = attachment_name(s, missing_names)
    width = s.get("embed_width") or 500
    cap = (intent or s.get("section_suggestion")
           or (s.get("retrieval") or {}).get("summary_sentence") or "figure")
    return (f"> [!figure] s{int(s['slide_id'])} — {cap} (T{tier_of(s)})\n"
            f"> ![[{attach_root}/{name}|{width}]]")


def resolve_note_paths(lec, note_arg, in_place):
    """(src, dst) for the note, refusing to clobber the draft by accident.

    `--note note_draft` (no .md) used to make `.replace(".md", ...)` a no-op, so
    the rendered output overwrote the source draft even without --in-place.
    """
    src = os.path.join(lec, note_arg)
    if not os.path.isfile(src):
        print(f"ERROR: {src} not found", file=sys.stderr)
        sys.exit(2)
    if in_place:
        return src, src
    base, ext = os.path.splitext(src)
    dst = base + ".rendered" + (ext or ".md")
    if os.path.abspath(dst) == os.path.abspath(src):
        print(f"ERROR: output path would overwrite the source note ({src}). "
              "Pass --in-place if that is what you want.", file=sys.stderr)
        sys.exit(2)
    return src, dst


def main():
    ap = argparse.ArgumentParser(
        description="Expand [[EMBED sN]] placeholders -> Obsidian callouts + audit",
        epilog="Note: [!figure] is not a built-in Obsidian callout type; add a CSS "
               "snippet for it or the callouts render with the default note style.")
    ap.add_argument("lecture_dir")
    ap.add_argument("--note", default="note_draft.md")
    ap.add_argument("--slug", default=None, help="attachment slug (default: dir name)")
    ap.add_argument("--attach-root", default=None,
                    help="attachment path pattern, '{slug}' substituted "
                         f"(default: {DEFAULT_ATTACH_ROOT!r}, or "
                         "paths.vault_attach_pattern from config.yaml)")
    ap.add_argument("--attach-dir", default=None,
                    help="real directory holding the attachments; when given, each "
                         "embedded filename is checked to exist there")
    ap.add_argument("--in-place", action="store_true", help="overwrite the note (else writes .rendered.md)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    lec = os.path.abspath(args.lecture_dir)
    slug = args.slug or os.path.basename(lec)
    attach_root = resolve_attach_root(args.attach_root, lec).replace("{slug}", slug)
    by_id = load_final(lec)
    note_path, dst = resolve_note_paths(lec, args.note, args.in_place)

    with open(note_path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    out, referenced, removed, inline = [], set(), [], 0
    missing_names = []
    in_fence = False

    for line in lines:
        # A ``` / ~~~ fence toggles code context. Placeholders inside a code
        # block are being shown as examples, not requested as embeds.
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue

        m_all = list(PLACEHOLDER.finditer(line))
        if not m_all:
            out.append(line)
            continue
        only = PLACEHOLDER.fullmatch(line.strip())
        if only:  # placeholder on its own line -> clean col-0 callout
            sid, intent = int(only.group(1)), (only.group(2) or "").strip()
            s = by_id.get(sid)
            if s is None:
                removed.append((sid, "unknown_slide")); continue
            if tier_of(s) > 2 or s.get("embed_suppressed_reason"):
                removed.append((sid, f"tier{tier_of(s)}/suppressed")); continue
            if out and out[-1].strip() != "":
                out.append("")
            out.append(callout(attach_root, s, intent, missing_names))
            out.append("")
            referenced.add(sid)
        else:  # inline placeholder -> ref marker in place, callout after the line
            queued = []
            def repl(m):
                sid = int(m.group(1)); intent = (m.group(2) or "").strip()
                s = by_id.get(sid)
                if s is None or tier_of(s) > 2 or s.get("embed_suppressed_reason"):
                    removed.append((sid, "inline_bad_tier")); return ""
                referenced.add(sid); queued.append((s, intent))
                return f"(見 s{sid} 圖 ↓)"
            new_line = PLACEHOLDER.sub(repl, line)
            out.append(new_line)
            for s, intent in queued:
                inline += 1
                out.append("")
                out.append(callout(attach_root, s, intent, missing_names))
                out.append("")

    # Audit
    embeddable = {sid for sid, s in by_id.items()
                  if tier_of(s) <= 2 and not s.get("embed_suppressed_reason")}
    missing = sorted(embeddable - referenced)
    print(f"Rendered: {len(referenced)} embeds | removed {len(removed)} bad refs | {inline} inline-fixed")
    print(f"  attachment root: {attach_root}")
    if removed:
        print("  REMOVED (T3/suppressed/unknown referenced):")
        for sid, why in removed[:15]:
            print(f"    - s{sid}: {why}")
    if missing_names:
        print(f"  [WARN] {len(missing_names)} slide(s) had neither attachment_name "
              f"nor filename; names were fabricated: "
              f"{', '.join('s'+str(x) for x in missing_names[:10])}")
    if args.attach_dir:
        absent = []
        for sid in sorted(referenced):
            name = attachment_name(by_id[sid], [])
            if not os.path.isfile(os.path.join(args.attach_dir, name)):
                absent.append(f"s{sid}:{name}")
        if absent:
            print(f"  [WARN] {len(absent)} embedded file(s) not present in "
                  f"{args.attach_dir}: {', '.join(absent[:10])}")
        else:
            print(f"  OK: all {len(referenced)} embedded files exist in {args.attach_dir}")
    if missing:
        print(f"  [WARN] {len(missing)} Tier-1/2 slides never referenced (no figure placed): "
              f"{', '.join('s'+str(x) for x in missing[:20])}")
    else:
        print("  OK: all Tier-1/2 slides referenced at least once")

    rendered = "\n".join(out) + "\n"
    if args.dry_run:
        print("[dry-run] not writing"); return
    with open(dst, "w", encoding="utf-8") as f:
        f.write(rendered)
    print(f"-> {dst}")


if __name__ == "__main__":
    main()
