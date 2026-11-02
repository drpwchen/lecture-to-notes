"""scripts/gemini_asr.py without the network: repetition-loop detection, the resend /
drop path in main(), and the defaults (env-only key, --via api)."""
import json
import os
import sys

import pytest

import gemini_asr as G

SPEECH = [f"這是第 {i} 句不同的內容，講到 L{i % 5} 的 nerve block。" for i in range(200)]
LOOP = ["謝謝。", "辛苦了。", "謝謝大家。", "好。"]


def _lines(texts, step=3):
    return [(i * step, t) for i, t in enumerate(texts)]


def _part(texts, step=3):
    return "\n".join(f"[{t // 60:02d}:{t % 60:02d}] {x}" for t, x in _lines(texts, step)) + "\n"


def test_clean_speech_has_no_loop():
    assert G.find_loop(_lines(SPEECH)) is None


def test_backchannel_is_not_a_loop():
    # hands-on teaching: many short "好。" / "對。" between real lines must not trigger
    texts = []
    for i, s in enumerate(SPEECH[:120]):
        texts += [s, "好。" if i % 2 else "對。"]
    assert G.find_loop(_lines(texts)) is None


def test_four_line_cycle_is_found_at_its_start():
    texts = SPEECH[:146] + ["我們休息五分鐘。"] + LOOP * 120
    li = G.find_loop(_lines(texts))
    assert li is not None and 146 <= li <= 147 + len(LOOP)


def test_short_input_never_loops():
    assert G.find_loop(_lines(LOOP * 5)) is None   # < LOOP_W lines


