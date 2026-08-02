# -*- coding: utf-8 -*-
"""detect_language.py <course_work_dir>  [--json]

Phase-0 language + collapse detector. 0 Claude tokens.

Reads every clips/*/transcript.txt (+ transcript.json for collapse signals),
counts CJK vs Latin letters, and emits a language verdict used to pick the
whisper --initial-prompt glossary and the L2/L3 templates.

Verdict:
  zh        cjk_ratio >= 0.60
  en        latin_ratio >= 0.85 (and cjk_ratio < 0.10)
  bilingual otherwise

collapse_suspect: whisper looping/hallucination — many consecutive segments
that are ~1 token long or ~<=1.2s duration (a known failure on silence/music).

Output: prints a one-line summary; with --json prints the full dict, and always
writes <course_work_dir>/_lang.json.
"""
import json
import re
import sys
from pathlib import Path

CJK = re.compile(r"[一-鿿㐀-䶿]")
LATIN = re.compile(r"[A-Za-z]")


def char_ratios(text):
    cjk = len(CJK.findall(text))
    lat = len(LATIN.findall(text))
    total = cjk + lat
    if total == 0:
        return 0.0, 0.0, 0
    return cjk / total, lat / total, total


def collapse_signals(segs):
    """Return (collapse_suspect, n_short, n_segs). Heuristic over segments."""
    if not segs:
        return False, 0, 0
    short = 0
    run = 0
    max_run = 0
    for s in segs:
        txt = (s.get("text") or "").strip()
        dur = float(s.get("end", 0)) - float(s.get("start", 0))
        ntok = len(txt.split())
        is_short = (ntok <= 1) or (0 < dur <= 1.2 and ntok <= 2)
        if is_short:
            short += 1
            run += 1
            max_run = max(max_run, run)
        else:
            run = 0
    n = len(segs)
    # collapse if a long unbroken run of trivial segments, or a high overall share
    suspect = (max_run >= 12) or (n >= 20 and short / n >= 0.5)
    return suspect, short, n


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    want_json = "--json" in sys.argv[1:]
    wd = Path(args[0])

    texts = []
    n_segs = 0
    collapse = False
    short_total = 0
    per_clip = []
    for tp in sorted(wd.glob("clips/*/transcript.txt")):
        t = tp.read_text(encoding="utf-8", errors="ignore")
        texts.append(t)
        cr, lr, tot = char_ratios(t)
        jp = tp.parent / "transcript.json"
        segs = []
        if jp.exists():
            try:
                segs = json.load(open(jp, encoding="utf-8"))
            except Exception:
                segs = []
        cs, ns, nseg = collapse_signals(segs)
        collapse = collapse or cs
        short_total += ns
        n_segs += nseg
        per_clip.append({"clip": tp.parent.name, "cjk_ratio": round(cr, 3),
                         "latin_ratio": round(lr, 3), "chars": tot,
                         "collapse_suspect": cs, "segs": nseg})

    full = "\n".join(texts)
    cr, lr, tot = char_ratios(full)
    if cr >= 0.60:
        lang = "zh"
    elif lr >= 0.85 and cr < 0.10:
        lang = "en"
    else:
        lang = "bilingual"

    # confidence = distance from the nearest decision boundary, clamped 0..1
    if lang == "zh":
        conf = min(1.0, (cr - 0.60) / 0.40 + 0.5)
    elif lang == "en":
        conf = min(1.0, (lr - 0.85) / 0.15 + 0.5)
    else:
        conf = 0.5
    conf = round(max(0.0, conf), 2)

    out = {
        "course": wd.name,
        "lang": lang,
        "confidence": conf,
        "cjk_ratio": round(cr, 3),
        "latin_ratio": round(lr, 3),
        "total_letters": tot,
        "collapse_suspect": collapse,
        "short_segments": short_total,
        "total_segments": n_segs,
        "per_clip": per_clip,
    }
    (wd / "_lang.json").write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    if want_json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        flag = " ⚠️COLLAPSE" if collapse else ""
        print(f"{wd.name}: lang={lang} conf={conf} cjk={cr:.2f} lat={lr:.2f} "
              f"segs={n_segs}{flag}")


if __name__ == "__main__":
    main()
