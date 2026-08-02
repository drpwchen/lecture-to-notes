# Pre-release audit summary

This pipeline ran privately for about a year before it was published. Before
cutting v0.1.0 the whole tree was audited — roughly 6,800 lines across 22
scripts, plus the documentation — by four independent reviewers working in
parallel (transcription core, OCR/slides, post-processing, doc↔code
consistency), backed by mechanical grep passes for hardcoded paths, cross-repo
imports and identifying content. About 130 findings came out of it. Everything
in the "release blockers" and "silently wrong output" tiers below was fixed
before this repo existed.

This document is the honest version of "hardened before release". It is here
because the failure classes are more interesting than the individual bugs, and
because two of the audit's own findings turned out to be wrong in instructive
ways.

## What the audit was looking for

Not "does it work" — it had been working for a year on one machine, for one
person. The question was: **what breaks the moment it runs somewhere else, and
what has been quietly producing worse output all along without anyone noticing?**

## Classes of bugs found

**1. Machine-shaped assumptions (release blockers).** Hardcoded model
directories on a specific drive letter, imports of a private helper module,
a cooperative GPU pause-flag pointing at a path that exists on exactly one
computer, a vault attachment convention baked into every generated image link.
All of these fail immediately elsewhere — which is the good case. Fixed by
routing every path through config + environment variables with working defaults,
and by vendoring the shared helpers so the tree is self-contained.

**2. Silent degradation — the expensive class.** Optional dependencies that,
when missing, produced *wrong* output instead of *less* output. The worst
example: without `scikit-image`, slide dedup fell back to grayscale histogram
similarity, which rates any two same-template slides ~0.95 alike; a 120-frame
deck collapsed to about 5 "canonical" slides and the pipeline reported success.
The fix is a principle now applied throughout: **a missing optional dependency
disables its feature loudly and says what you lost — it never substitutes a
worse proxy.** Same class: OCR missing → every slide looks decorative → the VLM
stage skips everything → a confident, empty note.

**3. Off-by-one errors in time and index.** Frame extraction at `fps=1/N`
emits its first frame at t=0, but the code assumed t=N, so every slide
timestamp was systematically 15 seconds late and transcript grounding attached
the wrong spoken words to every slide, on every run of that path. Separately,
the PDF path numbered slides from 0 while the video path numbered from 1, so
every embed reference, attachment name and human-facing slide number was off by
one for PDF-sourced lectures. Both silent.

**4. Success reported on empty output.** Zero-segment transcriptions exited 0
with `success: true`. Guard clusters that were supposed to catch mass failure
were bypassed when the input set was empty (zero calls → zero failures → guard
passes). Fixed by asserting on absolute counts, not just ratios.

**5. Destructive "repair" paths.** The transcript re-transcription helper could,
on an empty replacement list, delete the entire time range it was meant to fix —
in place, over the only backup, which it overwrote on every run. Non-idempotent
merges lost data on a second invocation. This is the category worth stealing as
a checklist item: *any code path that repairs data must be idempotent and must
refuse to write an empty result over a non-empty one.*

**6. Missing timeouts and unbounded waits.** Subprocesses without timeouts hold
a GPU forever when CUDA wedges. A stale pause-flag file meant an infinite sleep
loop. Command lines built by concatenating every image path hit the Windows
32,767-character argv limit on long lectures.

**7. Doc↔code drift.** The documented default model was not the code's default
model. One hard rule and two other passages in the same file gave three
different "default" batch sizes. A field was documented as removed, listed in
the schema, and still emitted. Documentation was restructured so each fact lives
in exactly one place, with the rest pointing at it.

## Two errata — the methodology lessons

These are the findings the audit got wrong. They are more useful than the ones
it got right.

**Erratum 1: scope the grep to the blast radius, not to the directory.** A
checkpoint file was flagged as dead weight because nothing in the audited
directory read it. Something outside the directory did — a separate crash-resume
script depended on it, and deleting the write would have broken hours-long
recovery. Lesson: "no consumers" is a claim about the search scope, not about
the code. State the scope you searched, or search wider.

**Erratum 2: measure before assigning severity.** A fuzzy-match call was flagged
as near-certain to produce false keyword hits. Measurement showed the opposite:
that function normalizes by the length of the needle, so an absent short term
scores 50–75, not the ~88 the reviewer assumed. The real defect was narrower
(substring-inside-word matches like `motion` inside `emotional`) and got a
narrower fix. Lesson: a severity assigned from reading the code is a hypothesis.
Reading is how you find candidates; running is how you rank them.

Both errata share a shape: a confident conclusion drawn from a partial view,
stated without the caveat that would have made it checkable. The fix in both
cases cost minutes; noticing cost a review round.

## What is still open

Slide dedup can still over-merge decks built from a single sparse template, on
the SSIM side rather than the histogram side. Synthetic fixtures were not enough
to convict it, and tightening the threshold blind risks the opposite failure.
It needs an A/B on real lecture material before the merge condition changes.

## What was deliberately not published

The benchmark fixture set (real lecture slides, not ours to distribute) and the
private configuration. `ocr_bench/` ships as a harness with a bring-your-own
fixtures README.
