"""Shared JSONL logger + metadata writer for lecture-to-notes stages.

Each stage writes events to ``<out_dir>/logs/progress_<stage>.jsonl``
(line-buffered, so ``tail -F`` is real-time). Main Claude reads these
to know what's happening without staring at 20-minute black-box stdout.

Event types
-----------
- start         : stage began (one per process invocation)
- progress      : periodic per-item update (every N items, caller decides)
- heartbeat     : fixed 30s tick with gpu_mem_mb + rss_mb snapshot
- slide_done    : successfully processed one item
- slide_error   : one item failed (others may continue)
- retry         : fallback path taken (e.g. batch=8 OOM -> batch=4, or VLM HTTP retry)
- stage_done    : stage finished (one per process, success or error)
- heartbeat_died: heartbeat thread crashed (extremely rare — visibility net)

Every event carries a ``status`` field (``running`` | ``success`` |
``error`` | ``retry``) so monitors can filter via:

    tail -F logs/progress_*.jsonl | grep '"status":"error"'
    tail -F logs/progress_*.jsonl | grep '"event":"heartbeat"'

Time is measured with ``time.monotonic()`` (immune to NTP / sleep /
timezone shifts mid-stage). Wall-clock ``ts`` is also included for human
correlation.

Run identity
------------
A per-run UUID is established on first StageLogger construction in the
lecture dir (written to ``metadata.json``). All subsequent stages in the
same dir read and reuse it. Every event includes ``run_id`` so logs +
summaries + artifacts cross-reference cleanly.

Heartbeat hardening
-------------------
1. daemon=True thread so a hung main process can still be killed
2. ``_hb_last_seen`` monotonic timestamp — main loop can assert "still
   alive" without relying on the JSONL file being readable
3. atexit hook stops the thread on normal exit / unhandled exception
4. Exceptions inside the heartbeat loop are *emitted as events*
   (status=error, event=heartbeat_died) instead of vanishing silently
   into a daemon thread
"""
from __future__ import annotations

import atexit
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime

# Fallback build identity for installs that are not a git checkout (a tarball /
# pip install has no .git, so git_hash() would report "unknown" forever). Bump
# on release; git_hash() prefers the real commit when one is available.
__version__ = "0.1.0"


