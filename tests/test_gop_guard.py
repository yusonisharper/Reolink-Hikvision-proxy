"""StreamSource._guard_gop: keyframes often enough for Reolink's mp4 writer (600 pieces per GOP,
two per NAL unit), and AccessUnit.slices from AnnexBParser."""
import asyncio

from bcproxy.bcmedia import AccessUnit, AnnexBParser
from bcproxy.media import StreamInfo, StreamSource

SC = b"\x00\x00\x00\x01"


def _run_gop(slices: int, pictures: int, fps: int = 25) -> list[int]:
    """Feed one keyframe and `pictures` P pictures; return the picture numbers that asked for a keyframe."""
    asked: list[int] = []

    async def main() -> None:
        src = StreamSource("t/main", "rtsp://x", StreamInfo("H265", 2688, 1520, fps))

        async def cb() -> None:
            asked.append(src._since_key)

        src.keyframe_cb = cb
        src._guard_gop(AccessUnit(b"", True, slices))
        for _ in range(pictures):
            src._guard_gop(AccessUnit(b"", False, slices))
            await asyncio.sleep(0)              # let the request task finish

    asyncio.run(main())
    return asked


def test_three_slices_ask_before_100_pictures():
    # 600 // (2 * 3) = 100 pictures; ask one second (25 pictures) early, then every half second, 3 times
    assert _run_gop(3, 150) == [75, 87, 99]


def test_one_slice_allows_300_pictures():
    assert _run_gop(1, 400) == [275, 287, 299]


def test_two_slices_at_15_fps():
    assert _run_gop(2, 200, fps=15) == [135, 142, 149]


def test_gop_below_the_limit_is_left_alone():
    # a single-slice camera with a 12 s GOP at 20 fps (Hikvision DS-2CD7526G0-IZHS, H.265+)
    assert _run_gop(1, 238, fps=20) == []


def test_keyframe_resets_the_count():
    async def main() -> list[int]:
        asked: list[int] = []
        src = StreamSource("t/main", "rtsp://x", StreamInfo("H265", 2688, 1520, 25))

        async def cb() -> None:
            asked.append(src._since_key)

        src.keyframe_cb = cb
        for _ in range(3):
            src._guard_gop(AccessUnit(b"", True, 3))
            for _ in range(80):
                src._guard_gop(AccessUnit(b"", False, 3))
                await asyncio.sleep(0)
        return asked

    assert asyncio.run(main()) == [75, 75, 75]


def test_no_callback_no_request():
    async def main() -> None:
        src = StreamSource("t/main", "rtsp://x", StreamInfo("H265", 2688, 1520, 25))
        src._guard_gop(AccessUnit(b"", True, 3))
        for _ in range(200):
            src._guard_gop(AccessUnit(b"", False, 3))
        assert src._gop_task is None

    asyncio.run(main())


def test_parser_counts_slices():
    p = AnnexBParser("H265")
    first, other = b"\x02\x01\x80\x11", b"\x02\x01\x00\x22"     # TRAIL_R, first_slice_segment_in_pic 1 / 0
    # a picture is complete when the next one starts; the last NAL unit waits for the next start code
    aus = p.feed(SC + first + SC + other + SC + other + SC + first + SC + first + SC)
    assert [a.slices for a in aus] == [3, 1]


