"""2026-10-05: no CTranslate2 model may be destroyed inside a transcribe process.

Run: python test_ct2_teardown.py   (no GPU; fakes stand in for faster_whisper)
"""
import gc
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import transcribe_video as tv  # noqa: E402

DESTROYED: list = []


class FakeModel:
    def __init__(self, fail_with=None):
        self.fail_with = fail_with

    def __del__(self):
        DESTROYED.append(self)

    def transcribe(self, *a, **k):
        if self.fail_with:
            raise RuntimeError(self.fail_with)
        return iter([types.SimpleNamespace(start=0, end=1, text="x")]), object()


class FakePipeline:
    def __init__(self, model):
        self.model = model

    def transcribe(self, *a, **k):
        return self.model.transcribe()


class NullLog:
    def __getattr__(self, _):
        return lambda *a, **k: None


def _fw_module():
    return types.SimpleNamespace(BatchedInferencePipeline=FakePipeline)


class FallbackTests(unittest.TestCase):
    def setUp(self):
        tv._KEEPALIVE.clear()   # earlier tests' parked fakes die here
        gc.collect()
        DESTROYED.clear()
        self.p = mock.patch.dict(sys.modules, {"faster_whisper": _fw_module()})
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def _run(self, factory, attempt, sizes=(4, 2)):
        return tv.transcribe_with_fallback("a.wav", factory, {}, "en", list(sizes),
                                           None, NullLog(), attempt=attempt)

    def test_oom_batched_raises_retry_and_keeps_model(self):
        with self.assertRaises(tv.OomRetry) as cm:
            self._run(lambda: FakeModel("CUDA out of memory"), 0)
        self.assertEqual(cm.exception.next_attempt, 1)
        gc.collect()
        self.assertEqual(DESTROYED, [])
        self.assertTrue(any(isinstance(m, FakeModel) for m in tv._KEEPALIVE))

    def test_oom_at_last_batch_moves_to_sequential(self):
        with self.assertRaises(tv.OomRetry) as cm:
            self._run(lambda: FakeModel("out of memory"), 1)
        self.assertEqual(cm.exception.next_attempt, 2)   # plan [4,2,1]
        gc.collect()
        self.assertEqual(DESTROYED, [])

    def test_sequential_failure_raises_without_destroy(self):
        with self.assertRaises(RuntimeError):
            self._run(lambda: FakeModel("out of memory"), 2)
        with self.assertRaises(RuntimeError):
            self._run(lambda: FakeModel("boom"), 2)
        gc.collect()
        self.assertEqual(DESTROYED, [])

    def test_non_oom_batched_error_is_not_retried(self):
        with self.assertRaises(RuntimeError):
            self._run(lambda: FakeModel("bad compute type"), 0)
        gc.collect()
        self.assertEqual(DESTROYED, [])

    def test_success_paths(self):
        _, _, bs, fb, _ = self._run(lambda: FakeModel(), 0)
        self.assertEqual((bs, fb), (4, False))
        _, _, bs, fb, _ = self._run(lambda: FakeModel(), 2)
        self.assertEqual((bs, fb), (1, True))

    def test_no_in_process_del_in_source(self):
        src = open(tv.__file__, encoding="utf-8").read()
        code = "\n".join(l for l in src.splitlines()
                         if not l.lstrip().startswith("#") and "`" not in l)
        self.assertNotIn("del model", code)
        self.assertNotIn("gc.collect", code)


class ParentLoopTests(unittest.TestCase):
    def test_parent_relaunches_with_next_attempt(self):
        d = tempfile.mkdtemp()
        handoff = os.path.join(d, tv.HANDOFF_NAME)
        tj = os.path.join(d, "transcript.json")
        calls = []

        def fake_run(cmd, **kw):
            att = int(cmd[cmd.index("--attempt") + 1])
            calls.append(att)
            if att < 2:   # OOM twice, then sequential succeeds
                with open(handoff + ".oom", "w") as f:
                    json.dump({"next_attempt": att + 1}, f)
                return types.SimpleNamespace(returncode=tv.OOM_RETRY_RC)
            with open(tj, "w") as f:
                json.dump([], f)
            with open(handoff, "w") as f:
                json.dump({"n_segments": 0}, f)
            return types.SimpleNamespace(returncode=0)

        args = types.SimpleNamespace(keep_audio=True, model="m", lang="en",
                                     retry_timeout=None)
        with mock.patch.object(tv.subprocess, "run", fake_run):
            rc = tv._run_parent(args, d, os.path.join(d, "audio.wav"), tj,
                                handoff, "x.m4a")
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [0, 1, 2])
        self.assertFalse(os.path.exists(handoff + ".oom"))

    def test_parent_failed_child_no_retry(self):
        d = tempfile.mkdtemp()
        handoff = os.path.join(d, tv.HANDOFF_NAME)
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return types.SimpleNamespace(returncode=2)

        args = types.SimpleNamespace(keep_audio=True, model="m", lang="en",
                                     retry_timeout=None)
        with mock.patch.object(tv.subprocess, "run", fake_run):
            rc = tv._run_parent(args, d, os.path.join(d, "a.wav"),
                                os.path.join(d, "t.json"), handoff, "x")
        self.assertEqual((rc, len(calls)), (2, 1))


