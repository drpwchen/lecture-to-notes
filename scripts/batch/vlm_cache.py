# -*- coding: utf-8 -*-
"""vlm_cache.py — reuse prior VLM signals across a full re-cut via dhash.

The full-batch re-cut at a finer sampling interval re-extracts frames, but most
slide *content* is unchanged. Re-classifying every frame with the VLM (Haiku,
billed) is wasteful. This maps a stable content dhash -> prior vlm_signals so an
unchanged frame is reused for free; only genuinely new frames (page-turn
in-betweens, the needle-entry moment, etc.) fall through to the VLM.

Two subcommands:

  build  <golden_course_dir>  -o cache.json
      Scan clips/*/slides_vlm.json for entries that already carry vlm_signals.
      Use their dedup.dhash (from add_dhash) or compute it from the frame.
      Emit cache.json = [{dhash, signals}, ...] (dedup by dhash).

  apply  <work_dir> --cache cache.json [--threshold 6]
      Read <work_dir>/_haiku/manifest.json (from haiku_prep). For each frame,
      look up its dhash (clips/<clip>/frame_dhash.json, else compute) and match
      the nearest cache dhash within --threshold hamming.
        hit  -> append {gid, ...signals, source:"vlm-cache-reuse"} to cache_out.json
        miss -> keep in manifest_pending.json (same schema as manifest)
      Prints reuse rate. The orchestrator then VLM-reads only the pending frames,
      concatenates its results with cache_out.json into haiku_out.json, and runs
      haiku_apply.
"""
import json
import sys
from pathlib import Path

import imagehash
from PIL import Image


import re

# Zoom UI / weather / sign-in tokens that the old minicpm pass mis-captured as
# anatomical labels (lesson #3 pollution). Substring match; covers common OCR
# garbles (聊天->劇天, 舉手->最手/翠手/卑手, 視->检视).
ZOOM_UI = ["聊天", "劇天", "人員", "舉手", "最手", "翠手", "卑手", "取得控制權",
           "控制權", "檢視", "检视", "照相機", "照相格", "照相", "簽到", "保密",
           "視訊", "麥克風", "共享", "邀請", "錄製", "反應", "暫停", "結束會議",
           "多雲", "時睛", "未證", "靜音", "解除"]
_TIMECODE = re.compile(r"\d{1,2}[:.]\d{2}([:.]\d{2})?\s*(AM|PM)?|\d{2,4}[-/.]\d{1,2}[-/.]\d{1,2}|\d+°")


def clean_labels(labels):
    """Drop Zoom-UI / weather / timestamp noise from a visible_labels list."""
    if not labels:
        return labels
    out = []
    for l in labels:
        s = str(l).strip()
        if not s:
            continue
        if any(tok in s for tok in ZOOM_UI):
            continue
        if _TIMECODE.search(s) and len(s) <= 25:
            continue
        out.append(l)
    return out


# --- cache-paste guard (2026-07-24) ----------------------------------------
# 幀標記污染的根因：64-bit dhash 在「母版相同、只有內文不同」的連續投影片上碰撞，
# seed/apply 把別張投影片的 visible_labels 貼過來。防護：dhash 命中後，還要把
# cached labels 的 token 比對該 frame 自己的 quick OCR（與 audit_label_alignment.py
# 同一套指標），重疊率不足就降級為 miss（vlm_skip，交給後續 VLM pass）。
# 無標記 / OCR 太短的幀無從判斷 → 照舊放行（與稽核器只評 OCR≥60 字一致）。
GUARD_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-]{2,}|[一-鿿]{2,}")
GUARD_MIN_OVERLAP = 0.20
GUARD_MIN_OCR_CHARS = 60


def _norm_tokens(text):
    return {t.lower() for t in GUARD_TOKEN_RE.findall(text or "")}


