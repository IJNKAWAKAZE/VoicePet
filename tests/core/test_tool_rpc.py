from dataclasses import FrozenInstanceError

import pytest

from core.tool_rpc import (
    RpcError,
    RpcProtocolError,
    RpcRequest,
    RpcResponse,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
)


def execute_params():
    return {
        "proposal": {
            "call_id": "call-1",
            "tool_name": "system_info",
            "arguments": {},
        },
        "decision": {
            "allowed": True,
            "risk": "R0",
            "confirmation": "none",
            "call_fingerprint": "a" * 64,
            "policy_version": 1,
        },
        "authorization": None,
        "context": {
            "turn_id": "00000000-0000-0000-0000-000000000001",
            "correlation_id": "00000000-0000-0000-0000-000000000002",
            "tool_call_id": "call-1",
        },
    }


def test_request_round_trip_is_deterministic_and_immutable():
    request = RpcRequest("request-1", "execute", execute_params())

    encoded = encode_request(request)
    decoded = decode_request(encoded)

    assert encoded.endswith(b"\n")
    assert encoded == encode_request(request)
    assert decoded == request
    with pytest.raises(FrozenInstanceError):
        request.method = "ping"
    with pytest.raises(TypeError):
        decoded.params["authorization"] = "changed"


def test_success_and_error_response_round_trip():
    success = RpcResponse("request-1", result={"status": "success"})
    failure = RpcResponse(
        "request-2",
        error=RpcError(-32602, "请求参数无效"),
    )

    assert decode_response(encode_response(success)) == success
    assert decode_response(encode_response(failure)) == failure


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff\n",
        b'{"jsonrpc":"2.0","id":"a","id":"b","method":"ping","params":{}}\n',
        b'{"jsonrpc":"2.0","id":"a","method":"ping","params":{"value":NaN}}\n',
        b'{"jsonrpc":"2.0","id":"a","method":"ping","params":{},"extra":1}\n',
        b'[]\n',
    ],
)
def test_request_decoder_rejects_invalid_or_ambiguous_json(payload):
    with pytest.raises(RpcProtocolError):
        decode_request(payload)


@pytest.mark.parametrize(
    ("request_id", "method", "params"),
    [
        ("", "ping", {}),
        ("a" * 65, "ping", {}),
        ("bad id", "ping", {}),
        ("id", "unknown", {}),
        ("id", "ping", {"extra": True}),
        ("id", "cancel", {}),
        ("id", "cancel", {"request_id": "bad id"}),
        ("id", "execute", {}),
    ],
)
def test_request_contract_rejects_bad_identifiers_methods_and_params(
    request_id,
    method,
    params,
):
    with pytest.raises(RpcProtocolError):
        RpcRequest(request_id, method, params)


def test_codec_rejects_oversize_lines_and_does_not_echo_secret():
    secret = "private-secret-value"
    payload = (
        '{"jsonrpc":"2.0","id":"a","method":"ping","params":{"secret":"'
        + secret
        + '"}}\n'
    ).encode()

    with pytest.raises(RpcProtocolError) as captured:
        decode_request(payload, max_line_bytes=32)

    assert secret not in str(captured.value)


def test_response_requires_exactly_one_result_or_error():
    with pytest.raises(RpcProtocolError):
        RpcResponse("request-1")
    with pytest.raises(RpcProtocolError):
        RpcResponse(
            "request-1",
            result={},
            error=RpcError(-32603, "内部错误"),
        )
