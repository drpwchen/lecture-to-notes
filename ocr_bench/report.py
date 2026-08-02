"""Aggregate ocr_bench/results/*.json into a side-by-side report.md.

Production KPIs only. Highlights the deterministic-stability and runaway columns
because those — not raw CER — are what decide the engine for this pipeline.
"""
import json, os, glob, sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")


def main():
    files = sorted(glob.glob(os.path.join(RES, "*.json")))
    if not files:
        print("no results/*.json — run run_bench.py first")
        return
    rows = [json.load(open(f, encoding="utf-8"))["summary"] for f in files]

    cols = [
        ("engine", "engine"),
        ("json_valid_rate", "json_valid ⭐"),
        ("error_rate", "error_rate ⭐"),
        ("max_repetition_score", "max_rep ⭐"),
        ("mean_stability_cv", "stability_cv ⭐"),
        ("mean_text_chars", "chars"),
        ("mean_n_blocks", "blocks"),
        ("mean_confidence", "conf"),
        ("mean_latency_s", "latency_s ⭐"),
    ]
    lines = ["# OCR benchmark report", "",
             "Production KPIs on `fixtures/` (28 slides). ⭐ = decisive. "
             "Lower is better for error_rate, max_rep, stability_cv, latency; "
             "higher for json_valid, chars, blocks, conf.", "",
             "| " + " | ".join(h for _, h in cols) + " |",
             "|" + "|".join("---" for _ in cols) + "|"]
    for r in rows:
        cells = []
        for key, _ in cols:
            v = r.get(key, "")
            cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")

    # per-category stability (pathological slides must be stable)
    lines += ["", "## Stability CV by category (run-to-run; ~0 = deterministic)", ""]
    cats = set()
    for r in rows:
        cats |= set((r.get("stability_cv_by_category") or {}))
    cats = sorted(cats)
    lines.append("| engine | " + " | ".join(cats) + " |")
    lines.append("|---|" + "|".join("---" for _ in cats) + "|")
    for r in rows:
        sc = r.get("stability_cv_by_category") or {}
        lines.append("| " + r["engine"] + " | " + " | ".join(str(sc.get(c, "")) for c in cats) + " |")

    lines += ["", "## Reading",
              "- A generative engine with high `max_rep` or non-zero `stability_cv` "
              "on mri/sono/photo is reintroducing the autoregressive runaway HARD RULE #6 removed — reject as default.",
              "- Prefer the engine with json_valid=1.0, ~0 stability_cv, low max_rep, "
              "and the best text yield on dense_text_* / table. Route the other as a "
              "per-slide confidence fallback, not a global swap."]

    out = os.path.join(HERE, "report.md")
    open(out, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\n-> {out}")


if __name__ == "__main__":
    main()
