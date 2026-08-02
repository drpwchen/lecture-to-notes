# -*- coding: utf-8 -*-
r"""Locate each clip inside its source recording by cross-correlating transcripts.

    python xcorr_media_offsets.py OUT.json \
        --clip-asr  D:/work/asr            \  # one subdir per clip, each with transcript.json
        --recording NAME=C:/…/rec_dir      \  # repeatable; dir holds transcript.json
        [--index media_index.json] [--n 6] [--bin 5] [--min-votes 40]

Why this exists: wall-clock anchors derived from what the speaker SAID ("we'll
break for 10 minutes") are unreliable — on the IMPS 2026-07 workshop one
recording's assumed start was 44 min off, which silently mis-assigned every clip
to the wrong course segment. Transcript-to-transcript cross-correlation replaces
the guess with a measurement: shared N-grams vote on (recording_time − clip_time)
and the mode of that histogram is the clip's true internal offset.

Output per clip: {rec, offset_s, votes, low_confidence}. Output per recording:
the measured start clock = median(clip capture time − clip offset), plus the
spread across clips, which IS the accuracy check — a few seconds means it is
right, minutes mean the clips do not belong to that recording (or the timestamps
are not capture starts).

==DST caveat==: capture times are read and compared as NAIVE local datetimes. A
shoot that straddles a daylight-saving transition (or clips shot in two zones)
will be off by the offset change for the clips on the far side. Taiwan has no
DST, so this has never bitten in practice; if it matters for your footage,
compare the measured start against a known wall clock before trusting it.
"""
import argparse, collections, datetime, json, os, statistics, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import atomic_write_json, load_segments, match_stem


def grams(segs, n):
    g = collections.defaultdict(list)
    for s in segs:
        tx = "".join(ch for ch in (s.get("text") or "") if not ch.isspace())
        t = float(s.get("start", 0))
        for i in range(len(tx) - n + 1):
            g[tx[i:i + n]].append(t)
    return g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--clip-asr", required=True)
    ap.add_argument("--recording", action="append", required=True, help="NAME=DIR")
    ap.add_argument("--index", help="media_capture_index.py output (enables measured start clocks)")
    ap.add_argument("--name-map", help='JSON {"asr_dir_name": "media_file"} when the clip ASR '
                                       "dirs were renamed away from the media file stems")
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--bin", type=int, default=5, help="offset histogram bin, seconds")
    ap.add_argument("--min-votes", type=int, default=40)
    ap.add_argument("--max-gram-freq", type=int, default=4,
                    help="skip N-grams appearing more often than this in the recording")
    a = ap.parse_args()

    far = {}
    for spec in a.recording:
        name, _, d = spec.partition("=")
        tpath = os.path.join(d, "transcript.json")
        if not os.path.isfile(tpath):
            sys.exit(f"ERROR: recording '{name}' has no transcript at {tpath}\n"
                     "  --recording wants NAME=DIR where DIR holds the "
                     "transcript.json of the full room recording. Transcribe it "
                     "first (transcribe_video.py); the cross-correlation is "
                     "transcript-to-transcript.")
        far[name] = grams(load_segments(tpath), a.n)
        print("recording %-28s %d grams" % (name, len(far[name])), flush=True)

    clips = {}
    for sub in sorted(os.listdir(a.clip_asr)):
        p = os.path.join(a.clip_asr, sub, "transcript.json")
        if os.path.exists(p):
            clips[sub] = grams(load_segments(p), a.n)

    res = {}
    for name, cg in clips.items():
        best = None
        for rec, fg in far.items():
            votes = collections.Counter()
            for gram, ts in cg.items():
                fts = fg.get(gram)
                if not fts or len(fts) > a.max_gram_freq:
                    continue
                for t in ts:
                    for ft in fts:
                        votes[round((ft - t) / a.bin) * a.bin] += 1
            if not votes:
                continue
            smooth = {k: v + votes.get(k - a.bin, 0) + votes.get(k + a.bin, 0)
                      for k, v in votes.items()}
            off, score = max(smooth.items(), key=lambda kv: kv[1])
            if best is None or score > best[2]:
                best = (rec, off, score)
        # low_confidence travels WITH the result: the ⚠️ below only ever reached
        # the console, so a downstream reader of the JSON could not tell a
        # measured offset from a guess — which is the 44-minute misalignment this
        # script exists to catch.
        res[name] = ({"rec": best[0], "offset_s": best[1], "votes": best[2],
                      "low_confidence": best[2] < a.min_votes}
                     if best else None)
        print("%-58s -> %-24s offset %6ss votes %5d%s" % (
            name[:58], best[0] if best else "—", best[1] if best else "—",
            best[2] if best else 0,
            "" if best and best[2] >= a.min_votes else "   ⚠️ low-confidence"), flush=True)

    starts = {}
    if a.index:
        with open(a.index, encoding="utf8") as fh:
            rows = json.load(fh)
        idx = {os.path.splitext(r["file"])[0]: r for r in rows}
        # asr dir name -> media file: index row may carry it, else --name-map, else stem match
        nmap = {}
        if a.name_map:
            with open(a.name_map, encoding="utf8") as fh:
                nmap = json.load(fh)
        for r in rows:
            if r.get("asr_name"):
                nmap.setdefault(r["asr_name"], r["file"])
        by_rec = collections.defaultdict(list)
        unmatched = []
        for name, x in res.items():
            if not x or x["votes"] < a.min_votes:
                continue
            row = None
            tgt = nmap.get(name)
            if tgt:
                row = idx.get(os.path.splitext(tgt)[0])
            if row is None:
                row = match_stem(name, idx)
            if not row or not row.get("start"):
                unmatched.append(name)
                continue
            cs = datetime.datetime.fromisoformat(row["start"])
            by_rec[x["rec"]].append(cs - datetime.timedelta(seconds=x["offset_s"]))
        print()
        if unmatched:
            print("⚠️ %d 支 clip 對不到 index 的拍攝時間，量不出錄音起點："
                  % len(unmatched), unmatched[:6], "…" if len(unmatched) > 6 else "")
            print("   修法＝讓 --clip-asr 的子目錄名等於媒體檔 stem，或給 --name-map "
                  "{\"asr目錄名\": \"媒體檔名\"}，或在 index 每列加 asr_name 欄")
        for rec, cand in by_rec.items():
            mid = datetime.datetime.fromtimestamp(statistics.median(c.timestamp() for c in cand))
            spread = (max(cand) - min(cand)).total_seconds()
            starts[rec] = {"measured_start": mid.isoformat(sep=" "), "n_clips": len(cand),
                           "spread_s": round(spread, 1)}
            flag = "OK" if spread <= 30 else "⚠️ 離散過大——clip 可能不屬於此錄音，或時間戳不是「開始」時間"
            print("%-28s measured start %s  (n=%d, spread %.0fs)  %s"
                  % (rec, mid.strftime("%Y-%m-%d %H:%M:%S"), len(cand), spread, flag))

    atomic_write_json(a.out, {"clips": res, "recordings": starts,
                              "params": {"n": a.n, "bin": a.bin,
                                         "min_votes": a.min_votes}}, indent=1)
    print("\n-> %s" % a.out)
    print("下一步：用 measured_start + start_s 重算每段的時鐘，==別用口述推估==；"
          "再抽一支影片對內容驗收（標題與畫面要吻合）。")


if __name__ == "__main__":
    main()
