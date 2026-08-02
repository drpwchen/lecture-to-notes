# -*- coding: utf-8 -*-
"""split_segments.py <clip_dir>  [--max-dur 8] [--soft-dur 4] [--backup]

Re-segment a word-timestamped transcript.json into sentence-level units so that
L1's time-interleave aligns text to slides tightly. 0 Claude tokens.

Conference Mandarin yields 20-30s whisper segments that span several slide
changes; interleaving such a block by its START time detaches narration from the
slide it describes. Using per-word timings (transcribe_video.py --word-timestamps)
we cut each segment at sentence punctuation, at a hard max duration, or at a comma
once the buffer is already long.

Input : <clip_dir>/transcript.json  (must contain per-segment "words")
Output: <clip_dir>/transcript.json  (sentence-level; original kept as
        transcript_unsplit.json when --backup)
        <clip_dir>/transcript.txt   (regenerated)

Segments lacking "words" are passed through unchanged (graceful fallback).
"""
import json
import re
import sys
from pathlib import Path

HARD = set("。！？!?…")          # sentence enders
SOFT = set("，,、；;:：")          # clause breaks


def flush(words):
    if not words:
        return None
    text = "".join(w["word"] for w in words).strip()
    if not text:
        return None
    return {"start": round(words[0]["start"], 2),
            "end": round(words[-1]["end"], 2),
            "text": text}


def split_no_words(seg, max_dur):
    """Segment whose word alignment failed (words missing/empty). Split text by
    sentence punctuation, else by char chunks, and interpolate timestamps across
    the segment duration. Approximate but far better than a 200s+ block."""
    text = (seg.get("text") or "").strip()
    start, end = seg["start"], seg["end"]
    dur = end - start
    if dur <= max_dur or not text:
        return [{"start": round(start, 2), "end": round(end, 2), "text": text}] if text else [seg]
    parts = [p for p in re.split(r"(?<=[。！？!?…])", text) if p.strip()]
    # enforce the duration cap on every part: a sentence (or punctuation-less run)
    # that still maps to > max_dur of audio gets char-chunked further, so no
    # output block spans more time than one slide window.
    sec_per_char = dur / max(1, len(text))
    capped = []
    for p in parts:
        if len(p) * sec_per_char <= max_dur:
            capped.append(p)
        else:
            clen = max(1, int(max_dur / sec_per_char))
            capped.extend(p[i:i + clen] for i in range(0, len(p), clen))
    parts = capped or [text]
    out, tot, acc = [], sum(len(p) for p in parts) or 1, 0
    for p in parts:
        s = start + dur * acc / tot
        acc += len(p)
        e = start + dur * acc / tot
        out.append({"start": round(s, 2), "end": round(e, 2), "text": p.strip()})
    return out


def split_segment(seg, max_dur, soft_dur, gap_break=3.0):
    words = seg.get("words")
    if not words:
        return split_no_words(seg, max_dur)  # alignment failed → interpolate
    out = []
    buf = []

    def emit():
        r = flush(buf)
        if r:
            out.append(r)

    for w in words:
        if buf:
            # Break BEFORE adding a word that would push the piece past max_dur, or
            # across a long silence gap. Sparse/long-pause segments (few words over
            # minutes) otherwise collapse into one multi-minute block.
            gap = w["start"] - buf[-1]["end"]
            if gap > gap_break or (w["end"] - buf[0]["start"]) > max_dur:
                emit()
                buf = []
        buf.append(w)
        tail = (w["word"] or "").strip()
        last_char = tail[-1] if tail else ""
        cur = w["end"] - buf[0]["start"]
        if last_char in HARD or (last_char in SOFT and cur >= soft_dur):
            emit()
            buf = []
    emit()
    return out or [{"start": seg["start"], "end": seg["end"],
                    "text": (seg.get("text") or "").strip()}]


def main():
    argv = sys.argv[1:]
    max_dur = float(argv[argv.index("--max-dur") + 1]) if "--max-dur" in argv else 8.0
    soft_dur = float(argv[argv.index("--soft-dur") + 1]) if "--soft-dur" in argv else 4.0
    backup = "--backup" in argv
    clip = Path([a for a in argv if not a.startswith("--")
                 and a not in (str(max_dur), str(soft_dur))][0])

    tp = clip / "transcript.json"
    segs = json.load(open(tp, encoding="utf-8"))

    new = []
    n_with_words = 0
    for s in segs:
        if s.get("words"):
            n_with_words += 1
        new.extend(split_segment(s, max_dur, soft_dur))

    if backup:
        (clip / "transcript_unsplit.json").write_text(
            json.dumps(segs, ensure_ascii=False, indent=2), encoding="utf-8")

    # drop the bulky word arrays from the final sentence-level file
    for s in new:
        s.pop("words", None)
    tp.write_text(json.dumps(new, ensure_ascii=False, indent=2), encoding="utf-8")

    txt = clip / "transcript.txt"
    with open(txt, "w", encoding="utf-8") as f:
        for r in new:
            m, sec = int(r["start"] // 60), int(r["start"] % 60)
            f.write(f"[{m:02d}:{sec:02d}] {r['text']}\n")

    import statistics
    before = [s["end"] - s["start"] for s in segs]
    after = [s["end"] - s["start"] for s in new]
    print(f"split: {len(segs)} -> {len(new)} segments "
          f"({n_with_words}/{len(segs)} had words)")
    if before and after:
        print(f"  mean dur {statistics.mean(before):.1f}s -> {statistics.mean(after):.1f}s | "
              f"max {max(before):.1f}s -> {max(after):.1f}s | "
              f">15s {sum(d>15 for d in before)} -> {sum(d>15 for d in after)}")
    else:
        print("  (empty transcript — nothing to split)")


if __name__ == "__main__":
    main()
