# ocr_bench — OCR engine A/B/C benchmark

Picks the Stage B/B2 OCR engine **empirically, on your own slide distribution**
rather than on published benchmark numbers. Lecture slides are an unusual
corpus — sonograms, MRI panels, dense CJK text, decorative title cards — and
engines that win on document benchmarks routinely fall apart on them.

## Bring your own fixtures

This repo ships the harness, **not** the fixture set: the slides it was tuned
against are real lecture material and are not ours to publish. To use it,
create your own:

```
ocr_bench/
├── fixtures/                 your slide images (.jpg / .png)
└── fixtures_manifest.json    one entry per fixture
```

`fixtures_manifest.json` is a JSON array; `run_bench.py` keys it by `fixture`:

```json
[
  {
    "fixture": "dense_text_zh__lecture01__frame_0004.jpg",
    "category": "dense_text_zh",
    "expected_terms": ["gabapentin", "神經病理性疼痛"]
  }
]
```

- `category` — free-form label used to group the report (e.g. `dense_text_zh`,
  `dense_text_en`, `table`, `chart`, `algorithm`, `sono`, `mri`, `xray`,
  `anatomy_or_photo`, `title_decorative`). Pick categories that reflect where
  *your* slides actually hurt.
- `expected_terms` — optional hand-keyed terms (drug names, abbreviations,
  anatomy). When present, `run_bench.py` reports recall = fraction of expected
  terms found in the extracted text. Without it, raw text yield (chars/blocks)
  is the recall proxy — weaker, because an engine that hallucinates fluent
  nonsense scores well on yield.

A useful set is 20–30 images spread across categories, deliberately including
the pathological ones (ultrasound, MRI, photos with no text at all). Those are
what separate the engines.

## Layout

```
ocr_bench/
├── schema.py                 canonical ocr_output schema + validate()
├── adapters/
│   ├── rapidocr_adapter.py       baseline (runs in the main env)
│   └── paddleocr_vl_adapter.py   fallback candidate (own venv)
├── run_bench.py              run one adapter ×N, compute KPIs -> results/<engine>.json
├── report.py                 aggregate results/*.json -> report.md
└── results/                  per-engine KPI json (created on first run)
```

The Surya adapter is not here: it lives at `../scripts/adapters/surya_adapter.py`
because production Stage B2 (`scripts/ocr_surya.py`) runs it, and production must
not depend on a benchmark tree. `run_bench.py` resolves `--engine surya` to that
path, so there is exactly one copy.

Every adapter emits the **same schema** (one JSON object per image). Engines are
adapters; the pipeline owns the schema. Adapters run as **subprocesses in their
own venv**, so torch / CUDA / paddle dependency sets never collide.

## Run

```bash
# Baseline (main env, RapidOCR already installed)
python run_bench.py --engine rapidocr --runs 3

# Surya (isolated venv)
uv venv /path/to/surya-venv --python 3.12
uv pip install --python /path/to/surya-venv/bin/python torch --index-url https://download.pytorch.org/whl/cu124
uv pip install --python /path/to/surya-venv/bin/python surya-ocr pillow
python run_bench.py --engine surya --python /path/to/surya-venv/bin/python --runs 3

# PaddleOCR-VL (isolated venv) — fallback
uv venv /path/to/paddle-venv --python 3.12
uv pip install --python /path/to/paddle-venv/bin/python paddleocr paddlepaddle-gpu
python run_bench.py --engine paddleocr_vl --python /path/to/paddle-venv/bin/python --runs 3

# Aggregate
python report.py
```

On Windows the venv interpreter is `<venv>\Scripts\python.exe` instead of
`<venv>/bin/python`.

## Decisive KPIs (⭐)

`json_valid_rate`, `repetition_score` (runaway output), `stability_cv`
(run-to-run variance — must be ~0 on pathological MRI/sono/photo inputs),
`latency_s`, plus text yield.

`stability_cv` is the one people forget. An engine that returns different text
on identical input across runs cannot be debugged downstream, however good its
average score looks.

The adapter API for surya/paddle is version-sensitive. If an adapter prints
`INIT FAILED` to stderr, check the version it printed and adjust the call inside
that adapter — that is what the adapter layer is for.
