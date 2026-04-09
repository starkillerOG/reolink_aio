import unittest

from reolink_aio.baichuan.udp_protocol import BaichuanUdpConnection, MAGIC_UDP_BC, MTU

TEST_HOST = "test-host"
TEST_PORT = 12345


class _FakeDatagramTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        self.sent.append((data, addr))

    def is_closing(self) -> bool:
        return False


class _FakeProtocol:
    def __init__(self, host_id: int) -> None:
        self.host_id = host_id


class TestUdpFragmentation(unittest.IsolatedAsyncioTestCase):
    async def test_send_without_wait_splits_large_bc_frames_to_fit_mtu(self) -> None:
        connection = BaichuanUdpConnection(TEST_HOST)
        connection._transport = _FakeDatagramTransport()  # type: ignore[assignment]
        connection._protocol = _FakeProtocol(host_id=884)  # type: ignore[assignment]
        connection._port = TEST_PORT

        payload = (bytes(range(256)) * 8) + (b"tail" * 54) + b"abc"
        self.assertEqual(len(payload), 2267)

        await connection.send_without_wait(payload)

        datagram = connection._transport
        self.assertEqual(len(datagram.sent), 2)

        first_packet, first_addr = datagram.sent[0]
        second_packet, second_addr = datagram.sent[1]
        self.assertEqual(first_addr, (TEST_HOST, TEST_PORT))
        self.assertEqual(second_addr, (TEST_HOST, TEST_PORT))

        max_payload_size = MTU - 20
        self.assertEqual(first_packet[0:4].hex(), MAGIC_UDP_BC)
        self.assertEqual(second_packet[0:4].hex(), MAGIC_UDP_BC)
        self.assertEqual(int.from_bytes(first_packet[4:8], "little"), 884)
        self.assertEqual(int.from_bytes(second_packet[4:8], "little"), 884)
        self.assertEqual(int.from_bytes(first_packet[12:16], "little"), 0)
        self.assertEqual(int.from_bytes(second_packet[12:16], "little"), 1)
        self.assertEqual(int.from_bytes(first_packet[16:20], "little"), max_payload_size)
        self.assertEqual(int.from_bytes(second_packet[16:20], "little"), len(payload) - max_payload_size)
        self.assertEqual(first_packet[20:], payload[:max_payload_size])
        self.assertEqual(second_packet[20:], payload[max_payload_size:])
