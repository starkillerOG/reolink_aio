import aiounittest
import asyncio
import time

from reolink_aio.api import Host

USER = "Test"
PASSWORD = "12345678"
HOST = "192.168.80.43"
PORT = 80


class TestLogin(aiounittest.AsyncTestCase):
    def setUp(self):
        self._loop = asyncio.new_event_loop()
        self.addCleanup(self._loop.close)
        self._user = USER
        self._password = PASSWORD
        self._host = HOST
        self._port = PORT

    def tearDown(self):
        self._loop.close()

    def test_succes(self):
        host = Host(
            host        = self._host,
            port        = self._port,
            username    = self._user,
            password    = self._password,
        )
        assert self._loop.run_until_complete(host.login())
        assert host.session_active
        self._loop.run_until_complete(host.logout())

    def test_wrong_password(self):
        host = Host(
            host        = self._host,
            port        = self._port,
            username    = self._user,
            password    = "wrongpass"
        )
        assert not self._loop.run_until_complete(host.login())
        assert not host.session_active
        assert not self._loop.run_until_complete(host.get_host_data())
        assert not self._loop.run_until_complete(host.get_states())
        assert not self._loop.run_until_complete(host.get_motion_state(0))
        assert not self._loop.run_until_complete(host.get_stream_source(0))
        assert not self._loop.run_until_complete(host.set_ftp(0, False))

    def test_wrong_user(self):
        host = Host(
            host=self._host,
            port=self._port,
            username="wronguser",
            password=self._password,
        )
        assert not self._loop.run_until_complete(host.login())
        assert not host.session_active

    def test_wrong_host(self):
        host = Host(
            host="192.168.1.0",
            port=self._port,
            username=self._user,
            password=self._password,
        )
        assert not self._loop.run_until_complete(host.login())
        assert not host.session_active
#endof class TestLogin


