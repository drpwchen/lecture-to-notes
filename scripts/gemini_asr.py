"""Online transcription with Gemini (audio in, verbatim text out): the primary transcript for lectures, or a
second source next to a local ASR for far-field hall recordings.

Measured 2026-10-24 on 6 audience-seat clips from a PA hall: gemini-3.8-flash hit 64/78 reference
terms/numbers vs 42 for breeze25 and 51 for a 3-ASR LLM merge, with 0 leftover mishearings; two runs gave
identical scores. It is an LLM: keep the local transcript as well and treat any number that only Gemini has
as unconfirmed until a slide or a second source agrees.

Two routes to the same model family (same 5 hall clips: CLI 52/64, API 51/64); the default is env
LECTURE_GEMINI_VIA, else config.yaml `gemini.via`, else api:
  --via api — generativelanguage REST API with GEMINI_API_KEY (paid usage is billed to that key).
  --via agy — the Antigravity CLI (`agy`) in headless mode, on the quota of the Google account it is signed
      into. Headless agy only works inside WSL Ubuntu (native Windows agy prints nothing). In plan mode, with
      the chunk's folder added to the workspace, the agent opens the mp3 with its file-reading tool, which
      attaches the audio; no shell permission is needed. agy is flaky (read_file sometimes auto-denied, empty
      runs, tool-call JSON before the text), so each chunk gets up to 3 tries, then falls back to the API.
      ==Terms of service — read before using --via agy on a consumer plan.== Google's Antigravity FAQ, as
      relayed on the Google AI Developers Forum (2026-10-16), says running headless agy inside third-party
      coding agents is not supported on consumer plans (incl. Pro), even when the official unmodified binary
      is invoked as a local subprocess, and can lead to account suspension; direct scripts and CI pipelines
      are described as allowed (no automated account switching, no extracting OAuth credentials). Whether a
      script that an AI coding agent launches counts as "direct" is not settled. Check the current FAQ
      yourself; when in doubt use --via api. The risk is the user's own account.
==Text quality is equal; raw timestamps are NOT== — the CLI's clock stretches (a 4-min clip ends at 4:40-6:20).
So every chunk is re-anchored to the local transcript (ts_anchor.py: character alignment; Gemini's text is
kept, the local clock wins), on by default when <output-dir>/transcript.txt exists. Measured vs large-v3:
15 course clips CLI median +12 s / p90 44 s -> 0 s / 11 s; hall 10-min chunks CLI +61 s / p90 140 s ->
+3 s / 17 s (anchored to Qwen3-ASR; API raw +3 s / 21 s). The unanchored lines are kept in
transcript_gemini.raw.txt.
Both routes can return "blocked by Gemini's filters" on medical talks: the API then has no `candidates`.
That is reported with its promptFeedback, not hidden.

HARD RULE 10 applies: non-PHI recordings only (audio leaves the machine).

Usage:
  python gemini_asr.py <media> --output-dir DIR [--start 2026-10-23T13:12:01] [--via agy|api] [--lang en|zh]
                       [--anchor LOCAL.txt|none]   (default DIR/transcript.txt when it exists)
Writes DIR/transcript_gemini.txt: one line per sentence, "[HH:MM:SS] text" — wall clock if --start
is given, else elapsed time — and DIR/transcript_gemini.json ([{start, end, text, source}], elapsed
seconds, the shape transcript.json has). Chunks of --chunk seconds (default 600) are sent one at a time;
a chunk that already has its .part file is skipped, so a rerun resumes. A chunk blocked on every route
is noted in <part>.blocked (a rerun asks Gemini again); with --fill-blocked MODEL that chunk's
time range is transcribed locally instead (cached in <part>.fill, source = MODEL) and the run exits 0;
without it the chunk is left out and the run exits 2.
Silent truncation (2026-10-25, first full-course run, 15 recordings): Gemini sometimes stops partway
through a chunk (3 of 87 chunks lost 3-9 min) or returns the whole chunk on one line with inline stamps.
The text is split at inline stamps; any >= GAP s stretch where the local clock has speech but Gemini has no
line is sent to Gemini again for just that range (<part>.gap<s>), and only if that also comes back short is it
filled with --fill-blocked (cached <part>.gap<s>.fill). All 3 real gaps were recovered by the retry.
Repetition loop (2026-11-02, 1 of 44 chunks of a zh course): Gemini fell into a 4-line cycle ("謝謝。辛苦了。謝謝大家。好。")
for 500+ lines with stamps running to 29:16 of a 10:00 chunk. The gap check cannot see it (the range HAS lines).
Detector: in any LOOP_W consecutive lines, distinct (normalised) texts / LOOP_W < LOOP_R (that chunk 0.13; the
43 clean chunks of the same course >= 0.63). On a hit the part is moved to <part>.loop and the whole chunk is
sent once more (the manual fix that worked); if the retry loops too, the lines from the loop start on are dropped
and the gap check re-requests / fills that range. A looping gap retry is cut the same way (-> local fill).
"""
import argparse, base64, datetime as dt, json, os, re, subprocess, sys, tempfile, time, urllib.error, urllib.request, uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ts_anchor import anchor, parse as parse_local  # noqa: E402

