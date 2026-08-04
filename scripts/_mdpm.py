# -*- coding: utf-8 -*-
"""AVCHD (.MTS/.M2TS) recording-clock reader.

Why this exists: an AVCHD camcorder writes ==no container timestamp at all== —
`ffprobe` shows neither `creation_time` nor any QuickTime tag, so
`media_capture_index.py` used to classify every `.MTS` as "no capture time" and
the whole alignment layer went dark on exactly the material that needs it most
(conference footage, which is nearly always AVCHD).

The clock is there, just not in the container: every AVCHD stream carries an
MDPM (Modified Digital-video Pack Metadata) block inside an SEI NAL, holding the
recording date and time to the second.

Layout, verified against the 2024-05 Conference-Y batch (20 files):

    b"MDPM" … 0x18 tz  0x20        yy(BCD)  mm(BCD)
              0x19 dd(BCD)  hh(BCD)  mm(BCD)  ss(BCD)

Entries are 5 bytes (1 tag + 4 data). ==The run is located by the 0x18 date tag,
not by the leading entry count==, whose alignment varies between encoders — the
count-based parse silently returned None on all 20 files of that batch.

==This is the recording START.== Verified on that batch: `start + duration`
landed within 0.2–3.2 s of the file mtime on every one of the 20 files, and
consecutive 2 GB split parts chained end-to-start exactly. Note the corollary —
==for AVCHD, mtime is the recording END, not the start==; anything that treats
mtime as a start hypothesis is wrong by the clip's whole duration.

==The camcorder clock still may be wrong.== On that same batch it read exactly
+1 day +5m30s off real time. This module reports what the camera wrote; correct
it with a measured device offset (see `reference/multi-camera.md`
§device-clock-calibration), never by assuming.
"""
import datetime

_EXT = (".mts", ".m2ts")


def _bcd(b):
    return (b >> 4) * 10 + (b & 0x0F)


def _scan(buf):
    pos = buf.find(b"MDPM")
    while pos >= 0:
        win = buf[pos:pos + 256]
        for j in range(len(win) - 10):
            # 0x18 = date pack, 0x19 = time pack, and win[j+2] is the BCD
            # century byte, which is 0x20 for every date this format can hold.
            if win[j] != 0x18 or win[j + 2] != 0x20 or win[j + 5] != 0x19:
                continue
            try:
                return datetime.datetime(
                    2000 + _bcd(win[j + 3]), _bcd(win[j + 4] & 0x1F),
                    _bcd(win[j + 6] & 0x3F), _bcd(win[j + 7] & 0x3F),
                    _bcd(win[j + 8] & 0x7F), _bcd(win[j + 9] & 0x7F))
            except (ValueError, IndexError):
                continue    # a false 0x18 hit inside video payload; keep looking
        pos = buf.find(b"MDPM", pos + 4)
    return None


def parse_mdpm(path, scan_bytes=8_000_000, chunk=262_144):
    """Recording START as a naive datetime, or None if no MDPM date/time found.

    ==Reads progressively, smallest window first.== The first MDPM block sits in
    the opening SEI — measured at byte 970 on the 2024-05 batch — so 256 KB
    answers essentially every file. That matters because the originals live on
    a slow NAS: at the old flat 8 MB read, indexing a 74-clip course meant
    pulling ~600 MB across the network to recover 20 bytes of clock. Only files
    that do not answer early pay for a wider read, up to `scan_bytes`.
    """
    try:
        with open(path, "rb") as fh:
            buf = b""
            while len(buf) < scan_bytes:
                more = fh.read(min(chunk, scan_bytes - len(buf)))
                if not more:
                    break
                # Re-scan from a little before the seam so an MDPM block split
                # across two reads is not missed.
                start = max(0, len(buf) - 260)
                buf += more
                got = _scan(buf[start:])
                if got:
                    return got
    except OSError:
        return None
    return None


def is_avchd(path):
    return str(path).lower().endswith(_EXT)