class TestGetData(aiounittest.AsyncTestCase):
    def setUp(self):
        self._loop = asyncio.new_event_loop()
        self.addCleanup(self._loop.close)

        self._user      = USER
        self._password  = PASSWORD
        self._host      = HOST
        self._port      = PORT

        self._host_device = Host(
            host        = self._host,
            port        = self._port,
            username    = self._user,
            password    = self._password,
        )
        assert self._loop.run_until_complete(self._host_device.login())
        assert self._host_device.session_active

    def test1_settings(self):
        assert self._loop.run_until_complete(self._host_device.get_host_data())
        self._host_device.is_admin

        assert self._host_device.host is not None
        assert self._host_device.port is not None
        assert self._host_device.channels is not None
        assert self._host_device.onvif_port is not None
        assert self._host_device.mac_address is not None
        assert self._host_device.serial is not None
        assert self._host_device.nvr_name is not None
        assert self._host_device.sw_version is not None
        assert self._host_device.model is not None
        assert self._host_device.manufacturer is not None
        assert self._host_device.rtmp_port is not None
        assert self._host_device.rtsp_port is not None
        assert self._host_device.stream is not None
        assert self._host_device.protocol is not None

        assert self._host_device.hdd_info is not None
        assert self._host_device.ptz_supported is not None

        self._host_device._users.append({"level": "guest", "userName": "guest"})
        self._host_device._username = "guest"
        assert not self._host_device.is_admin

    def test2_states(self):
        assert self._loop.run_until_complete(self._host_device.get_states())
        assert self._loop.run_until_complete(self._host_device.get_motion_state(0)) is not None

        self._host_device._ptz_support[0] = True
        self._host_device._ptz_presets[0]["test"] = 123

        assert (
            self._loop.run_until_complete(self._host_device.get_switchable_capabilities(0))
            is not None
        )

    def test3_images(self):
        assert self._loop.run_until_complete(self._host_device.get_snapshot(0)) is not None
        assert self._loop.run_until_complete(self._host_device.get_snapshot(100)) is None
        assert self._loop.run_until_complete(self._host_device.get_snapshot(0)) is not None
        assert self._loop.run_until_complete(self._host_device.get_stream_source(0)) is not None

    def test4_properties(self):
        assert self._loop.run_until_complete(self._host_device.get_states())

        assert self._host_device.motion_detection_state(0) is not None
        assert self._host_device.is_ia_enabled(0) is not None
        assert self._host_device.ftp_enabled(0) is not None
        assert self._host_device.email_enabled(0) is not None
        assert self._host_device.ir_enabled(0) is not None
        assert self._host_device.whiteled_enabled(0) is not None
        assert self._host_device.daynight_state(0) is not None
        assert self._host_device.recording_enabled(0) is not None
        assert self._host_device.audio_alarm_enabled(0) is not None
        assert self._host_device.ptz_presets(0) == {}  # Cam has no ptz
        assert self._host_device.sensititivy_presets(0) is not None

        get_ptz_response = [
            {
                "cmd": "GetPtzPreset",
                "code": 0,
                "value": {
                    "PtzPreset": [
                        {"enable": 0, "name": "Preset_1", "id": 0},
                        {"enable": 1, "name": "Preset_2", "id": 1},
                    ]
                },
            }
        ]
        self._host_device.map_channel_json_response(get_ptz_response, 0)
        assert self._host_device._ptz_presets[0] is not None
        assert self._host_device._ptz_presets_settings[0] is not None
        assert not self._loop.run_until_complete(
            self._host_device.send_setting([{"cmd": "wrong_command"}])
        )

        for _ in range(1):
            """FTP state."""
            assert self._loop.run_until_complete(self._host_device.set_ftp(0, True))
            assert self._host_device.ftp_enabled(0)
            assert self._loop.run_until_complete(self._host_device.set_ftp(0, False))
            assert not self._host_device.ftp_enabled(0)

            """Email state."""
            assert self._loop.run_until_complete(self._host_device.set_email(0, True))
            assert self._host_device.email_enabled(0)
            assert self._loop.run_until_complete(self._host_device.set_email(0, False))
            assert not self._host_device.email_enabled(0)

            """Audio state."""
            assert self._loop.run_until_complete(self._host_device.set_audio(0, True))
            assert self._host_device.audio_alarm_enabled(0)
            assert self._loop.run_until_complete(self._host_device.set_audio(0, False))
            assert not self._host_device.audio_alarm_enabled(0)

            """ir state."""
            assert self._loop.run_until_complete(self._host_device.set_ir_lights(0, True))
            assert self._host_device.ir_enabled(0)
            assert self._loop.run_until_complete(self._host_device.set_ir_lights(0, False))
            assert not self._host_device.ir_enabled(0)

            """Daynight state."""
            assert self._loop.run_until_complete(self._host_device.set_daynight(0, "Auto"))
            assert self._host_device.daynight_state(0)
            assert self._loop.run_until_complete(self._host_device.set_daynight(0, "Color"))
            assert not self._host_device.daynight_state(0)

            """Recording state."""
            assert self._loop.run_until_complete(self._host_device.set_recording(0, True))
            assert self._host_device.recording_enabled(0)
            assert self._loop.run_until_complete(self._host_device.set_recording(0, False))
            assert not self._host_device.recording_enabled(0)

            """Motion detection state."""
            assert self._loop.run_until_complete(self._host_device.set_motion_detection(0, True))
            assert self._host_device.motion_detection_state(0) is not None  # Ignore state
            assert self._loop.run_until_complete(self._host_device.set_motion_detection(0, False))

            assert self._loop.run_until_complete(self._host_device.get_states())

            assert (
                self._loop.run_until_complete(self._host_device.get_stream_source(0)) is not None
            )

            assert (
                self._loop.run_until_complete(
                    self._host_device.set_ptz_command(0, "RIGHT", speed=10)
                )
                == False
            )
            assert (
                self._loop.run_until_complete(
                    self._host_device.set_ptz_command(0, "GOTO", preset=1)
                )
                == False
            )
            assert (
                self._loop.run_until_complete(self._host_device.set_ptz_command(0, "STOP"))
                == False
            )

            assert self._loop.run_until_complete(self._host_device.set_sensitivity(0, value=10))
            assert self._loop.run_until_complete(
                self._host_device.set_sensitivity(0, value=45, preset=0)
            )

            """ White Led State (Spotlight )  """
            """ required tests """
            """    turn off , night mode off """
            """    turn on, night mode off """
            """    turn off, , night mode on """
            """    turn on, night mode on , auto mode """
            """    turn off, night mode on, scheduled """
            """    turn on,  night mode on, scheduled mode """
            """    Turn on, NM on, auto Bright = 0 """
            """    Turn on, NM on, auto Bright = 100 """
            """    incorrect mode not 0,1,3 """
            """    incorrect brightness < 0 """
            """    incorrect brightness > 100 """


            assert self._loop.run_until_complete(self._host_device.set_whiteled(0, False,50,0))
            assert self._loop.run_until_complete(self._host_device.set_whiteled(0, True,50,0))
            assert self._loop.run_until_complete(self._host_device.set_whiteled(0, False,50,1))
            assert self._loop.run_until_complete(self._host_device.set_whiteled(0, True,50,1))
            assert self._loop.run_until_complete(self._host_device.set_whiteled(0, False,50,3))
            assert self._loop.run_until_complete(self._host_device.set_whiteled(0, True,50,3))
            """  so that effect can be seen on spotlight wait 2 seconds between changes """
            time.sleep(2)
            assert self._loop.run_until_complete(self._host_device.set_whiteled(0, True,0,1))
            time.sleep(2)
            assert self._loop.run_until_complete(self._host_device.set_whiteled(0, True,100,1))
            assert self._loop.run_until_complete(self._host_device.set_whiteled(0, True,100,2))
            time.sleep(2)
            """ now turn off light - does not require an assert """
            self.loop.run_until_complete(self._host_device.set_whiteled(0, False,50,0))
            """ with incorrect values the routine should return a False """
            assert not self._loop.run_until_complete(self._host_device.set_whiteled(0, True,-10,1))
            assert not self._loop.run_until_complete(self._host_device.set_whiteled(0, True,1000,1))
            """  now tests for setting the schedule for spotlight when night mode non auto"""
            assert self._loop.run_until_complete(self._host_device.set_spotlight_lighting_schedule(0, 5, 30, 17, 30))
            assert self._loop.run_until_complete(self._host_device.set_spotlight_lighting_schedule(0, 7, 30, 19, 30))
            # invalid parameters
            assert not self._loop.run_until_complete(self._host_device.set_spotlight_lighting_schedule(0, -1, 0, 18, 0))
            assert not self._loop.run_until_complete(self._host_device.set_spotlight_lighting_schedule(0, 24, 0, 18, 0))
            assert not self._loop.run_until_complete(self._host_device.set_spotlight_lighting_schedule(0, 6, -2, 18, 0))
            assert not self._loop.run_until_complete(self._host_device.set_spotlight_lighting_schedule(0, 6, 60, 18, 0))
            assert not self._loop.run_until_complete(self._host_device.set_spotlight_lighting_schedule(0, 6, 0, -3, 0))
            assert not self._loop.run_until_complete(self._host_device.set_spotlight_lighting_schedule(0, 6, 0, 24, 0))
            assert not self._loop.run_until_complete(self._host_device.set_spotlight_lighting_schedule(0, 6, 0, 18, -4))
            assert not self._loop.run_until_complete(self._host_device.set_spotlight_lighting_schedule(0, 18, 59, 19, 0))
            assert not self._loop.run_until_complete(self._host_device.set_spotlight_lighting_schedule(0, 18, 29, 18, 30))
            #  query should end time equals start time be an error
            assert not self._loop.run_until_complete(self._host_device.set_spotlight_lighting_schedule(0, 6, 0, 6, 0))
            #
            # check simplified call
            assert self._loop.run_until_complete(self._host_device.set_spotlight(0, True))
            assert self._loop.run_until_complete(self._host_device.set_spotlight(0, False))

            # test of siren
            assert self._loop.run_until_complete(self._host_device.set_siren(0, True))
            assert self._loop.run_until_complete(self._host_device.set_siren(0, False))


    def tearDown(self):
        self._loop.run_until_complete(self._host_device.logout())
        self._loop.close()
