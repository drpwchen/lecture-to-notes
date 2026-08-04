# -*- coding: utf-8 -*-
"""Index the capture time of every photo/video in one or more folders.

    python media_capture_index.py OUT.json DIR [DIR ...] [--label mine=DIR2]

Capture time source of truth, in order:
  1. video: `com.apple.quicktime.creationdate` (carries the +08:00 offset)
  2. video: `creation_time` (UTC — this script adds --utc-offset hours)
  3. video: AVCHD `.MTS` — the MDPM pack inside the stream (`_mdpm.py`).
     ==An AVCHD camcorder writes no container timestamp==, so without this every
     .MTS reads as "no capture time" and the alignment layer goes dark on
     conference footage, which is nearly always AVCHD.
  4. audio: a timestamp embedded in the FILENAME (`…240526_1119…`) — voice
     recorders name files from their own clock, and that is a device claim, not
     a filesystem artefact
  5. photo: EXIF DateTimeOriginal (36867), else DateTime (306)

==Never use file mtime.== A zip/Drive/Immich round-trip rewrites mtime to the
upload time; on the IMPS 2026-07 batch every mtime was hours off while every
QuickTime creationdate was right to the second.

==Every device has its own clock, and clocks are wrong.== On the 2024-05 增生醫
學會 batch three devices disagreed: the camcorder by +1 day +5m30s, a Canon
stills camera by +72m30s, against a voice recorder that turned out to be right.
`--clock-offset` applies a MEASURED per-device correction; see
`reference/multi-camera.md` §device-clock-calibration for how to measure one.
==Never guess an offset== — an assumed one is how material lands in the wrong
session.

==The timestamp is the START of the recording, not the end.== Measured on that
batch: for each source recording, the implied recording start computed from 4–13
independent clips scattered by 3–7 s under the start hypothesis vs 268–843 s
under the end hypothesis (clip lengths 27–963 s). See
`reference/multi-camera.md` §timestamp-semantics.

Alignment backbone (`--emit-alignment alignment.json`)
------------------------------------------------------
==Capture timestamps are HYPOTHESES; transcript cross-correlation is EVIDENCE.==
`--emit-alignment` writes the hypothesis set — one row per source with its
claimed `capture_start`, where that claim came from (`start_source`), and a
`reliable` flag that is FALSE whenever the claim came from mtime or is absent.
An unreliable start must never be used to align material downstream; it is
recorded so a human can see what is missing, not so a script can use it.

The evidence check is `xcorr_media_offsets.py`: when two or more audio-bearing
sources claim overlapping capture windows, their real relative offset is
measured transcript-to-transcript. This script PRINTS the exact command and
does not run it — the cross-correlation costs GPU/CPU time (every source has to
be transcribed first), so the user decides when to spend it.

Feed the result back with `--xcorr-results xcorr.json`: the claimed offset
(from capture times) is compared against the measured one, and any pair
disagreeing by more than 5 s is flagged `"conflict": true` on both sources with
a loud warning. ==Nothing is ever auto-corrected.== A conflict means one of the
two clocks is lying, and which one is a judgement call — on the IMPS 2026-07
batch an assumed start was 44 minutes off and silently mis-assigned every clip.
Conflicts are a warning, not an error: the run still exits 0.
"""
import argparse, datetime, fnmatch, json, os, re, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import atomic_write_json, match_stem, require_binaries
from _mdpm import is_avchd, parse_mdpm

VIDEO = (".mov", ".mp4", ".m4v", ".mts", ".avi", ".mkv")
PHOTO = (".jpg", ".jpeg", ".heic", ".png", ".tif", ".tiff")
# Audio files are NOT part of the main index (adding rows would change what
# every existing --index consumer sees). They are probed only for the
# alignment view, where an audio-only room recording is usually the anchor.
AUDIO = (".m4a", ".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma")

# Claimed vs measured offsets further apart than this are a conflict, not drift.
CONFLICT_TOLERANCE_S = 5.0


