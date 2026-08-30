"""RPC failure classification.

Three chains' worth of failure shapes, all of which cost a real build:
  Robinhood: -32000 'log query timed out' / 'logs matched by query exceeds limit'
  Ink:       -32602 'query exceeds max results 20000, retry with the range X-Y'
             and a bare HTTP 500 on an oversized window
An execution revert is deterministic and must NOT be retried.
"""
import inspect

import pytest

from mintscout.rpc import (RpcClient, RpcError, is_range_reducible,
                           parse_range_hint)


@pytest.mark.parametrize("msg", [
    "log query timed out",
    "logs matched by query exceeds limit of 10000",
    "query exceeds max results 20000, retry with the range 54011362-54015249",
    "block range greater than 10000 max",
    "response size exceeded",
])
def test_range_reducible_messages(msg):
    assert is_range_reducible(msg)


@pytest.mark.parametrize("msg", [
    "execution reverted",
    "insufficient funds",
    "nonce too low",
])
def test_not_range_reducible(msg):
    assert not is_range_reducible(msg)


def test_parses_ink_range_hint():
    hint = parse_range_hint(
        "query exceeds max results 20000, retry with the range 54011362-54015249")
    assert hint == (54011362, 54015249)
    assert parse_range_hint("log query timed out") is None


def test_rpc_error_is_runtime_error_so_handler_order_matters():
    """RpcError subclasses RuntimeError.

    get_logs_chunked catches both. If the RuntimeError clause were written
    first it would swallow every structured RPC error and the range-reducing
    logic would never run -- which is exactly what happened, and it killed two
    Ink builds before it was spotted. Pin the ordering.
    """
    assert issubclass(RpcError, RuntimeError)
    src = inspect.getsource(RpcClient.get_logs_chunked)
    assert src.index("except RpcError") < src.index("except RuntimeError"), (
        "the RpcError handler must precede the RuntimeError handler")
