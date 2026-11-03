# `transcript.json` contract

What Stages A–E and the web export need from a transcript, written down so an
external ASR — MLX Whisper on Apple Silicon, a hosted STT service, a human
transcript, creator captions — can feed the pipeline **without re-running
Stage 0**. `scripts/import_transcript.py` (0.10.0) produces it from SRT, WebVTT
or whisper-style JSON — see [The importer](#the-importer) below. It documents
what the code does today (0.10.0); file:line references point at the evidence.

## The file

`<lecture dir>/transcript.json` is a **bare JSON list** of segment objects,
UTF-8, sorted by `start`:

```json
[
  {"start": 0.0,  "end": 4.38, "text": " Good morning, everyone."},
  {"start": 4.38, "end": 9.1,  "text": " Today: the brachial plexus.",
   "words": [{"start": 4.38, "end": 4.9, "word": " Today"}, "…"]}
]
```

| Key | Type | Required | Notes |
|---|---|---|---|
| `start` | number, seconds from the start of the media | **yes** | Writers round to 2 decimals (`transcribe_video.py:833-844`). |
| `end` | number, seconds | **yes** | `>= start`. |
| `text` | string | **yes** | Never `null` — `flag_asr_suspects.py` runs a regex over it. Leading whitespace is fine (faster-whisper keeps it). |
| `words` | list of `{start, end, word}` | no | Only `batch/split_segments.py` reads it; segments without it are interpolated. |

Nothing else is read. In particular no consumer reads `avg_logprob`,
`no_speech_prob`, a language, or any top-level metadata from this file, and
there is no top-level metadata in it — the list *is* the file. Extra keys on a
segment are ignored by every reader, so an importer may carry provider-specific
fields through, but nothing will use them.

### Use the bare list, not `{"segments": [...]}`

`_common.load_segments` (`_common.py:171-196`) accepts both shapes, and so do
`ground_slides.py`, `flag_asr_suspects.py`, `xcorr_media_offsets.py`,
`query_near_field.py` and `retranscribe_segment.py`. But
`build_single_talk_web.py`, `batch/build_L1.py`, `batch/detect_language.py` and
`batch/split_segments.py` call `json.load` directly and expect a list. An
importer that writes the bare list works with all of them.

### Keys that are strictly required, by reader

| Reader | Needs | Breaks on |
|---|---|---|
| `ground_slides.py` (Stage E) | `start`, `end`, `text` via `.get` | nothing — tolerant |
| `flag_asr_suspects.py` | `text` | `text: null` |
| `xcorr_media_offsets.py` | `text`, `start` via `.get` | nothing — tolerant |
| `query_near_field.py` | `start` | missing `start` |
| `retranscribe_segment.py` (collapse repair) | `start`, `end` | missing either |
| `build_single_talk_web.py` | `start`, `end`, `text`; bare list | missing keys, dict form |
| `batch/split_segments.py` | `start`, `end`; each word's `start`/`end`/`word` | missing keys, malformed word, dict form |

## `transcript.txt`

Written next to it from the same segments by `_common.write_transcript_lines`
(`_common.py:214-223`): one line per segment, `[MM:SS] text` (`[H:MM:SS]` past an
hour), text stripped. `audit_note.py` measures transcript coverage from this
file, so an importer should write it too — call `write_transcript_lines` rather
than re-implementing the format.

## Provenance lives beside the transcript, not in it

Engine, model and language are recorded by the run logger, not in
`transcript.json`:

- `metadata.json` → `stages.transcribe`: `whisper_model`, `lang`,
  `detected_language`, `n_segments`, … and `engine: "groq"` on the Groq path
  (a local run is recognisable only by `engine` being absent).
- `runs.jsonl` (one level up): one row per run with the same fields.

An importer should record the same way (`_log.StageLogger`), with
`engine` set to its provider (e.g. `"mlx-whisper"`, `"import:srt"`) and the
model name it was given. That is a convention for the importer to adopt, not
something any stage reads today.

Since 0.10.0 every stage also writes `provenance.json` next to its artifacts:
for `transcript.json`/`.txt` the tool, engine, model, provider, network use,
parameters and the hashes of the inputs (the media, or the imported source
file). `python scripts/provenance.py verify <lecture dir>` reports any artifact
whose file changed after its stage wrote it, or whose input changed since —
e.g. grounding that predates a re-transcription. Paths outside the lecture dir
are stored as `external:<basename>`, never as an absolute path.

## Rules for an importer

1. **Never modify the source file.** Read it, write a new `transcript.json`
   (atomically — `_common.atomic_write_json`) into the lecture dir.
2. **Refuse to overwrite** an existing `transcript.json` unless asked (`--force`),
   and keep the old one as a backup when asked.
3. Convert every time to **float seconds from the start of the media**. SRT/VTT
   timecodes, millisecond integers and per-chunk offsets all end up here.
4. Emit segments sorted by `start`, `end >= start`, `text` a string.
5. Write `transcript.txt` with `write_transcript_lines`.
6. Record provenance (above).

## The importer

```bash
python scripts/import_transcript.py SOURCE --out-dir "$OUT_DIR" \
    [--format auto|srt|vtt|json] [--engine mlx-whisper] [--model large-v3] \
    [--media lecture.mp4] [--offset SEC] [--force [--backup]]
```

It follows the six rules above: the source is only read (importing a file onto
itself is refused), an existing transcript needs `--force` (`--backup` keeps it as
`.bak-import-<timestamp>`), times become float seconds rounded to the millisecond,
segments are sorted and a segment with `end < start` is an error — nothing is
written — rather than a silent fix. Caption styling (`<i>`, VTT classes and inline
timestamps, ASS overrides such as `{\an8}`) is stripped; the words are not touched. A VTT `<v Name>`
voice is kept as an extra `speaker` key (ignored by every reader). Word timings
are kept only when every word of a segment has them. Other JSON shapes:
`--list-key utterances --start-key begin --end-key finish --text-key words
--time-unit ms`. `--media` fingerprints the recording into `provenance.json` and
warns if the transcript runs past its end.

### Apple Silicon / MLX Whisper — untested

`mlx-whisper` writes the same `{"segments": [{start, end, text, words?}]}` JSON as
openai-whisper, so by its documented format `import_transcript.py --format json
--engine mlx-whisper --model <name>` is the adapter: run MLX Whisper yourself,
import its output, continue with Stage A. **The maintainer has no Mac and has not
run this.** The same goes for an Apple Silicon resource profile in `gpu_check.py`
(unified-memory pressure, Metal availability): not implemented. Reports and a PR
with a real run are welcome — the contract above is what it has to meet.

## Things that rewrite `transcript.json` later

So an importer knows what happens downstream: `retranscribe_segment.py`
(automatic collapse repair, local path only) rewrites the file after backing it
up to `.bak`, and `batch/split_segments.py` replaces it with sentence-level
segments and drops `words` (backup only with `--backup`). Stages A–E and the
web export only read it.
