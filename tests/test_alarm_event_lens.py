"""Tests for AlarmEvents that come per lens (subChannel) for one channel."""

import unittest

from reolink_aio.api import Host

# cmd_id 33 push of a dual lens OMVI 3i WiFi on channel 2 of a Home Hub (issue #211)
TWO_LENS_EVENTS = """<?xml version="1.0" encoding="UTF-8" ?>
<body>
<AlarmEventList version="1.1">
<AlarmEvent version="1.1">
<channelId>2</channelId>
<subChannel>0</subChannel>
<status>{status_0}</status>
<AItype>{ai_0}</AItype>{smart_0}
<recording>0</recording>
<timeStamp>0</timeStamp>
</AlarmEvent>
<AlarmEvent version="1.1">
<channelId>2</channelId>
<subChannel>1</subChannel>
<status>{status_1}</status>
<AItype>{ai_1}</AItype>{smart_1}
<recording>0</recording>
<timeStamp>0</timeStamp>
</AlarmEvent>
<AlarmEvent version="1.1">
<channelId>0</channelId>
<status>none</status>
<AItype>none</AItype>
<recording>0</recording>
<timeStamp>0</timeStamp>
</AlarmEvent>
</AlarmEventList>
</body>
"""


def crossline(index: int) -> str:
    return f"<smartAiTypeList><smartAiType><type>crossline</type><index>{index}</index></smartAiType></smartAiTypeList>"


class TestLensAlarmEvents(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.host = Host("1.2.3.4", "admin", "password")
        self.addAsyncCleanup(self.host._aiohttp_session.close)
        self.host._channels = [0, 2]
        self.host._stream_channels = [0, 2]
        for channel in (0, 2):
            self.host._motion_detection_states[channel] = False
            self.host._ai_detection_states[channel] = {"people": False, "vehicle": False, "dog_cat": False}
        self.host.baichuan._ai_detect[2] = {"crossline": {0: {"state": False, "people": False}, 1: {"state": False, "people": False}}}

    def push(self, status_0: str, ai_0: str, status_1: str, ai_1: str, smart_0: str = "", smart_1: str = "") -> None:
        xml = TWO_LENS_EVENTS.format(status_0=status_0, ai_0=ai_0, status_1=status_1, ai_1=ai_1, smart_0=smart_0, smart_1=smart_1)
        self.host.baichuan._parse_xml(33, xml)

    async def test_detection_on_first_lens_is_kept(self) -> None:
        self.push("MD", "people,dog_cat", "none", "none")
        self.assertTrue(self.host._motion_detection_states[2])
        self.assertEqual(self.host._ai_detection_states[2], {"people": True, "vehicle": False, "dog_cat": True})
        self.assertFalse(self.host._motion_detection_states[0])

    async def test_detection_on_second_lens_is_kept(self) -> None:
        self.push("none", "none", "MD", "vehicle")
        self.assertTrue(self.host._motion_detection_states[2])
        self.assertEqual(self.host._ai_detection_states[2], {"people": False, "vehicle": True, "dog_cat": False})

    async def test_detections_on_both_lenses_combined(self) -> None:
        self.push("MD", "people", "MD,visitor", "vehicle")
        self.assertTrue(self.host._motion_detection_states[2])
        self.assertTrue(self.host._visitor_states[2])
        self.assertEqual(self.host._ai_detection_states[2], {"people": True, "vehicle": True, "dog_cat": False})

    async def test_no_detection_clears_states(self) -> None:
        self.push("MD", "people", "none", "none")
        self.push("none", "none", "none", "none")
        self.assertFalse(self.host._motion_detection_states[2])
        self.assertEqual(self.host._ai_detection_states[2], {"people": False, "vehicle": False, "dog_cat": False})

    async def test_smart_ai_on_first_lens_is_kept(self) -> None:
        self.push("MD", "people", "none", "none", smart_0=crossline(1))
        self.assertTrue(self.host.baichuan.smart_ai_state(2, "crossline", 0))
        self.assertFalse(self.host.baichuan.smart_ai_state(2, "crossline", 1))

    async def test_same_smart_ai_on_both_lenses_combined(self) -> None:
        self.push("MD", "people", "MD", "people", smart_0=crossline(1), smart_1=crossline(2))
        self.assertTrue(self.host.baichuan.smart_ai_state(2, "crossline", 0))
        self.assertTrue(self.host.baichuan.smart_ai_state(2, "crossline", 1))

    async def test_missing_keys_are_not_added(self) -> None:
        xml = TWO_LENS_EVENTS.format(status_0="MD", ai_0="people", status_1="none", ai_1="none", smart_0="", smart_1="")
        xml = xml.replace("<AItype>people</AItype>", "").replace("<AItype>none</AItype>", "", 1)
        self.host._ai_detection_states[2]["vehicle"] = True
        self.host.baichuan._parse_xml(33, xml)
        self.assertTrue(self.host._motion_detection_states[2])
        self.assertTrue(self.host._ai_detection_states[2]["vehicle"])  # untouched, no AItype was sent


if __name__ == "__main__":
    unittest.main()
