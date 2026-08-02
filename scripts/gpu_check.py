"""Pre-flight GPU VRAM gate for the lecture-to-notes pipeline.

Goes beyond "is there free VRAM" -- also detects:

1. **Heavy concurrent users**: another process owning >1GB AND GPU
   utilization >40% means a model is *actively* running. Whisper/VLM
   will queue or fragment. Block.
2. **Fragmented free memory**: nvidia-smi reports raw free bytes, but
   CUDA may fail to allocate even when nominally enough is free (other
   process holds non-contiguous chunks, or the driver is in a bad
   state). We run a *real* allocation test in a subprocess so the
   probe's context is reclaimed by the OS on exit.
3. **Transient spikes**: a one-off util burst shouldn't kill the run.
   Poll 3 times at 2s intervals; only exit 2 if blocked all three times.

Usage
-----
    python gpu_check.py [--min-free-mb 5000] [--util-threshold 40]
                        [--polls 3] [--poll-interval 2.0]
                        [--alloc-fraction 0.8] [--alloc-cap-mb 0]
                        [--out-dir DIR] [--json] [--quiet]

Exit codes
----------
    0 = ok (enough VRAM, no other heavy users, alloc test passed)
    1 = warning (another process holds >1GB but util is low -- proceed)
    2 = blocked (insufficient free / heavy concurrent user / alloc failed)

    NOTE for shell callers: 1 means "proceed with a caveat", not failure.
    Under ``set -e`` test explicitly, e.g. ``gpu_check.py || [ $? -le 1 ]``.

Sizing (float16 weights + decode workspace, measured on this pipeline):
    faster-whisper large-v3   ~4.5 GB  -> default ``--min-free-mb 5000``
    faster-whisper breeze25   ~4.5 GB  (same large-v3 architecture)
    BatchedInferencePipeline  +~1.5 GB -> pass ``--min-free-mb 6000``
    minicpm-v:8b Q4 (ollama)  ~5.5 GB  -> pass ``--min-free-mb 6000``

The pipeline runs whisper and VLM **sequentially** (never in parallel),
so peak is single-model. Don't add them.

Multi-GPU: all queries and the allocation probe target ONE device — the first
entry of ``CUDA_VISIBLE_DEVICES`` if set, else device 0 — matching what the
CUDA-using stages will actually grab.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

# _log is sibling; import is best-effort so the script still works if invoked
# in isolation — but say so, or a missing logger looks like a missing --out-dir.
try:
    from _log import StageLogger
except Exception as _log_err:  # noqa: BLE001
    print(f"WARNING: _log unavailable ({_log_err}); gpu_check will not write "
          "progress_gpu_check.jsonl", file=sys.stderr)
    StageLogger = None  # type: ignore


def target_gpu_index() -> int:
    """Physical GPU this check applies to.

    ``CUDA_VISIBLE_DEVICES=1`` makes CUDA's device 0 the machine's device 1, and
    nvidia-smi always speaks physical indices — so a check that hardcoded 0
    would report on a card the stage never touches. Non-numeric entries (UUIDs)
    and empty values fall back to 0.
    """
    raw = (os.environ.get("CUDA_VISIBLE_DEVICES") or "").strip()
    if not raw:
        return 0
    first = raw.split(",")[0].strip()
    try:
        return max(0, int(first))
    except ValueError:
        return 0


# ----------------------------------------------------------------------
# nvidia-smi probe
# ----------------------------------------------------------------------

def query_gpu(gpu_index: int = 0) -> dict | None:
    """Single nvidia-smi snapshot of one GPU. None if nvidia-smi is missing.

    ``--id`` plus taking the first line matters: on a multi-GPU host the
    unfiltered query prints one row per card, the 4-way unpack raised
    ValueError, and the caller read that as "no nvidia-smi -> CPU mode, exit 0".
    The whole VRAM gate silently passed on exactly the machines with the most
    contention.
    """
    try:
        out = subprocess.check_output(
            ['nvidia-smi', f'--id={gpu_index}',
             '--query-gpu=memory.used,memory.total,memory.free,utilization.gpu',
             '--format=csv,noheader,nounits'],
            stderr=subprocess.DEVNULL, text=True, timeout=3,
        ).strip().splitlines()
        if not out:
            return None
        used, total, free, util = [int(x.strip()) for x in out[0].split(',')]
    except (subprocess.SubprocessError, FileNotFoundError, ValueError, OSError):
        return None

    procs: list[dict] = []
    n_unattributed = 0
    try:
        p_out = subprocess.check_output(
            ['nvidia-smi', f'--id={gpu_index}',
             '--query-compute-apps=pid,process_name,used_memory',
             '--format=csv,noheader,nounits'],
            stderr=subprocess.DEVNULL, text=True, timeout=3,
        ).strip()
        for line in p_out.splitlines():
            parts = [x.strip() for x in line.split(',')]
            if len(parts) < 3:
                continue
            try:
                mem = int(parts[2])
            except ValueError:
                # Windows WDDM reports per-process memory as [N/A] — the driver,
                # not nvidia-smi, owns allocation there. Keep the process (it IS
                # a real GPU user) with mem_mb=None and count it, so the caller
                # can explain why heavy-user detection is degraded instead of
                # pretending the GPU is idle.
                n_unattributed += 1
                procs.append({'pid': parts[0], 'name': parts[1], 'mem_mb': None})
                continue
            procs.append({'pid': parts[0], 'name': parts[1], 'mem_mb': mem})
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass

    return {'gpu_index': gpu_index,
            'used_mb': used, 'total_mb': total, 'free_mb': free,
            'util_pct': util, 'processes': procs,
            'n_unattributed_procs': n_unattributed}


def classify(info: dict, min_free_mb: int, util_threshold: int) -> dict:
    """Apply rules to one snapshot. Returns dict with verdict + reasons."""
    # No self-PID exclusion: this process never allocates CUDA memory (the
    # probe runs in a short-lived child), so our own PID cannot appear here —
    # the old filter compared against a PID that was never in the list.
    heavy_others = [p for p in info['processes']
                    if p['mem_mb'] is not None and p['mem_mb'] >= 1024]
    reasons: list[str] = []
    if info['free_mb'] < min_free_mb:
        reasons.append(f"free {info['free_mb']}MB < threshold {min_free_mb}MB")
    if heavy_others and info['util_pct'] > util_threshold:
        names = ", ".join(f"{p['name']}({p['mem_mb']}MB)" for p in heavy_others)
        reasons.append(
            f"util {info['util_pct']}% > {util_threshold}% with heavy user(s): {names}")
    if reasons:
        verdict = 'blocked'
    elif heavy_others:
        verdict = 'warning'
    else:
        verdict = 'ok'
    return {'verdict': verdict, 'reasons': reasons,
            'heavy_other_processes': heavy_others}


# ----------------------------------------------------------------------
# Real allocation test (in a subprocess so context is freed on exit)
# ----------------------------------------------------------------------

# Probe strategy: try CUDA Driver API via ctypes first (no torch dep — works
# anywhere nvcuda.dll/libcuda.so is present). Fall back to torch if ctypes
# can't load the driver. Both paths run in a subprocess so the allocation is
# released by OS process exit, even if the driver leaks the context.
#
# Device 0 here is correct under CUDA_VISIBLE_DEVICES, not a bug: the child
# inherits the env, and both the driver API (cuDeviceGet) and torch ('cuda:0')
# index into the ALREADY-FILTERED device list, so ordinal 0 is the same card the
# transcription stage will use. Only nvidia-smi needs the physical index —
# see target_gpu_index().
_ALLOC_PROBE = r"""
import ctypes, sys