# ----------------------------------------------------------------------
# Process metrics
# ----------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _gpu_mem_mb() -> int | None:
    """Used VRAM (MB) on GPU 0. Cheap. None if nvidia-smi unavailable."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used",
             "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL, text=True, timeout=2,
        ).strip().splitlines()
        if out:
            return int(out[0].strip())
    except (subprocess.SubprocessError, FileNotFoundError, ValueError, OSError):
        pass
    return None


def _rss_mb() -> int | None:
    try:
        import psutil  # type: ignore
        return int(psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024))
    except Exception:
        return None


# ----------------------------------------------------------------------
# metadata.json — single source of truth for run identity + stage status
# ----------------------------------------------------------------------

def _metadata_path(out_dir: str) -> str:
    return os.path.join(out_dir, "metadata.json")


def read_metadata(out_dir: str) -> dict:
    p = _metadata_path(out_dir)
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_metadata_atomic(out_dir: str, meta: dict) -> None:
    """Atomic write: temp file + rename. Avoids half-written corruption
    if process is killed mid-write."""
    p = _metadata_path(out_dir)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


_meta_lock = threading.Lock()

_LOCK_WARNED: set[str] = set()


@contextlib.contextmanager
def _cross_process_lock(target_path: str, timeout_s: float = 10.0,
                        poll_s: float = 0.05):
    """Advisory O_EXCL lock-file around a read-modify-write of ``target_path``.

    A ``threading.Lock`` only serializes threads inside ONE interpreter, and the
    pipeline runs stages as separate processes (transcribe spawns
    retranscribe_segment; the batch runner runs clips side by side), so two
    processes could both read metadata.json and the second ``os.replace`` would
    drop the first one's stage record.

    Deliberately non-fatal: on timeout we warn once and proceed with the plain
    atomic write, exactly as before this lock existed. Losing a log record is
    bad; wedging a 40-minute transcription behind a stale lock is worse. A lock
    file older than ``3 * timeout_s`` is assumed to belong to a crashed process
    and is stolen.
    """
    lock_path = target_path + ".lock"
    deadline = time.monotonic() + timeout_s
    fd = None
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_path) > max(30.0, timeout_s * 3):
                    os.remove(lock_path)   # stale: holder died before releasing
                    continue
            except OSError:
                pass
        except OSError:
            break  # unwritable dir etc. — never block the caller on locking
        if time.monotonic() >= deadline:
            if lock_path not in _LOCK_WARNED:
                _LOCK_WARNED.add(lock_path)
                print(f"WARNING: could not acquire {lock_path} within {timeout_s}s; "
                      "proceeding unlocked (a concurrent writer may overwrite this "
                      "record)", file=sys.stderr)
            break
        time.sleep(poll_s)

    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.remove(lock_path)
            except OSError:
                pass


def update_metadata(out_dir: str, mutate) -> dict:
    """Read-modify-write metadata.json under a thread + cross-process lock.

    ``mutate(meta_dict) -> None`` modifies in place.
    Returns the post-mutation dict.
    """
    with _meta_lock, _cross_process_lock(_metadata_path(out_dir)):
        meta = read_metadata(out_dir)
        mutate(meta)
        _write_metadata_atomic(out_dir, meta)
        return meta


def lecture_name(out_dir: str) -> str:
    """Directory name of ``out_dir``, insensitive to trailing separators.

    ``os.path.basename('/a/b/')`` is ``''``; every caller wants ``'b'``.
    """
    return os.path.basename(os.path.abspath(out_dir))


def ensure_run_id(out_dir: str, lecture: str | None = None,
                  media_path: str | None = None) -> str:
    """Read run_id from metadata.json; create one + bootstrap metadata
    if absent. Idempotent: calling twice returns the same UUID.
    """
    meta = read_metadata(out_dir)
    if meta.get("run_id"):
        return meta["run_id"]

    rid = str(uuid.uuid4())

    def bootstrap(m: dict) -> None:
        # Another stage may have raced us — re-check inside the lock.
        if m.get("run_id"):
            return
        m["run_id"] = rid
        m["lecture"] = lecture or lecture_name(out_dir)
        m["started_at"] = _now_iso()
        m["hostname"] = (os.environ.get("COMPUTERNAME")
                         or os.environ.get("HOSTNAME") or "")
        m["pipeline_version"] = git_hash()
        if media_path:
            m["media"] = _media_fingerprint(media_path)
        m["stages"] = {}

    after = update_metadata(out_dir, bootstrap)
    return after["run_id"]


def _media_fingerprint(path: str) -> dict:
    """Cheap stable media fingerprint: size + mtime + sha256 of first/last 64KB.

    Full file sha256 of a 2 GB lecture takes ~10s and isn't worth it just
    to detect "same file re-run". Size + mtime + endpoint-hash catches
    every realistic re-run scenario.
    """
    try:
        st = os.stat(path)
    except OSError:
        return {"path": path, "size_bytes": None, "fp_sha256_8": None}
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            h.update(f.read(65536))
            if st.st_size > 131072:
                f.seek(-65536, 2)
                h.update(f.read(65536))
    except OSError:
        return {"path": path, "size_bytes": st.st_size, "fp_sha256_8": None}
    return {
        "path": path,
        "size_bytes": st.st_size,
        "mtime": int(st.st_mtime),
        "fp_sha256_8": h.hexdigest()[:8],
    }


# ----------------------------------------------------------------------
# StageLogger
# ----------------------------------------------------------------------

class StageLogger:
    """JSONL-backed stage logger with heartbeat + metadata integration.

    Files land in ``<out_dir>/logs/progress_<stage>.jsonl``. The logs/
    subdir is created on demand.
    """

    def __init__(self, stage: str, out_dir: str,
                 extra: dict | None = None,
                 stdout_mirror: bool = True,
                 lecture: str | None = None,
                 media_path: str | None = None):
        self.stage = stage
        self.out_dir = out_dir
        self.logs_dir = os.path.join(out_dir, "logs")
        os.makedirs(self.logs_dir, exist_ok=True)
        self.path = os.path.join(self.logs_dir, f"progress_{stage}.jsonl")

        # run_id bootstraps metadata.json on first stage in this lecture dir.
        self.run_id = ensure_run_id(out_dir, lecture=lecture, media_path=media_path)

        # buffering=1 -> line-buffered (text mode). Append so reruns leave a
        # trail. Each run's events are bracketed by start / stage_done so
        # downstream readers can split on start events.
        self._fp = open(self.path, "a", encoding="utf-8", buffering=1)
        self._t0 = time.monotonic()
        self._stage_started_at = _now_iso()
        self._extra = dict(extra or {})
        self._lock = threading.Lock()
        self._stdout_mirror = stdout_mirror

        # Heartbeat state
        self._hb_stop: threading.Event | None = None
        self._hb_thread: threading.Thread | None = None
        # Monotonic timestamp of last successful heartbeat emit. None until
        # heartbeat() is entered. Main thread can read this without locking
        # (single-writer, single-reader of a float-ish value).
        self.hb_last_seen: float | None = None

        # Stage cleanup state
        self._closed = False
        self._stage_done_called = False
        self._warned_after_close = False
        atexit.register(self._atexit_cleanup)

        # A crash between the temp write and the rename leaves metadata.json.tmp
        # behind; it is never read, but it accumulates and makes a lecture dir
        # look half-written. Only an OLD one is removed — a fresh .tmp may belong
        # to a stage that is mid-write right now.
        meta_tmp = _metadata_path(out_dir) + ".tmp"
        try:
            if os.path.isfile(meta_tmp) and time.time() - os.path.getmtime(meta_tmp) > 60:
                os.remove(meta_tmp)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Low-level emission
    # ------------------------------------------------------------------

    def emit(self, event: str, status: str = "running", **fields) -> None:
        if self._closed:
            # Silently dropping these hid a real bug class (a stage emitting
            # after close(), so its last events never reached the JSONL). Say it
            # once — repeating it per event would bury the actual output.
            if not self._warned_after_close:
                self._warned_after_close = True
                print(f"WARNING: {self.stage} logger emitted {event!r} after close(); "
                      "this and any later events are not written to "
                      f"{self.path}", file=sys.stderr)
            return
        row = {
            "ts": _now_iso(),
            "run_id": self.run_id,
            "stage": self.stage,
            "event": event,
            "status": status,
            "elapsed_monotonic_s": round(time.monotonic() - self._t0, 3),
        }
        row.update(self._extra)
        row.update(fields)
        line = json.dumps(row, ensure_ascii=False, default=str)
        with self._lock:
            try:
                self._fp.write(line + "\n")
            except (OSError, ValueError):
                # File closed unexpectedly — fail silent rather than crash stage
                pass
        if self._stdout_mirror:
            short = f"[{self.stage}/{event}/{status}]"
            extras = " ".join(f"{k}={v}" for k, v in fields.items()
                              if k in ("idx", "total", "slide_id",
                                       "error", "batch_size", "tier",
                                       "attempt", "queue_remaining"))
            msg = f"{short} {extras}".rstrip()
            try:
                print(msg, flush=True)
            except UnicodeEncodeError:
                enc = sys.stdout.encoding or "utf-8"
                print(msg.encode(enc, errors="replace").decode(enc),
                      flush=True)

    # ------------------------------------------------------------------
    # Convenience event methods
    # ------------------------------------------------------------------

    def start(self, **fields) -> None:
        self.emit("start", status="running", **fields)

    def progress(self, **fields) -> None:
        self.emit("progress", status="running", **fields)

    def item_done(self, **fields) -> None:
        self.emit("item_done", status="success", **fields)

    def item_error(self, error: str, **fields) -> None:
        self.emit("item_error", status="error", error=error, **fields)

    def retry(self, reason: str, **fields) -> None:
        self.emit("retry", status="retry", reason=reason, **fields)

    def stage_done(self, success: bool = True, **fields) -> None:
        self._stage_done_called = True
        self.emit("stage_done",
                  status="success" if success else "error",
                  success=success, **fields)
        # Mirror into metadata.json so consumers can see at-a-glance status.
        stage_record = {
            "started_at": self._stage_started_at,
            "ended_at": _now_iso(),
            "elapsed_s": round(time.monotonic() - self._t0, 1),
            "success": success,
            **{k: v for k, v in fields.items()
               if isinstance(v, (str, int, float, bool, list, dict, type(None)))},
        }

        def mutate(m: dict) -> None:
            m.setdefault("stages", {})[self.stage] = stage_record
            m["last_updated"] = _now_iso()

        try:
            update_metadata(self.out_dir, mutate)
        except Exception:
            # Metadata write must never block stage completion
            self.emit("metadata_write_failed", status="error")

    # ------------------------------------------------------------------
    # 30s heartbeat — hardened
    # ------------------------------------------------------------------

    def _heartbeat_loop(self, interval_s: float) -> None:
        assert self._hb_stop is not None
        try:
            while not self._hb_stop.wait(interval_s):
                self.emit("heartbeat",
                          status="running",
                          gpu_mem_mb=_gpu_mem_mb(),
                          rss_mb=_rss_mb())
                self.hb_last_seen = time.monotonic()
        except BaseException as e:  # noqa: BLE001 — true catch-all to surface
            # Daemon thread exceptions would otherwise vanish to stderr. We
            # *want* them in the JSONL so a stalled main can be diagnosed.
            try:
                self.emit("heartbeat_died", status="error",
                          error=f"{type(e).__name__}: {e}"[:300])
            except Exception:
                pass

    @contextlib.contextmanager
    def heartbeat(self, interval_s: float = 30.0):
        """Run a heartbeat thread for the body's duration.

        NOT re-entrant: ``_hb_stop`` / ``_hb_thread`` are single slots, so a
        nested ``with log.heartbeat()`` would overwrite the outer thread's
        handles and the inner block's exit would leave the outer thread running
        forever with nothing able to stop it. Refuse loudly instead of leaking a
        thread — a stage wanting two cadences should emit progress() directly.
        """
        if self._hb_thread is not None:
            raise RuntimeError(
                f"heartbeat() is already active for stage {self.stage!r} and "
                "cannot be nested; close the outer heartbeat block first")
        self._hb_stop = threading.Event()
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(interval_s,),
            name=f"hb-{self.stage}",
            daemon=True,
        )
        # Seed last_seen so main can detect "heartbeat never even started"
        self.hb_last_seen = time.monotonic()
        self._hb_thread.start()
        try:
            yield self
        finally:
            self._hb_stop.set()
            self._hb_thread.join(timeout=2.0)
            self._hb_thread = None
            self._hb_stop = None

    def is_heartbeat_stale(self, max_age_s: float = 90.0) -> bool:
        """True if heartbeat hasn't ticked in max_age_s. Used by main
        thread to detect a dead heartbeat thread."""
        if self.hb_last_seen is None:
            return False  # heartbeat never started — not stale, just absent
        return (time.monotonic() - self.hb_last_seen) > max_age_s

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        # Long-lived processes that build one logger per item would otherwise
        # accumulate an atexit handler per logger, all firing at shutdown against
        # already-closed files.
        try:
            atexit.unregister(self._atexit_cleanup)
        except Exception:
            pass
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._fp.flush()
            except Exception:
                pass
            try:
                self._fp.close()
            except Exception:
                pass

    def _atexit_cleanup(self) -> None:
        """Fires on normal exit and most unhandled exceptions.

        Ensures: (a) heartbeat thread is signaled to stop; (b) if the
        stage exited *without* calling stage_done (e.g. uncaught
        exception), we record that in metadata so the trace isn't
        ambiguous between "in progress" and "crashed".
        """
        if self._hb_stop is not None:
            self._hb_stop.set()
        if not self._stage_done_called and not self._closed:
            try:
                self.emit("stage_aborted", status="error",
                          reason="atexit reached without stage_done")
                update_metadata(self.out_dir, lambda m: m.setdefault("stages", {}).update({
                    self.stage: {
                        "started_at": self._stage_started_at,
                        "ended_at": _now_iso(),
                        "elapsed_s": round(time.monotonic() - self._t0, 1),
                        "success": False,
                        "aborted": True,
                    }
                }))
            except Exception:
                pass
        try:
            self.close()
        except Exception:
            pass


# ----------------------------------------------------------------------
# runs.jsonl — cross-run aggregate
# ----------------------------------------------------------------------

def append_run_summary(out_dir: str, summary: dict) -> str:
    """Append one line to ``runs.jsonl`` in out_dir's parent.

    Automatically injects ``run_id``, ``ts``, ``hostname`` if absent.
    """
    parent = os.path.dirname(os.path.abspath(out_dir)) or "."
    runs_path = os.path.join(parent, "runs.jsonl")

    if "run_id" not in summary:
        meta = read_metadata(out_dir)
        if meta.get("run_id"):
            summary["run_id"] = meta["run_id"]
    summary.setdefault("ts", _now_iso())
    summary.setdefault("hostname", os.environ.get("COMPUTERNAME")
                       or os.environ.get("HOSTNAME") or "")

    line = json.dumps(summary, ensure_ascii=False, default=str) + "\n"
    # Two clips finishing at once append concurrently; O_APPEND is only atomic
    # for small writes on POSIX and gives no such guarantee on Windows, so a
    # summary line can end up interleaved into another and neither parses.
    with _cross_process_lock(runs_path):
        with open(runs_path, "a", encoding="utf-8") as f:
            f.write(line)
    return runs_path


def git_hash(scripts_dir: str | None = None) -> str:
    """Short commit of the scripts checkout, or ``v<__version__>`` outside git.

    An installed copy (tarball / pip / a skill dir that is not a repo) has no
    commit, and reporting "unknown" in every runs.jsonl row made the field
    useless for exactly the users who cannot check git themselves.
    """
    cwd = scripts_dir or os.path.dirname(os.path.abspath(__file__))
    try:
        out = subprocess.check_output(
            ["git", "-C", cwd, "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL, text=True, timeout=2,
        ).strip()
        if out:
            return out
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    return f"v{__version__}"
