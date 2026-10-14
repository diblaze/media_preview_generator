"""nodes.client: a request the node may have run (no answer in time) is told apart from one that never reached it."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from media_preview_generator.nodes.client import NodeAddress, NodeClient, NodeTimeoutError, NodeUnreachableError


def _client(error: Exception) -> NodeClient:
    session = MagicMock(spec=requests.Session)
    session.request.side_effect = error
    return NodeClient(NodeAddress("http://node:9494", "secret-key"), session=session)


class TestTimeouts:
    def test_no_answer_after_the_request_was_sent_is_a_timeout(self):
        with pytest.raises(NodeTimeoutError) as raised:
            _client(requests.ReadTimeout("read timed out")).start_task({"op": "x"})
        assert isinstance(raised.value, NodeUnreachableError)  # callers that only know "unreachable" still catch it
        assert "secret-key" not in str(raised.value)

    @pytest.mark.parametrize(
        "error",
        [requests.ConnectTimeout("connect timed out"), requests.ConnectionError("refused")],
        ids=["connect-timeout", "refused"],
    )
    def test_a_request_that_never_reached_the_node_is_not_a_timeout(self, error):
        with pytest.raises(NodeUnreachableError) as raised:
            _client(error).start_task({"op": "x"})
        assert not isinstance(raised.value, NodeTimeoutError)
