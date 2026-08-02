# -*- coding: utf-8 -*-
"""Look at a folder of dropped lecture material and print the pipeline to run.

    python route_inputs.py <dir> [--recursive] [--json] [--out-dir DIR]

The skill has three entry paths (video / PDF deck / loose slide images) and the
combinations are where material gets dropped: an audio recording plus a folder
of phone photos is a perfectly good lecture, but only if the photos are ordered
by capture time and the audio is the timeline. This script classifies what is
actually in the folder and prints the ordered, copy-pastable commands for that
combination — including the questions the skill requires a human to answer.

==Plan only. This never executes anything and never writes a file.== It is a
router, so a wrong guess here should cost a re-read, not a 40-minute GPU run.

Two things are deliberately NOT defaulted:
  * ==the transcription language==, which is printed as a REQUIRED QUESTION.
    Guessing it wrong (--lang zh on English audio) makes Whisper hallucinate
    Chinese from accented English and the whole transcript is unusable.
  * ==the capture-time alignment==, whenever two or more independent sources
    are present. Step 0 then emits the hypotheses and the xcorr command that
    checks them, because filename order and mtime have both silently dropped
    and reordered material before.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

VIDEO_EXT = (".mp4", ".mov", ".mkv", ".avi", ".m4v", ".mts")
AUDIO_EXT = (".m4a", ".mp3", ".wav", ".flac", ".aac", ".ogg")
PDF_EXT = (".pdf",)
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff")
# Formats with no extraction path of their own: converting them is a separate
# decision (and a separate tool), not something this router should invent.
CONVERT_FIRST_EXT = (".pptx", ".ppt", ".docx", ".doc", ".key", ".odp")

# Below this, loose images read as incidental photos rather than a slide deck.
DECK_MIN_IMAGES = 3


def classify(d, recursive=False):
    """-> {"video": [...], "audio": [...], "pdf": [...], "image": [...],
           "convert_first": [...], "other": [...]} with absolute paths."""
    buckets = {"video": [], "audio": [], "pdf": [], "image": [],
               "convert_first": [], "other": []}
    if recursive:
        walked = []
        for root, _dirs, files in os.walk(d):
            walked += [os.path.join(root, f) for f in files]
        paths = sorted(walked)
    else:
        # scandir, not glob: a folder named "2026-07 [workshop]" is a character
        # class to glob and silently matches nothing.
        paths = sorted(os.path.join(d, e.name)
                       for e in os.scandir(d) if e.is_file())
    for p in paths:
        ext = os.path.splitext(p)[1].lower()
        if ext in VIDEO_EXT:
            buckets["video"].append(p)
        elif ext in AUDIO_EXT:
            buckets["audio"].append(p)
        elif ext in PDF_EXT:
            buckets["pdf"].append(p)
        elif ext in IMAGE_EXT:
            buckets["image"].append(p)
        elif ext in CONVERT_FIRST_EXT:
            buckets["convert_first"].append(p)
        else:
            buckets["other"].append(p)
    return buckets


def _q(p):
    """Quote a path for a copy-pastable command."""
    return f'"{p}"' if (" " in p or "(" in p) else p


def build_plan(buckets, in_dir, out_dir):
    """-> {"inputs": {...}, "questions": [...], "steps": [...], "warnings": [...]}

    Each step is {"n", "what", "why", "cmd"|None}. Commands are strings with
    real paths already substituted.
    """
    video, audio = buckets["video"], buckets["audio"]
    pdf, images = buckets["pdf"], buckets["image"]
    deck_images = images if len(images) >= DECK_MIN_IMAGES else []

    steps, warnings = [], []
    questions = []
    # Where the transcript Stage E grounds against lives; moves into a per-source
    # ASR subdir as soon as there is more than one recording.
    ground_dir = out_dir

    has_speech = bool(video or audio)
    if has_speech:
        questions.append(
            "REQUIRED QUESTION — what language did the speaker(s) use? "
            "(English / Mandarin / bilingual code-switching; any accented "
            "speakers?) There is no default: --lang guessed wrong makes Whisper "
            "hallucinate and the transcript is unusable. Ask the user, then "
            "substitute <LANG> below.")

    # How many independent capture devices/sources are in play.
    n_source_kinds = sum(bool(x) for x in (video, audio, deck_images))
    scripts = HERE

    def add(what, why, cmd=None):
        steps.append({"n": len(steps) + 1, "what": what, "why": why, "cmd": cmd})

    # --- Step 0: alignment backbone ----------------------------------
    if n_source_kinds >= 2 or len(video) + len(audio) >= 2:
        add("capture-time alignment index",
            "two or more independent sources: their capture starts are the only "
            "honest way to interleave them, and they are hypotheses that need "
            "the xcorr evidence check. Filename order and mtime have both "
            "silently reordered material before.",
            f'python {_q(os.path.join(scripts, "media_capture_index.py"))} '
            f'{_q(os.path.join(out_dir, "media_index.json"))} {_q(in_dir)} '
            f'--emit-alignment {_q(os.path.join(out_dir, "alignment.json"))}')
        warnings.append(
            "Read alignment.json before aligning anything: any source with "
            "reliable=false has no trustworthy capture start and must not be "
            "aligned on — ask the user for the real order instead.")

    # --- Transcription ------------------------------------------------
    speech_files = video + audio
    if speech_files:
        add("GPU pre-flight",
            "Whisper needs ~5-6 GB free VRAM; running it against a loaded model "
            "OOMs mid-stage or fragments silently.",
            f'python {_q(os.path.join(scripts, "gpu_check.py"))} '
            f'--out-dir {_q(out_dir)} --min-free-mb 6000')
        multi = len(speech_files) > 1
        for f in speech_files:
            # One ASR dir per source when there are several: they all write
            # transcript.json, so a shared out-dir means each run silently
            # overwrites the last and only the final source survives.
            f_out = (os.path.join(out_dir, "asr",
                                  os.path.splitext(os.path.basename(f))[0])
                     if multi else out_dir)
            add(f"transcribe {os.path.basename(f)}",
                "local faster-whisper, 0 Claude tokens. --lang is mandatory."
                + (" Separate out-dir per source: they all write transcript.json."
                   if multi else ""),
                f'python {_q(os.path.join(scripts, "transcribe_video.py"))} '
                f'{_q(f)} --output-dir {_q(f_out)} --lang <LANG> '
                f'--batch-size 3 --beam-size 10')
        first_speech = speech_files[0]
        asr_dir = (os.path.join(out_dir, "asr",
                                os.path.splitext(os.path.basename(first_speech))[0])
                   if multi else out_dir)
        ground_dir = asr_dir
        add("flag ASR suspects",
            "flags garbled terms for synthesis to verify. ==Never auto-corrects== "
            "— a confident wrong term is worse than a visible garble."
            + (" Run once per ASR dir." if multi else ""),
            f'python {_q(os.path.join(scripts, "flag_asr_suspects.py"))} '
            f'--dir {_q(asr_dir)}')
        if multi:
            add("measure the real offsets between the sources",
                "the capture times from step 0 are hypotheses; this is the "
                "measurement that confirms them. Needs every source transcribed "
                "(step above), which is why it comes here.",
                f'python {_q(os.path.join(scripts, "xcorr_media_offsets.py"))} '
                f'{_q(os.path.join(out_dir, "xcorr.json"))} '
                f'--clip-asr {_q(os.path.join(out_dir, "asr"))} '
                f'--recording <REC_NAME>=<ASR dir of the full-room source> '
                f'--index {_q(os.path.join(out_dir, "media_index.json"))}')
            warnings.append(
                "Multiple audio sources: decide which ONE is the note's timeline "
                "before Stage E, and feed xcorr.json back through "
                "media_capture_index --xcorr-results to see whether the capture "
                "times agree with the measurement. ==Never force an alignment "
                "the measurement contradicts.==")

    # --- Slides -------------------------------------------------------
    slide_route = None
    if pdf:
        slide_route = "pdf"
        add("extract PDF text + page renders",
            "PDF text is canonical: more accurate than OCR, faster, 0 tokens. "
            "Preferred over video frames whenever a deck exists.",
            f'# fitz: write {_q(os.path.join(out_dir, "pdf_text.json"))} + '
            f'render each page to {_q(os.path.join(out_dir, "slides"))}/'
            f'page_NN.jpg  (source: {_q(pdf[0])})')
        add("build slides from the PDF",
            "Path B bridge into Stage D/E.",
            f'python {_q(os.path.join(scripts, "build_slides_from_pdf.py"))} '
            f'{_q(out_dir)}'
            + (' --audio-duration-sec <SECONDS>' if speech_files else ''))
        if len(pdf) > 1:
            warnings.append(f"{len(pdf)} PDFs present — the command above uses "
                            "one lecture dir; run one lecture at a time.")
    elif deck_images:
        slide_route = "images"
        add(f"build slides from {len(deck_images)} images",
            "Path B-images bridge: orders by EXIF capture time (mtime and "
            "filename are the fallbacks and get flagged), then emits the same "
            "schema Stage D/E expect.",
            f'python {_q(os.path.join(scripts, "build_slides_from_images.py"))} '
            f'{_q(in_dir)} -o {_q(out_dir)}'
            + (' --audio-duration-sec <SECONDS>' if speech_files else ''))
        warnings.append(
            "Check the ordering summary that command prints. If it warns that "
            "capture-time and filename order disagree, look at the slides "
            "before synthesising anything.")
    elif video:
        slide_route = "video-frames"
        add("extract frames (Stage A)",
            "no deck available, so the slides come from the video itself.",
            f'python {_q(os.path.join(scripts, "extract_slides.py"))} '
            f'{_q(video[0])} --output-dir {_q(out_dir)}')
        add("quick OCR (Stage B)",
            "RapidOCR text + entropy metrics; required — without it every slide "
            "looks decorative to the Stage D gate.",
            f'python {_q(os.path.join(scripts, "quick_ocr.py"))} {_q(out_dir)}')
        add("semantic dedup (Stage C)",
            "collapses near-identical frames into canonical slides.",
            f'python {_q(os.path.join(scripts, "dedup_semantic.py"))} '
            f'{_q(out_dir)}')
    else:
        slide_route = None
        if speech_files:
            warnings.append(
                "No slides of any kind — this becomes a transcript-only note. "
                "If the user has a deck (PDF or photos), get it before "
                "synthesising: a transcript-only note loses every figure.")

    if slide_route:
        add("VLM signals (Stage D)",
            "semantic signals per canonical slide (no OCR — Stage B/PDF text is "
            "the text source). ==Never skip for speed.==",
            f'python {_q(os.path.join(scripts, "vlm_signals.py"))} {_q(out_dir)}')
        if speech_files:
            if ground_dir != out_dir:
                # ground_slides.py reads slides_vlm.json AND transcript.json from
                # one directory, so the chosen timeline's transcript has to be
                # the one sitting next to the slides.
                add("pick the timeline transcript",
                    "Stage E reads slides_vlm.json and transcript.json from the "
                    "SAME dir. Decide which source is the note's timeline (the "
                    "one whose clock the slides are aligned to) and put its "
                    "transcript there.",
                    f'copy {_q(os.path.join(ground_dir, "transcript.json"))} '
                    f'{_q(os.path.join(out_dir, "transcript.json"))}'
                    '   # <- swap in whichever ASR dir is the timeline')
            add("transcript grounding (Stage E)",
                "ties each slide to the words spoken over it. ==Never skip.==",
                f'python {_q(os.path.join(scripts, "ground_slides.py"))} '
                f'{_q(out_dir)}')
        add("Stage F synthesis (Claude)",
            "the only step that costs tokens: read the stage outputs and write "
            "the vault note, then render embeds + finalize.", None)

    # --- Things this router refuses to guess about ---------------------
    for p in buckets["convert_first"]:
        warnings.append(
            f"{os.path.basename(p)}: convert to PDF first (export from "
            "PowerPoint/Word), then re-run this router — there is no conversion "
            "step in this pipeline and adding one silently would be a guess "
            "about fonts and layout.")
    if images and not deck_images:
        warnings.append(
            f"{len(images)} image(s) present but fewer than {DECK_MIN_IMAGES} — "
            "treated as incidental photos, not a deck. Pass the folder to "
            "build_slides_from_images.py explicitly if they really are slides.")
    if len(video) > 1:
        warnings.append(
            f"{len(video)} videos: if they are segments of ONE talk, the "
            "capture-time alignment in step 0 is what orders them — do NOT "
            "concatenate by filename.")

    return {
        "input_dir": in_dir,
        "out_dir": out_dir,
        "inputs": {k: [os.path.basename(p) for p in v]
                   for k, v in buckets.items() if v},
        "slide_route": slide_route,
        "questions": questions,
        "steps": steps,
        "warnings": warnings,
    }


def print_plan(plan):
    print(f"material: {plan['input_dir']}")
    for k, v in plan["inputs"].items():
        shown = ", ".join(v[:6]) + (" …" if len(v) > 6 else "")
        print(f"  {k:<14} {len(v):>3}  {shown}")
    print(f"  slide route:   {plan['slide_route'] or 'NONE (transcript-only)'}")

    if plan["questions"]:
        print("\n== ask the user FIRST ==")
        for q in plan["questions"]:
            print(f"  ?? {q}")

    print("\n== plan ==")
    for s in plan["steps"]:
        print(f"\n  {s['n']}. {s['what']}")
        print(f"     why: {s['why']}")
        if s["cmd"]:
            print(f"     {s['cmd']}")
        else:
            print("     (no command — this step is Claude's)")

    if plan["warnings"]:
        print("\n== warnings ==")
        for w in plan["warnings"]:
            print(f"  !! {w}")
    print("\n(plan only — nothing was run and nothing was written)")


def main():
    ap = argparse.ArgumentParser(
        description="Classify dropped lecture material and print the pipeline "
                    "to run. Plan only — never executes anything.")
    ap.add_argument("dir", help="folder holding the dropped material")
    ap.add_argument("--recursive", action="store_true",
                    help="also scan subfolders (default: top level only)")
    ap.add_argument("--out-dir", default=None,
                    help="lecture output dir to put in the printed commands "
                         "(default: <dir>/_lecture)")
    ap.add_argument("--json", action="store_true",
                    help="emit the machine-readable plan instead of the "
                         "human-readable one")
    args = ap.parse_args()

    in_dir = os.path.abspath(args.dir)
    if not os.path.isdir(in_dir):
        print(f"ERROR: not a directory: {in_dir}", file=sys.stderr)
        sys.exit(2)
    out_dir = os.path.abspath(args.out_dir) if args.out_dir else os.path.join(
        in_dir, "_lecture")

    buckets = classify(in_dir, args.recursive)
    if not any(buckets[k] for k in ("video", "audio", "pdf", "image",
                                    "convert_first")):
        print(f"ERROR: no usable lecture material in {in_dir} "
              f"(looked for video/audio/pdf/images)", file=sys.stderr)
        sys.exit(2)

    plan = build_plan(buckets, in_dir, out_dir)
    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    else:
        print_plan(plan)


if __name__ == "__main__":
    main()
