"""Flag suspect ASR tokens in a lecture transcript. FLAGS ONLY — never rewrites.

Reads transcript.json, writes asr_suspects.txt: every token that is neither a
real term nor a term on the lecture's own slides, with the closest slide terms
as hints. The transcript itself is left byte-identical. Zero GPU, zero Claude
tokens, runs in seconds.

WHY FLAG-ONLY (measured 2026-07-26 on 8 lectures — read before "improving" this)
    Two automatic correction passes were built and both failed the same way:

    1. ollama gemma free-form proofreading (the original Step 2b, retired):
         pneumomagination -> pneumomeningocele   (context said hypomyelination)
         adjustation      -> adjustment          (context said gestation)
       It was fed pdf_text.json as its "slide glossary", but image-based decks
       yield almost nothing from fitz (46 pages -> 842 chars; 118 pages -> 117
       chars), so it was correcting from parametric knowledge, ungrounded.

    2. glossary-grounded mechanical replacement (also rejected): every
       replacement was forced to be a term appearing literally in the slide OCR.
       Still produced ~20 wrong edits in 78 on the same 8 lectures:
         blader        -> header       (bladder absent from slides, header present)
         clotinic      -> clinic       (should be clonic, as in tonic-clonic)
         revalence     -> evidence     (should be prevalence)
         transsection  -> resection    (should be transection)
         percentileDQ73 -> Percentiles (destroyed the DQ 73 datum)

    The root cause defeats both: ==the correct term is frequently absent from the
    slides==, so the nearest grounded candidate is merely a look-alike. Being
    grounded does not make a substitution right.

    And the cost of a wrong edit is asymmetric. A raw garble ('sacropenia',
    'pneumomagination') makes Claude stop and verify at synthesis. A confident
    wrong term ('header', 'clinic') does not. Laundering a detectable error into
    an undetectable one is strictly worse than leaving it alone.

    So: flag, and let synthesis decide with the full transcript, the slides, and
    domain knowledge in context. Do not add an auto-apply flag to this script.

Usage:
    python flag_asr_suspects.py --dir <lecture-dir> [--min-len 4] [--top 3]
                                [--wordlist PATH] [--acronyms PATH]

Inputs (in --dir):
    transcript.json                             required
    slides_grounded.json / slides_vlm.json /
    slides_final.json / pdf_text.json /
    glossary.txt                                optional; any found are merged
    data/real_words.txt required (or --wordlist), data/real_acronyms.txt optional
                                                (or --acronyms)

Output (in --dir):
    asr_suspects.txt
"""
import argparse
import json
import os
import re
import sys

from _common import load_segments

ASCII_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9'\-]*")
ACRONYM = re.compile(r"^[A-Z0-9]{2,6}$")
STRIP_PUNCT = re.compile(r"^[^A-Za-z0-9]+|[^A-Za-z0-9]+$")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")

GLOSSARY_FILES = ["slides_grounded.json", "slides_vlm.json", "slides_final.json",
                  "pdf_text.json", "glossary.txt"]

STOPWORDS = set("""
about above after again against all also and any are been before being both but
can cannot come could did does doing done down during each even ever every few
for from further get give given going good got had has have having her here him
his how however into its itself just keep know large last later least less let
like little long look made make many may more most much must need never new
next not now often once one only other our out over own put quite rather really
right said same say see seem seen she should show shown since small some such
sure take tell than that the their them then there these they thing think this
those though three through thus time today together too took two under until
upon use used using very want was way well went were what when where whether
which while who whole why will with within without word work would year yes yet
you your
""".split())


def load_protected(wordlist=None, acronyms=None):
    """Load the protected vocabulary (known-real words + acronyms).

    Both files are plain whitespace-separated word lists, so any list of real
    words works — the shipped one is only a convenience.
    """
    wpath = wordlist or os.path.join(DATA_DIR, "real_words.txt")
    apath = acronyms or os.path.join(DATA_DIR, "real_acronyms.txt")
    if not os.path.exists(wpath):
        sys.exit(
            f"ERROR: word list not found: {wpath}\n"
            "  This file is the 'known-real words' vocabulary — a token missing "
            "from it is what makes it a suspect.\n"
            "  Fix it either way:\n"
            "    1. Regenerate one:  python scripts/build_real_words.py "
            "--corpus <dir-of-md-files>\n"
            "    2. Point at an existing list:  --wordlist <path> "
            "[--acronyms <path>]\n"
            "  Format: plain text, whitespace-separated words."
        )
    with open(wpath, encoding="utf-8") as fh:
        words = set(fh.read().split())
    acros = set()
    if os.path.exists(apath):
        with open(apath, encoding="utf-8") as fh:
            acros = set(fh.read().split())
    elif acronyms:
        print(f"WARNING: --acronyms {apath} not found; no acronyms protected",
              file=sys.stderr)
    return words, acros