# Voice recorders (Sony/Olympus/Zoom/phone apps) name the file from their own
# clock. Accepts 240526_1119 / 20240526_111936 / 2024-05-26_11-19, anywhere in
# the name. Two-digit years are read as 20xx — these are recordings, not
# archives from 1998.
FILENAME_TS = re.compile(
    r"(?<!\d)(\d{2}|\d{4})[-_]?(\d{2})[-_]?(\d{2})[ _T-]+(\d{2})[-:]?(\d{2})(?:[-:]?(\d{2}))?(?!\d)")


def parse_filename_time(name):
    """-> datetime from a timestamp embedded in the filename, else None."""
    for m in FILENAME_TS.finditer(name):
        y, mo, d, hh, mm, ss = m.groups()
        y = int(y) + 2000 if len(y) == 2 else int(y)
        try:
            return datetime.datetime(y, int(mo), int(d), int(hh), int(mm),
                                     int(ss or 0))
        except ValueError:
            continue     # e.g. a resolution or bitrate that looks like a date
    return None


def refine_seconds_with_mtime(path, dt):
    """Recover the SECONDS of a filename timestamp, when mtime corroborates it.

    A recorder filename gives only `…_1119`, i.e. minute precision. This is the
    one place mtime is admissible: if mtime falls in the very same minute the
    device itself named the file, mtime is that device writing the file at
    record-start, not a copy artefact — a zip/sync round-trip would have to
    land inside a 60 s window to fake it. Corroborated on the 2024-05 Conference-Y
    batch: 5 of 5 recordings had mtime in the filename's minute.

    Returns (datetime, source_label). Falls back to the unrefined value.
    """
    try:
        mt = datetime.datetime.fromtimestamp(os.path.getmtime(path))
    except OSError:
        return dt, "filename"
    if mt.replace(second=0, microsecond=0) == dt.replace(second=0, microsecond=0):
        return mt.replace(microsecond=0), "filename+mtime-seconds"
    return dt, "filename"


def probe_video(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:format_tags:stream_tags", "-of", "json", path],
        capture_output=True, text=True, encoding="utf8", errors="replace").stdout
    try:
        d = json.loads(out)
    except Exception:
        return {}
    fmt = d.get("format", {})
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    raw = tags.get("com.apple.quicktime.creationdate") or tags.get("creation_time")
    model = tags.get("com.apple.quicktime.model")
    if not raw and is_avchd(path):
        # AVCHD carries the clock in the stream, not the container.
        dt = parse_mdpm(path)
        if dt:
            return {"duration_s": float(fmt.get("duration") or 0),
                    "creation_raw": dt.isoformat(sep=" "),
                    "time_source_hint": "avchd-mdpm", "model": model or "AVCHD"}
    return {"duration_s": float(fmt.get("duration") or 0),
            "creation_raw": raw, "model": model}


def probe_photo(path):
    # "Pillow isn't installed" and "this one file's EXIF is unreadable" used to
    # produce the same message on every photo, which reads as a corrupt library
    # rather than a missing pip install.
    try:
        from PIL import Image
    except ImportError:
        return {"duration_s": 0, "creation_raw": None,
                "error": "Pillow not installed — pip install Pillow "
                         "(pillow-heif too, for .heic)"}
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        if path.lower().endswith(".heic"):
            return {"duration_s": 0, "creation_raw": None,
                    "error": "pillow-heif not installed — pip install pillow-heif "
                             "to read .heic capture times"}
    try:
        with Image.open(path) as im:
            ex = im.getexif()
            val = ex.get(36867) or ex.get(306)
            if not val:
                val = ex.get_ifd(0x8769).get(36867)
            return {"duration_s": 0, "creation_raw": val, "model": ex.get(272)}
    except Exception as e:  # noqa: BLE001 — unreadable/absent EXIF, per file
        return {"duration_s": 0, "creation_raw": None,
                "error": f"EXIF unreadable: {e}"}