PROMPT_EN = ("Transcribe this English conference talk recording verbatim. It may have been recorded from the audience in "
             "a large hall with loudspeakers. Output only the transcript as plain text, with a [mm:ss] timestamp (time from "
             "the start of this audio) at the start of each sentence. Do not summarise, do not correct the speaker, do not "
             "add anything that is not audible; if a word is unintelligible write [inaudible].")
PROMPT_ZH = ("Transcribe this Taiwanese medical lecture recording verbatim. The speaker mixes Mandarin with English medical "
             "terms: write Mandarin in Traditional Chinese and keep English words in English exactly as spoken (do not "
             "translate them). Output only the transcript as plain text, with a [mm:ss] timestamp (time from the start of "
             "this audio) at the start of each sentence. Do not summarise, do not correct the speaker, do not add anything "
             "that is not audible; if a word is unintelligible write [inaudible].")
AGY_PREFIX = ("Open the file {f} in the workspace with your file-reading tool (it attaches the audio to the conversation). "
              "Do not run any shell command and do not write any file. Then: ")
AGY_SUFFIX = " If no audio reached you, reply exactly CANNOT_HEAR_AUDIO."
AGY_MODEL = "Gemini 3.8 Flash (Medium)"
TS = re.compile(r"^\s*\[(\d+):(\d+)(?::(\d+))?\]\s?(.*)")
FIRST_TS = re.compile(r"^\s*\[\d+:\d+", re.M)
# agy can stream part of a chunk and THEN hit the filter: the partial text arrives with this notice appended
# -> cut it off, the gap check redoes the rest
BLOCK_TAIL = re.compile(r"\s*This request was blocked by Gemini's filters.*", re.S)
GAP = 90   # s of local speech with no Gemini line = Gemini stopped early
LOOP_W, LOOP_R = 30, 0.3   # repetition loop: window of lines, distinct-text ratio below which it is a loop
INLINE_TS = re.compile(r"[ \t]+(?=\[\d+:\d+(?::\d+)?\])")


def api_key():
    """GEMINI_API_KEY from the environment (never printed)."""
    k = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not k:
        raise RuntimeError("GEMINI_API_KEY is not set: create a key at https://aistudio.google.com/apikey "
                           "and export it, or use --via agy")
    return k


def default_via():
    """--via default: env LECTURE_GEMINI_VIA -> config.yaml `gemini.via` -> "api"."""
    v = (os.environ.get("LECTURE_GEMINI_VIA") or "").strip()
    if not v:
        try:
            from _common import load_config
            v = str(((load_config() or {}).get("gemini") or {}).get("via") or "").strip()
        except Exception:  # noqa: BLE001 - no config / no pyyaml -> built-in default
            v = ""
    return v if v in ("agy", "api") else "api"


class Blocked(Exception):
    """Gemini refused the chunk (safety filter); message carries the evidence."""


def duration(p):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", p],
                       capture_output=True, text=True)
    return float(r.stdout.strip())


def call_api(model, key, audio, prompt):
    body = {"contents": [{"parts": [{"inline_data": {"mime_type": "audio/mp3", "data": base64.b64encode(audio).decode()}},
                                    {"text": prompt}]}], "generationConfig": {"temperature": 0}}
    req = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                                 data=json.dumps(body).encode(), headers={"x-goog-api-key": key, "Content-Type": "application/json"})
    # 429 / 5xx: wait and retry for up to ~26 h (free tier resets daily; paid tier rarely 429s).
    waited = 0
    while True:
        try:
            d = json.load(urllib.request.urlopen(req, timeout=900))
            break
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            code = getattr(e, "code", None)
            if code not in (None, 429, 500, 502, 503, 504) or waited > 26 * 3600:
                raise
            pause = 1800 if code == 429 and waited >= 600 else 120
            print(f"  {code or type(e).__name__}: waiting {pause} s (waited {waited} s so far)", flush=True)
            time.sleep(pause); waited += pause
    cands = d.get("candidates") or []
    if not cands or "parts" not in (cands[0].get("content") or {}):   # SAFETY stop: content without parts
        why = {"promptFeedback": d.get("promptFeedback"), "finishReason": cands[0].get("finishReason") if cands else None}
        raise Blocked(f"API returned no transcript: {json.dumps(why)[:300]}")
    return "".join(p.get("text", "") for p in cands[0]["content"]["parts"])


