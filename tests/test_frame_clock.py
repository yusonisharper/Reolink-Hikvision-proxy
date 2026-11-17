"""FrameClock: even live frame times from bursty arrivals, never going back."""
from bcproxy.media import FrameClock


def _stamps(arrivals, fps=25):
    c = FrameClock()
    return [c.stamp(a, fps) for a in arrivals]


def _kitchen_like(seconds=30, fps=25, stall=0.25, burst_every=10.0):
    """Frames every 1/fps, but before each keyframe the camera stops for `stall` and then sends what
    it held back all at once (DS-2CD2347G1-L, 2026-11-16)."""
    out, t, step = [], 0.0, 1 / fps
    n = int(seconds * fps)
    for i in range(n):
        cap = i * step
        if burst_every and i and i % int(burst_every * fps) == 0:
            t = max(t, cap + stall)
        t = max(t, cap + 0.005)
        out.append(t)
    return out


def test_burst_becomes_even():
    ts = _stamps(_kitchen_like())
    deltas = [b - a for a, b in zip(ts, ts[1:])]
    assert all(d > 0 for d in deltas)
    # after the first window: within 12 % of 40 ms (the stall nudges the measured interval), no 250 ms jump
    late = deltas[100:]
    assert max(late) < 0.045 and min(late) > 0.035


def test_follows_arrivals_long_term():
    arrivals = [i / 26.4 for i in range(26 * 120)]       # declared 30 fps, runs at 26.4
    ts = _stamps(arrivals, fps=30)
    assert abs(ts[-1] - arrivals[-1]) < 0.1


def test_stall_jumps_forward_and_never_back():
    arrivals = [i * 0.04 for i in range(200)]
    arrivals += [arrivals[-1] + 3 + i * 0.001 for i in range(50)]       # 3 s stall, then a burst
    arrivals += [arrivals[-1] + 0.04 * (i + 1) for i in range(300)]
    ts = _stamps(arrivals)
    assert all(b > a for a, b in zip(ts, ts[1:]))
    assert ts[200] >= arrivals[200] - 0.001          # jumped to now after the stall
    assert abs(ts[-1] - arrivals[-1]) < 0.2          # and caught up with the burst later


def test_first_frame_is_arrival():
    assert _stamps([5.0]) == [5.0]