def to_local(raw, utc_offset):
    if not raw:
        return None, None
    s = str(raw).strip()
    try:
        if s.endswith("Z"):
            return (datetime.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
                    + datetime.timedelta(hours=utc_offset)), "utc+%d" % utc_offset
        if "T" in s and ("+" in s[10:] or "-" in s[10:]):
            return datetime.datetime.fromisoformat(s).replace(tzinfo=None), "tz-tagged"
        if len(s) >= 19 and s[4] == ":":
            return datetime.datetime.strptime(s[:19], "%Y:%m:%d %H:%M:%S"), "exif"
        return datetime.datetime.fromisoformat(s[:19]), "iso"
    except Exception:
        return None, None


def parse_clock_offsets(specs):
    """['CANON*=-4350', 'label=+330'] -> [(selector, seconds), ...]."""
    out = []
    for s in specs or []:
        sel, _, val = s.rpartition("=")
        if not sel:
            sys.exit(f"ERROR: --clock-offset wants SELECTOR=SECONDS, got {s!r}")
        try:
            out.append((sel, float(val)))
        except ValueError:
            sys.exit(f"ERROR: --clock-offset seconds not a number in {s!r}")
    return out


def apply_clock_offsets(rows, offsets):
    """Shift capture times by a MEASURED per-device correction.

    A row matches a selector when the selector fnmatches its source label, its
    model, or its filename — so one flag can address a folder, a camera body or
    a filename pattern. The original stays in `capture_raw_start`: a corrected
    time that hides what the device actually claimed is unauditable.
    """
    n = 0
    for r in rows:
        for sel, secs in offsets:
            if not any(fnmatch.fnmatch(str(v or ""), sel)
                       for v in (r.get("source"), r.get("model"), r["file"])):
                continue
            r["clock_offset_s"] = secs
            if r.get("start"):
                r["capture_raw_start"] = r["start"]
                base = datetime.datetime.fromisoformat(r["start"])
                r["start"] = (base + datetime.timedelta(seconds=secs)
                              ).isoformat(sep=" ")
                if r.get("end"):
                    r["end"] = (datetime.datetime.fromisoformat(r["end"])
                                + datetime.timedelta(seconds=secs)
                                ).isoformat(sep=" ")
                r["time_source"] = (r.get("time_source") or "?") + "+offset"
            n += 1
            break
    return n


def _stem(name):
    return os.path.splitext(os.path.basename(name))[0]


def _parse_dt(s):
    try:
        return datetime.datetime.fromisoformat(s) if s else None
    except (TypeError, ValueError):
        return None


