"""Stage A: extract unique slide frames from a lecture video.

Strategy: interval sampling + perceptual hash dedup.
Scene detection alone often fails on lecture recordings because:
- Dark auditorium + projected slide = similar global structure
- Smooth transitions (animations, builds) don't trigger scene change

Instead: capture a frame every N seconds, then deduplicate using
perceptual hash on the CENTER-CROPPED image (ignoring dark borders).

Usage:
    python extract_slides.py <video_path> [--output-dir DIR] [--interval 15]
                             [--hash-threshold 40] [--group-drift-threshold N]

Outputs:
    slides/frame_NNNN.jpg   — representative frames (duplicates removed)
    slides/timestamps.json  — unique slide metadata [{slide, timestamp, ...}]

Exit codes: 0 ok · 1 bad input (missing video, zero frames) · 2 environment or
tool failure (ffmpeg missing / non-zero / timed out, dirty output directory).
"""
import argparse
import json
import os
import subprocess
import sys
import warnings

# Scoped, not blanket: large slide renders trip PIL's DecompressionBomb warning
# on every frame, which buried real warnings in the batch logs. Everything else
# (deprecations from PIL/numpy) stays visible.
try:
    from PIL import Image as _PILImage
    warnings.filterwarnings("ignore", category=_PILImage.DecompressionBombWarning)
except Exception:
    pass

try:
    from _common import fmt_hms, require_binaries
except ImportError:  # running the file from another cwd without scripts/ on path
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _common import fmt_hms, require_binaries

# Files this script itself creates inside slides/. Anything else in there was put
# there by a human or another tool (e.g. crop_multiup_pdf.py's slide_*.jpg) and
# must never be deleted by a rerun.
_OWN_JSON = {"timestamps.json"}


def frame_num_of(name):
    """`frame_0007.jpg` -> 7, or None if the name isn't ours."""
    stem = os.path.splitext(name)[0]
    if not stem.startswith("frame_"):
        return None
    try:
        return int(stem.split("_", 1)[1])
    except (IndexError, ValueError):
        return None


def frame_timestamp(frame_num, interval):
    """Seconds into the video for ffmpeg's Nth output frame.

    ffmpeg's ``fps=1/interval`` filter emits its FIRST frame at t=0 (it samples
    the stream from the start, it does not wait one period), and the image2
    muxer numbers output files from 1. So frame_0001 is t=0, frame_0002 is
    t=interval, and in general:

        t = (frame_num - 1) * interval

    The pre-2026-08-02 code used ``frame_num * interval``, which shifted every
    slide timestamp one full interval late (+15s at the default) and mis-aligned
    Stage E transcript grounding for every Path A run.
    """
    return max(0, (frame_num - 1)) * interval


def prepare_slides_dir(output_dir):
    """Create slides/ and refresh a previous run's own output.

    Deletion is pattern-scoped on purpose: only ``frame_*.jpg`` and this
    script's own ``timestamps.json`` are removed, and only when nothing else
    lives in the directory. If anything foreign is present (hand-picked stills,
    crop_multiup_pdf's slide_*.jpg, a deck's page_*.jpg) we refuse rather than
    delete someone else's work — the old code unconditionally removed every
    .jpg/.json/.txt in the folder.
    """
    slides_dir = os.path.join(output_dir, "slides")
    os.makedirs(slides_dir, exist_ok=True)

    entries = os.listdir(slides_dir)
    ours, foreign = [], []
    for f in entries:
        if frame_num_of(f) is not None and f.lower().endswith(".jpg"):
            ours.append(f)
        elif f in _OWN_JSON:
            ours.append(f)
        else:
            foreign.append(f)

    if foreign:
        print(f"ERROR: {slides_dir} contains {len(foreign)} file(s) this script did "
              f"not create: {', '.join(sorted(foreign)[:5])}"
              f"{' ...' if len(foreign) > 5 else ''}\n"
              "  Refusing to clean it. Move or delete them yourself, or pass a "
              "different --output-dir.", file=sys.stderr)
        sys.exit(2)

    for f in ours:
        os.remove(os.path.join(slides_dir, f))
    return slides_dir