def test_api_key_is_env_only(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        G.api_key()
    monkeypatch.setenv("GEMINI_API_KEY", " k ")
    assert G.api_key() == "k"


def _run(tmp_path, monkeypatch, parts, resend):
    """main() over cached .part files; Gemini is a stub returning `resend`."""
    for k, txt in enumerate(parts):
        (tmp_path / f"transcript_gemini.{k:03d}.part").write_text(txt, encoding="utf-8")
    calls = []
    monkeypatch.setattr(G, "duration", lambda p: 600.0 * len(parts) - 1)

    def fake(a, off, length, prompt, keybox, tag):
        calls.append((off, tag))
        return resend, "stub"
    monkeypatch.setattr(G, "gemini_range", fake)
    monkeypatch.setattr(G, "fill_local", lambda *x: pytest.fail("local fill not expected"))
    monkeypatch.setattr(sys, "argv", ["gemini_asr.py", "x.mp4", "--output-dir", str(tmp_path),
                                      "--anchor", "none", "--lang", "zh"])
    G.main()
    out = json.loads((tmp_path / "transcript_gemini.json").read_text(encoding="utf-8"))
    return out, calls


def test_loop_resend_clean(tmp_path, monkeypatch):
    looping = _part(SPEECH[:100] + LOOP * 60)
    clean = _part(SPEECH[:190])
    out, calls = _run(tmp_path, monkeypatch, [looping], clean)
    assert calls == [(0, "chunk000")]
    assert (tmp_path / "transcript_gemini.000.part.loop").exists()
    assert not any(x["text"] in LOOP for x in out) and len(out) == 190
    # rerun: .loop exists and the resent part is clean -> nothing is sent again
    out2, calls2 = _run(tmp_path, monkeypatch, [clean], clean)
    assert calls2 == [] and out2 == out


def test_loop_resend_still_loops_drops_the_loop(tmp_path, monkeypatch):
    looping = _part(SPEECH[:100] + LOOP * 60)
    out, calls = _run(tmp_path, monkeypatch, [looping], looping)
    assert len(calls) == 1
    assert [x["text"] for x in out] == SPEECH[:100]


def test_default_route_is_api_without_config(monkeypatch):
    monkeypatch.delenv("LECTURE_GEMINI_VIA", raising=False)
    assert G.default_via() == "api"   # no config.yaml in the repo
    monkeypatch.setenv("LECTURE_GEMINI_VIA", "agy")
    assert G.default_via() == "agy"
    monkeypatch.setenv("LECTURE_GEMINI_VIA", "bogus")
    assert G.default_via() == "api"


def test_no_local_secret_store_and_tos_note():
    src = open(G.__file__, encoding="utf-8").read()
    assert "Terms of service" in src
    assert "_platform" not in src and ".claude" not in src


# --- chunking, parsing, gaps, fills (review round 2026-11-02) -------------------------

def _main(tmp_path, monkeypatch, total, gem, fill=None, anchor_lines=None, extra=()):
    """main() with a stubbed clock and Gemini; gem(off, length, tag) -> text. Returns (exit, calls, fills)."""
    calls, fills = [], []
    monkeypatch.setattr(G, "duration", lambda p: float(total))

    def fake(a, off, length, prompt, keybox, tag):
        calls.append((round(off), round(length), tag))
        return gem(off, length, tag), "stub"
    monkeypatch.setattr(G, "gemini_range", fake)

    def fake_fill(media, off, clen, part, model, lang):
        fills.append((round(off), round(clen)))
        return [(float(t), f"LOCAL{t}") for t in range(0, int(clen), 10)]
    monkeypatch.setattr(G, "fill_local", fake_fill)
    anchor = "none"
    if anchor_lines is not None:
        p = tmp_path / "anchor.txt"
        p.write_text("".join(f"[{int(t) // 60:02d}:{int(t) % 60:02d}] {x}\n" for t, x in anchor_lines), encoding="utf-8")
        anchor = str(p)
    argv = ["gemini_asr.py", "x.mp4", "--output-dir", str(tmp_path), "--anchor", anchor, "--lang", "zh", *extra]
    if fill:
        argv += ["--fill-blocked", fill]
    monkeypatch.setattr(sys, "argv", argv)
    try:
        G.main()
        rc = 0
    except SystemExit as e:
        rc = e.code
    return rc, calls, fills


def test_exact_multiple_duration_sends_no_empty_chunk(tmp_path, monkeypatch):
    rc, calls, _ = _main(tmp_path, monkeypatch, 1200, lambda o, l, t: _part(SPEECH[:150], 4))
    assert rc == 0 and [(o, l) for o, l, _ in calls] == [(0, 600), (600, 600)]


def test_tail_chunk_is_sent_with_its_own_length(tmp_path, monkeypatch):
    rc, calls, _ = _main(tmp_path, monkeypatch, 1250, lambda o, l, t: _part(SPEECH[:10], 4))
    assert rc == 0 and [(o, l) for o, l, _ in calls] == [(0, 600), (600, 600), (1200, 50)]


def test_inline_stamps_are_split_into_lines():
    g = G.parse_part("[00:01] 第一句。 [00:05] 第二句。 [00:09] 第三句。")
    assert [t for t, _ in g] == [1, 5, 9] and g[1][1] == "第二句。"


def test_loop_from_first_line_falls_back_to_local_fill(tmp_path, monkeypatch):
    rc, calls, fills = _main(tmp_path, monkeypatch, 599, lambda o, l, t: _part(LOOP * 60), fill="breeze25")
    assert rc == 0 and len(calls) == 2 and fills == [(0, 599)]   # first try + one resend, then local
    out = json.loads((tmp_path / "transcript_gemini.json").read_text(encoding="utf-8"))
    assert out and all(x["source"] == "breeze25" for x in out)


def test_unfilled_gap_exits_2(tmp_path, monkeypatch):
    local = [(t, f"本機第 {t} 秒的字") for t in range(0, 600, 3)]
    def gem(off, length, tag):   # chunk: only the first 150 s; gap retry: short again
        return _part(SPEECH[:50], 3) if tag != "gap" else _part(SPEECH[:3], 3)
    rc, calls, fills = _main(tmp_path, monkeypatch, 599, gem, anchor_lines=local)
    assert rc == 2 and fills == [] and any(t == "gap" for _, _, t in calls)


def test_unfilled_gap_is_filled_when_allowed(tmp_path, monkeypatch):
    local = [(t, f"本機第 {t} 秒的字") for t in range(0, 600, 3)]
    def gem(off, length, tag):
        return _part(SPEECH[:50], 3) if tag != "gap" else _part(SPEECH[:3], 3)
    rc, calls, fills = _main(tmp_path, monkeypatch, 599, gem, fill="breeze25", anchor_lines=local)
    assert rc == 0 and len(fills) == 1


def test_ts_anchor_takes_the_local_clock():
    import ts_anchor
    loc = [(float(i * 10), s) for i, s in enumerate(SPEECH[:20])]
    gem = [(float(i * 13), s) for i, s in enumerate(SPEECH[:20])]   # same text, stretched clock
    fixed, cov = ts_anchor.anchor(gem, loc, 200)
    assert cov >= 0.9
    assert all(abs(t - i * 10) <= 2 for i, (t, _) in enumerate(fixed))


def test_primary_fallback_never_adopts_an_old_transcript(tmp_path, monkeypatch):
    import asr_gemini_primary as P
    (tmp_path / "transcript_anchor.txt").write_text("[00:00] x\n", encoding="utf-8")
    (tmp_path / "transcript.json").write_text("[]", encoding="utf-8")   # from an earlier run
    monkeypatch.setattr(P, "run", lambda cmd: 1)   # Gemini fails, and so does the local fallback
    monkeypatch.setattr(sys, "argv", ["asr_gemini_primary.py", "x.mp4", "-o", str(tmp_path), "--lang", "zh"])
    assert P.main() == 1
    assert not (tmp_path / "transcript.json").exists()
    assert list(tmp_path.glob("transcript.json.stale-*"))


def test_primary_passes_via_through(tmp_path, monkeypatch):
    import asr_gemini_primary as P
    (tmp_path / "transcript_anchor.txt").write_text("[00:00] x\n", encoding="utf-8")
    seen = []
    monkeypatch.setattr(P, "run", lambda cmd: seen.append(cmd) or 1)
    monkeypatch.setattr(sys, "argv", ["asr_gemini_primary.py", "x.mp4", "-o", str(tmp_path), "--lang", "zh",
                                      "--via", "api"])
    P.main()
    gem = [c for c in seen if any(str(x).endswith("gemini_asr.py") for x in c)][0]
    assert gem[gem.index("--via") + 1] == "api"
