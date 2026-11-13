# Design notes worth knowing before you change something

- **The transcript is never auto-corrected.** Suspect tokens are *flagged*
  (`asr_suspects.txt`); the transcript stays byte-identical. Two auto-correction
  passes were built, measured, and retired — details in
  `reference/decisions.md`. Treat each flag as a question, not a substitution.
- **The VLM does not do OCR.** Stage D asks a vision model for semantic signals
  only (what kind of slide is this, how complex, what is it about). Text comes
  from the OCR stages. Conflating the two was the source of a long-running "we
  have three duplicate OCR scripts" confusion.
- **Optional dependencies degrade loudly.** A missing optional package disables
  its feature and tells you what you lost. It never silently substitutes a worse
  method — that produced wrong output rather than less output, which is the
  single most expensive bug class in `docs/AUDIT_SUMMARY.md`.
- **Every stage output is a superset of the previous one**, so you can re-run one
  stage without redoing transcription or frame extraction. Keep the
  intermediates; they are how tier decisions get debugged.
- **Every artifact says what made it.** Each stage records tool, engine, model,
  parameters, network use and input hashes in `provenance.json` next to its
  output; `python scripts/provenance.py verify <dir>` flags a file edited after
  its stage wrote it, or built from an input that has changed since.
- **Names from data files never become paths.** A slug, an `attachment_name` or
  an embed target comes from a JSON file or an LLM-written note; it is checked to
  stay inside the directory you gave on the command line (symlinks followed)
  and refused otherwise — never rewritten into some other name.
