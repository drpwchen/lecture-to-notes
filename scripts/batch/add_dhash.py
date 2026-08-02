# -*- coding: utf-8 -*-
"""add_dhash.py <clip_dir | course_work_dir>  [--hash-size 8]

Compute a difference-hash (dhash) for every slide frame on the ORIGINAL frame
content (NOT the center-cropped phash used for dedup) so the hash is stable
across re-cuts that change crop/resize/sampling-interval. 0 Claude tokens.

Why a separate hash: extract_slides' phash is on a 65% center crop and is only
used for adjacent-frame dedup. For VLM-signal reuse across a full re-cut we need
a content hash of the whole frame; dhash (gradient of a 9x8 grayscale) is robust
to JPEG re-encoding and minor resampling.

For each clip dir (one, or all clips under a course):
  - reads slides/*.jpg
  - writes frame_dhash.json = {filename: dhash_hex}
  - if slides_dedup.json / slides_vlm.json exist, injects dedup.dhash into each
    entry whose frame still exists (idempotent overwrite).

Usage:
  python add_dhash.py D:/.../clips/00_00009
  python add_dhash.py D:/.../<course>        # all clips
"""
import json
import sys
from pathlib import Path

import imagehash
from PIL import Image


def dhash_hex(img_path, hash_size=8):
    with Image.open(img_path) as im:
        return str(imagehash.dhash(im.convert("RGB"), hash_size=hash_size))


def process_clip(clipdir, hash_size=8):
    slides = clipdir / "slides"
    if not slides.is_dir():
        return None
    dmap = {}
    for jpg in sorted(slides.glob("*.jpg")):
        try:
            dmap[jpg.name] = dhash_hex(jpg, hash_size)
        except Exception as e:
            print(f"  WARN dhash {jpg.name}: {e}", file=sys.stderr)
    (clipdir / "frame_dhash.json").write_text(
        json.dumps(dmap, ensure_ascii=False, indent=2), encoding="utf-8")

    # inject into dedup / vlm jsons if present
    for name in ("slides_dedup.json", "slides_vlm.json"):
        p = clipdir / name
        if not p.exists():
            continue
        try:
            arr = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        changed = False
        for s in arr:
            if isinstance(s, dict) and s.get("filename") in dmap:
                s.setdefault("dedup", {})["dhash"] = dmap[s["filename"]]
                changed = True
        if changed:
            json.dump(arr, open(p, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
    return len(dmap)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    hs = 8
    if "--hash-size" in sys.argv:
        hs = int(sys.argv[sys.argv.index("--hash-size") + 1])
    root = Path(args[0])

    clipdirs = []
    if (root / "slides").is_dir():
        clipdirs = [root]
    else:
        clipdirs = [d for d in sorted((root / "clips").glob("*")) if (d / "slides").is_dir()]

    total = 0
    for cd in clipdirs:
        n = process_clip(cd, hs)
        if n is not None:
            total += n
            print(f"  {cd.name}: {n} frames hashed")
    print(f"dhash done: {total} frames across {len(clipdirs)} clip(s)")


if __name__ == "__main__":
    main()