#endof class TestGetData


class TestSubscription(aiounittest.AsyncTestCase):
    def setUp(self):
        self._loop = asyncio.new_event_loop()
        self.addCleanup(self._loop.close)
        self._user = USER
        self._password = PASSWORD
        self._host = HOST
        self._port = PORT

    def tearDown(self):
        self._loop.close()

    def test_succes(self):
        host = Host(
            host=self._host,
            port=self._port,
            username=self._user,
            password=self._password,
        )
        assert self._loop.run_until_complete(host.login())
        assert host.session_active
        assert self._loop.run_until_complete(host.get_host_data())
        #subscribe_port = host.onvif_port
        self._loop.run_until_complete(host.logout())

        assert self._loop.run_until_complete(host.subscribe("192.168.1.1/fakewebhook"))
        assert self._loop.run_until_complete(host.renew())
        assert host.renewtimer > 0
        assert self._loop.run_until_complete(host.unsubscribe())
        assert host.renewtimer == 0

        assert not self._loop.run_until_complete(host.convert_time("no_time"))
        host._password = "notmypassword"
        assert not self._loop.run_until_complete(
            host.subscribe("192.168.1.1/fakewebhook")
        )
        host._host = "192.168.1.1"
        assert not self._loop.run_until_complete(
            host.subscribe("192.168.1.1/fakewebhook")
        )
#endof class TestSubscription
