from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from reolink_aio.baichuan.baichuan import Baichuan
from reolink_aio.exceptions import ApiError
from reolink_aio.typings import VOD_trigger

# A single FILE_FIND (cmd 15) page holding one clip triggered by motion + person.
_FILE_FIND_PAGE = """<body>
<FileInfoList version="1.1">
<FileInfo>
<name>Mp4Record/2024-01-15/RecM01_20240115_120000_120030.mp4</name>
<startTime><year>2024</year><month>1</month><day>15</day><hour>12</hour><minute>0</minute><second>0</second></startTime>
<endTime><year>2024</year><month>1</month><day>15</day><hour>12</hour><minute>0</minute><second>30</second></endTime>
<sizeL>1048576</sizeL>
<sizeH>0</sizeH>
<recordType>md, people</recordType>
</FileInfo>
</FileInfoList>
</body>"""

_CLIP_NAME = "Mp4Record/2024-01-15/RecM01_20240115_120000_120030.mp4"


def _page_for_day(day: int) -> str:
    """One FILE_FIND page holding a single motion clip on the given day."""
    return f"""<body>
<FileInfoList version="1.1">
<FileInfo>
<name>Mp4Record/2024-01-{day:02d}/RecM01_202401{day:02d}_120000_120030.mp4</name>
<startTime><year>2024</year><month>1</month><day>{day}</day><hour>12</hour><minute>0</minute><second>0</second></startTime>
<endTime><year>2024</year><month>1</month><day>{day}</day><hour>12</hour><minute>0</minute><second>30</second></endTime>
<sizeL>1048576</sizeL>
<sizeH>0</sizeH>
<recordType>md</recordType>
</FileInfo>
</FileInfoList>
</body>"""


def _info(
    name: str | None,
    record_type: str = "md",
    *,
    day: int = 15,
    size_l: int = 1048576,
    size_h: int = 0,
    with_times: bool = True,
) -> str:
    """Build one <FileInfo> block. name=None or with_times=False make it malformed."""
    name_tag = f"<name>{name}</name>" if name is not None else ""
    if with_times:
        times = (
            f"<startTime><year>2024</year><month>1</month><day>{day}</day>"
            "<hour>12</hour><minute>0</minute><second>0</second></startTime>"
            f"<endTime><year>2024</year><month>1</month><day>{day}</day>"
            "<hour>12</hour><minute>0</minute><second>30</second></endTime>"
        )
    else:
        times = "<startTime></startTime><endTime></endTime>"
    return (
        f"<FileInfo>{name_tag}{times}"
        f"<sizeL>{size_l}</sizeL><sizeH>{size_h}</sizeH>"
        f"<recordType>{record_type}</recordType></FileInfo>"
    )


def _wrap(*infos: str) -> str:
    """Wrap FileInfo blocks in a cmd-15 FileInfoList page."""
    return f'<body>\n<FileInfoList version="1.1">\n{"".join(infos)}\n</FileInfoList>\n</body>'


_EMPTY_PAGE = "<body></body>"