target_mb = int(sys.argv[1])
nbytes = target_mb * 1024 * 1024

# --- attempt 1: ctypes against nvcuda.dll / libcuda.so ---
def try_ctypes():
    try:
        if sys.platform == 'win32':
            cuda = ctypes.WinDLL('nvcuda.dll')
        else:
            cuda = ctypes.CDLL('libcuda.so.1')
    except OSError as e:
        return None, f'driver_load:{e}'
    # cuInit(unsigned int flags) -> CUresult
    cuda.cuInit.argtypes = [ctypes.c_uint]
    cuda.cuInit.restype = ctypes.c_int
    cuda.cuDeviceGet.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_int]
    cuda.cuDeviceGet.restype = ctypes.c_int
    cuda.cuDevicePrimaryCtxRetain.argtypes = [
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_int]
    cuda.cuDevicePrimaryCtxRetain.restype = ctypes.c_int
    cuda.cuCtxSetCurrent.argtypes = [ctypes.c_void_p]
    cuda.cuCtxSetCurrent.restype = ctypes.c_int
    cuda.cuMemAlloc_v2.argtypes = [
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_size_t]
    cuda.cuMemAlloc_v2.restype = ctypes.c_int
    cuda.cuMemFree_v2.argtypes = [ctypes.c_void_p]
    cuda.cuMemFree_v2.restype = ctypes.c_int
    cuda.cuDevicePrimaryCtxRelease.argtypes = [ctypes.c_int]
    cuda.cuDevicePrimaryCtxRelease.restype = ctypes.c_int

    r = cuda.cuInit(0)
    if r != 0:
        return False, f'cuInit={r}'
    dev = ctypes.c_int()
    r = cuda.cuDeviceGet(ctypes.byref(dev), 0)
    if r != 0:
        return False, f'cuDeviceGet={r}'
    ctx = ctypes.c_void_p()
    r = cuda.cuDevicePrimaryCtxRetain(ctypes.byref(ctx), dev)
    if r != 0:
        return False, f'cuDevicePrimaryCtxRetain={r}'
    cuda.cuCtxSetCurrent(ctx)
    ptr = ctypes.c_void_p()
    r = cuda.cuMemAlloc_v2(ctypes.byref(ptr), ctypes.c_size_t(nbytes))
    if r != 0:
        cuda.cuDevicePrimaryCtxRelease(dev)
        return False, f'cuMemAlloc={r}'
    cuda.cuMemFree_v2(ptr)
    cuda.cuDevicePrimaryCtxRelease(dev)
    return True, 'ctypes_ok'

