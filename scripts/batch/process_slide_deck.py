# -*- coding: utf-8 -*-
"""process_slide_deck.py — standardized PPTX/PDF slide-deck processing
(2026-07-04, closes the gap the pipeline audit flagged: pptx files were only
ever counted in manifest.json's `slides` list, never opened; PDF files got
build_pdf_text()'d but that JSON was never consumed downstream either).

This does NOT try to time-align a deck's pages to video frames — the earlier
pilot (pdf_slide_match_pilot_2026-07-04) found real multi-to-one mismatch risk
in text-similarity matching, so that stays a separate, human-checked track.
This script instead treats the deck as its OWN standalone appendix, the same
"Path B" contract build_slides_from_pdf.py already defines for video-less
lectures — build_L1.py appends it as an unordered reference section, not
interleaved into any clip's timeline.

For a .pptx/.ppt/.key: convert to PDF via LibreOffice headless first.
For a .pdf: used directly.

Usage: python process_slide_deck.py <course_dir> <relative_or_absolute_path_to_deck>
Output: <course_dir>/_decks/<deck_stem>/{slides/page_NN.jpg, pdf_text.json, slides_dedup.json}
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

SK = Path(__file__).resolve().parent.parent  # skill scripts/ dir (this file lives in scripts/batch/)
SOFFICE = Path("C:/Program Files/LibreOffice/program/soffice.exe")
PY = sys.executable


def convert_to_pdf(src_path, out_dir):
    """soffice --headless --convert-to pdf. Returns the produced pdf Path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [str(SOFFICE), "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(src_path)],
        capture_output=True, text=True, timeout=300,
    )
    pdf_path = out_dir / (src_path.stem + ".pdf")
    if r.returncode != 0 or not pdf_path.exists():
        raise RuntimeError(f"soffice convert failed rc={r.returncode}: {r.stdout[-300:]} {r.stderr[-300:]}")
    return pdf_path


def render_pages_and_text(pdf_path, deck_dir):
    import fitz
    slides_dir = deck_dir / "slides"
    slides_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(str(pdf_path))
    pages = []
    for i, page in enumerate(doc):
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))  # ~144dpi, matches quick_ocr's scale
        img_name = f"page_{i+1:02d}.jpg"
        pix.save(str(slides_dir / img_name))
        pages.append({"page": i + 1, "text": page.get_text().strip(), "image_filename": img_name})
    (deck_dir / "pdf_text.json").write_text(
        json.dumps(pages, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(pages)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("course_dir")
    ap.add_argument("deck_path", help="path to the .pptx/.ppt/.key/.pdf, relative to course source dir or absolute")
    args = ap.parse_args()

    course_dir = Path(args.course_dir)
    manifest = json.load(open(course_dir / "manifest.json", encoding="utf-8"))
    src_dir = Path(manifest["path"])

    deck_path = Path(args.deck_path)
    if not deck_path.is_absolute():
        deck_path = src_dir / deck_path
    if not deck_path.exists():
        print(f"ERROR: deck not found: {deck_path}", file=sys.stderr)
        sys.exit(1)

    deck_dir = course_dir / "_decks" / deck_path.stem
    if (deck_dir / "slides_dedup.json").exists():
        print(f"skip (already processed): {deck_dir}")
        return

    if deck_path.suffix.lower() in (".pptx", ".ppt", ".key"):
        conv_dir = deck_dir / "_converted"
        pdf_path = convert_to_pdf(deck_path, conv_dir)
        print(f"converted {deck_path.name} -> {pdf_path}")
    elif deck_path.suffix.lower() == ".pdf":
        pdf_path = deck_path
    else:
        print(f"ERROR: unsupported deck type {deck_path.suffix}", file=sys.stderr)
        sys.exit(1)

    n = render_pages_and_text(pdf_path, deck_dir)
    print(f"rendered {n} pages -> {deck_dir}")

    r = subprocess.run([PY, str(SK / "build_slides_from_pdf.py"), str(deck_dir)],
                        capture_output=True, text=True, timeout=300)
    print(r.stdout.strip())
    if r.returncode != 0:
        print(f"build_slides_from_pdf FAILED rc={r.returncode}: {r.stderr[-500:]}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
