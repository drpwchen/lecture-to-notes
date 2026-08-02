# -*- coding: utf-8 -*-
"""Read the near-field (clip) transcripts by wall clock or keyword.

    python query_near_field.py --asr D:/work/asr --index media_index.json \
        [--map namemap.json] 2026-07-26 10:30 10:36
    python query_near_field.py --asr … --index … --grep 骨盆底 [--ctx 3]

This is the tool for the "mine the clip audio back into the notes" step: the
clip mic sat next to the demo, so it hears the individual coaching that the
room mic lost. ==Cite what you take from it as「影片檔名 ＋ 時鐘」==, kept
distinct from citations of the room recording (which use internal seconds).
"""
import argparse, datetime, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import load_segments, match_stem


def clip_rows(asr_dir, index, name_map):
    with open(index, encoding="utf8") as fh:
        rows = json.load(fh)
    by_stem = {os.path.splitext(r["file"])[0]: r for r in rows}
    nmap = {}
    if name_map:
        with open(name_map, encoding="utf8") as fh:
            nmap = json.load(fh)
    for r in rows:
        if r.get("asr_name"):
            nmap.setdefault(r["asr_name"], r["file"])
    out = []
    for sub in sorted(os.listdir(asr_dir)):
        p = os.path.join(asr_dir, sub, "transcript.json")
        if not os.path.exists(p):
            continue
        tgt = nmap.get(sub)
        row = by_stem.get(os.path.splitext(tgt)[0]) if tgt else None
        if row is None:
            row = match_stem(sub, by_stem)
        if not row or not row.get("start"):
            print("⚠️ 無拍攝時間，略過：%s" % sub)
            continue
        segs = load_segments(p)
        cs = datetime.datetime.fromisoformat(row["start"])
        lines = [(cs + datetime.timedelta(seconds=float(s["start"])), (s.get("text") or "").strip())
                 for s in segs if (s.get("text") or "").strip()]
        out.append((sub, row.get("source", ""), lines))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("day", nargs="?")
    ap.add_argument("start", nargs="?")
    ap.add_argument("end", nargs="?")
    ap.add_argument("--asr", required=True)
    ap.add_argument("--index", required=True)
    ap.add_argument("--map")
    ap.add_argument("--grep")
    ap.add_argument("--ctx", type=int, default=2)
    a = ap.parse_args()

    # Two mutually exclusive query modes. Without this check, omitting --grep and
    # the positionals reached strptime(None) as a TypeError deep in a helper.
    if a.grep:
        if a.day or a.start or a.end:
            ap.error("--grep searches every clip; do not also pass day/start/end")
    elif not (a.day and a.start and a.end):
        ap.error("give either --grep <keyword> or all three of DAY START END "
                 "(e.g. 2026-07-26 10:30 10:36)")

    clips = clip_rows(a.asr, a.index, a.map)

    if a.grep:
        for name, src, lines in clips:
            for i, (t, tx) in enumerate(lines):
                if a.grep in tx:
                    print("=== %s（%s）%s" % (name, src, t.strftime("%m-%d %H:%M:%S")))
                    for tt, xx in lines[max(0, i - a.ctx): i + a.ctx + 1]:
                        print("   %s  %s" % (tt.strftime("%H:%M:%S"), xx))
        return

    def p(s):
        fmt = "%Y-%m-%d %H:%M:%S" if s.count(":") == 2 else "%Y-%m-%d %H:%M"
        return datetime.datetime.strptime(a.day + " " + s, fmt)

    d0, d1 = p(a.start), p(a.end)
    for name, src, lines in clips:
        win = [(t, x) for t, x in lines if d0 <= t <= d1]
        if not win:
            continue
        print("========== %s（%s）%d 行" % (name, src, len(win)))
        for t, x in win:
            print("%s  %s" % (t.strftime("%H:%M:%S"), x))
        print()


if __name__ == "__main__":
    main()
