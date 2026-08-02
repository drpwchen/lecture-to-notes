"""Build a per-lecture glossary.txt for whisper --initial-prompt-file.

A short (<=~900 char) domain prompt biases Whisper decoding toward the
lecture's jargon — the single cheapest win against accented/dense ASR
(see SKILL.md Step 2 accented-English notes). Pulls the lecture's own
vocabulary from pdf_text.json; prepend a hand-written --seed for speaker
name + key abbreviations spelled out.

Usage:
    python make_glossary.py <lecture_dir> \
        [--seed "<speaker name>. 重要詞彙: ICU-AW, sarcopenia, HMB, ..."] \
        [--max-chars 900] [--pages 10]
"""
import argparse, json, os, sys


def _page_texts(pt, pages, path):
    """Yield page text from either pdf_text.json shape.

    Writers disagree: a bare ``[{page, text}, ...]`` list, or ``{"pages": [...]}``
    (some also key pages by number). Iterating a dict gave its KEYS, so
    ``p.get`` raised AttributeError on a str.
    """
    if isinstance(pt, dict):
        pt = pt.get("pages") or list(pt.values())
    if not isinstance(pt, list):
        print(f"WARN: {path} is a {type(pt).__name__}, expected a list of pages "
              "— ignoring it", file=sys.stderr)
        return
    for p in pt[:pages]:
        if isinstance(p, dict):
            t = p.get("text") or ""
        elif isinstance(p, str):
            t = p
        else:
            continue
        if t:
            yield t


def _truncate_on_whitespace(text, max_chars):
    """Cut at the last whitespace at or before max_chars.

    A hard slice ends mid-token ("sarcopen"), and a fragment like that in
    whisper's initial_prompt biases decoding toward a non-word — the opposite of
    what the glossary is for. CJK has no spaces, so a text with no whitespace in
    the tail falls back to the hard cut.
    """
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    cut = max(head.rfind(" "), head.rfind("\n"), head.rfind("\t"))
    # Only honor the boundary if it doesn't throw away most of the budget.
    if cut >= int(max_chars * 0.8):
        return head[:cut].rstrip()
    return head


def main():
    ap = argparse.ArgumentParser(description="Build whisper glossary.txt from pdf_text + seed")
    ap.add_argument("lecture_dir")
    ap.add_argument("--seed", default="", help="Hand-written seed: speaker + spelled-out abbreviations")
    ap.add_argument("--max-chars", type=int, default=900, help="Whisper initial_prompt should stay small")
    ap.add_argument("--pages", type=int, default=10, help="How many leading pdf_text pages to mine")
    args = ap.parse_args()

    d = args.lecture_dir
    extra = ""
    pt_file = os.path.join(d, "pdf_text.json")
    if os.path.isfile(pt_file):
        with open(pt_file, encoding="utf-8") as f:
            pt = json.load(f)
        extra = "\n".join(_page_texts(pt, args.pages, pt_file))

    text = (args.seed + ("\n" + extra if extra else "")).strip()
    if not text:
        print(f"WARN {os.path.basename(d)}: no seed and no pdf_text — empty glossary", file=sys.stderr)
    text = _truncate_on_whitespace(text, args.max_chars)
    out = os.path.join(d, "glossary.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"OK {os.path.basename(d)}: glossary.txt {len(text)} chars -> {out}")


if __name__ == "__main__":
    main()
