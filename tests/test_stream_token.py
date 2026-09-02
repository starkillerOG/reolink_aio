from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

from reolink_aio.api import DEFAULT_RTMP_AUTH_METHOD, Host
from reolink_aio.enums import VodRequestType


class TestStreamToken(unittest.IsolatedAsyncioTestCase):
    """Session tokens must not be embedded in stream URLs after they expire or are invalidated."""

    async def asyncSetUp(self) -> None:
        self.host = Host(
            host="192.168.1.10",
            username="admin",
            password="secret",
            port=80,
            use_https=False,
            protocol="rtmp",
            rtmp_auth_method="TOKEN",
        )
        self.host._stream_channels = [0]
        self.host._rtmp_port = 1935
        self.host._url = "http://192.168.1.10:80/cgi-bin/api.cgi"
        self.login_http_calls = 0

        async def fake_send(body, param=None, expected_response_type="json", retry=3):
            cmd = ""
            if body:
                cmd = body[0].get("cmd", "")
            if param:
                cmd = param.get("cmd", cmd)
            if cmd == "Login":
                self.login_http_calls += 1
                return [{"cmd": "Login", "code": 0, "value": {"Token": {"name": "fresh-token", "leaseTime": 3600}}}]
            if cmd == "Logout":
                return "ok"
            return [{"cmd": cmd or "GetDevInfo", "code": 0, "value": {}}]

        self.host.send = fake_send  # type: ignore[method-assign]

    async def asyncTearDown(self) -> None:
        session = self.host._aiohttp_session
        if session is not None and not session.closed:
            await session.close()

    def _set_token(self, name: str, *, valid_for: timedelta | None = None, expired: bool = False) -> None:
        self.host._token = name
        if expired:
            self.host._lease_time = datetime.now() - timedelta(seconds=5)
        else:
            self.host._lease_time = datetime.now() + (valid_for or timedelta(hours=1))

    def test_token_valid_respects_lease_time(self) -> None:
        self.assertFalse(self.host._token_valid())
        self.assertFalse(self.host.session_active)

        self._set_token("abc", valid_for=timedelta(hours=1))
        self.assertTrue(self.host._token_valid())
        self.assertTrue(self.host.session_active)

        self._set_token("abc", expired=True)
        self.assertFalse(self.host._token_valid())
        self.assertFalse(self.host.session_active)

    def test_rtmp_source_embeds_valid_token(self) -> None:
        self._set_token("valid-token")
        url = self.host.get_rtmp_stream_source(0)
        self.assertIsNotNone(url)
        self.assertIn("token=valid-token", url)
        self.assertNotIn("password=", url)

    def test_rtmp_source_does_not_embed_expired_token(self) -> None:
        self._set_token("stale-token", expired=True)
        url = self.host.get_rtmp_stream_source(0)
        self.assertIsNotNone(url)
        self.assertNotIn("stale-token", url)
        self.assertNotIn("token=", url)
        self.assertIn("user=admin", url)
        self.assertIn("password=secret", url)

    def test_rtmp_source_does_not_embed_token_after_expire_session_marker(self) -> None:
        # expire_session() backdates the lease but keeps the cached token string.
        self._set_token("stale-token")
        self.host._lease_time = datetime.now() - timedelta(seconds=5)
        url = self.host.get_rtmp_stream_source(0)
        self.assertIsNotNone(url)
        self.assertNotIn("stale-token", url)
        self.assertIn("password=secret", url)

    def test_rtmp_password_auth_never_uses_token(self) -> None:
        self.host._rtmp_auth_method = DEFAULT_RTMP_AUTH_METHOD
        self._set_token("valid-token")
        url = self.host.get_rtmp_stream_source(0)
        self.assertIsNotNone(url)
        self.assertNotIn("token=", url)
        self.assertIn("password=secret", url)

    async def test_stream_source_refreshes_expired_token_via_login(self) -> None:
        self._set_token("stale-token", expired=True)
        url = await self.host.get_stream_source(0, "main", check=False)
        self.assertIsNotNone(url)
        self.assertGreaterEqual(self.login_http_calls, 1)
        self.assertNotIn("stale-token", url)
        self.assertIn("token=fresh-token", url)

    async def test_stream_source_does_not_relogin_when_token_still_valid(self) -> None:
        self._set_token("valid-token", valid_for=timedelta(hours=1))
        url = await self.host.get_stream_source(0, "main", check=False)
        self.assertIsNotNone(url)
        self.assertEqual(self.login_http_calls, 0)
        self.assertIn("token=valid-token", url)

        url = await self.host.get_stream_source(0, "main", check=False)
        self.assertEqual(self.login_http_calls, 0)
        self.assertIn("token=valid-token", url)

    async def test_stream_source_replaces_camera_invalidated_token(self) -> None:
        """Camera dropped the session before lease expiry; verify path must not keep the stale token."""
        self._set_token("stale-token", valid_for=timedelta(hours=1))

        async def recovering_send(body, param=None, expected_response_type="json", retry=3):
            cmd = ""
            if body:
                cmd = body[0].get("cmd", "")
            if param:
                cmd = param.get("cmd", cmd)
            if cmd == "GetDevInfo":
                # Simulate send() recovery: camera rejected the token, a new one was issued.
                self.host._token = "fresh-token"
                self.host._lease_time = datetime.now() + timedelta(hours=1)
                return [{"cmd": "GetDevInfo", "code": 0, "value": {}}]
            if cmd == "Login":
                self.login_http_calls += 1
                return [{"cmd": "Login", "code": 0, "value": {"Token": {"name": "fresh-token", "leaseTime": 3600}}}]
            if cmd == "Logout":
                return "ok"
            return [{"cmd": cmd or "GetDevInfo", "code": 0, "value": {}}]

        self.host.send = recovering_send  # type: ignore[method-assign]
        url = await self.host.get_stream_source(0, "main", check=False)
        self.assertIsNotNone(url)
        self.assertEqual(self.login_http_calls, 0)
        self.assertNotIn("stale-token", url)
        self.assertIn("token=fresh-token", url)

    async def test_vod_playback_does_not_embed_expired_token(self) -> None:
        self._set_token("stale-token", expired=True)
        _mime, url = await self.host.get_vod_source(0, "RecS01_20240101_120000_120200.mp4", request_type=VodRequestType.PLAYBACK)
        self.assertGreaterEqual(self.login_http_calls, 1)
        self.assertNotIn("stale-token", url)
        self.assertIn("token=fresh-token", url)

    async def test_vod_playback_uses_valid_token_without_relogin(self) -> None:
        self._set_token("valid-token", valid_for=timedelta(hours=1))
        _mime, url = await self.host.get_vod_source(0, "RecS01_20240101_120000_120200.mp4", request_type=VodRequestType.PLAYBACK)
        self.assertEqual(self.login_http_calls, 0)
        self.assertIn("token=valid-token", url)

    async def test_ensure_valid_token_skips_login_when_lease_is_valid(self) -> None:
        self._set_token("valid-token", valid_for=timedelta(hours=1))
        self.host.login = AsyncMock(side_effect=AssertionError("login() should not re-authenticate"))  # type: ignore[method-assign]
        await self.host._ensure_valid_token(verify=False)
        self.host.login.assert_not_called()
        self.assertEqual(self.host._token, "valid-token")
