"""Per-artifact provenance: which stage, tool, engine, model, parameters and inputs
produced each file in a lecture dir (0.10.0).

Every stage that writes an artifact calls :func:`record` once it is written. The
result is one JSON file next to the artifacts::

    <lecture dir>/provenance.json
    {
      "schema": 1,
      "root": ".",
      "artifacts": {
        "slides_grounded.json": {
          "stage": "ground", "tool": "ground_slides.py", "pipeline_version": "…",
          "run_id": "…", "recorded_at": "2026-11-03T10:00:00+08:00",
          "provider": "local", "engine": "python", "model": null, "network": "none",
          "params": {"window_s": 30},
          "digest": {"kind": "file", "bytes": 81234, "sha256": "…"},
          "inputs": {"transcript.json": {"kind": "file", "bytes": …, "sha256": "…"},
                     "slides_vlm.json": {…}}
        }, …
      }
    }

Only the latest record per artifact is kept. Because every record carries the
hashes of its inputs, the chain can be checked later: ``python provenance.py
verify <dir>`` reports an artifact as ``modified`` when the file no longer
matches what its stage wrote (edited by hand, or rewritten by something that
does not record), and ``stale`` when an input changed after it was produced
(e.g. ``transcript.json`` re-transcribed after grounding ran).

Fields
------
- ``provider``: where the computation ran — ``local``, ``groq``, ``gemini``,
  or whatever a caller passes (an LLM vendor for Stage F).
- ``network``: ``none`` | ``loopback`` (local ollama) | ``internet``.
- ``engine`` / ``model``: the software and model that did the work.
- ``digest``: ``sha256`` of the whole file; files over 256 MB (media) carry
  ``sample_sha256`` over the first and last MiB plus the size instead, and a
  directory carries one hash over the sorted (relative name, sha256) list.

Privacy: paths are stored relative to the root. A file outside the root (the
source media, a vault note) is keyed ``external:<basename>`` with its digest,
never by its absolute path — a provenance file can sit in a folder you share.

Recording never fails a stage: an error is printed as a WARNING and the stage
carries on, since the artifact itself is already written.

CLI
---
    python provenance.py verify <dir | provenance.json> [--json]
    python provenance.py record <dir> --stage synthesis --output slides_final.json \\
        [--output note_draft.md] [--input slides_grounded.json] [--input transcript.txt] \\
        [--provider NAME] [--engine NAME] [--model NAME] [--network internet] \\
        [--param key=value]
``record`` exists for the artifacts no script writes — the Stage F synthesis is
produced by an LLM agent, so the agent (or its harness) records it this way.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from typing import Any, Iterable

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from _common import is_within  # noqa: E402

FILENAME = "provenance.json"
SCHEMA = 1
HASH_FULL_MAX = 256 * 1024 * 1024
_SAMPLE = 1 << 20

_VERSION_CACHE: list[str] = []


def _pipeline_version() -> str:
    if not _VERSION_CACHE:
        try:
            from _log import git_hash
            _VERSION_CACHE.append(git_hash())
        except Exception:  # noqa: BLE001
            _VERSION_CACHE.append("unknown")
    return _VERSION_CACHE[0]


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def digest(path: Any) -> dict | None:
    """Content digest of a file or directory; None when it does not exist."""
    path = os.fspath(path)
    try:
        if os.path.isdir(path):
            h = hashlib.sha256()
            n = total = 0
            for dirpath, dirnames, files in os.walk(path):
                dirnames.sort()
                for f in sorted(files):
                    fp = os.path.join(dirpath, f)
                    rel = os.path.relpath(fp, path).replace(os.sep, "/")
                    h.update(rel.encode("utf-8") + b"\0" + _sha256_file(fp).encode() + b"\n")
                    n += 1
                    total += os.path.getsize(fp)
            return {"kind": "dir", "files": n, "bytes": total, "sha256": h.hexdigest()}
        if not os.path.isfile(path):
            return None
        size = os.path.getsize(path)
        if size <= HASH_FULL_MAX:
            return {"kind": "file", "bytes": size, "sha256": _sha256_file(path)}
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            h.update(fh.read(_SAMPLE))
            fh.seek(-_SAMPLE, 2)
            h.update(fh.read(_SAMPLE))
        return {"kind": "file", "bytes": size, "sample_sha256": h.hexdigest()}
    except OSError as e:
        return {"kind": "error", "error": f"{type(e).__name__}: {e}"[:200]}


def _key(path: str, root: str) -> str:
    if is_within(root, path):
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(root))
        return rel.replace(os.sep, "/")
    return "external:" + os.path.basename(os.path.normpath(path))


def snapshot(paths: Iterable[Any], root: Any) -> dict:
    """Digest ``paths`` NOW, keyed like :func:`record` keys them.

    For a stage that rewrites one of its own inputs in place (Stage B2 updates
    ``slides_dedup.json``): take the snapshot before writing and pass it as
    ``inputs=``.
    """
    root = os.fspath(root)
    out = {}
    for p in paths:
        if p is None:
            continue
        p = os.fspath(p)
        out[_key(p, root)] = digest(p)
    return out


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record(root: Any, stage: str, outputs: Iterable[Any], inputs: Any = (), *,
           tool: str | None = None, provider: str = "local", engine: str | None = None,
           model: str | None = None, network: str = "none",
           params: dict | None = None, manifest_dir: Any = None,
           pipeline_version: str | None = None) -> bool:
    """Record one stage run for each of ``outputs``. Returns False on failure.

    ``root``: the directory the keys are relative to (the lecture dir).
    ``inputs``: paths, or a dict from :func:`snapshot` taken before an in-place
    rewrite. ``manifest_dir``: where ``provenance.json`` lives (default ``root``).
    ``pipeline_version``: pass one the caller already has (default: ``_log.git_hash()``).
    """
    try:
        root = os.fspath(root)
        mdir = os.fspath(manifest_dir) if manifest_dir is not None else root
        inputs_d = inputs if isinstance(inputs, dict) else snapshot(inputs, root)
        try:
            from _log import read_metadata
            run_id = read_metadata(root).get("run_id")
        except Exception:  # noqa: BLE001
            run_id = None
        base = {
            "stage": stage,
            "tool": tool or os.path.basename(sys.argv[0] or "") or None,
            "pipeline_version": pipeline_version or _pipeline_version(),
            "run_id": run_id,
            "recorded_at": _now(),
            "provider": provider,
            "engine": engine,
            "model": model,
            "network": network,
            "params": params or {},
            "inputs": inputs_d,
        }
        entries = {}
        for p in outputs:
            if p is None:
                continue
            p = os.fspath(p)
            entries[_key(p, root)] = dict(base, digest=digest(p))
        if not entries:
            return True
        mpath = os.path.join(mdir, FILENAME)
        try:
            rel_root = os.path.relpath(root, mdir).replace(os.sep, "/")
        except ValueError:  # another drive: keys cannot be re-resolved from mdir
            rel_root = "."
        from _log import _cross_process_lock
        with _cross_process_lock(mpath):
            data = _read(mpath)
            data["schema"] = SCHEMA
            data["root"] = rel_root
            arts = data.get("artifacts")
            if not isinstance(arts, dict):
                arts = {}
            arts.update(entries)
            data["artifacts"] = dict(sorted(arts.items()))
            tmp = mpath + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2, default=str)
            os.replace(tmp, mpath)
        return True
    except Exception as e:  # noqa: BLE001 — provenance must never fail a stage
        print(f"WARNING: provenance not recorded for stage {stage!r}: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return False


def _same(a: dict | None, b: dict | None) -> bool:
    if a is None or b is None:
        return a is b
    for k in ("sha256", "sample_sha256"):
        if k in a or k in b:
            return a.get(k) == b.get(k) and a.get("bytes") == b.get("bytes")
    return a == b


def verify(target: Any) -> list[dict]:
    """Check every recorded artifact. Status: ok | modified | missing | stale."""
    target = os.fspath(target)
    mpath = target if os.path.isfile(target) else os.path.join(target, FILENAME)
    data = _read(mpath)
    mdir = os.path.dirname(os.path.abspath(mpath))
    root = os.path.normpath(os.path.join(mdir, data.get("root") or "."))
    rows = []
    for key, ent in sorted((data.get("artifacts") or {}).items()):
        row = {"artifact": key, "stage": ent.get("stage"), "status": "ok", "detail": []}
        if key.startswith("external:"):
            row["status"] = "external"
            row["detail"].append("outside the root; not re-checked")
            rows.append(row)
            continue
        cur = digest(os.path.join(root, key))
        if cur is None:
            row["status"] = "missing"
        elif not _same(cur, ent.get("digest")):
            row["status"] = "modified"
            row["detail"].append("file differs from what its stage wrote")
        for ikey, idig in sorted((ent.get("inputs") or {}).items()):
            if ikey.startswith("external:") or ikey == key:
                continue  # an in-place rewrite lists its own pre-image as input
            icur = digest(os.path.join(root, ikey))
            if not _same(icur, idig):
                if row["status"] == "ok":
                    row["status"] = "stale"
                row["detail"].append(f"input {ikey} changed since")
        rows.append(row)
    return rows


def _cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Per-artifact provenance: verify or record.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="check recorded artifacts against the files on disk")
    v.add_argument("target", help="lecture dir or a provenance.json")
    v.add_argument("--json", action="store_true")
    r = sub.add_parser("record", help="record an artifact no script wrote (e.g. Stage F)")
    r.add_argument("root", help="lecture dir")
    r.add_argument("--stage", required=True)
    r.add_argument("--output", action="append", required=True, help="artifact path (repeatable)")
    r.add_argument("--input", action="append", default=[], help="input path (repeatable)")
    r.add_argument("--tool", default=None)
    r.add_argument("--provider", default="local")
    r.add_argument("--engine", default=None)
    r.add_argument("--model", default=None)
    r.add_argument("--network", default="none", choices=["none", "loopback", "internet"])
    r.add_argument("--param", action="append", default=[], help="key=value (repeatable)")
    a = ap.parse_args(argv)

    if a.cmd == "verify":
        if not os.path.exists(a.target):
            print(f"ERROR: not found: {a.target}", file=sys.stderr)
            return 2
        rows = verify(a.target)
        if a.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            for row in rows:
                extra = ("  — " + "; ".join(row["detail"])) if row["detail"] else ""
                print(f"{row['status']:9s} {row['artifact']}  [{row['stage']}]{extra}")
            counts: dict[str, int] = {}
            for row in rows:
                counts[row["status"]] = counts.get(row["status"], 0) + 1
            print("summary: " + (", ".join(f"{k}={n}" for k, n in sorted(counts.items())) or "no records"))
        return 1 if any(r_["status"] in ("modified", "missing", "stale") for r_ in rows) else 0

    root = a.root
    resolve = (lambda p: p if os.path.isabs(p) else os.path.join(root, p))
    params = {}
    for kv in a.param:
        k, _, val = kv.partition("=")
        params[k] = val
    outs = [resolve(p) for p in a.output]
    missing = [p for p in outs if not os.path.exists(p)]
    if missing:
        print(f"ERROR: output(s) not found: {missing}", file=sys.stderr)
        return 2
    ok = record(root, a.stage, outs, [resolve(p) for p in a.input], tool=a.tool or "manual",
                provider=a.provider, engine=a.engine, model=a.model, network=a.network,
                params=params)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_cli())
