# Security policy

## Reporting a vulnerability

Please report security issues **privately** through GitHub:
**Security → Report a vulnerability** on this repository
(Private Vulnerability Reporting). Do not open a public issue for them.

Useful in a report: the version or commit, the input that triggers it (a
minimal transcript, note, manifest or file name is ideal), and what happens.

Public discussion of a fix happens after it ships; the changelog entry says what
was fixed without reproducing the exploit.

## Scope notes

- The synced HTML viewer (`export_web.py` + `scripts/layout2/`) treats all note,
  transcript and OCR text as untrusted — see the 0.8.0 entry in `CHANGELOG.md`.
  A way to get script execution or a network request out of an exported page is
  in scope.
- Stages 0–E run locally on files you give it. Audio leaves the machine only
  with `--engine groq`; Stage D talks to the ollama server at `OLLAMA_URL`
  (default `127.0.0.1`). Stage F synthesis is done by whichever LLM your agent
  uses, and that model sees the stage outputs.
