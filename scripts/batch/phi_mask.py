# -*- coding: utf-8 -*-
"""phi_mask.py <image | dir>  [--out PATH] [--band 0.06] [--mode blur|black] [--inplace]

Redact the Zoom top UI band (participant sign-in names = PHI) before any frame
is output/synced to collaborators. 0 Claude tokens.

The Zoom meeting toolbar + participant strip sits in the top ~6% of the frame on
this batch's recordings. Simple, robust approach: blur (default) or black-fill a
horizontal band across the top. Blur preserves the slide's visual context while
making any sign-in text unreadable.

Usage:
  python phi_mask.py frame.jpg --out frame_masked.jpg
  python phi_mask.py D:/.../_L3/figures --out D:/.../_L3/figures_phi   # dir -> dir
  python phi_mask.py D:/.../figures --inplace                          # overwrite
"""
import sys
from pathlib import Path

from PIL import Image, ImageFilter


def mask_image(src, dst, band=0.06, mode="blur"):
    im = Image.open(src).convert("RGB")
    w, h = im.size
    bh = max(1, int(h * band))
    top = im.crop((0, 0, w, bh))
    if mode == "black":
        top = Image.new("RGB", (w, bh), (0, 0, 0))
    else:
        # heavy blur, enough that any text is unrecoverable
        top = top.resize((max(1, w // 20), max(1, bh // 4))).resize((w, bh))
        top = top.filter(ImageFilter.GaussianBlur(8))
    im.paste(top, (0, 0))
    dst.parent.mkdir(parents=True, exist_ok=True)
    im.save(dst, quality=90)


def main():
    argv = sys.argv[1:]
    band = 0.06
    mode = "blur"
    out = None
    inplace = "--inplace" in argv
    if "--band" in argv:
        band = float(argv[argv.index("--band") + 1])
    if "--mode" in argv:
        mode = argv[argv.index("--mode") + 1]
    if "--out" in argv:
        out = argv[argv.index("--out") + 1]
    pos = [a for a in argv if not a.startswith("--")
           and a not in ([out] if out else []) and a not in (str(band), mode)]
    src = Path(pos[0])

    if src.is_dir():
        dst_dir = Path(out) if out else src
        n = 0
        for jpg in sorted(list(src.glob("*.jpg")) + list(src.glob("*.png"))):
            dst = (jpg if inplace else dst_dir / jpg.name)
            mask_image(jpg, dst, band, mode)
            n += 1
        print(f"phi_mask: {n} images -> {dst_dir} (band={band}, {mode})")
    else:
        dst = Path(src if inplace else (out or src.with_name(src.stem + "_masked" + src.suffix)))
        mask_image(src, dst, band, mode)
        print(f"phi_mask: {src.name} -> {dst} (band={band}, {mode})")


if __name__ == "__main__":
    main()
