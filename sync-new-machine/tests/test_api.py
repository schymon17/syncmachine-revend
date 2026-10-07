from __future__ import annotations

import pytest

from revend_sync.api import ApiError, Client, encode_body, sign

from .fake_api import KEY_ID, MACHINE_ID, SECRET, FakeApi


def test_signature_matches_the_server_algorithm():
    # Vector computed by PHP with VerifyMachineApiSignature's algorithm:
    # base64_encode(hash_hmac('sha256', implode("\n", [METHOD, /path, body, timestamp, nonce]), secret, true))
    body = b'{"machineId":"RVM_3000_1234567890"}'
    assert (
        sign(SECRET, "POST", "/api/revend/machine/v2/heartbeat", body, "1700000000", "nonce-0000000000000000")
        == "FFuzTxqepY0nDuVPk0wTrBJHE0ltnpD18BzXFW+DWOw="
    )


def test_signed_request_is_accepted(api, client):
    body = encode_body({"machineId": MACHINE_ID, "timestamp": "2026-10-07T00:00:00Z", "kind": "heartbeat"})
    response = client.post(api.base + "/api/revend/machine/v2/heartbeat", body, "heartbeat-key-0001")
    assert response.status == 202
    assert api.got("heartbeat")[0]["kind"] == "heartbeat"


def test_wrong_secret_is_a_permanent_error(api):
    with pytest.raises(ApiError) as caught:
        Client(KEY_ID, "x" * 40).post(
            api.base + "/api/revend/machine/v2/heartbeat", b'{"machineId":"%s"}' % MACHINE_ID.encode()
        )
    assert caught.value.status == 401
    assert caught.value.code == "invalid_signature"
    assert not caught.value.retryable


def test_a_drifting_machine_clock_is_corrected_from_the_server_date():
    api = FakeApi(clock_skew=900).start()  # machine 15 minutes behind
    try:
        client = Client(KEY_ID, SECRET)
        url = api.base + "/api/revend/machine/v2/register"
        body = encode_body({"machineId": MACHINE_ID})
        with pytest.raises(ApiError) as caught:
            client.post(url, body)
        assert caught.value.code == "stale_timestamp"
        assert caught.value.retryable
        # The failed response carried the server time; the retry is signed with it.
        assert client.post(url, body).status == 200
    finally:
        api.stop()


def test_unreachable_api_is_a_retryable_network_error():
    with pytest.raises(ApiError) as caught:
        Client(KEY_ID, SECRET).post("http://127.0.0.1:9/api/revend/machine/v2/heartbeat", b"{}")
    assert caught.value.network
    assert caught.value.retryable


@pytest.mark.parametrize(
    "status,retryable", [(429, True), (500, True), (503, True), (422, False), (403, False)]
)
def test_http_errors_are_classified(api, client, status, retryable):
    api.fail_with = status
    with pytest.raises(ApiError) as caught:
        client.post(api.base + "/api/revend/machine/v2/status", encode_body({"machineId": MACHINE_ID}))
    assert caught.value.retryable is retryable