# --- attempt 2: torch fallback ---
def try_torch():
    try:
        import torch
    except Exception as e:
        return None, f'no_torch:{e}'
    if not torch.cuda.is_available():
        return None, 'torch_no_cuda'
    n = nbytes // 4
    try:
        t = torch.zeros(n, dtype=torch.float32, device='cuda:0')
        del t
        torch.cuda.synchronize()
        torch.cuda.empty_cache()
        return True, 'torch_ok'
    except RuntimeError as e:
        return False, str(e).splitlines()[0][:200]

ok, detail = try_ctypes()
if ok is None:
    # Driver missing or couldn't probe via ctypes — try torch.
    ok2, detail2 = try_torch()
    if ok2 is None:
        print(f"SKIP:{detail}|{detail2}", file=sys.stderr)
        sys.exit(3)  # treated as 'skip' by parent
    ok, detail = ok2, detail2

if ok:
    print(f"OK:{target_mb}MB:{detail}")
    sys.exit(0)
print(f"ALLOC_FAIL:{detail}", file=sys.stderr)
sys.exit(5)
"""


def cuda_alloc_test(target_mb: int, timeout: int = 30) -> tuple[str, str]:
    """Run torch.zeros() in a fresh subprocess so context is reclaimed on exit.

    Returns (status, detail):
        ('pass', '...MB')         — allocation succeeded
        ('skip', 'no torch')      — torch unavailable; treat as pass
        ('skip', 'no cuda')       — torch present but CUDA off; treat as pass
        ('fail', error_message)   — driver said no; this is a real block
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _ALLOC_PROBE, str(target_mb)],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ('fail', f"alloc probe hung > {timeout}s — likely driver wedged")
    except (subprocess.SubprocessError, OSError) as e:
        return ('skip', f"could not spawn probe: {e}")

    if proc.returncode == 0:
        return ('pass', proc.stdout.strip() or f"{target_mb}MB")
    if proc.returncode == 3:
        # Neither ctypes nor torch could probe — driver/runtime not loadable
        # here. Don't block on this: the real stages (whisper via ctranslate2)
        # have their own CUDA path and will fail loudly if broken.
        err = (proc.stderr or '').strip().splitlines()
        return ('skip', err[-1] if err else 'cannot probe cuda')
    err = (proc.stderr or proc.stdout).strip().splitlines()
    return ('fail', err[-1] if err else f"exit {proc.returncode}")


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exit codes: 0 = ok, 1 = warning (other GPU users, low util — "
               "safe to proceed), 2 = blocked (do not start a GPU stage). "
               "Under `set -e`, 1 will abort your script: test with "
               "`python gpu_check.py || [ $? -le 1 ]`.")
    ap.add_argument('--min-free-mb', type=int, default=5000,
                    help='Minimum free VRAM in MB (default 5000 for sequential whisper; pass 6000 for --batched)')
    ap.add_argument('--util-threshold', type=int, default=40,
                    help='Util %% above which a heavy other process is treated as actively running (default 40)')
    ap.add_argument('--polls', type=int, default=3,
                    help='How many times to poll before declaring blocked (default 3)')
    ap.add_argument('--poll-interval', type=float, default=2.0,
                    help='Seconds between polls (default 2.0)')
    ap.add_argument('--alloc-fraction', type=float, default=0.8,
                    help='Target alloc size as fraction of free VRAM (default 0.80). '
                         'The old 0.10 probed ~500MB to "prove" a 4.5GB contiguous '
                         'allocation would work — it could not fail in the '
                         'fragmentation scenario it exists to catch.')
    ap.add_argument('--alloc-cap-mb', type=int, default=0,
                    help='Upper bound on the alloc target in MB. 0 (default) means '
                         'cap at --min-free-mb, i.e. probe roughly what the model '
                         'will actually need.')
    ap.add_argument('--skip-alloc-test', action='store_true',
                    help='Skip the subprocess CUDA allocation probe entirely')
    ap.add_argument('--out-dir', default=None,
                    help='If set, emit progress_gpu_check.jsonl in this dir')
    ap.add_argument('--quiet', action='store_true')
    ap.add_argument('--json', action='store_true',
                    help='Print the full info dict as JSON to stdout')
    args = ap.parse_args()

    # Validation via ap.error, not assert: `python -O` strips asserts, and
    # --polls 0 used to skip the loop entirely and then trip
    # `assert final_snapshot is not None` with no explanation.
    if args.polls < 1:
        ap.error('--polls must be >= 1')
    if args.poll_interval < 0:
        ap.error('--poll-interval must be >= 0')
    if not 0 < args.alloc_fraction <= 1:
        ap.error('--alloc-fraction must be in (0, 1]')
    if args.min_free_mb < 0 or args.alloc_cap_mb < 0:
        ap.error('--min-free-mb and --alloc-cap-mb must be >= 0')

    gpu_index = target_gpu_index()

    log = None
    if args.out_dir and StageLogger is not None:
        try:
            log = StageLogger('gpu_check', args.out_dir,
                              extra={'min_free_mb': args.min_free_mb,
                                     'util_threshold': args.util_threshold},
                              stdout_mirror=not args.quiet)
            log.start(polls=args.polls, poll_interval=args.poll_interval)
        except Exception:
            log = None

    # --- Polling loop (3x at 2s intervals) ---
    snapshots: list[dict] = []
    final_snapshot: dict | None = None
    for i in range(args.polls):
        info = query_gpu(gpu_index)
        if info is None:
            if not args.quiet:
                print('GPU_CHECK: nvidia-smi unavailable (CPU mode assumed)',
                      file=sys.stderr)
            if log:
                log.stage_done(success=True, verdict='no_nvidia_smi',
                               exit_code=0)
                log.close()
            sys.exit(0)
        classification = classify(info, args.min_free_mb, args.util_threshold)
        snap = {**info, **classification, 'poll': i + 1}
        snapshots.append(snap)
        final_snapshot = snap
        if log:
            log.progress(poll=i + 1, free_mb=info['free_mb'],
                         util_pct=info['util_pct'],
                         verdict=classification['verdict'],
                         reasons=classification['reasons'])
        if classification['verdict'] != 'blocked':
            break  # Transient — proceed
        if i < args.polls - 1:
            time.sleep(args.poll_interval)

    if final_snapshot is None:  # unreachable: polls >= 1 and None exits above
        print('GPU_CHECK: internal error — no snapshot collected', file=sys.stderr)
        sys.exit(2)

    if final_snapshot['n_unattributed_procs'] and not args.quiet:
        print(f"GPU_CHECK: {final_snapshot['n_unattributed_procs']} process(es) hold "
              "the GPU but report no per-process memory ([N/A]) — normal on Windows "
              "WDDM, where the driver owns allocation. Heavy-user detection is "
              "degraded; the free-VRAM threshold and the alloc probe still apply.",
              file=sys.stderr)

    nvsmi_blocked = (final_snapshot['verdict'] == 'blocked'
                     and all(s['verdict'] == 'blocked' for s in snapshots))

    # --- CUDA allocation test (only if nvidia-smi numbers look ok) ---
    alloc_status = 'skipped'
    alloc_detail = 'numbers say blocked'
    alloc_target_mb = 0

    if not nvsmi_blocked and not args.skip_alloc_test:
        # Probe near what the model actually needs. Capped by BOTH a fraction of
        # free VRAM (never request more than exists — that would fail for the
        # wrong reason) and --min-free-mb (no point proving more than the stage
        # requires), floor 64MB so a tiny free pool still gets a real test.
        cap_mb = args.alloc_cap_mb or args.min_free_mb
        alloc_target_mb = max(64, min(int(final_snapshot['free_mb'] * args.alloc_fraction),
                                      cap_mb or final_snapshot['free_mb']))
        alloc_status, alloc_detail = cuda_alloc_test(alloc_target_mb)
        if log:
            # 'status' is reserved by StageLogger.emit for the event status;
            # passing it here used to collide, hence the old 'status_' typo-lookalike.
            log.progress(event_name='alloc_test',
                         target_mb=alloc_target_mb,
                         alloc_status=alloc_status, detail=alloc_detail)

    # --- Final verdict ---
    if nvsmi_blocked:
        verdict = 'blocked'
        reason = "; ".join(final_snapshot['reasons'])
    elif alloc_status == 'fail':
        verdict = 'blocked'
        reason = f"CUDA alloc test failed ({alloc_target_mb}MB): {alloc_detail}"
    elif final_snapshot['verdict'] == 'warning':
        verdict = 'warning'
        names = ", ".join(f"{p['name']}({p['mem_mb']}MB)"
                          for p in final_snapshot['heavy_other_processes'])
        reason = f"other GPU users present but util low: {names}"
    else:
        verdict = 'ok'
        reason = ''

    exit_code = {'ok': 0, 'warning': 1, 'blocked': 2}[verdict]
    final_snapshot['alloc_test'] = {'status': alloc_status,
                                    'detail': alloc_detail,
                                    'target_mb': alloc_target_mb}
    final_snapshot['final_verdict'] = verdict
    final_snapshot['final_reason'] = reason
    final_snapshot['polls_taken'] = len(snapshots)

    if args.json:
        print(json.dumps(final_snapshot, ensure_ascii=False, indent=2))
    elif not args.quiet:
        print(f"GPU_CHECK: {verdict.upper()} | gpu={gpu_index} | "
              f"free={final_snapshot['free_mb']}MB/{final_snapshot['total_mb']}MB | "
              f"util={final_snapshot['util_pct']}% | "
              f"polls={len(snapshots)} | "
              f"alloc={alloc_status}({alloc_target_mb}MB)")
        if final_snapshot['heavy_other_processes']:
            print("  Other heavy GPU users (>=1GB):")
            for p in final_snapshot['heavy_other_processes']:
                print(f"    PID {p['pid']:>6} {p['name']:<40} {p['mem_mb']:>5} MB")
        if reason:
            print(f"  Reason: {reason}")
        if verdict == 'blocked':
            print("  → wait for other tasks or close GPU apps before re-running.")

    if log:
        log.stage_done(success=(verdict != 'blocked'),
                       verdict=verdict, exit_code=exit_code,
                       polls_taken=len(snapshots),
                       alloc_status=alloc_status,
                       alloc_target_mb=alloc_target_mb,
                       reason=reason)
        log.close()
    sys.exit(exit_code)


if __name__ == '__main__':
    main()