def guard_ok(sig, ocr):
    """True = 這組 cached signals 可以貼到帶這個 ocr dict 的 frame 上。"""
    labels = [l for l in ((sig or {}).get("visible_labels") or [])
              if isinstance(l, str)]
    lt = _norm_tokens(" ".join(labels))
    if not lt:
        return True  # 無標記可核對（純影像類 signals），不致貼錯敘述
    ocr = ocr or {}
    ocr_text = " ".join(filter(None, [
        ocr.get("clean_text"), ocr.get("quick_text"),
        ocr.get("quick_title_guess")]))
    if len(ocr_text) < GUARD_MIN_OCR_CHARS:
        return True  # OCR 太短無從判斷（圖為主的畫面）
    return len(lt & _norm_tokens(ocr_text)) / len(lt) >= GUARD_MIN_OVERLAP


def dhash_of(img_path):
    with Image.open(img_path) as im:
        return imagehash.dhash(im.convert("RGB"), hash_size=8)


def _to_hash(hexstr):
    try:
        return imagehash.hex_to_hash(hexstr)
    except Exception:
        return None


def cmd_build(argv):
    out = "cache.json"
    if "-o" in argv:
        out = argv[argv.index("-o") + 1]
    wd = Path([a for a in argv if not a.startswith("-") and a != out][0])

    seen = set()
    cache = []
    n_clips = 0
    for vp in sorted(wd.glob("clips/*/slides_vlm.json")):
        n_clips += 1
        clipdir = vp.parent
        dmap = {}
        fdp = clipdir / "frame_dhash.json"
        if fdp.exists():
            dmap = json.load(open(fdp, encoding="utf-8"))
        arr = json.load(open(vp, encoding="utf-8"))
        for s in arr:
            if not isinstance(s, dict):
                continue
            sig = s.get("vlm_signals")
            if not sig:
                continue
            fn = s.get("filename")
            dh = (s.get("dedup", {}) or {}).get("dhash") or dmap.get(fn)
            if not dh and fn:
                src = clipdir / "slides" / fn
                if src.exists():
                    dh = str(dhash_of(src))
            if not dh or dh in seen:
                continue
            seen.add(dh)
            if isinstance(sig, dict) and sig.get("visible_labels"):
                sig = dict(sig)
                sig["visible_labels"] = clean_labels(sig["visible_labels"])
            cache.append({"dhash": dh, "signals": sig})
    Path(out).write_text(json.dumps(cache, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    print(f"cache built: {len(cache)} unique frames from {n_clips} clip(s) -> {out}")


def cmd_apply(argv):
    cache_path = argv[argv.index("--cache") + 1]
    threshold = 6
    if "--threshold" in argv:
        threshold = int(argv[argv.index("--threshold") + 1])
    wd = Path([a for a in argv if not a.startswith("-")
               and a not in (cache_path, str(threshold))][0])

    cache = json.load(open(cache_path, encoding="utf-8"))
    cache_h = [( _to_hash(c["dhash"]), c["signals"]) for c in cache]
    cache_h = [(h, s) for h, s in cache_h if h is not None]

    hd = wd / "_haiku"
    manifest = json.load(open(hd / "manifest.json", encoding="utf-8"))

    # per-clip dhash maps
    dmaps = {}

    def frame_dhash(clip, fn):
        if clip not in dmaps:
            fdp = wd / "clips" / clip / "frame_dhash.json"
            dmaps[clip] = json.load(open(fdp, encoding="utf-8")) if fdp.exists() else {}
        dh = dmaps[clip].get(fn)
        if dh:
            return _to_hash(dh)
        src = wd / "clips" / clip / "slides" / fn
        return dhash_of(src) if src.exists() else None

    # per-clip filename -> ocr dict (for the paste guard)
    omaps = {}

    def frame_ocr(clip, fn):
        if clip not in omaps:
            dp = wd / "clips" / clip / "slides_dedup.json"
            m = {}
            if dp.exists():
                for s in json.load(open(dp, encoding="utf-8")):
                    if isinstance(s, dict) and s.get("filename"):
                        m[s["filename"]] = s.get("ocr") or {}
            omaps[clip] = m
        return omaps[clip].get(fn)

    cache_out = []
    pending = []
    rejects = 0
    for m in manifest:
        h = frame_dhash(m["clip"], m["filename"])
        best = None
        bestd = 1 << 30
        if h is not None:
            for ch, sig in cache_h:
                d = h - ch
                if d < bestd:
                    bestd, best = d, sig
        if (best is not None and bestd <= threshold
                and not guard_ok(best, frame_ocr(m["clip"], m["filename"]))):
            rejects += 1
            best = None
        if best is not None and bestd <= threshold:
            entry = {"gid": m["gid"]}
            entry.update(best)
            entry["source"] = "vlm-cache-reuse"
            entry["_cache_hamming"] = bestd
            cache_out.append(entry)
        else:
            pending.append(m)

    (hd / "cache_out.json").write_text(
        json.dumps(cache_out, ensure_ascii=False, indent=2), encoding="utf-8")
    (hd / "manifest_pending.json").write_text(
        json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8")
    total = len(manifest)
    hits = len(cache_out)
    rate = (hits / total * 100) if total else 0.0
    print(f"vlm_cache apply: {hits}/{total} reused ({rate:.1f}%), "
          f"{rejects} guard-rejected, "
          f"{len(pending)} pending VLM -> {hd/'manifest_pending.json'}")


def cmd_seed(argv):
    """Headless: write slides_vlm.json for one clip directly from a dhash cache,
    NO LLM. Cache hits get their reused vlm_signals; misses are marked vlm_skip
    (a later billed pass classifies them). Lets ground_slides + build_L1 run
    unattended overnight on ~90%-covered signals.

      vlm_cache.py seed <clip_dir> --cache cache.json [--threshold 6]
    """
    cache_path = argv[argv.index("--cache") + 1]
    threshold = 6
    if "--threshold" in argv:
        threshold = int(argv[argv.index("--threshold") + 1])
    clip = Path([a for a in argv if not a.startswith("-")
                 and a not in (cache_path, str(threshold))][0])

    cache = json.load(open(cache_path, encoding="utf-8"))
    cache_h = [(_to_hash(c["dhash"]), c["signals"]) for c in cache]
    cache_h = [(h, s) for h, s in cache_h if h is not None]

    dedup = json.load(open(clip / "slides_dedup.json", encoding="utf-8"))
    fdp = clip / "frame_dhash.json"
    dmap = json.load(open(fdp, encoding="utf-8")) if fdp.exists() else {}

    hits = misses = rejects = 0
    for s in dedup:
        if not isinstance(s, dict):
            continue
        fn = s.get("filename")
        dh = (s.get("dedup", {}) or {}).get("dhash") or dmap.get(fn)
        h = _to_hash(dh) if dh else None
        best, bestd = None, 1 << 30
        if h is not None:
            for ch, sig in cache_h:
                d = h - ch
                if d < bestd:
                    bestd, best = d, sig
        rejected = (best is not None and bestd <= threshold
                    and not guard_ok(best, s.get("ocr")))
        if rejected:
            rejects += 1
            best = None
        if best is not None and bestd <= threshold:
            s["vlm_skip"] = False
            s["skip_reason"] = None
            s["vlm_signals"] = dict(best)
            s["vlm_signals"]["source"] = "vlm-cache-reuse"
            hits += 1
        else:
            s["vlm_skip"] = True
            s["skip_reason"] = "vlm_cache_guard_reject" if rejected else "vlm_cache_miss"
            s["vlm_signals"] = None
            misses += 1
        s["pipeline_stage"] = "vlm"
    json.dump(dedup, open(clip / "slides_vlm.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"seed {clip.name}: {hits} reused, {misses} miss(skip), "
          f"{rejects} guard-rejected -> slides_vlm.json")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("build", "apply", "seed"):
        print(__doc__)
        sys.exit(2)
    if sys.argv[1] == "build":
        cmd_build(sys.argv[2:])
    elif sys.argv[1] == "seed":
        cmd_seed(sys.argv[2:])
    else:
        cmd_apply(sys.argv[2:])


if __name__ == "__main__":
    main()