def _wsl(path):
    p = os.path.abspath(path).replace("\\", "/")
    return f"/mnt/{p[0].lower()}{p[2:]}"


def call_agy(mp3, prompt, tries=3, min_chars=200):
    d, name = _wsl(os.path.dirname(mp3)), os.path.basename(mp3)
    full = AGY_PREFIX.format(f=name) + prompt + AGY_SUFFIX
    tag = uuid.uuid4().hex[:8]   # unique staging names so parallel runs do not collide
    script = ("#!/bin/bash\n"
              f"cd '{d}'\n"
              f"cat > /tmp/agy_asr_{tag}.txt <<'PROMPT_EOF'\n{full}\nPROMPT_EOF\n"
              f"timeout 900 $HOME/.local/bin/agy -p \"$(cat /tmp/agy_asr_{tag}.txt)\" --add-dir '{d}' --mode plan "
              f"--model \"{AGY_MODEL}\" 2>&1\n"
              f"rm -f /tmp/agy_asr_{tag}.txt\n")
    # bytes, not text: text mode on Windows would turn \n into \r\n and bash chokes on \r
    subprocess.run(["wsl.exe", "-d", "Ubuntu", "--", "bash", "-lc", f"cat > /tmp/agy_asr_{tag}.sh"],
                   input=script.encode("utf-8"), check=True)
    last = ""
    try:
        for i in range(1, tries + 1):
            r = subprocess.run(["wsl.exe", "-d", "Ubuntu", "--", "bash", f"/tmp/agy_asr_{tag}.sh"],
                               stdout=subprocess.PIPE, timeout=1000)
            txt = r.stdout.decode("utf-8", "replace").strip()
            m = FIRST_TS.search(txt)
            if m and "CANNOT_HEAR_AUDIO" not in txt and len(BLOCK_TAIL.sub("", txt)) - m.start() > min_chars:
                if BLOCK_TAIL.search(txt):
                    print(f"  agy try {i}: blocked mid-output, keeping the part before the filter notice", flush=True)
                return BLOCK_TAIL.sub("", txt[m.start():])   # drop agent chatter / tool-call JSON before the transcript
            last = txt
            kind = "blocked" if "blocked by Gemini's filters" in txt else "failed"
            print(f"  agy try {i}/{tries} {kind}: {txt[:120]!r}", flush=True)
    finally:
        subprocess.run(["wsl.exe", "-d", "Ubuntu", "--", "rm", "-f", f"/tmp/agy_asr_{tag}.sh"])
    if "blocked by Gemini's filters" in last:
        raise Blocked("agy: blocked by Gemini's filters")
    raise RuntimeError(f"agy gave no transcript after {tries} tries: {last[:200]!r}")


def fill_local(media, off, clen, part, model, lang):
    """Transcribe [off, off+clen) of media with the local ASR; lines in chunk-relative seconds."""
    fill = part + ".fill"
    if not os.path.exists(fill):
        with tempfile.TemporaryDirectory(dir=os.path.dirname(part)) as td:
            wav = os.path.join(td, "chunk.wav")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(off), "-t", str(clen), "-i", media,
                            "-ac", "1", "-ar", "16000", wav], check=True)
            subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "transcribe_video.py"),
                            wav, "-o", td, "--lang", lang, "--model", model])
            got = os.path.join(td, "transcript.txt")   # rc can be -1 from the CTranslate2 teardown crash
            if not os.path.exists(got) or not parse_local(got):
                raise RuntimeError(f"local {model} fill produced no transcript")
            os.replace(got, fill)
    return parse_local(fill)


