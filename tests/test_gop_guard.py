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