class RecLog:
    """StageLogger stand-in: records events and whether the handoff existed yet."""

    def __init__(self, rec, handoff):
        self.rec, self.handoff = rec, handoff

    def emit(self, name, **kw):
        self.rec.append(("emit", name))

    def stage_done(self, **kw):
        self.rec.append(("stage_done", kw.get("success"), os.path.exists(self.handoff)))

    def __getattr__(self, _):
        return lambda *a, **k: None


class GroqChildTests(unittest.TestCase):
    """2026-10: the real child main() on the groq path, fakes only at the edges."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.media = os.path.join(self.d, "talk.m4a")
        open(self.media, "wb").close()
        self.handoff = os.path.join(self.d, tv.HANDOFF_NAME)
        self.rec, self.groq_calls = [], []

        def fake_groq(media, **kw):
            self.groq_calls.append(media)
            return [{"start": 0.0, "end": 2.0, "text": "hello"},
                    {"start": 2.0, "end": 4.0, "text": "world"}]

        def fake_summary(out_dir, row):
            self.rec.append(("runs.jsonl", row["engine"], os.path.exists(self.handoff)))

        def fake_extract(*a, **k):
            self.rec.append(("extract_audio",))

        def no_lease(*a, **k):
            raise RuntimeError("test stops here")

        self.patches = [
            mock.patch.dict(sys.modules, {
                "groq_asr": types.SimpleNamespace(transcribe_groq=fake_groq),
                "retranscribe_segment": types.SimpleNamespace(detect_collapses=lambda r: [])}),
            mock.patch.object(tv, "require_binaries", lambda *a: None),
            mock.patch.object(tv, "load_config", lambda: {}),
            mock.patch.object(tv, "resolve_model", lambda m, c: m),
            mock.patch.object(tv, "probe_duration_seconds", lambda p: 321.5),
            mock.patch.object(tv, "StageLogger", lambda *a, **k: RecLog(self.rec, self.handoff)),
            mock.patch.object(tv, "lecture_name", lambda d: "talk"),
            mock.patch.object(tv, "append_run_summary", fake_summary),
            mock.patch.object(tv, "git_hash", lambda: "test"),
            mock.patch.object(tv, "extract_audio", fake_extract),
            mock.patch.object(tv, "gpu_lease_ctx", lambda *a, **k: mock.Mock(__enter__=no_lease)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def _child(self, cmd):
        # cmd is what _run_parent would exec; run it as an in-process child
        with mock.patch.object(sys, "argv", ["transcribe_video.py", *cmd[2:]]):
            return types.SimpleNamespace(returncode=tv.main())

    def test_groq_success_parent_rc0(self):
        argv = ["transcribe_video.py", self.media, "--lang", "en", "--engine", "groq",
                "--output-dir", self.d]
        children = []

        def fake_run(cmd, **kw):
            children.append(cmd)
            return self._child(cmd)

        with mock.patch.object(sys, "argv", argv), mock.patch.object(tv.subprocess, "run", fake_run):
            rc = tv.main()   # no --decode-only: this is the parent
        self.assertEqual(rc, 0)
        self.assertEqual(len(children), 1)
        self.assertEqual(len(self.groq_calls), 1)
        # handoff written AFTER stage_done and runs.jsonl, then consumed by the parent
        self.assertIn(("stage_done", True, False), self.rec)
        self.assertIn(("runs.jsonl", "groq", False), self.rec)
        self.assertFalse(os.path.exists(self.handoff))
        with open(os.path.join(self.d, "transcript.json"), encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)), 2)

    def test_groq_handoff_contents(self):
        cmd = ["py", "tv", self.media, "--lang", "en", "--engine", "groq", "--output-dir", self.d,
               "--decode-only", "--keep-audio", "--attempt", "0"]
        self.assertEqual(self._child(cmd).returncode, 0)
        with open(self.handoff, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"n_segments": 2, "device_cpu": False, "duration_s": 321.5})

    def test_oom_relaunch_skips_groq_and_extract(self):
        with open(os.path.join(self.d, "audio.wav"), "wb") as f:
            f.write(b"RIFF")
        cmd = ["py", "tv", self.media, "--lang", "en", "--engine", "groq", "--output-dir", self.d,
               "--decode-only", "--keep-audio", "--attempt", "1"]
        rc = self._child(cmd).returncode   # stops at the (faked) GPU lease
        self.assertEqual(rc, 1)
        self.assertEqual(self.groq_calls, [])
        self.assertNotIn(("extract_audio",), self.rec)
        self.assertIn(("emit", "groq_skipped"), self.rec)
        self.assertIn(("emit", "audio_reused"), self.rec)


if __name__ == "__main__":
    unittest.main(verbosity=2)