def gemini_range(a, off, length, prompt, keybox, tag):
    """Gemini text for [off, off+length) of a.media: agy first (when --via agy), then the API. Raises Blocked."""
    with tempfile.TemporaryDirectory(dir=a.output_dir) as td:   # inside output dir: WSL must reach it
        mp3 = os.path.join(td, f"{tag}.mp3")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(off), "-t", str(length), "-i", a.media,
                        "-ac", "1", "-ar", "16000", "-b:a", "48k", mp3], check=True)
        agy_err = None
        if a.via == "agy":
            try:   # min_chars scaled to the range: a fixed 200 rejects every short tail chunk
                return call_agy(mp3, prompt, min_chars=min(200, int(length * 2))), "agy"
            except (Blocked, RuntimeError, subprocess.SubprocessError, OSError) as e:   # OSError: no wsl.exe
                agy_err = e
                print(f"  agy gave up ({e}); falling back to the API", flush=True)
        try:
            keybox[0] = keybox[0] or api_key()
        except RuntimeError as e:
            if agy_err is None:
                raise
            raise RuntimeError(f"--via agy failed ({agy_err}) and the API fallback has no key ({e})") from None
        return call_api(a.model, keybox[0], open(mp3, "rb").read(), prompt), "api"


def parse_part(txt):
    # Gemini sometimes returns a whole chunk on ONE line with the [mm:ss] stamps inline (2026-10-25: 3 of 5
    # chunks of one talk) -> without this split each such chunk became a single 10-minute "sentence"
    g = []
    for line in INLINE_TS.sub(chr(10), BLOCK_TAIL.sub("", txt)).splitlines():
        m = TS.match(line)
        if m:
            h, mi, s = (0, int(m[1]), int(m[2])) if m[3] is None else (int(m[1]), int(m[2]), int(m[3]))
            g.append((h * 3600 + mi * 60 + s, m[4].strip()))
    return g


def _norm(x):
    return re.sub(r"[\W_]+", "", x.lower())


