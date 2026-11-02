# Re-anchor the timestamps of an LLM transcript (Gemini / agy) to a local ASR transcript of the same audio.
# Why: agy timestamps stretch with time (2026-10-24: 4-min clips end at 4:40-6:20; 10-min hall chunks drift
# +48..+88 s), while the text is better than any local ASR. Local whisper-family timestamps are accurate, so
# keep the Gemini text and borrow the local clock.
# How: both transcripts -> normalised character streams (CJK chars + latin letters/digits, lowercased);
# difflib matching blocks (>= MINRUN chars) map Gemini chars to local chars; each local char gets a time by
# linear interpolation inside its line. A Gemini line's new time = local time of its first matched char,
# minus the chars before it at the local speaking rate. Unmatched lines are interpolated between anchored
# neighbours (or shifted by the nearest anchor's offset at the ends). Output is monotonic.
#   python scripts/ts_anchor.py <gemini.txt> <local.txt> <out.txt> [--duration S]
# As a library: anchor(gem_lines, loc_lines, duration) with lines = [(sec, text)].
import argparse, difflib, re, sys

TS = re.compile(r'^\s*\[(\d+):(\d+)(?::(\d+))?\]\s?(.*)')
KEEP = re.compile(r'[a-z0-9]|[㐀-鿿]')
MINRUN = 4


def parse(path):
    out = []
    for l in open(path, encoding='utf-8'):
        m = TS.match(l)
        if m:
            s = int(m[1]) * 60 + int(m[2]) if m[3] is None else int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])
            out.append((s, m[4].strip()))
    return out


try:   # small whisper models write simplified Chinese in zh mode; Gemini writes traditional
    import opencc
    _s2t = opencc.OpenCC('s2t').convert
except ImportError:
    _s2t = lambda t: t


def norm(t):
    return KEEP.findall(_s2t(t.lower()))


def anchor(gem, loc, duration=None):
    # local char stream with per-char times
    lc, lt = [], []
    for i, (t, txt) in enumerate(loc):
        ch = norm(txt)
        t2 = loc[i + 1][0] if i + 1 < len(loc) else (duration or t + max(2, len(ch) / 5))
        t2 = max(t2, t)
        for k, c in enumerate(ch):
            lc.append(c); lt.append(t + (t2 - t) * k / max(1, len(ch)))
    rate = len(lc) / max(1, (lt[-1] - lt[0])) if len(lt) > 1 else 5.0   # chars per second
    gc, gl = [], []
    for i, (_, txt) in enumerate(gem):
        for c in norm(txt):
            gc.append(c); gl.append(i)
    sm = difflib.SequenceMatcher(None, gc, lc, autojunk=False)
    first = {}   # gemini line -> (char offset within line, local time)
    line_start = {}
    for idx, li in enumerate(gl):
        line_start.setdefault(li, idx)
    for a, b, n in sm.get_matching_blocks():
        if n < MINRUN:
            continue
        for k in range(n):
            li = gl[a + k]
            if li not in first:
                first[li] = (a + k - line_start[li], lt[b + k])
    est = {li: t - off / rate for li, (off, t) in first.items()}
    # drop anchors that go backwards (a Gemini line matched to a repeated phrase elsewhere)
    keep, last = {}, -1e9
    for li in sorted(est):
        if est[li] >= last - 1:
            keep[li] = est[li]; last = est[li]
    ks = sorted(keep)
    new = []
    for i, (t, txt) in enumerate(gem):
        if i in keep:
            nt = keep[i]
        elif ks:
            lo = max((k for k in ks if k < i), default=None); hi = min((k for k in ks if k > i), default=None)
            if lo is not None and hi is not None:
                # interpolate on the original Gemini clock between the two anchors
                g0, g1 = gem[lo][0], gem[hi][0]
                f = (t - g0) / (g1 - g0) if g1 > g0 else (i - lo) / (hi - lo)
                nt = keep[lo] + f * (keep[hi] - keep[lo])
            else:
                k = lo if lo is not None else hi
                nt = t + (keep[k] - gem[k][0])
        else:
            nt = t
        new.append([max(0.0, nt), txt])
    for i in range(1, len(new)):
        new[i][0] = max(new[i][0], new[i - 1][0])
    return [(round(t), txt) for t, txt in new], len(keep) / max(1, len(gem))


def fmt(s):
    s = int(s)
    return f'[{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}]' if s >= 3600 else f'[{s // 60:02d}:{s % 60:02d}]'


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('gemini'); ap.add_argument('local'); ap.add_argument('out')
    ap.add_argument('--duration', type=float)
    a = ap.parse_args()
    lines, cov = anchor(parse(a.gemini), parse(a.local), a.duration)
    open(a.out, 'w', encoding='utf-8').write(''.join(f'{fmt(t)} {x}\n' for t, x in lines))
    print(f'{a.out}: {len(lines)} lines, anchored {cov:.0%}', file=sys.stderr)
