"""Detection capabilities on camera-reported stream channels."""

import unittest
from xml.etree import ElementTree as XML

from reolink_aio.api import Host


class MultilensDetectionCapabilitiesTest(unittest.IsolatedAsyncioTestCase):
    async def test_second_lens_follows_baichuan_abilities(self) -> None:
        host = Host("camera", "user", "password")
        try:
            # Keep channel requests out of this unit test; detection discovery
            # must still inspect every camera-reported stream channel.
            host._channels = []
            host._stream_channels = [0, 1, 2]
            host._num_channels = 1
            host._is_dual_lens = True
            host._sub_channels = {0: {None}, 1: {None}, 2: {None}}

            host.baichuan._abilities = {
                0: {None: XML.fromstring("<item><motion>1</motion><aitype>0</aitype></item>")},
                1: {None: XML.fromstring("<item><motion>1</motion><aitype>22</aitype></item>")},
                2: {None: XML.fromstring("<item />")},
            }

            await host.baichuan.get_channel_data()
            host.construct_capabilities()

            self.assertTrue(host.supported(1, "motion_detection"))
            self.assertTrue(host.supported(1, "ai_people"))
            self.assertTrue(host.supported(1, "ai_vehicle"))
            self.assertTrue(host.supported(1, "ai_dog_cat"))
            self.assertEqual(set(host._ai_detection_states[1]), {"people", "vehicle", "dog_cat"})

            host.baichuan._parse_xml(
                33,
                """<body><EventList><AlarmEvent><channelId>1</channelId>
                <status>MD</status><AItype>people,dog_cat</AItype>
                </AlarmEvent></EventList></body>""",
            )
            self.assertEqual(host._ai_detection_states[1], {"people": True, "vehicle": False, "dog_cat": True})
            self.assertFalse(host.supported(2, "motion_detection"))
            self.assertNotIn(2, host._ai_detection_states)
        finally:
            await host._aiohttp_session.close()


if __name__ == "__main__":
    unittest.main()
