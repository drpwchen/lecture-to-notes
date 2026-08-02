"""Build the real-word / real-acronym lists that protect correct transcript
tokens from being 'corrected' into a slide term.

A token counts as real if it appears in >= MIN_BOOKS DISTINCT books of a local
reference corpus. Cross-book agreement is what makes this work: an OCR typo or
an ASR garble shows up in one book at most, a genuine term shows up in many.
Measured discrimination on the author's own corpus (2026-07-26):
    articulation  89 books   mitocondrial       0 books
    dysfunction  218 books   disfunction        1 book
    sarcopenia    25 books   sacropenia         1 book
    maturation    80 books   pneumomeningocele  0 books

A "corpus" is just a directory whose immediate subdirectories are books, each
holding .md files anywhere below it. Any markdown reference library works; there
is nothing medical about the builder itself.

    python build_real_words.py --corpus <dir> [--min-books 3] [--files-per-book 15]

You do NOT have to run this: data/real_words.txt as shipped/generated is usable
as-is, and flag_asr_suspects.py can be pointed at any word list with --wordlist.

Outputs (next to the skill, under data/):
    real_words.txt     lowercase words
    real_acronyms.txt  ALL-CAPS tokens (ICU, MRI, EEG, CBC, ...)
"""
import argparse
import collections
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import TEXTBOOK_MD

DEFAULT_CORPUS = str(TEXTBOOK_MD)
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
WORD = re.compile(r"[A-Za-z][a-z]{2,}")
ACRO = re.compile(r"\b[A-Z]{2,6}\b")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", default=None,
                    help="Directory whose subdirectories are books of .md files "
                         f"(default: {DEFAULT_CORPUS}, if it exists)")
    ap.add_argument("--min-books", type=int, default=3)
    ap.add_argument("--files-per-book", type=int, default=15)
    ap.add_argument("--max-bytes", type=int, default=300_000)
    args = ap.parse_args()

    corpus = args.corpus or DEFAULT_CORPUS
    if not os.path.isdir(corpus):
        sys.exit(
            f"ERROR: corpus directory not found: {corpus}\n"
            "  --corpus wants a directory whose immediate subdirectories are "
            "books, each containing .md files (at any depth):\n"
            "      <corpus>/<book-name>/**/*.md\n"
            "  Any markdown reference library works — nothing here is "
            "medicine-specific.\n"
            "  You probably don't need this at all: data/real_words.txt is "
            "usable as shipped, and flag_asr_suspects.py takes --wordlist "
            "<path> to use any other list."
        )
    args.corpus = corpus

    t0 = time.time()
    books = [d for d in sorted(os.listdir(args.corpus))
             if os.path.isdir(os.path.join(args.corpus, d))]
    wc, ac = collections.Counter(), collections.Counter()
    nfiles = 0

    for b in books:
        words, acros, taken = set(), set(), 0
        for dp, _dn, fn in os.walk(os.path.join(args.corpus, b)):
            for f in fn:
                if taken >= args.files_per_book:
                    break
                if not f.lower().endswith(".md"):
                    continue
                try:
                    with open(os.path.join(dp, f), encoding="utf-8",
                              errors="replace") as fh:
                        txt = fh.read(args.max_bytes)
                except OSError:
                    continue
                taken += 1
                nfiles += 1
                words.update(w.lower() for w in WORD.findall(txt))
                acros.update(ACRO.findall(txt))
            if taken >= args.files_per_book:
                break
        wc.update(words)
        ac.update(acros)

    os.makedirs(DATA, exist_ok=True)
    for name, counter in (("real_words.txt", wc), ("real_acronyms.txt", ac)):
        keep = sorted(k for k, c in counter.items() if c >= args.min_books)
        with open(os.path.join(DATA, name), "w", encoding="utf-8") as f:
            f.write("\n".join(keep) + "\n")
        print(f"{name}: {len(keep)} entries")
    print(f"books={len(books)} files={nfiles} elapsed={time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
