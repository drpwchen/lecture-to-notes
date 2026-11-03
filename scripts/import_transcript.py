"""Import an external transcript into a lecture dir as ``transcript.json`` + ``.txt``.

For when the words come from somewhere else — MLX Whisper on Apple Silicon, a
hosted speech-to-text service, creator captions, a human transcript — and Stage 0
should not run again. The output follows ``docs/transcript-contract.md`` (public
repo): a bare JSON list of ``{start, end, text[, words]}`` in float seconds from
the start of the media, sorted by ``start``; ``transcript.txt`` written by the
same helper the local engine uses. Stages A–E and the web export then run
unchanged.

    python import_transcript.py SOURCE --out-dir DIR
        [--format auto|srt|vtt|json] [--engine NAME] [--model NAME] [--lang CODE]
        [--media FILE] [--offset SEC] [--force] [--backup]
        [--list-key KEY] [--start-key KEY] [--end-key KEY] [--text-key KEY]
        [--time-unit s|ms]

Formats
-------
- ``srt``  SubRip: ``HH:MM:SS,mmm --> HH:MM:SS,mmm`` cues.
- ``vtt``  WebVTT: ``[HH:]MM:SS.mmm --> …`` cues; NOTE/STYLE/REGION blocks and
  cue settings are skipped. A ``<v Name>`` voice span is kept as ``speaker``.
- ``json`` whisper-style output — openai-whisper, faster-whisper dumps, WhisperX,
  and (by its documented format, untested here) mlx-whisper all write
  ``{"segments": [{start, end, text, words?}]}``; a bare list of the same also
  works. Other JSON shapes: name the list and keys with ``--list-key`` /
  ``--start-key`` / ``--end-key`` / ``--text-key``, and ``--time-unit ms`` for
  millisecond integers (e.g. ``--list-key utterances --time-unit ms``).
- ``auto`` (default) picks by extension, then by content.

Caption styling (``<i>``, ``<b>``, ``<font>``, VTT ``<c.x>`` classes and inline
``<00:00:01.000>`` timestamps, ASS ``{\\an8}`` overrides) is removed; the words
are not touched — this is a format conversion, not a correction (HARD RULE 3).
Word timings are kept when every word of a segment has ``start``/``end``/``word``
(WhisperX leaves unaligned words without times; such a segment loses its
``words`` and is interpolated downstream, as the contract allows).

Rules (the contract's "Rules for an importer")
-----------------------------------------------
1. The source file is only read. Importing a file onto itself is refused.
2. An existing ``transcript.json`` is never overwritten without ``--force``;
   ``--backup`` keeps it as ``transcript.json.bak-import-<timestamp>`` (``.txt`` too).
3. Times become float seconds (rounded to the millisecond) from the start of the
   media; ``--offset`` shifts a transcript of a later chunk.
4. Segments are sorted by ``start``; a segment with ``end < start`` or a
   non-numeric time is an error (nothing is written) — never silently fixed.
5. ``transcript.txt`` is written with ``_common.write_transcript_lines``.
6. Provenance: ``metadata.json`` → ``stages.transcribe`` and ``runs.jsonl`` get
   ``engine`` (``--engine``, default ``import:<format>``) and ``whisper_model``
   (``--model``), as the local engine records; ``provenance.json`` records the
   source file's hash, the format and options.

Exit codes: 0 written, 2 bad input / refused (nothing written).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from _common import (UnsafePathError, atomic_write_json, contained_path,  # noqa: E402
                     write_transcript_lines)


class ImportError_(ValueError):
    """The source cannot be converted faithfully; nothing is written."""


# ----------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------

_TS = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{1,2})[,.](\d{1,3})$")
_CUE = re.compile(r"^\s*(\S+)\s+-->\s+(\S+)(?:\s+.*)?$")
_TAG = re.compile(r"</?(?:i|b|u|s|font|c|ruby|rt|lang|span)(?:[.\s][^>]*)?>", re.I)
_VOICE = re.compile(r"<v(?:\.[^\s>]*)?\s+([^>]*)>", re.I)
_VOICE_END = re.compile(r"</v>", re.I)
_INLINE_TS = re.compile(r"<\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3}>")
_ASS = re.compile(r"\{\\[^}]*\}")


def parse_timecode(tc: str) -> float:
    """``HH:MM:SS,mmm`` / ``HH:MM:SS.mmm`` / ``MM:SS.mmm`` -> seconds."""
    m = _TS.match(tc.strip())
    if not m:
        raise ImportError_(f"unparsable timecode {tc!r}")
    h, mi, s, frac = m.groups()
    ms = int(frac.ljust(3, "0"))
    return int(h or 0) * 3600 + int(mi) * 60 + int(s) + ms / 1000.0


def _clean_cue_text(lines: list[str]) -> tuple[str, str | None]:
    speaker = None
    out = []
    for ln in lines:
        m = _VOICE.search(ln)
        if m and speaker is None:
            speaker = m.group(1).strip() or None
        ln = _VOICE.sub("", ln)
        ln = _VOICE_END.sub("", ln)
        ln = _INLINE_TS.sub("", ln)
        ln = _TAG.sub("", ln)
        ln = _ASS.sub("", ln)
        ln = ln.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        ln = ln.strip()
        if ln:
            out.append(ln)
    return " ".join(out), speaker


def _blocks(text: str) -> list[list[str]]:
    blocks, cur = [], []
    for ln in text.splitlines():
        if ln.strip():
            cur.append(ln.rstrip("\r"))
        elif cur:
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)
    return blocks


def parse_srt(text: str) -> list[dict]:
    segs = []
    for b in _blocks(text):
        i = next((k for k, ln in enumerate(b) if "-->" in ln), None)
        if i is None:
            if len(b) == 1 and b[0].strip().isdigit():
                continue  # a stray index line
            raise ImportError_(f"SRT block without a timing line: {b[0][:60]!r}")
        m = _CUE.match(b[i])
        if not m:
            raise ImportError_(f"bad SRT timing line {b[i][:80]!r}")
        txt, spk = _clean_cue_text(b[i + 1:])
        seg = {"start": parse_timecode(m.group(1)), "end": parse_timecode(m.group(2)), "text": txt}
        if spk:
            seg["speaker"] = spk
        segs.append(seg)
    return segs


def parse_vtt(text: str) -> list[dict]:
    text = text.lstrip("﻿")
    if not text.startswith("WEBVTT"):
        raise ImportError_("not a WebVTT file (no WEBVTT header)")
    segs = []
    for n, b in enumerate(_blocks(text)):
        if n == 0 and b[0].startswith("WEBVTT"):
            continue
        if b[0].startswith(("NOTE", "STYLE", "REGION")):
            continue
        i = next((k for k, ln in enumerate(b) if "-->" in ln), None)
        if i is None:
            raise ImportError_(f"VTT block without a timing line: {b[0][:60]!r}")
        m = _CUE.match(b[i])
        if not m:
            raise ImportError_(f"bad VTT timing line {b[i][:80]!r}")
        txt, spk = _clean_cue_text(b[i + 1:])
        seg = {"start": parse_timecode(m.group(1)), "end": parse_timecode(m.group(2)), "text": txt}
        if spk:
            seg["speaker"] = spk
        segs.append(seg)
    return segs


def _num(v, what: str, scale: float) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        if isinstance(v, str):
            try:
                return float(v) * scale
            except ValueError:
                pass
        raise ImportError_(f"{what} is not a number: {v!r}")
    return float(v) * scale


def parse_json(data, list_key=None, start_key="start", end_key="end", text_key="text",
               time_unit="s") -> list[dict]:
    scale = 0.001 if time_unit == "ms" else 1.0
    if list_key:
        if not isinstance(data, dict) or not isinstance(data.get(list_key), list):
            raise ImportError_(f"JSON has no list under {list_key!r}")
        items = data[list_key]
    elif isinstance(data, list):
        items = data
    elif isinstance(data, dict) and isinstance(data.get("segments"), list):
        items = data["segments"]
    else:
        raise ImportError_("JSON is neither a list of segments nor {\"segments\": [...]} "
                           "— name the list with --list-key")
    segs = []
    for k, it in enumerate(items):
        if not isinstance(it, dict):
            raise ImportError_(f"segment {k} is not an object")
        for key in (start_key, end_key):
            if key not in it:
                raise ImportError_(f"segment {k} has no {key!r} (set --start-key/--end-key)")
        txt = it.get(text_key)
        if txt is None:
            txt = ""
        if not isinstance(txt, str):
            raise ImportError_(f"segment {k} {text_key!r} is not a string")
        seg = {"start": _num(it[start_key], f"segment {k} {start_key}", scale),
               "end": _num(it[end_key], f"segment {k} {end_key}", scale),
               "text": txt}
        words = it.get("words")
        if isinstance(words, list) and words:
            ok = []
            for w in words:
                if not (isinstance(w, dict) and isinstance(w.get("word", w.get("text")), str)
                        and isinstance(w.get("start"), (int, float))
                        and isinstance(w.get("end"), (int, float))):
                    ok = None
                    break
                ok.append({"start": float(w["start"]) * scale, "end": float(w["end"]) * scale,
                           "word": w.get("word", w.get("text"))})
            if ok:
                seg["words"] = ok
        segs.append(seg)
    return segs


def detect_format(path: str, head: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".srt",):
        return "srt"
    if ext in (".vtt",):
        return "vtt"
    if ext in (".json",):
        return "json"
    h = head.lstrip("﻿").lstrip()
    if h.startswith("WEBVTT"):
        return "vtt"
    if h[:1] in "[{":
        return "json"
    if "-->" in head:
        return "srt"
    raise ImportError_(f"cannot tell the format of {os.path.basename(path)} — pass --format")


def normalise(segs: list[dict], offset: float = 0.0) -> list[dict]:
    """Shift, round to ms, validate, sort. Raises ImportError_ on a bad segment."""
    out = []
    bad = []
    for k, s in enumerate(segs):
        st, en = s["start"] + offset, s["end"] + offset
        if st < 0 or en < st:
            bad.append((k, st, en))
            continue
        seg = dict(s, start=round(st, 3), end=round(en, 3))
        if "words" in s:
            seg["words"] = [dict(w, start=round(w["start"] + offset, 3),
                                 end=round(w["end"] + offset, 3)) for w in s["words"]]
        out.append(seg)
    if bad:
        k, st, en = bad[0]
        raise ImportError_(f"{len(bad)} segment(s) with negative time or end < start "
                           f"(first: #{k} start={st} end={en}) — fix the source or --offset")
    out.sort(key=lambda s: s["start"])   # stable: ties keep source order
    return out


def load_source(path: str, fmt: str, a) -> tuple[str, list[dict]]:
    with open(path, encoding="utf-8-sig") as fh:
        text = fh.read()
    if fmt == "auto":
        fmt = detect_format(path, text[:2000])
    if fmt == "srt":
        return fmt, parse_srt(text)
    if fmt == "vtt":
        return fmt, parse_vtt(text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ImportError_(f"invalid JSON: {e}") from None
    return "json", parse_json(data, a.list_key, a.start_key, a.end_key, a.text_key, a.time_unit)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Import an external transcript (SRT / VTT / JSON) "
                                             "as transcript.json + transcript.txt")
    ap.add_argument("source")
    ap.add_argument("--out-dir", "-o", required=True, help="lecture dir")
    ap.add_argument("--format", choices=["auto", "srt", "vtt", "json"], default="auto")
    ap.add_argument("--engine", default=None,
                    help="what produced the source, e.g. mlx-whisper (default: import:<format>)")
    ap.add_argument("--model", default=None, help="model name of the source ASR, if known")
    ap.add_argument("--provider", default="external",
                    help="where the source ASR ran: local / a vendor name (default: external)")
    ap.add_argument("--lang", default=None, help="language of the speech, recorded only")
    ap.add_argument("--media", default=None,
                    help="the recording this transcript belongs to (fingerprinted for "
                         "provenance; also checks the transcript does not run past it)")
    ap.add_argument("--offset", type=float, default=0.0, help="seconds added to every time")
    ap.add_argument("--force", action="store_true", help="replace an existing transcript.json")
    ap.add_argument("--backup", action="store_true",
                    help="with --force: keep the old transcript as .bak-import-<timestamp>")
    ap.add_argument("--list-key", default=None, help="JSON: key of the segment list")
    ap.add_argument("--start-key", default="start")
    ap.add_argument("--end-key", default="end")
    ap.add_argument("--text-key", default="text")
    ap.add_argument("--time-unit", choices=["s", "ms"], default="s", help="JSON times")
    a = ap.parse_args(argv)

    def fail(msg: str) -> int:
        print(f"ERROR: {msg}", file=sys.stderr)
        return 2

    if not os.path.isfile(a.source):
        return fail(f"source not found: {a.source}")
    os.makedirs(a.out_dir, exist_ok=True)
    try:
        tj = contained_path(a.out_dir, "transcript.json")
        tt = contained_path(a.out_dir, "transcript.txt")
    except UnsafePathError as e:
        return fail(str(e))
    src_real = os.path.normcase(os.path.realpath(a.source))
    if src_real in (os.path.normcase(os.path.realpath(tj)), os.path.normcase(os.path.realpath(tt))):
        return fail("the source IS the output file — import from a copy (the source is never modified)")
    if os.path.exists(tj) and not a.force:
        return fail(f"{tj} exists — pass --force to replace it (add --backup to keep it)")

    try:
        fmt, segs = load_source(a.source, a.format, a)
        segs = normalise(segs, a.offset)
    except (ImportError_, UnicodeDecodeError) as e:
        return fail(f"{os.path.basename(a.source)}: {e}")
    if not segs:
        return fail(f"{os.path.basename(a.source)}: no segments found")

    if a.media:
        if not os.path.isfile(a.media):
            return fail(f"--media not found: {a.media}")
        try:
            import subprocess
            dur = float(subprocess.check_output(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", a.media], text=True, timeout=60).strip())
            last = max(s["end"] for s in segs)
            if last > dur + 5:
                print(f"WARNING: transcript ends at {last:.1f}s but the media is {dur:.1f}s "
                      "— wrong file, or a missing --offset?", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 — a missing ffprobe only skips the check
            print(f"  (duration check skipped: {e})", file=sys.stderr)

    if os.path.exists(tj) and a.backup:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for p in (tj, tt):
            if os.path.exists(p):
                shutil.copy2(p, f"{p}.bak-import-{stamp}")
                print(f"  backup   {os.path.basename(p)}.bak-import-{stamp}")

    engine = a.engine or f"import:{fmt}"
    n_words = sum(1 for s in segs if "words" in s)
    log = None
    try:
        from _log import StageLogger
        log = StageLogger("transcribe", a.out_dir, stdout_mirror=False,
                          extra={"engine": engine}, media_path=a.media)
        log.start(source=os.path.basename(a.source), format=fmt)
    except Exception as e:  # noqa: BLE001 — logging must not block an import
        print(f"WARNING: run log unavailable ({e})", file=sys.stderr)

    atomic_write_json(tj, segs)
    write_transcript_lines(segs, tt)

    if log is not None:
        log.stage_done(success=True, n_segments=len(segs), engine=engine,
                       whisper_model=a.model, lang=a.lang, imported_from=fmt)
        log.close()
        try:
            from _log import append_run_summary, git_hash, lecture_name
            append_run_summary(a.out_dir, {
                "date": time.strftime("%Y-%m-%d"), "lecture": lecture_name(a.out_dir),
                "stage": "transcribe", "n_segments": len(segs), "lang": a.lang,
                "engine": engine, "whisper_model": a.model, "success": True,
                "pipeline_version": git_hash()})
        except Exception as e:  # noqa: BLE001
            print(f"WARNING: runs.jsonl not written ({e})", file=sys.stderr)
    try:
        from provenance import record
        record(a.out_dir, "transcribe", [tj, tt], [a.source] + ([a.media] if a.media else []),
               tool="import_transcript.py", provider=a.provider, engine=engine, model=a.model,
               network="none", params={"format": fmt, "offset": a.offset, "lang": a.lang,
                                       "time_unit": a.time_unit if fmt == "json" else None,
                                       "n_segments": len(segs), "n_with_words": n_words})
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: provenance not recorded ({e})", file=sys.stderr)

    span = max(s["end"] for s in segs)
    print(f"imported {len(segs)} segments ({fmt}, {n_words} with word timings, "
          f"ends {span:.1f}s) -> {tj}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