def build_alignment(rows, xcorr_path=None):
    """Rows (index rows + audio rows) -> the alignment.json object.

    Returns (obj, n_conflicts). Never mutates the index rows and never
    "corrects" a timestamp: the whole point is to surface disagreement.
    """
    sources = []
    notes = [
        "capture timestamps are HYPOTHESES; transcript cross-correlation "
        "(xcorr_media_offsets.py) is the EVIDENCE that confirms or refutes them",
        "reliable=false means the start came from file mtime or is absent — "
        "such a start MUST NOT be used to align material; treat it as unknown",
        "timestamps are the START of the recording, not the end (measured on "
        "the IMPS 2026-07 batch)",
    ]
    by_file = {}
    for r in rows:
        kind = {"photo": "image"}.get(r["kind"], r["kind"])
        start = r.get("start")
        if start:
            # Prefer what the probe actually used (avchd-mdpm / filename / exif);
            # fall back to the coarse per-kind label for older callers.
            src = r.get("time_source") or ("exif" if kind == "image" else "ffprobe")
        else:
            # Last-resort hypothesis, recorded so the gap is visible — and
            # marked unreliable so nothing downstream may act on it.
            try:
                start = datetime.datetime.fromtimestamp(
                    os.path.getmtime(r["path"])).isoformat(sep=" ")
                src = "mtime"
            except OSError:
                start, src = None, "none"
        s = {
            "file": r["file"],
            "path": r.get("path"),
            "kind": kind,
            "capture_start": start,
            "start_source": src,
            "duration_s": (r.get("duration_s") or None),
            # mtime and "nothing" are the unreliable ones; a device-written
            # clock (container tag, EXIF, AVCHD MDPM, recorder filename) is a
            # real claim — possibly a wrong one, which is what xcorr is for.
            "reliable": not src.startswith(("mtime", "none")),
        }
        sources.append(s)
        by_file[_stem(r["file"])] = s

    unreliable = [s["file"] for s in sources if not s["reliable"]]
    if unreliable:
        notes.append(f"{len(unreliable)} source(s) have no trustworthy capture "
                     f"start: {', '.join(unreliable[:8])}"
                     + (" …" if len(unreliable) > 8 else ""))

    # --- which sources could be cross-checked against each other ------
    audio_bearing = [s for s in sources
                     if s["kind"] in ("video", "audio") and s["reliable"]
                     and s["capture_start"] and s["duration_s"]]
    overlaps = []
    for i in range(len(audio_bearing)):
        for j in range(i + 1, len(audio_bearing)):
            a_, b_ = audio_bearing[i], audio_bearing[j]
            sa, sb = _parse_dt(a_["capture_start"]), _parse_dt(b_["capture_start"])
            if not sa or not sb:
                continue
            ea = sa + datetime.timedelta(seconds=a_["duration_s"])
            eb = sb + datetime.timedelta(seconds=b_["duration_s"])
            if sa < eb and sb < ea:
                overlaps.append((a_["file"], b_["file"]))
    if len(audio_bearing) >= 2 and overlaps:
        notes.append(f"{len(overlaps)} pair(s) of audio-bearing sources claim "
                     "overlapping capture windows — verify with "
                     "xcorr_media_offsets.py before trusting the claimed offsets")

    # --- evidence comparison ------------------------------------------
    n_conflict = 0
    if xcorr_path:
        try:
            with open(xcorr_path, encoding="utf-8") as fh:
                xc = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            notes.append(f"--xcorr-results unreadable ({e}); no evidence check ran")
            xc = None
        if xc:
            recs = xc.get("recordings") or {}
            for clip_name, cl in (xc.get("clips") or {}).items():
                if not cl:
                    continue
                rec_name = cl.get("rec")
                clip_s = by_file.get(_stem(clip_name)) or match_stem(
                    _stem(clip_name), by_file)
                rec_s = by_file.get(_stem(rec_name or "")) or (
                    match_stem(_stem(rec_name), by_file) if rec_name else None)
                if not clip_s or not rec_s:
                    notes.append(f"xcorr clip '{clip_name}' -> rec '{rec_name}': "
                                 "one of them is not in this index; not checked")
                    continue
                if not (clip_s["reliable"] and rec_s["reliable"]):
                    notes.append(f"xcorr clip '{clip_name}' vs '{rec_name}': "
                                 "no reliable capture start on both sides, so "
                                 "there is no claim to check the measurement "
                                 "against (measurement stands alone)")
                    continue
                ct, rt = _parse_dt(clip_s["capture_start"]), _parse_dt(rec_s["capture_start"])
                if not ct or not rt:
                    continue
                claimed = (ct - rt).total_seconds()
                measured = float(cl.get("offset_s") or 0.0)
                delta = abs(claimed - measured)
                if delta > CONFLICT_TOLERANCE_S:
                    n_conflict += 1
                    clip_s["conflict"] = True
                    rec_s["conflict"] = True
                    detail = (f"CONFLICT {clip_s['file']} in {rec_s['file']}: "
                              f"capture times claim offset {claimed:.0f}s, xcorr "
                              f"measured {measured:.0f}s (差 {delta:.0f}s > "
                              f"{CONFLICT_TOLERANCE_S:.0f}s)")
                    notes.append(detail + " — NOT auto-corrected; decide which "
                                 "clock is wrong before aligning")
                    print("!! " + detail, file=sys.stderr)
                    if cl.get("low_confidence"):
                        print("   (xcorr flagged this clip low_confidence — the "
                              "measurement itself may be the weak side)",
                              file=sys.stderr)
            # A recording's own measured start vs what its file metadata claims.
            for rec_name, rr in recs.items():
                rec_s = by_file.get(_stem(rec_name)) or match_stem(_stem(rec_name), by_file)
                if not rec_s or not rec_s["reliable"]:
                    continue
                mt, rt = _parse_dt(rr.get("measured_start")), _parse_dt(rec_s["capture_start"])
                if not mt or not rt:
                    continue
                delta = abs((mt - rt).total_seconds())
                if delta > CONFLICT_TOLERANCE_S:
                    n_conflict += 1
                    rec_s["conflict"] = True
                    detail = (f"CONFLICT {rec_s['file']}: metadata start "
                              f"{rec_s['capture_start']} vs xcorr measured start "
                              f"{rr.get('measured_start')} (差 {delta:.0f}s)")
                    notes.append(detail + " — NOT auto-corrected")
                    print("!! " + detail, file=sys.stderr)

    return ({"generated": datetime.datetime.now().isoformat(timespec="seconds"),
             "sources": sources, "notes": notes}, n_conflict)