def find_loop(g):
    """Index of the first line of a repetition loop in parsed lines g, or None.
    Loop = a LOOP_W-line window with distinct/LOOP_W < LOOP_R; it starts at the window's first line
    whose text recurs >= 3 times inside that window."""
    keys = [_norm(x) for _, x in g]
    for i in range(len(keys) - LOOP_W + 1):
        w = keys[i:i + LOOP_W]
        if len(set(w)) / LOOP_W < LOOP_R:
            return i + next(j for j, x in enumerate(w) if w.count(x) >= 3)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("media"); ap.add_argument("--output-dir", required=True)
    ap.add_argument("--start", help="ISO wall-clock time of the recording start")
    ap.add_argument("--via", choices=["agy", "api"], default=default_via(),
                    help="agy = Antigravity CLI headless via WSL, falls back to the API on failure (read the ToS "
                         "note in the module docstring first); api = REST API with GEMINI_API_KEY. Default: env "
                         "LECTURE_GEMINI_VIA, else config.yaml gemini.via, else api")
    ap.add_argument("--lang", choices=["en", "zh"], default="en", help="zh = Taiwanese Mandarin with English terms")
    ap.add_argument("--model", default="gemini-3.8-flash", help="API model id (the API route and the agy fallback)")
    ap.add_argument("--chunk", type=int, default=600)
    ap.add_argument("--anchor", help="local transcript ([MM:SS] or [H:MM:SS] lines, elapsed time) to re-anchor "
                                     "timestamps to; default <output-dir>/transcript.txt if present; 'none' = off")
    ap.add_argument("--fill-blocked", metavar="MODEL", help="transcribe chunks Gemini refuses with this local model "
                                                            "alias (e.g. breeze25) instead of leaving a gap")
    ap.add_argument("--fill-lang", choices=["zh", "en", "bilingual", "auto"],
                    help="transcribe_video --lang for --fill-blocked (default: --lang)")
    a = ap.parse_args()
    os.makedirs(a.output_dir, exist_ok=True)
    prompt = PROMPT_ZH if a.lang == "zh" else PROMPT_EN
    keybox = [None]
    t0 = dt.datetime.fromisoformat(a.start) if a.start else None
    if a.anchor is None:
        a.anchor = os.path.join(a.output_dir, "transcript.txt")
        a.anchor = a.anchor if os.path.exists(a.anchor) else "none"
    local = parse_local(a.anchor) if a.anchor != "none" else None
    print("anchor:", a.anchor if local else "none (raw Gemini timestamps; the CLI route's drift)", flush=True)
    stamp = lambda sec: (t0 + dt.timedelta(seconds=sec)).strftime("%H:%M:%S") if t0 else str(dt.timedelta(seconds=int(sec)))
    total = duration(a.media); lines = []; raw = []; blocked = []; filled = []; segs = []; gaps = []; loops = []; unfilled = []
    for k, off in enumerate(range(0, int(total) + 1, a.chunk)):
        part = os.path.join(a.output_dir, f"transcript_gemini.{k:03d}.part")
        clen = min(a.chunk, total - off)
        if clen < 1:   # duration an exact multiple of --chunk (or a sub-second tail): nothing to send
            break
        if not os.path.exists(part):
            t = time.time()
            try:
                txt, route = gemini_range(a, off, clen, prompt, keybox, f"chunk{k:03d}")
            except Blocked as e:
                open(part + ".blocked", "w", encoding="utf-8").write(str(e) + "\n")
                print(f"chunk {k} @{off}s: BLOCKED on every route — {e}", flush=True)
                if not a.fill_blocked:
                    blocked.append(k); continue
                g = fill_local(a.media, off, clen, part, a.fill_blocked, a.fill_lang or a.lang)
                print(f"chunk {k}: filled with local {a.fill_blocked} ({len(g)} lines)", flush=True)
                filled.append(k)
                lines += [f"[{stamp(off + t)}] {x}" for t, x in g]
                segs += [(off + t, x, a.fill_blocked) for t, x in g]
                continue
            open(part, "w", encoding="utf-8").write(txt)
            print(f"chunk {k} @{off}s via {route}: {len(txt.split())} words, {time.time() - t:.0f} s", flush=True)
        g = parse_part(open(part, encoding="utf-8").read())
        li = find_loop(g)
        if li is not None and not os.path.exists(part + ".loop"):   # first sighting: keep evidence, resend the chunk
            print(f"chunk {k}: repetition loop from line {li} ({g[li][1]!r}, raw stamp {g[li][0]} s, last stamp "
                  f"{max(t for t, _ in g)} s of {clen:.0f}) -> moved to .loop, resending the chunk", flush=True)
            os.replace(part, part + ".loop")
            try:
                txt, route = gemini_range(a, off, clen, prompt, keybox, f"chunk{k:03d}")
            except Blocked as e:
                print(f"chunk {k}: resend blocked ({e}); keeping the looping text up to the loop", flush=True)
                txt = open(part + ".loop", encoding="utf-8").read()
            open(part, "w", encoding="utf-8").write(txt)
            g = parse_part(txt); li = find_loop(g)
            print(f"chunk {k}: resend " + (f"still loops from line {li}" if li is not None else "is clean"), flush=True)
        if li is not None:
            print(f"chunk {k}: dropping {len(g) - li} looping lines from line {li}; the gap check redoes that range",
                  flush=True)
            loops.append((k, round(off + g[li][0]))); g = g[:li]
            if not g:   # looped from the first line: nothing to anchor, treat like a blocked chunk
                if not a.fill_blocked:
                    blocked.append(k); continue
                g = fill_local(a.media, off, clen, part, a.fill_blocked, a.fill_lang or a.lang)
                filled.append(k)
                lines += [f"[{stamp(off + t)}] {x}" for t, x in g]
                segs += [(off + t, x, a.fill_blocked) for t, x in g]
                continue
        raw +=[f"[{stamp(off + t)}] {x}" for t, x in g]
        if local and g:
            win = [(t - off, x) for t, x in local if off - 5 <= t < off + clen + 5]
            fixed, cov = anchor(g, win, clen) if win else (g, 0.0)
            if cov >= 0.5:
                g = [(min(t, clen), x) for t, x in fixed]
            else:   # sparse anchors from a hallucinating local model can be worse than none (2026-10-25: p90
                # 102 s at 17 % coverage); the CLI route's error is a stretch, so squeeze the raw clock into the chunk
                last = max(t for t, _ in g)
                s = min(1.0, (clen - 4) / last) if last > 0 else 1.0
                g = [(t * s, x) for t, x in g]
            print(f"chunk {k}: anchored {cov:.0%} of lines" + ("" if cov >= 0.5 else " -> too few, linear rescale"), flush=True)
        segs_k = [(t, x, "gemini") for t, x in g]
        # Gemini can stop mid-chunk without saying so (2026-10-25: one talk's chunk 9 ended at 4:06 of
        # 10:00, 5.5 min of speech lost; chunk 11 began 3 min late). The local clock shows where speech is: any
        # >= GAP s stretch with >= 5 local lines but no Gemini line is treated like a blocked chunk.
        if local and g:
            win = [t - off for t, _ in local if off <= t < off + clen]
            ts = sorted(t for t, _ in g)
            for lo, hi in zip([0.0] + ts, ts + [clen]):
                if hi - lo < GAP or sum(lo + 5 < t < hi - 5 for t in win) < 5:
                    continue
                print(f"chunk {k}: Gemini has nothing for {lo:.0f}-{hi:.0f} s while the local clock has speech", flush=True)
                gaps.append((k, round(off + lo), round(off + hi)))
                gp = f"{part}.gap{int(lo)}"
                if not os.path.exists(gp):   # first: ask Gemini again for just the missing range
                    try:
                        open(gp, "w", encoding="utf-8").write(gemini_range(a, off + lo, hi - lo, prompt, keybox, "gap")[0])
                    except Blocked as e:
                        open(gp, "w", encoding="utf-8").write("")
                        print(f"chunk {k}: gap retry blocked — {e}", flush=True)
                rg = parse_part(open(gp, encoding="utf-8").read())
                rli = find_loop(rg)
                if rli is not None:   # cut it: the length check below then sends the range to the local fill
                    print(f"chunk {k}: gap retry loops from line {rli}, cut", flush=True)
                    rg = rg[:rli]
                if rg and max(t for t, _ in rg) >= (hi - lo) * 0.7:
                    rwin = [(t - off - lo, x) for t, x in local if off + lo - 5 <= t < off + hi + 5]
                    fixed, cov = anchor(rg, rwin, hi - lo) if rwin else (rg, 0.0)
                    if cov < 0.5:
                        sc = min(1.0, (hi - lo - 4) / max(1, max(t for t, _ in rg)))
                        fixed = [(t * sc, x) for t, x in rg]
                    segs_k += [(lo + min(t, hi - lo), x, "gemini") for t, x in fixed]
                    gaps[-1] += ("gemini-retry",)
                    print(f"chunk {k}: gap re-transcribed by Gemini ({len(rg)} lines, anchored {cov:.0%})", flush=True)
                elif a.fill_blocked:
                    fl = fill_local(a.media, off + lo, hi - lo, f"{part}.gap{int(lo)}", a.fill_blocked, a.fill_lang or a.lang)
                    segs_k += [(lo + t, x, a.fill_blocked) for t, x in fl]
                    gaps[-1] += (a.fill_blocked,)
                    print(f"chunk {k}: gap filled with local {a.fill_blocked} ({len(fl)} lines)", flush=True)
                else:   # no --fill-blocked: the range stays empty -> say so in the exit code, like a blocked chunk
                    gaps[-1] += ("UNFILLED",)
                    unfilled.append((k, round(off + lo), round(off + hi)))
                    print(f"chunk {k}: gap {lo:.0f}-{hi:.0f} s left empty (no --fill-blocked)", flush=True)
            segs_k.sort(key=lambda s: s[0])
        lines += [f"[{stamp(off + t)}] {x}" for t, x, _ in segs_k]
        segs += [(off + t, x, src) for t, x, src in segs_k]
    out = os.path.join(a.output_dir, "transcript_gemini.txt")
    open(out, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    open(os.path.join(a.output_dir, "transcript_gemini.raw.txt"), "w", encoding="utf-8").write("\n".join(raw) + "\n")
    # end = next line's start (capped at 30 s), the shape transcribe_video's transcript.json has
    js = [{"start": round(t, 2), "end": round(max(t, min(segs[i + 1][0] if i + 1 < len(segs) else total, t + 30)), 2),
           "text": x, "source": src} for i, (t, x, src) in enumerate(segs)]
    with open(os.path.join(a.output_dir, "transcript_gemini.json"), "w", encoding="utf-8") as f:
        json.dump(js, f, ensure_ascii=False, indent=1)
    print("wrote", out, len(lines), "lines")
    if gaps:
        print(f"Gemini gaps (chunk, start s, end s, filled by): {gaps}", flush=True)
    if loops:
        print(f"repetition loops left after the resend (chunk, start s; lines dropped, gap check took the range): "
              f"{loops}", flush=True)
    if filled:
        print(f"filled chunks (local {a.fill_blocked}, see *.fill): {filled}", flush=True)
    if unfilled:
        print(f"UNFILLED gaps (chunk, start s, end s) — rerun with --fill-blocked MODEL: {unfilled}", flush=True)
    if blocked:
        print(f"BLOCKED chunks (no transcript, see *.blocked): {blocked}", flush=True)
    if blocked or unfilled:
        sys.exit(2)


if __name__ == "__main__":
    main()
