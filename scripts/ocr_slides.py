"""DEPRECATED shim — this stage was renamed to ``vlm_signals.py`` (2026-08-02).

The old name said "OCR", but this stage never did OCR: it asks a VLM for
STRUCTURED SIGNALS (content_type, visual_complexity, ...). The misnomer was the
root of the "we have three duplicate OCR scripts" confusion. Real OCR lives in
quick_ocr.py (Stage B, RapidOCR) and ocr_surya.py (Stage B2, Surya).

Kept because existing callers (batch runners, SKILL.md examples) invoke
this path. Same argv, same exit code, same output files.
"""
import os
import runpy
import sys

_TARGET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vlm_signals.py")

print("DEPRECATED: ocr_slides.py was renamed to vlm_signals.py (it emits VLM "
      "signals, not OCR text). Update your call; this shim will be removed.",
      file=sys.stderr)

sys.argv[0] = _TARGET
runpy.run_path(_TARGET, run_name="__main__")