def _walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)


def load_glossary(d):
    terms, stats = {}, []
    for fn in GLOSSARY_FILES:
        path = os.path.join(d, fn)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
        texts = []
        if fn.endswith(".json"):
            try:
                texts = list(_walk_strings(json.loads(raw)))
            except Exception:
                texts = [raw]
        else:
            texts = [raw]
        before = len(terms)
        for t in texts:
            for m in ASCII_TOKEN.finditer(t):
                w = STRIP_PUNCT.sub("", m.group(0))
                if len(w) >= 2:
                    terms.setdefault(w.lower(), w)
        stats.append(f"{fn}: +{len(terms) - before}")
    return terms, stats


def lev(a, b, cap):
    if a == b:
        return 0
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur, row_min = [i], i
        for j, cb in enumerate(b, 1):
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            cur.append(v)
            row_min = min(row_min, v)
        if row_min > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def hints(token, terms, top):
    """Closest slide terms, purely as reading aids for synthesis."""
    low = token.lower()
    cap = max(2, int(len(low) * 0.5))
    scored = []
    for key, surface in terms.items():
        if abs(len(key) - len(low)) > cap:
            continue
        d = lev(low, key, cap)
        if 0 < d <= cap:
            scored.append((d, d / max(len(low), len(key)), surface))
    scored.sort()
    return scored[:top]


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--min-len", type=int, default=4)
    ap.add_argument("--top", type=int, default=3, help="Hints per suspect")
    ap.add_argument("--wordlist", default=None,
                    help="Path to the known-real word list "
                         "(default: <skill>/data/real_words.txt)")
    ap.add_argument("--acronyms", default=None,
                    help="Path to the known-real acronym list "
                         "(default: <skill>/data/real_acronyms.txt)")
    args = ap.parse_args()

    tpath = os.path.join(args.dir, "transcript.json")
    segs = load_segments(tpath)
    real_words, real_acros = load_protected(args.wordlist, args.acronyms)
    terms, gstats = load_glossary(args.dir)

    # token -> (first segment index, occurrence count)
    suspects = {}
    for idx, seg in enumerate(segs):
        for m in ASCII_TOKEN.finditer(seg.get("text", "")):
            tok = STRIP_PUNCT.sub("", m.group(0))
            low = tok.lower()
            if not tok or low in STOPWORDS or low in terms:
                continue
            if ACRONYM.match(tok):
                if tok in real_acros:
                    continue
            else:
                if len(tok) < args.min_len or low in real_words:
                    continue
            first, n = suspects.get(low, (idx, 0))
            suspects[low] = (first, n + 1)

    lines = [
        "ASR suspect tokens — FLAGS ONLY, transcript untouched",
        f"Segments: {len(segs)}   Slide glossary terms: {len(terms)}",
        f"Protected vocab: {len(real_words)} words / {len(real_acros)} acronyms",
        "  glossary sources: " + ", ".join(gstats) if gstats else
        "  glossary sources: NONE (no slide OCR — hints unavailable)",
        "",
        "Each line: token (xN occurrences, first at segment) -> closest slide terms.",
        "Hints are look-alikes, NOT decisions — the correct term is often absent",
        "from the slides. Resolve each from transcript context at synthesis.",
        "",
    ]
    ordered = sorted(suspects.items(), key=lambda kv: (-kv[1][1], kv[1][0]))
    for low, (first, n) in ordered:
        hs = hints(low, terms, args.top) if terms else []
        hint_str = ", ".join(f"{s} (d={d})" for d, _r, s in hs) or "—"
        lines.append(f"  {low!r:28} x{n:<3} seg{first:<5} ~ {hint_str}")

    lines += ["", f"Total suspects: {len(ordered)}"]
    out = os.path.join(args.dir, "asr_suspects.txt")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n".join(lines[:8]))
    print(f"... {len(ordered)} suspects -> {out}")


if __name__ == "__main__":
    main()
