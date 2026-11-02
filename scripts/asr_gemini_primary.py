"""Gemini as the primary transcript, breeze25 as the clock and the cross-check (2026-10-25).

Why: on 15 course clips in 3 categories Gemini hit 97-100 % of the reference terms vs 64-85 % for the
best local model (per category). The local transcript keeps two jobs:
timestamps, and a second source to catch Gemini's content errors (name/term swaps, numbers that
conflict with the slide — found by note writers on a 15-recording run). turbo clocks as well at ~6x the speed
but its words are too poor to check against (31 % term coverage on hands-on audio), so the default
anchor is breeze25 at its benchmark settings; `--anchor-model turbo` = clock only, fast.

Flow, per media file:
  1. local anchor  transcribe_video --model breeze25 -> DIR/transcript_anchor.{txt,json}
  2. Gemini        gemini_asr.py --anchor transcript_anchor.txt --fill-blocked breeze25
                   (chunks Gemini's filter refuses are transcribed by breeze25 for that range only)
  3. promote       transcript_gemini.json -> DIR/transcript.json + transcript.txt (each line keeps
                   `source`: gemini | breeze25), so split_segments / build_L1 / audit_note need no change
  4. fallback      Gemini failed outright (quota, network, every route down) -> plain breeze25
                   transcribe_video into DIR, exactly the pre-2026-10-25 path
DIR/transcript_source.json records which path produced transcript.json.

HARD RULE 10: audio leaves the machine. Run this only where the user has said yes to sending recordings
out (a project may record a standing yes in its AGENTS.md); everywhere else use transcribe_video.py directly.
The Gemini route (API or Antigravity CLI) is gemini_asr.py's --via default; see its docstring.

Usage:
  python asr_gemini_primary.py <media> -o DIR --lang zh|en|bilingual [--start ISO] [--extra ARG ...]
  --extra is passed to the breeze25 fallback transcribe_video call (e.g. --word-timestamps).
Exit 0 = DIR/transcript.json written (by either path); 1 = nothing usable.
"""
import argparse, datetime as dt, json, os, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _common import write_transcript_lines  # noqa: E402

TV = os.path.join(HERE, "transcribe_video.py")
GA = os.path.join(HERE, "gemini_asr.py")


def run(cmd):
    print("$", " ".join(cmd), flush=True)
    return subprocess.run(cmd).returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("media"); ap.add_argument("-o", "--output-dir", required=True)
    ap.add_argument("--lang", required=True, choices=["zh", "en", "bilingual"])
    ap.add_argument("--start", help="ISO wall-clock start (only changes transcript_gemini.txt stamps)")
    ap.add_argument("--anchor-model", default="breeze25")
    ap.add_argument("--fill-model", default="breeze25")
    ap.add_argument("--via", choices=["agy", "api"], help="passed to gemini_asr.py (default: its own --via default)")
    ap.add_argument("--extra", nargs=argparse.REMAINDER, default=[], help="extra args for the breeze25 fallback")
    a = ap.parse_args()
    d = a.output_dir
    os.makedirs(d, exist_ok=True)
    anc_txt = os.path.join(d, "transcript_anchor.txt")

    # 1. anchor (kept apart from transcript.* so transcribe_video's "already done" check never sees it)
    if not os.path.exists(anc_txt):
        tmp = os.path.join(d, "_anchor")
        os.makedirs(tmp, exist_ok=True)
        rc = run([sys.executable, TV, a.media, "-o", tmp, "--lang", a.lang, "--model", a.anchor_model]
                 + (["--beam-size", "1"] if a.anchor_model == "turbo" else []))   # turbo = clock only
        if os.path.exists(os.path.join(tmp, "transcript.txt")):   # rc -1 = CTranslate2 teardown crash, output fine
            for ext in ("txt", "json"):
                if os.path.exists(os.path.join(tmp, f"transcript.{ext}")):
                    os.replace(os.path.join(tmp, f"transcript.{ext}"), os.path.join(d, f"transcript_anchor.{ext}"))
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"anchor {a.anchor_model} failed (rc={rc}); Gemini will run with raw timestamps", flush=True)

    # 2. Gemini
    cmd = [sys.executable, GA, a.media, "--output-dir", d, "--lang", "en" if a.lang == "en" else "zh",
           "--anchor", anc_txt if os.path.exists(anc_txt) else "none",
           "--fill-blocked", a.fill_model, "--fill-lang", a.lang] + (["--via", a.via] if a.via else [])
    if a.start:
        cmd += ["--start", a.start]
    grc = run(cmd)
    gj = os.path.join(d, "transcript_gemini.json")
    segs = json.load(open(gj, encoding="utf-8")) if grc == 0 and os.path.exists(gj) else []
    src = {"at": dt.datetime.now().isoformat(timespec="seconds"), "anchor": a.anchor_model if os.path.exists(anc_txt) else None}

    if segs:   # 3. promote
        with open(os.path.join(d, "transcript.json"), "w", encoding="utf-8") as f:
            json.dump(segs, f, ensure_ascii=False, indent=1)
        write_transcript_lines(segs, os.path.join(d, "transcript.txt"))
        n_fill = sum(s.get("source") != "gemini" for s in segs)
        src.update(primary="gemini", lines=len(segs), filled_lines=n_fill, fill_model=a.fill_model if n_fill else None)
    else:      # 4. fallback
        print(f"Gemini gave no transcript (rc={grc}) -> local {a.fill_model} for the whole file", flush=True)
        tj = os.path.join(d, "transcript.json")
        if os.path.exists(tj):   # an older run's output must not pass for this run's fallback result
            stale = tj + ".stale-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            os.replace(tj, stale)
            print(f"existing transcript.json predates this run -> kept as {os.path.basename(stale)}", flush=True)
        rc = run([sys.executable, TV, a.media, "-o", d, "--lang", a.lang, "--model", a.fill_model] + a.extra)
        if not os.path.exists(tj):
            print(f"local {a.fill_model} failed too (rc={rc})", flush=True)
            return 1
        src.update(primary=a.fill_model, reason=f"gemini_asr rc={grc}")
    with open(os.path.join(d, "transcript_source.json"), "w", encoding="utf-8") as f:
        json.dump(src, f, ensure_ascii=False, indent=1)
    print("transcript_source:", json.dumps(src, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
