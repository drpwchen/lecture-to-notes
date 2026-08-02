"""Run one OCR adapter over the fixture set N times and compute production KPIs.

Adapters run as SUBPROCESSES (optionally via a separate venv python) so each
engine's torch/CUDA/paddle deps stay isolated. Output schema is validated by
schema.py — schema-validity is itself a KPI.

KPIs (production-oriented, not paper CER):
  json_valid_rate      ⭐ fraction of (image x run) with schema-valid output
  error_rate           ⭐ fraction with an adapter error
  repetition_score     ⭐ max repeated-3gram fraction in extracted text (runaway proxy)
  stability_cv         ⭐ mean per-fixture coeff-of-variation of char count across runs
                          (pathological MRI/sono/photo should be ~0)
  latency_s            ⭐ mean per-image wall time
  mean_confidence        engine self-reported
  text_chars / n_blocks  yield proxies for recall

Usage:
  python run_bench.py --engine rapidocr [--python <venv_python>] [--runs 3]
  python run_bench.py --engine surya --python C:\\path\\venv\\Scripts\\python.exe
"""
import argparse, json, os, re, subprocess, sys, statistics
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import schema  # noqa: E402

FIX_DIR = os.path.join(HERE, "fixtures")
RES_DIR = os.path.join(HERE, "results")
# Values are paths relative to HERE. surya_adapter now lives under
# scripts/adapters/ because production (ocr_surya.py Stage B2) runs it — the
# benchmark borrows the production adapter, never the reverse (moved 2026-08-02).
ADAPTERS = {
    "rapidocr": os.path.join("adapters", "rapidocr_adapter.py"),
    "surya": os.path.join("..", "scripts", "adapters", "surya_adapter.py"),
    "paddleocr_vl": os.path.join("adapters", "paddleocr_vl_adapter.py"),
}


def repetition_score(text):
    """Max fraction a single 3-gram occupies — high = runaway/looping output."""
    toks = re.findall(r"\w+", text.lower())
    if len(toks) < 6:
        return 0.0
    grams = Counter(tuple(toks[i:i + 3]) for i in range(len(toks) - 2))
    return round(max(grams.values()) / max(1, len(grams)), 3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", required=True, choices=list(ADAPTERS))
    ap.add_argument("--python", default=sys.executable, help="interpreter for the adapter (venv)")
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()

    manifest = {m["fixture"]: m for m in
                json.load(open(os.path.join(HERE, "fixtures_manifest.json"), encoding="utf-8"))}
    fixtures = sorted(f for f in os.listdir(FIX_DIR) if f.lower().endswith((".jpg", ".png")))
    paths = [os.path.join(FIX_DIR, f) for f in fixtures]
    adapter = os.path.abspath(os.path.join(HERE, ADAPTERS[args.engine]))
    if not os.path.isfile(adapter):
        print(f"ERROR: adapter for '{args.engine}' not found at {adapter}",
              file=sys.stderr)
        sys.exit(2)
    os.makedirs(RES_DIR, exist_ok=True)

    # per-fixture list of run records
    by_fix = {f: [] for f in fixtures}
    n_valid = n_total = n_error = 0

    for r in range(args.runs):
        proc = subprocess.run([args.python, adapter, *paths],
                              capture_output=True, text=True, encoding="utf-8")
        if proc.returncode != 0:
            print(f"  run {r}: adapter exited {proc.returncode}\n{proc.stderr[-800:]}", file=sys.stderr)
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                n_total += 1
                continue
            n_total += 1
            ok, _ = schema.validate(d)
            if ok:
                n_valid += 1
            if d.get("error"):
                n_error += 1
            fx = d.get("fixture")
            if fx in by_fix:
                txt = schema.full_text(d)
                terms = manifest.get(fx, {}).get("expected_terms") or []
                low = txt.lower()
                recall = (sum(1 for t in terms if str(t).lower() in low) / len(terms)
                          if terms else None)
                by_fix[fx].append({
                    "valid": ok, "error": d.get("error"),
                    "chars": len(txt), "n_blocks": len(d.get("blocks", [])),
                    "rep": repetition_score(txt),
                    "conf": d.get("page_confidence", 0.0),
                    "latency": d.get("latency_s", 0.0),
                    "recall": recall,
                })
        print(f"  run {r+1}/{args.runs} done")

    # aggregate
    def cv(vals):
        vals = [v for v in vals if v is not None]
        if len(vals) < 2 or statistics.mean(vals) == 0:
            return 0.0
        return round(statistics.pstdev(vals) / statistics.mean(vals), 3)

    per_fix = {}
    cat_stability = {}
    all_chars, all_blocks, all_rep, all_conf, all_lat, all_cv = [], [], [], [], [], []
    all_recall = []
    for fx, runs in by_fix.items():
        if not runs:
            per_fix[fx] = {"category": manifest.get(fx, {}).get("category"), "runs": 0}
            continue
        chars = [x["chars"] for x in runs]
        cat = manifest.get(fx, {}).get("category", "?")
        stab = cv(chars)
        per_fix[fx] = {
            "category": cat, "runs": len(runs),
            "mean_chars": round(statistics.mean(chars), 1),
            "mean_blocks": round(statistics.mean(x["n_blocks"] for x in runs), 1),
            "max_rep": max(x["rep"] for x in runs),
            "mean_conf": round(statistics.mean(x["conf"] for x in runs), 3),
            "mean_latency": round(statistics.mean(x["latency"] for x in runs), 3),
            "stability_cv": stab,
            "any_error": any(x["error"] for x in runs),
        }
        all_chars.append(statistics.mean(chars))
        all_blocks.append(statistics.mean(x["n_blocks"] for x in runs))
        all_rep.append(max(x["rep"] for x in runs))
        all_conf.append(statistics.mean(x["conf"] for x in runs))
        all_lat.append(statistics.mean(x["latency"] for x in runs))
        all_cv.append(stab)
        cat_stability.setdefault(cat, []).append(stab)
        rec_vals = [x["recall"] for x in runs if x.get("recall") is not None]
        if rec_vals:
            all_recall.append(statistics.mean(rec_vals))

    summary = {
        "engine": args.engine, "runs": args.runs, "n_fixtures": len(fixtures),
        "json_valid_rate": round(n_valid / n_total, 3) if n_total else 0.0,
        "error_rate": round(n_error / n_total, 3) if n_total else 0.0,
        "mean_repetition_score": round(statistics.mean(all_rep), 3) if all_rep else None,
        "max_repetition_score": round(max(all_rep), 3) if all_rep else None,
        "mean_stability_cv": round(statistics.mean(all_cv), 3) if all_cv else None,
        "stability_cv_by_category": {k: round(statistics.mean(v), 3) for k, v in sorted(cat_stability.items())},
        "mean_term_recall": round(statistics.mean(all_recall), 3) if all_recall else None,
        "mean_text_chars": round(statistics.mean(all_chars), 1) if all_chars else 0,
        "mean_n_blocks": round(statistics.mean(all_blocks), 1) if all_blocks else 0,
        "mean_confidence": round(statistics.mean(all_conf), 3) if all_conf else 0,
        "mean_latency_s": round(statistics.mean(all_lat), 3) if all_lat else 0,
    }
    out = {"summary": summary, "per_fixture": per_fix}
    res_path = os.path.join(RES_DIR, f"{args.engine}.json")
    json.dump(out, open(res_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n-> {res_path}")


if __name__ == "__main__":
    main()
