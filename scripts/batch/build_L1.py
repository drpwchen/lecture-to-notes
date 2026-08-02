#!/usr/bin/env python3
"""build_L1.py <xlf_dir> [--granularity coarse|fine|both]

L1 = transcript↔slide complete alignment note. Pure script, 0 Claude tokens,
zero reordering, zero loss. Every slide frame + every transcript sentence (with
timestamp) interleaved by time, per clip.

  coarse : canonical (deduped) frames only — one per distinct view (~clean visible_labels)
  fine   : ALL sampled frames (~every 15s) — does NOT trust dedup; nothing dropped

Output → <xlf_dir>/_L1/  (NOT the vault):
  L1_coarse.md / L1_fine.md   + figures/  (cited frames copied, shared superset)
"""
import json, os, sys, glob, shutil


def ts(s):
    s = int(s or 0)
    return f"{s // 60:02d}:{s % 60:02d}"


def build(xlf_dir, gran, fig_dir, fig_rel):
    man = json.load(open(os.path.join(xlf_dir, "manifest.json"), encoding="utf-8"))
    out = [f"# L1 逐字稿↔投影片對應 — {man['name']}",
           f"- 課程:{man['name']}",
           f"granularity = **{gran}** | 純腳本對齊,逐字稿原文(含 ASR 誤字),圖文按時間嚴格對齊\n"]
    copied = set()
    for ci, c in enumerate(man["clips"]):
        cdir = os.path.join(xlf_dir, "clips", c["name"])
        gp = os.path.join(cdir, "slides_grounded.json")
        tp = os.path.join(cdir, "transcript.json")
        if not os.path.exists(gp):
            # audio-only clip (no slides): emit transcript-only L1
            segs = json.load(open(tp, encoding="utf-8")) if os.path.exists(tp) else []
            if segs:
                out.append(f"\n## clip {ci:02d} — {c['name']} [{c.get('src', '?')}] (純音訊)\n")
                for s in segs:
                    txt = (s.get("text") or "").strip()
                    if txt:
                        out.append(f"`{ts(s.get('start', 0))}` {txt}")
            continue
        g = json.load(open(gp, encoding="utf-8"))
        slides = g if isinstance(g, list) else g.get("slides", [])
        segs = json.load(open(tp, encoding="utf-8")) if os.path.exists(tp) else []
        if gran == "coarse":
            slides = [s for s in slides if (s.get("dedup", {}) or {}).get("is_canonical")]
        # 2026-07-04 fix: used to drop EVERY vlm_skip frame outright (a
        # vlm_cache-miss slide has no vlm_signals yet, but its image + quick_text
        # OCR are already there) — this silently deleted content wholesale on
        # courses with low dhash reuse (memory: "US-dynamic courses ... low dhash
        # reuse"), and did so even in `fine` granularity despite its docstring
        # promising "nothing dropped". Keep the frame; render_slide_event() below
        # falls back to raw quick_text when vlm_signals/visible_labels are absent.

        # `src` 一併印出：clip 編號 = manifest 陣列位置 = AVCHD 流水號時序
        # (clip_order.py)，但檔名才是分段提案要引用的真實來源 —— 印在標題上，
        # 分段 agent 就不必自己回去對照 manifest。
        out.append(f"\n## clip {ci:02d} — {c['name']} [{c.get('src', '?')}]\n")
        if not segs and not slides:
            out.append("(此 clip 無逐字稿與投影片 — 可能為靜音/空白片段)\n")
            continue

        # cover span: each slide covers from its start until the next slide's start
        ss = sorted(slides, key=lambda s: s.get("timestamp_start", 0))
        cover = {}
        for i, s in enumerate(ss):
            cover[id(s)] = ss[i + 1].get("timestamp_start") if i + 1 < len(ss) else None

        ev = [(s.get("timestamp_start", 0), 0, "slide", s) for s in slides]
        ev += [(float(s.get("start", 0)), 1, "seg", s) for s in segs]
        ev.sort(key=lambda x: (x[0], x[1]))

        for t, _, kind, o in ev:
            if kind == "slide":
                fn = o.get("filename", "")
                vs = o.get("vlm_signals", {}) or {}
                canon = (o.get("dedup", {}) or {}).get("is_canonical")
                labels = vs.get("visible_labels", []) or []
                ctype = ",".join(vs.get("content_type", []) or []) or "?"
                tag = f"c{ci:02d}_{fn}"
                src = os.path.join(cdir, "slides", fn)
                if os.path.exists(src) and tag not in copied:
                    shutil.copy2(src, os.path.join(fig_dir, tag))
                    copied.add(tag)
                mark = "" if canon else " ·近似帧"
                if o.get("vlm_skip"):
                    mark += " ·VLM未處理"
                ce = cover.get(id(o))
                span = f"{ts(t)}-{ts(ce)}" if ce else f"{ts(t)}→"
                out.append(f"\n**🎞 {span} — {fn}** `[{ctype}{mark}]`")
                out.append(f"![{tag}]({fig_rel}/{tag})")
                if labels:
                    out.append("> 頁面標記: " + " / ".join(labels[:10]))
                elif o.get("vlm_skip") or not vs:
                    # No VLM-cleaned labels available. This covers BOTH vlm_skip
                    # (cache-miss / decorative) AND vlm_failed frames (2026-07-09,
                    # WP1-2: process_slide sets vlm_signals=None on VLM error, so
                    # `not vs` — degrade to raw quick_text instead of silently
                    # dropping the frame's text). The frame image is always kept
                    # above; here we recover whatever OCR text exists.
                    qt = ((o.get("ocr", {}) or {}).get("quick_text") or "").strip()
                    if qt:
                        out.append("> 頁面文字(原始OCR,未經VLM分類): " +
                                    " / ".join(qt.splitlines()[:6]))
                out.append("")
            else:
                txt = (o.get("text") or "").strip()
                if txt:
                    out.append(f"`{ts(o.get('start',0))}` {txt}")

    # 2026-07-04: append any standalone slide decks (PPTX/PDF processed via
    # process_slide_deck.py) as a separate appendix. These have NO time
    # relationship to the clip transcripts above — never claim one; the deck's
    # own page order is the only ordering signal available.
    decks_root = os.path.join(xlf_dir, "_decks")
    if os.path.isdir(decks_root):
        for deck_name in sorted(os.listdir(decks_root)):
            dp = os.path.join(decks_root, deck_name, "slides_dedup.json")
            if not os.path.exists(dp):
                continue
            pages = json.load(open(dp, encoding="utf-8"))
            out.append(f"\n## 附錄：投影片檔案「{deck_name}」（非影片來源，未與逐字稿時間軸對齊，僅依頁碼順序）\n")
            for p in pages:
                fn = p.get("filename", "")
                tag = f"deck_{deck_name}_{fn}"
                src = os.path.join(decks_root, deck_name, "slides", fn)
                if os.path.exists(src) and tag not in copied:
                    shutil.copy2(src, os.path.join(fig_dir, tag))
                    copied.add(tag)
                text = (p.get("ocr", {}) or {}).get("quick_text", "").strip()
                out.append(f"\n**📄 p.{p.get('slide_id', '?')} — {fn}**")
                out.append(f"![{tag}]({fig_rel}/{tag})")
                if text:
                    out.append("> " + " / ".join(text.splitlines()[:6]))
                out.append("")

    return "\n".join(out), len(copied)


def main(xlf_dir, gran="both"):
    xlf_dir = xlf_dir.rstrip("/\\")
    l1 = os.path.join(xlf_dir, "_L1")
    fig_dir = os.path.join(l1, "figures")
    os.makedirs(fig_dir, exist_ok=True)
    grans = ["coarse", "fine"] if gran == "both" else [gran]
    for gr in grans:
        md, nfig = build(xlf_dir, gr, fig_dir, "figures")
        p = os.path.join(l1, f"L1_{gr}.md")
        open(p, "w", encoding="utf-8").write(md)
        print(f"  L1_{gr}.md  chars={len(md)} figs_copied~{nfig} → {p}")


if __name__ == "__main__":
    g = "both"
    if "--granularity" in sys.argv:
        g = sys.argv[sys.argv.index("--granularity") + 1]
    main(sys.argv[1], g)