def _camera(gop: int, pictures: int, fps: int = 20, slices: int = 1, answer_after: int = 13,
            fixed: bool = False, viewer_asks: tuple[int, ...] = (), stop_after: int | None = None,
            extras: tuple[int, ...] = (), jitter: tuple[int, ...] = (), monkeypatch=None) -> list[int]:
    """A camera that makes a keyframe of its own `gop` pictures after the last keyframe, any keyframe
    (DS-2CD2347G1-L, H.265+: 282 at 20 fps), or every `gop` pictures whatever happens (fixed), and
    answers a request `answer_after` pictures later. Returns the picture numbers that asked."""
    import bcproxy.media as media
    clock = [0.0]
    monkeypatch.setattr(media.time, "monotonic", lambda: clock[0])
    asked: list[int] = []

    async def main() -> None:
        src = StreamSource("t/main", "rtsp://x", StreamInfo("H265", 2688, 1520, fps))
        pending: list[int] = []
        last_key, last_own, k = 0, 0, 0

        async def cb() -> None:
            asked.append(n)
            pending.append(n + answer_after)

        src.keyframe_cb = cb
        for n in range(pictures):
            clock[0] = n / fps
            if n in viewer_asks:
                src.note_keyframe_request()
                pending.append(n + answer_after)
            length = gop + (jitter[k % len(jitter)] if jitter else 0)
            own = n == 0 or (n - (last_own if fixed else last_key)) == length
            own = (own and (stop_after is None or n <= stop_after)) or n in extras
            key = own or any(p <= n for p in pending)
            if own:
                last_own, k = n, k + 1
            if key:
                last_key = n
                pending[:] = [p for p in pending if p > n]
            src._guard_gop(AccessUnit(b"", key, slices))
            await asyncio.sleep(0)

    asyncio.run(main())
    return asked


def test_own_gop_within_limit_stops_the_requests(monkeypatch):
    # Backyard: its own keyframe at 282 comes before the answer to the request made at 280 (one second
    # before 300 at 20 fps); after two such GOPs nothing is asked
    asked = _camera(282, 282 * 10, monkeypatch=monkeypatch)
    assert asked == [280, 293 + 280]


def test_answer_before_own_keyframe_makes_no_extra_keyframes(monkeypatch):
    # the answer comes first and the camera counts again from it: GOPs of 281, one keyframe each
    asked = _camera(282, 282 * 6, answer_after=1, monkeypatch=monkeypatch)
    assert asked == list(range(280, 282 * 6, 281))


def test_fixed_rhythm_camera(monkeypatch):
    asked = _camera(288, 288 * 10, fixed=True, answer_after=7, monkeypatch=monkeypatch)
    assert len(asked) <= 3 and asked[0] == 280


def test_viewer_keyframes_do_not_break_the_rhythm(monkeypatch):
    asked = _camera(282, 282 * 10, viewer_asks=(1000, 1700), monkeypatch=monkeypatch)
    assert asked == [280, 293 + 280]


def test_extra_keyframes_of_the_camera_teach_nothing(monkeypatch):
    # H.265+ adds a keyframe at a sudden change in the picture
    asked = _camera(282, 282 * 10, extras=(1500, 2000), monkeypatch=monkeypatch)
    assert asked == [280, 293 + 280]


def test_gop_that_wanders_a_little(monkeypatch):
    # Backyard, measured: 281 to 283 pictures
    asked = _camera(282, 282 * 12, jitter=(1, -1, 0, 1, 0, -1), monkeypatch=monkeypatch)
    assert len(asked) == 2


def test_own_gop_beyond_the_limit_keeps_asking(monkeypatch):
    # 3 slices: limit 100 pictures, never reached by a 282-picture GOP
    asked = _camera(282, 282 * 3, slices=3, monkeypatch=monkeypatch)
    # asked at 80, again half a second later (the answer takes 13 pictures), then 80 after the answers
    assert asked[:3] == [80, 90, 103 + 80]


def test_missing_camera_keyframe_brings_requests_back(monkeypatch):
    # the camera stops making its own keyframes after picture 2000
    asked = _camera(282, 282 * 12, stop_after=2000, monkeypatch=monkeypatch)
    assert asked[:2] == [280, 293 + 280]
    # its last own keyframe came at 1996; once 282 + 5 pictures pass without one, the guard is back to
    # its normal rule and asks at once. The answer comes 13 pictures later: a GOP of 301 pictures, the
    # one that Reolink's mp4 writer loses when the camera's rhythm breaks
    assert asked[2] == 1996 + 288
