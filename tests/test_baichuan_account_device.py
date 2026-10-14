from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from reolink_aio.baichuan.baichuan import Baichuan
from reolink_aio.exceptions import ApiError, CredentialsInvalidError, LoginAccountDeviceError

NONCE_V3 = "<body><Encryption version=\"1.1\"><type>md5</type><nonce>abc</nonce><sigVer>v3</sigVer><publicKeyAlgo>X25519</publicKeyAlgo></Encryption></body>"
LOGIN_OK = "<body><DeviceInfo version=\"1.1\"></DeviceInfo></body>"


class TestBaichuanAccountDevice(unittest.IsolatedAsyncioTestCase):
    def _baichuan(self, login_result: str | Exception) -> Baichuan:
        baichuan = Baichuan(host="127.0.0.1", username="admin", password="password", http_api=SimpleNamespace(nvr_name="test", _is_nvr=True, _is_hub=True, _enc_range={0: []}))
        baichuan._connect_if_needed = AsyncMock()  # type: ignore[method-assign]
        baichuan._close_battery_connection = Mock()  # type: ignore[method-assign]
        baichuan._first_login = False  # noqa: SLF001

        async def send(cmd_id: int, enc_type=None, message_class: str = "", body: str = "", **kwargs) -> str:
            if message_class == "1465":
                return NONCE_V3
            if isinstance(login_result, Exception):
                raise login_result
            return login_result

        baichuan.send = send  # type: ignore[method-assign]
        return baichuan

    async def test_sig_ver_v3_local_device_logs_in(self) -> None:
        baichuan = self._baichuan(LOGIN_OK)
        await baichuan.login()
        self.assertTrue(baichuan._logged_in)  # noqa: SLF001

    async def test_sig_ver_v3_rejected_login_is_account_device(self) -> None:
        baichuan = self._baichuan(ApiError("login deny", rspCode=406))
        with self.assertRaises(LoginAccountDeviceError):
            await baichuan.login()

    async def test_sig_ver_v3_wrong_password_is_invalid_credentials(self) -> None:
        baichuan = self._baichuan(ApiError("bad credentials", rspCode=401))
        with self.assertRaises(CredentialsInvalidError):
            await baichuan.login()
