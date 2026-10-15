"""Tests for the encrypted (digest login) HTTP session."""

from reolink_aio.http_crypto import HttpCrypto, digest_response, parse_digest_challenge


def test_digest_response_rfc2617() -> None:
    # same formula as the web UI: HA1=MD5(user:realm:pass), HA2=MD5(POST:uri)
    resp = digest_response("admin", "pass", "NVR", "nonce", "00000002", "cnonce", "auth")
    assert len(resp) == 32 and resp == resp.lower()


def test_parse_challenge() -> None:
    chal = parse_digest_challenge('Digest qop="auth", realm="NVR",nonce="abc123", stale="FALSE", nc="00000002"')
    assert chal == {"qop": "auth", "realm": "NVR", "nonce": "abc123", "stale": "FALSE", "nc": "00000002"}


def test_crypto_roundtrip_and_padding() -> None:
    crypto = HttpCrypto("admin", "pass", "nonce", "cnonce")
    msg = '[{"cmd":"GetDevInfo","action":0,"param":{}}]'
    enc = crypto.encrypt(msg)
    assert crypto.decrypt(enc) == msg
    assert len(enc) % 4 == 0
    assert crypto.decrypt("[{plain json}]") is None


def test_encrypted_query_counter() -> None:
    crypto = HttpCrypto("admin", "pass", "nonce", "cnonce")
    crypto.init_counts({"countTotal": 10, "checkBasic": 5})
    assert crypto.decrypt(crypto.encrypted_query({"cmd": "Snap", "channel": 0})) == "countId=3&checkNum=6&cmd=Snap&channel=0"
    assert crypto.decrypt(crypto.encrypted_query({})) == "countId=3&checkNum=7"