def print_xcorr_hint(alignment, out_path):
    """Print the exact verification command. This script never runs it: the
    cross-correlation needs every source transcribed first (GPU time), so
    spending it is the user's call."""
    srcs = [s for s in alignment["sources"] if s["kind"] in ("video", "audio")]
    if len(srcs) < 2:
        return
    longest = max(srcs, key=lambda s: s.get("duration_s") or 0)
    print("\n== verify these hypotheses with the evidence check ==")
    print("  1) transcribe every source (transcribe_video.py --lang ... ), one "
          "output dir per file")
    print("  2) then run:")
    print("     python xcorr_media_offsets.py xcorr.json \\")
    print("         --clip-asr <dir of per-clip ASR subdirs> \\")
    print(f"         --recording {_stem(longest['file'])}=<ASR dir of "
          f"{longest['file']}> \\")
    print(f"         --index <this index json>")
    print(f"  3) feed it back: media_capture_index.py ... --emit-alignment "
          f"{out_path} --xcorr-results xcorr.json")
    print("  ==Do not align anything on capture times alone when the sources "
          "overlap.==")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("dirs", nargs="+", help="DIR or LABEL=DIR")
    ap.add_argument("--utc-offset", type=float, default=8)
    ap.add_argument("--emit-alignment", metavar="PATH",
                    help="also write alignment.json: per-source capture-start "
                         "hypotheses with a reliable flag, plus the xcorr "
                         "verification command. Additive — the index itself is "
                         "unchanged.")
    ap.add_argument("--xcorr-results", metavar="PATH",
                    help="xcorr_media_offsets.py output; claimed vs measured "
                         "offsets are compared and disagreements > "
                         f"{CONFLICT_TOLERANCE_S:.0f}s are flagged as conflicts "
                         "(warning, never an auto-correction)")
    ap.add_argument("--clock-offset", action="append", metavar="SELECTOR=SECONDS",
                    help="apply a MEASURED per-device clock correction, e.g. "
                         "'*.MTS=-86730' or 'Canon*=-4350'. SELECTOR fnmatches "
                         "the source label, the model, or the filename. The "
                         "device's own claim is kept in capture_raw_start. "
                         "==Only ever pass a measured offset== — how to measure "
                         "one is in reference/multi-camera.md.")
    a = ap.parse_args()
    require_binaries("ffprobe")
    if a.xcorr_results and not a.emit_alignment:
        sys.exit("ERROR: --xcorr-results only has an effect together with "
                 "--emit-alignment <path>")

    rows = []
    audio_rows = []   # alignment-only; never written to the index (CLI compat)
    for spec in a.dirs:
        label, _, d = spec.partition("=") if "=" in spec else ("", "", spec)
        if not os.path.isdir(d):
            sys.exit(f"ERROR: not a directory: {d}")
        # scandir, not glob: a folder named "2026-07 [workshop]" is a character
        # class to glob, so it silently matched nothing and reported 0 files.
        for p in sorted(os.path.join(d, e.name)
                        for e in os.scandir(d) if e.is_file()):
            ext = os.path.splitext(p)[1].lower()
            kind = "video" if ext in VIDEO else ("photo" if ext in PHOTO else None)
            if not kind and ext in AUDIO and a.emit_alignment:
                # ffprobe reads container tags on audio too. Collected only for
                # the alignment view — an audio-only room recording is usually
                # the anchor everything else is aligned to.
                ainfo = probe_video(p)
                adt, asrc = to_local(ainfo.get("creation_raw"), a.utc_offset)
                if not adt:
                    # A voice recorder usually writes no container tag but does
                    # name the file from its clock.
                    adt = parse_filename_time(os.path.basename(p))
                    if adt:
                        adt, asrc = refine_seconds_with_mtime(p, adt)
                audio_rows.append({
                    "source": label or os.path.basename(os.path.normpath(d)),
                    "file": os.path.basename(p), "path": p, "kind": "audio",
                    "duration_s": round(ainfo.get("duration_s") or 0, 1),
                    "start": adt.isoformat(sep=" ") if adt else None,
                    "time_source": asrc if adt else None,
                })
                continue
            if not kind:
                continue
            info = probe_video(p) if kind == "video" else probe_photo(p)
            dt, src = to_local(info.get("creation_raw"), a.utc_offset)
            src = info.get("time_source_hint") or src
            rows.append({
                "source": label or os.path.basename(os.path.normpath(d)),
                "file": os.path.basename(p), "path": p, "kind": kind,
                "size_mb": round(os.path.getsize(p) / 1e6, 1),
                "duration_s": round(info.get("duration_s") or 0, 1),
                "start": dt.isoformat(sep=" ") if dt else None,
                "end": (dt + datetime.timedelta(seconds=info.get("duration_s") or 0)
                        ).isoformat(sep=" ") if dt else None,
                "time_source": src, "model": info.get("model"),
                "creation_raw": info.get("creation_raw"),
                "error": info.get("error"),
            })
    n_shift = apply_clock_offsets(rows + audio_rows,
                                  parse_clock_offsets(a.clock_offset))
    if n_shift:
        print("applied a measured clock offset to %d source(s); their device's "
              "own claim is kept in capture_raw_start" % n_shift)
    # Timed files first in chronological order, then the ones with no capture
    # time, alphabetically. (The old key sorted None as the string "9", which
    # happened to work only because ISO dates start with "2".)
    rows.sort(key=lambda r: (r["start"] is None, r["start"] or "", r["file"]))
    atomic_write_json(a.out, rows, indent=1)
    notime = [r["file"] for r in rows if not r["start"]]
    errs = sorted({r["error"] for r in rows if r.get("error")})
    for r in rows:
        print("%-8s %-44s %-19s %6.0fs %8.1fMB %s" % (
            r["source"], r["file"][:44], r["start"] or "NO-TIME",
            r["duration_s"], r["size_mb"], r["model"] or ""))
    print("%d files -> %s ; total %.1f h video" % (
        len(rows), a.out, sum(r["duration_s"] for r in rows) / 3600))
    if notime:
        print("!! no capture time (do NOT fall back to mtime — ask the user):", notime)
    for e in errs:
        print("!! %s" % e)

    if a.emit_alignment:
        align, n_conflict = build_alignment(rows + audio_rows, a.xcorr_results)
        atomic_write_json(a.emit_alignment, align, indent=1)
        n_ok = sum(1 for s in align["sources"] if s["reliable"])
        print("\nalignment -> %s ; %d/%d sources have a trustworthy capture start"
              % (a.emit_alignment, n_ok, len(align["sources"])))
        if n_ok < len(align["sources"]):
            print("!! the rest carry reliable=false (mtime or nothing) — "
                  "==they must not be aligned on==; ask the user for the real "
                  "capture order instead of guessing")
        if n_conflict:
            print("!! %d capture-time/xcorr CONFLICT(s) — see alignment.json "
                  "\"notes\". Nothing was auto-corrected." % n_conflict)
        print_xcorr_hint(align, a.emit_alignment)


if __name__ == "__main__":
    main()