class TestSearchVodTypeFilefindFallback(unittest.IsolatedAsyncioTestCase):
    def _make_host(self) -> Baichuan:
        return Baichuan(
            host="127.0.0.1",
            username="user",
            password="password",
            http_api=SimpleNamespace(
                camera_uid=lambda channel: "UID123_0",
                nvr_name="test",
                _updating=False,
            ),
        )

    async def test_falls_back_to_filefind_on_cmd_272_status_405(self) -> None:
        baichuan = self._make_host()
        calls: list[int] = []

        async def fake_send(
            cmd_id: int, channel: int = 0, body: str = "", **_kwargs: object
        ) -> str:
            calls.append(cmd_id)
            if cmd_id == 272:
                raise ApiError("received status code 405 from cmd_id 272", rspCode=405)
            if cmd_id == 14:
                return "<body><handle>7</handle></body>"
            if cmd_id == 15:
                # First page returns the clip, subsequent pages are empty so the
                # paging loop terminates.
                return _FILE_FIND_PAGE if calls.count(15) == 1 else "<body></body>"
            return "<ok/>"

        baichuan.send = AsyncMock(side_effect=fake_send)  # type: ignore[method-assign]

        start = datetime(2024, 1, 15, 8, 0, 0)
        end = datetime(2024, 1, 15, 20, 0, 0)
        vod_type_dict, vod_dict = await baichuan.search_vod_type(
            0, start, end, stream="main"
        )

        # cmd 272 was attempted, then the FILE_FIND open/list/close chain ran.
        self.assertIn(272, calls)
        self.assertIn(14, calls)
        self.assertIn(15, calls)
        self.assertIn(16, calls)  # handle closed

        # The clip is classified under both of its triggers.
        self.assertEqual(set(vod_type_dict), {_CLIP_NAME})
        self.assertTrue(vod_type_dict[_CLIP_NAME] & VOD_trigger.MOTION)
        self.assertTrue(vod_type_dict[_CLIP_NAME] & VOD_trigger.PERSON)

        motion = vod_dict[VOD_trigger.MOTION]
        person = vod_dict[VOD_trigger.PERSON]
        self.assertEqual(len(motion), 1)
        self.assertEqual(len(person), 1)
        self.assertEqual(motion[0].file_name, _CLIP_NAME)
        self.assertEqual(motion[0].start_time, datetime(2024, 1, 15, 12, 0, 0))
        self.assertEqual(motion[0].size, 1048576)
        # Triggers not present in recordType stay empty.
        self.assertEqual(vod_dict[VOD_trigger.VEHICLE], [])

    async def test_iterates_each_day_of_a_multi_day_window(self) -> None:
        # The firmware searches one day per FILE_FIND open, so a multi-day
        # window must trigger one open/list/close chain per calendar day.
        baichuan = self._make_host()
        opens = 0
        served: set[int] = set()

        async def fake_send(
            cmd_id: int, channel: int = 0, body: str = "", **_kwargs: object
        ) -> str:
            nonlocal opens
            if cmd_id == 272:
                raise ApiError("received status code 405 from cmd_id 272", rspCode=405)
            if cmd_id == 14:
                opens += 1
                return "<body><handle>7</handle></body>"
            if cmd_id == 15:
                # First page of each day's open yields that day's clip; the
                # firmware maps the open window's start day onto the results.
                if opens not in served:
                    served.add(opens)
                    return _page_for_day(14 + opens)  # 15, 16, 17
                return "<body></body>"
            return "<ok/>"

        baichuan.send = AsyncMock(side_effect=fake_send)  # type: ignore[method-assign]

        start = datetime(2024, 1, 15, 8, 0, 0)
        end = datetime(2024, 1, 17, 20, 0, 0)
        vod_type_dict, vod_dict = await baichuan.search_vod_type(
            0, start, end, stream="main"
        )

        # One open per day in the [15, 17] window, and one clip harvested each.
        self.assertEqual(opens, 3)
        self.assertEqual(len(vod_type_dict), 3)
        motion = vod_dict[VOD_trigger.MOTION]
        self.assertEqual(len(motion), 3)
        self.assertEqual(
            sorted(f.start_time.day for f in motion), [15, 16, 17]
        )

    async def test_accumulates_clips_across_multiple_pages_in_one_day(self) -> None:
        # A single day's search can span several cmd-15 pages; every page's new
        # clips must be collected until an empty page ends the paging loop.
        baichuan = self._make_host()
        pages = iter([_wrap(_info("clipA")), _wrap(_info("clipB")), _EMPTY_PAGE])

        async def fake_send(
            cmd_id: int, channel: int = 0, body: str = "", **_kwargs: object
        ) -> str:
            if cmd_id == 272:
                raise ApiError("received status code 405 from cmd_id 272", rspCode=405)
            if cmd_id == 14:
                return "<body><handle>7</handle></body>"
            if cmd_id == 15:
                return next(pages)
            return "<ok/>"

        baichuan.send = AsyncMock(side_effect=fake_send)  # type: ignore[method-assign]

        vod_type_dict, vod_dict = await baichuan.search_vod_type(
            0, datetime(2024, 1, 15, 0, 0, 0), datetime(2024, 1, 15, 23, 59, 59), stream="main"
        )
        self.assertEqual(set(vod_type_dict), {"clipA", "clipB"})
        self.assertEqual(len(vod_dict[VOD_trigger.MOTION]), 2)

    async def test_deduplicates_a_clip_returned_for_two_adjacent_days(self) -> None:
        # A clip straddling midnight is returned by both days' searches; the
        # shared `seen` set must keep it from being counted twice.
        baichuan = self._make_host()
        opens = 0
        served: set[int] = set()
        overlap = _wrap(_info("overlap_clip"))

        async def fake_send(
            cmd_id: int, channel: int = 0, body: str = "", **_kwargs: object
        ) -> str:
            nonlocal opens
            if cmd_id == 272:
                raise ApiError("received status code 405 from cmd_id 272", rspCode=405)
            if cmd_id == 14:
                opens += 1
                return "<body><handle>7</handle></body>"
            if cmd_id == 15:
                if opens not in served:
                    served.add(opens)
                    return overlap
                return _EMPTY_PAGE
            return "<ok/>"

        baichuan.send = AsyncMock(side_effect=fake_send)  # type: ignore[method-assign]

        vod_type_dict, vod_dict = await baichuan.search_vod_type(
            0, datetime(2024, 1, 15, 0, 0, 0), datetime(2024, 1, 16, 23, 59, 59), stream="main"
        )
        self.assertEqual(opens, 2)  # both days searched
        self.assertEqual(set(vod_type_dict), {"overlap_clip"})
        self.assertEqual(len(vod_dict[VOD_trigger.MOTION]), 1)  # not double-counted

    async def test_day_without_a_handle_is_skipped_and_search_continues(self) -> None:
        # A day with no recordings returns no handle; that day is skipped (no
        # cmd 15/16) and the loop still searches the remaining days.
        baichuan = self._make_host()
        opens = 0
        served = False
        calls: list[int] = []

        async def fake_send(
            cmd_id: int, channel: int = 0, body: str = "", **_kwargs: object
        ) -> str:
            nonlocal opens, served
            calls.append(cmd_id)
            if cmd_id == 272:
                raise ApiError("received status code 405 from cmd_id 272", rspCode=405)
            if cmd_id == 14:
                opens += 1
                return _EMPTY_PAGE if opens == 1 else "<body><handle>7</handle></body>"
            if cmd_id == 15:
                if not served:
                    served = True
                    return _wrap(_info("day2_clip", day=16))
                return _EMPTY_PAGE
            return "<ok/>"

        baichuan.send = AsyncMock(side_effect=fake_send)  # type: ignore[method-assign]

        vod_type_dict, _ = await baichuan.search_vod_type(
            0, datetime(2024, 1, 15, 0, 0, 0), datetime(2024, 1, 16, 23, 59, 59), stream="main"
        )
        self.assertEqual(opens, 2)  # both days opened
        self.assertEqual(set(vod_type_dict), {"day2_clip"})
        self.assertEqual(calls.count(16), 1)  # only the day with a handle is closed

    async def test_classifies_every_record_type_token(self) -> None:
        # Guards the full token->trigger table (and the substring matching) so a
        # renamed enum or a token collision is caught.
        cases = {
            "md": VOD_trigger.MOTION,
            "pir": VOD_trigger.MOTION,
            "other": VOD_trigger.MOTION,
            "io": VOD_trigger.IO,
            "people": VOD_trigger.PERSON,
            "face": VOD_trigger.FACE,
            "vehicle": VOD_trigger.VEHICLE,
            "dog_cat": VOD_trigger.ANIMAL,
            "visitor": VOD_trigger.DOORBELL,
            "package": VOD_trigger.PACKAGE,
            "cry": VOD_trigger.CRYING,
            "crossline": VOD_trigger.CROSSLINE,
            "intrusion": VOD_trigger.INTRUSION,
            "loitering": VOD_trigger.LINGER,
            "legacy": VOD_trigger.FORGOTTEN_ITEM,
            "loss": VOD_trigger.TAKEN_ITEM,
        }
        for token, trig in cases.items():
            with self.subTest(token=token):
                baichuan = self._make_host()
                served = False

                async def fake_send(
                    cmd_id: int, channel: int = 0, body: str = "",
                    _token: str = token, **_kwargs: object,
                ) -> str:
                    nonlocal served
                    if cmd_id == 272:
                        raise ApiError("received status code 405 from cmd_id 272", rspCode=405)
                    if cmd_id == 14:
                        return "<body><handle>7</handle></body>"
                    if cmd_id == 15:
                        if not served:
                            served = True
                            return _wrap(_info("clip", record_type=_token))
                        return _EMPTY_PAGE
                    return "<ok/>"

                baichuan.send = AsyncMock(side_effect=fake_send)  # type: ignore[method-assign]

                vod_type_dict, vod_dict = await baichuan.search_vod_type(
                    0, datetime(2024, 1, 15, 0, 0, 0), datetime(2024, 1, 15, 23, 59, 59), stream="main"
                )
                self.assertTrue(vod_type_dict["clip"] & trig)
                self.assertEqual(len(vod_dict[trig]), 1)

    async def test_malformed_file_info_entries_are_skipped(self) -> None:
        # Entries missing a name or an unparseable time are skipped, while valid
        # siblings on the same page are still harvested.
        baichuan = self._make_host()
        served = False
        page = _wrap(
            _info(None),  # no name
            _info("no_times", with_times=False),  # unparseable time
            _info("good_clip"),
        )

        async def fake_send(
            cmd_id: int, channel: int = 0, body: str = "", **_kwargs: object
        ) -> str:
            nonlocal served
            if cmd_id == 272:
                raise ApiError("received status code 405 from cmd_id 272", rspCode=405)
            if cmd_id == 14:
                return "<body><handle>7</handle></body>"
            if cmd_id == 15:
                if not served:
                    served = True
                    return page
                return _EMPTY_PAGE
            return "<ok/>"

        baichuan.send = AsyncMock(side_effect=fake_send)  # type: ignore[method-assign]

        vod_type_dict, vod_dict = await baichuan.search_vod_type(
            0, datetime(2024, 1, 15, 0, 0, 0), datetime(2024, 1, 15, 23, 59, 59), stream="main"
        )
        self.assertEqual(set(vod_type_dict), {"good_clip"})
        self.assertEqual(len(vod_dict[VOD_trigger.MOTION]), 1)

    async def test_size_combines_high_and_low_dwords(self) -> None:
        # Clips larger than 4 GiB carry a non-zero sizeH; size must be the 64-bit
        # combination sizeL + (sizeH << 32).
        baichuan = self._make_host()
        served = False

        async def fake_send(
            cmd_id: int, channel: int = 0, body: str = "", **_kwargs: object
        ) -> str:
            nonlocal served
            if cmd_id == 272:
                raise ApiError("received status code 405 from cmd_id 272", rspCode=405)
            if cmd_id == 14:
                return "<body><handle>7</handle></body>"
            if cmd_id == 15:
                if not served:
                    served = True
                    return _wrap(_info("big_clip", size_l=100, size_h=1))
                return _EMPTY_PAGE
            return "<ok/>"

        baichuan.send = AsyncMock(side_effect=fake_send)  # type: ignore[method-assign]

        _, vod_dict = await baichuan.search_vod_type(
            0, datetime(2024, 1, 15, 0, 0, 0), datetime(2024, 1, 15, 23, 59, 59), stream="main"
        )
        self.assertEqual(vod_dict[VOD_trigger.MOTION][0].size, (1 << 32) + 100)

    async def test_close_failure_does_not_break_the_search(self) -> None:
        # A cmd 16 (close handle) failure is swallowed so results are still
        # returned.
        baichuan = self._make_host()
        served = False

        async def fake_send(
            cmd_id: int, channel: int = 0, body: str = "", **_kwargs: object
        ) -> str:
            nonlocal served
            if cmd_id == 272:
                raise ApiError("received status code 405 from cmd_id 272", rspCode=405)
            if cmd_id == 14:
                return "<body><handle>7</handle></body>"
            if cmd_id == 15:
                if not served:
                    served = True
                    return _wrap(_info("clip"))
                return _EMPTY_PAGE
            if cmd_id == 16:
                raise ApiError("close failed", rspCode=400)
            return "<ok/>"

        baichuan.send = AsyncMock(side_effect=fake_send)  # type: ignore[method-assign]

        vod_type_dict, _ = await baichuan.search_vod_type(
            0, datetime(2024, 1, 15, 0, 0, 0), datetime(2024, 1, 15, 23, 59, 59), stream="main"
        )
        self.assertEqual(set(vod_type_dict), {"clip"})

    async def test_non_405_api_error_is_not_swallowed(self) -> None:
        baichuan = self._make_host()

        async def fake_send(
            cmd_id: int, channel: int = 0, body: str = "", **_kwargs: object
        ) -> str:
            if cmd_id == 272:
                raise ApiError("received status code 400 from cmd_id 272", rspCode=400)
            return "<ok/>"

        baichuan.send = AsyncMock(side_effect=fake_send)  # type: ignore[method-assign]

        with self.assertRaises(ApiError):
            await baichuan.search_vod_type(
                0, datetime(2024, 1, 15), datetime(2024, 1, 15), stream="main"
            )


if __name__ == "__main__":
    unittest.main()