def probe_duration(video_path):
    """Video duration in seconds, or None if ffprobe can't tell us."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            capture_output=True, text=True, timeout=60)
        return float(out.stdout.strip())
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def extract_frames(video_path, slides_dir, interval, timeout_s):
    """Extract frames at regular intervals using ffmpeg."""
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vf", f"fps=1/{interval}",
        "-q:v", "2",
        os.path.join(slides_dir, "frame_%04d.jpg")
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                timeout=timeout_s)
    except subprocess.TimeoutExpired:
        n_partial = len([f for f in os.listdir(slides_dir)
                         if frame_num_of(f) is not None])
        print(f"ERROR: ffmpeg timed out after {timeout_s}s "
              f"({n_partial} frames written so far — an incomplete set).\n"
              f"  Try a larger --interval (currently {interval}s), a longer "
              f"--ffmpeg-timeout, or split the video first.", file=sys.stderr)
        sys.exit(2)

    frame_files = sorted(f for f in os.listdir(slides_dir)
                         if frame_num_of(f) is not None and f.lower().endswith(".jpg"))

    if result.returncode != 0:
        # ffmpeg often exits non-zero on a truncated container while still having
        # decoded almost everything. Accept that ONLY when the frame count is
        # within 10% of what the duration predicts; otherwise this is a real
        # failure and continuing would produce a 3-frame "lecture".
        dur = probe_duration(video_path)
        expected = int(dur / interval) + 1 if dur else None
        stderr_tail = (result.stderr or "")[-800:]
        if expected and expected > 0 and len(frame_files) >= 0.9 * expected:
            print(f"WARNING: ffmpeg returned {result.returncode} but produced "
                  f"{len(frame_files)}/{expected} expected frames (within 10%) — "
                  f"continuing.\n  ffmpeg stderr tail:\n{stderr_tail}",
                  file=sys.stderr)
        else:
            got = f"{len(frame_files)}"
            want = f"{expected}" if expected else "unknown (ffprobe failed)"
            print(f"ERROR: ffmpeg returned {result.returncode}; extracted {got} "
                  f"frames, expected ~{want}.\n  ffmpeg stderr tail:\n{stderr_tail}",
                  file=sys.stderr)
            sys.exit(2)

    print(f"Extracted {len(frame_files)} frames at {interval}s intervals")
    return frame_files


def phash_cropped(img_path, size=32, crop_ratio=0.65):
    """Perceptual hash on center-cropped image (ignores dark borders)."""
    from PIL import Image
    with Image.open(img_path) as img:
        img = img.convert("L")
        w, h = img.size
        left = int(w * (1 - crop_ratio) / 2)
        top = int(h * (1 - crop_ratio) / 2)
        right = int(w * (1 + crop_ratio) / 2)
        bottom = int(h * (1 + crop_ratio) / 2)
        img = img.crop((left, top, right, bottom)).resize((size, size))
        pixels = list(img.getdata())
    avg = sum(pixels) / len(pixels)
    return "".join("1" if p > avg else "0" for p in pixels)


def hamming(h1, h2):
    return sum(c1 != c2 for c1, c2 in zip(h1, h2))


def dedup_frames(frame_files, slides_dir, interval, threshold=40,
                 drift_threshold=None):
    """Group consecutive similar frames, keep the last of each group.

    Two thresholds, not one. ``threshold`` is the step test (frame i vs the
    previous member) — that alone lets a slow build or an animation chain
    dozens of frames into one group, each step under the threshold while the
    first and last members share almost nothing. ``drift_threshold`` bounds the
    accumulated difference against the group's FIRST frame and splits the group
    when it is exceeded, so a long animated stretch yields several slides
    instead of one. Default is 2x the step threshold, which leaves ordinary
    static-slide grouping untouched (those frames sit far below the step
    threshold anyway) and only bites on genuine drift.
    """
    if drift_threshold is None:
        drift_threshold = threshold * 2
    hashes = [phash_cropped(os.path.join(slides_dir, f)) for f in frame_files]

    groups = []
    current_group = [0]
    for i in range(1, len(hashes)):
        step_ok = hamming(hashes[current_group[-1]], hashes[i]) < threshold
        drift_ok = hamming(hashes[current_group[0]], hashes[i]) < drift_threshold
        if step_ok and drift_ok:
            current_group.append(i)
        else:
            groups.append(current_group)
            current_group = [i]
    groups.append(current_group)

    unique_slides = []
    for g in groups:
        last_num = frame_num_of(frame_files[g[-1]])
        first_num = frame_num_of(frame_files[g[0]])
        ts = frame_timestamp(last_num, interval)
        first_ts = frame_timestamp(first_num, interval)
        unique_slides.append({
            "slide": len(unique_slides) + 1,
            "timestamp": ts,
            "time_str": fmt_hms(ts),
            "filename": frame_files[g[-1]],
            "group_size": len(g),
            "first_ts": first_ts
        })

    return unique_slides


def main():
    parser = argparse.ArgumentParser(description="Stage A: extract slides from video")
    parser.add_argument("video_path", help="Path to video file")
    parser.add_argument("--output-dir", "-o", default=None, help="Output directory")
    parser.add_argument("--interval", "-i", type=int, default=15,
                        help="Frame extraction interval in seconds (default: 15)")
    parser.add_argument("--hash-threshold", type=int, default=40,
                        help="Hamming distance threshold for consecutive-frame dedup "
                             "(default: 40, out of 1024 bits)")
    parser.add_argument("--group-drift-threshold", type=int, default=None,
                        help="Hamming distance from a group's FIRST frame that splits "
                             "the group; bounds chained drift on animated builds "
                             "(default: 2x --hash-threshold)")
    parser.add_argument("--ffmpeg-timeout", type=int, default=600,
                        help="Seconds to allow ffmpeg frame extraction (default: 600)")
    args = parser.parse_args()

    if args.interval <= 0:
        print("ERROR: --interval must be >= 1 second", file=sys.stderr)
        sys.exit(1)

    if not os.path.isfile(args.video_path):
        print(f"ERROR: File not found: {args.video_path}", file=sys.stderr)
        sys.exit(1)

    # Preflight before touching the output dir: a missing ffmpeg used to surface
    # as a bare FileNotFoundError traceback after the directory was already wiped.
    require_binaries("ffmpeg")

    output_dir = args.output_dir or os.path.dirname(os.path.abspath(args.video_path))

    slides_dir = prepare_slides_dir(output_dir)

    # Step 1: Extract frames
    frame_files = extract_frames(args.video_path, slides_dir, args.interval,
                                 args.ffmpeg_timeout)

    if not frame_files:
        # Clip shorter than interval → grab a single representative frame at
        # midpoint so short demo/case stubs don't hard-fail the whole course.
        # Its timestamp is reported as 0 (frame_0001), which is the correct
        # convention for a clip with only one slide.
        try:
            subprocess.run(["ffmpeg", "-y", "-i", args.video_path, "-vf", "thumbnail",
                            "-frames:v", "1", os.path.join(slides_dir, "frame_0001.jpg")],
                           capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            print("ERROR: ffmpeg thumbnail fallback timed out after 120s",
                  file=sys.stderr)
            sys.exit(2)
        frame_files = sorted(f for f in os.listdir(slides_dir)
                             if frame_num_of(f) is not None and f.lower().endswith(".jpg"))

    if not frame_files:
        print("ERROR: No frames extracted", file=sys.stderr)
        sys.exit(1)

    # Step 2: Deduplicate
    unique_slides = dedup_frames(frame_files, slides_dir, args.interval,
                                 args.hash_threshold, args.group_drift_threshold)
    print(f"Deduplicated: {len(frame_files)} frames → {len(unique_slides)} unique slides")

    # Step 3: Save metadata
    ts_path = os.path.join(slides_dir, "timestamps.json")
    with open(ts_path, "w", encoding="utf-8") as f:
        json.dump(unique_slides, f, ensure_ascii=False, indent=2)

    # Step 4: Remove non-representative frames to save space and speed up OCR.
    # Scoped to frame_*.jpg this run produced — see prepare_slides_dir.
    representative = {s["filename"] for s in unique_slides}
    removed = 0
    for f in frame_files:
        if f not in representative:
            os.remove(os.path.join(slides_dir, f))
            removed += 1
    if removed:
        print(f"Cleaned up {removed} duplicate frames, kept {len(representative)} representative slides")

    for s in unique_slides:
        print(f"  Slide {s['slide']:2d}: [{fmt_hms(s['first_ts'])}-{s['time_str']}] "
              f"{s['filename']} ({s['group_size']} frames)")

    print(f"\nDone: {len(unique_slides)} slides → {ts_path}")


if __name__ == "__main__":
    main()
