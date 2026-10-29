"""An MCP request must always produce a complete HTTP response.

`StreamableHTTPSessionManager` runs the MCP server in a task on the session
manager's task group and streams its output back over `send`. When that task
dies — a tool raised something the protocol layer did not convert, or the task
group was cancelled because the worker is shutting down mid-rollout — the write
stream closes and `handle_request` returns *normally*, having started the
response and never finished it.

uvicorn's reaction is to log `ASGI callable returned without completing
response` at ERROR and drop the socket. 555 of those reached Sentry in 30 days
(`tools/call list_document_sources`, `tools/call ngm_query_judicial`) carrying
no stack, no tool name and no status, because by the time uvicorn notices, the
request context is gone. The client sees a truncated stream, which it cannot
tell from a network fault, so it retries a call that already ran.

These pin both halves: nothing started -> a framed JSON-RPC error; something
started -> a clean end of body rather than a dropped connection.
"""

import json

import pytest

from jawafdehi_mcp.http_server import JawafdehiMCPServer, _ResponseGuard

from .test_http_integration import _dummy_receive, _make_scope, _SendRecorder

pytestmark = pytest.mark.asyncio(loop_scope="function")


class TestResponseGuard:
    """The bookkeeping, in isolation."""

    async def test_a_fresh_guard_has_seen_nothing(self):
        guard = _ResponseGuard(_SendRecorder())
        assert not guard.started
        assert not guard.complete

    async def test_start_then_body_is_a_complete_response(self):
        recorder = _SendRecorder()
        guard = _ResponseGuard(recorder)
        await guard({"type": "http.response.start", "status": 200, "headers": []})
        assert guard.started and not guard.complete
        await guard({"type": "http.response.body", "body": b"{}"})
        assert guard.complete

    async def test_a_streamed_body_is_incomplete_until_the_last_chunk(self):
        guard = _ResponseGuard(_SendRecorder())
        await guard({"type": "http.response.start", "status": 200, "headers": []})
        await guard({"type": "http.response.body", "body": b"a", "more_body": True})
        assert not guard.complete
        await guard({"type": "http.response.body", "body": b"b", "more_body": False})
        assert guard.complete

    async def test_it_forwards_every_message_untouched(self):
        """It observes the stream; it must not buffer or rewrite it."""
        recorder = _SendRecorder()
        guard = _ResponseGuard(recorder)
        sent = [
            {"type": "http.response.start", "status": 207, "headers": [(b"x", b"y")]},
            {"type": "http.response.body", "body": b"chunk", "more_body": True},
            {"type": "http.response.body", "body": b"", "more_body": False},
        ]
        for message in sent:
            await guard(message)
        assert recorder.messages == sent


class TestAbandonedResponses:
    """End to end through `_handle_http`, with the transport stubbed."""

    @staticmethod
    def _server_with(monkeypatch, handler):
        server = JawafdehiMCPServer()
        monkeypatch.setattr(server.session_manager, "handle_request", handler)
        return server

    async def test_a_handler_that_sends_nothing_gets_a_framed_error(self, monkeypatch):
        async def sends_nothing(scope, receive, send):
            return

        server = self._server_with(monkeypatch, sends_nothing)
        send = _SendRecorder()
        await server._handle_http(_make_scope([]), _dummy_receive, send)

        assert send.status == 500
        body = json.loads(send.body)
        assert body["jsonrpc"] == "2.0"
        assert body["id"] is None
        assert body["error"]["code"] == -32603

    async def test_a_handler_that_raises_gets_a_framed_error(self, monkeypatch):
        async def explodes(scope, receive, send):
            raise RuntimeError("tool blew up")

        server = self._server_with(monkeypatch, explodes)
        send = _SendRecorder()
        await server._handle_http(_make_scope([]), _dummy_receive, send)

        assert send.status == 500
        assert json.loads(send.body)["error"]["code"] == -32603

    async def test_a_raise_does_not_escape_to_uvicorn(self, monkeypatch):
        """Letting it through produces a bare 500 with no MCP framing."""

        async def explodes(scope, receive, send):
            raise RuntimeError("tool blew up")

        server = self._server_with(monkeypatch, explodes)
        await server._handle_http(_make_scope([]), _dummy_receive, _SendRecorder())

    async def test_a_half_streamed_response_is_closed_cleanly(self, monkeypatch):
        """Headers are already on the wire, so the status cannot change."""

        async def dies_mid_stream(scope, receive, send):
            await send(
                {"type": "http.response.start", "status": 200, "headers": []}
            )
            await send({"type": "http.response.body", "body": b"partial", "more_body": True})

        server = self._server_with(monkeypatch, dies_mid_stream)
        send = _SendRecorder()
        await server._handle_http(_make_scope([]), _dummy_receive, send)

        assert send.status == 200
        final = send.messages[-1]
        assert final["type"] == "http.response.body"
        assert final.get("more_body", False) is False

    async def test_a_raise_after_the_stream_started_also_closes_cleanly(
        self, monkeypatch
    ):
        async def dies_mid_stream(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"x", "more_body": True})
            raise RuntimeError("crashed after the headers")

        server = self._server_with(monkeypatch, dies_mid_stream)
        send = _SendRecorder()
        await server._handle_http(_make_scope([]), _dummy_receive, send)

        assert send.status == 200
        assert send.messages[-1].get("more_body", False) is False

    async def test_a_normal_response_is_left_exactly_alone(self, monkeypatch):
        """The no-op path, which is almost every request."""

        async def responds(scope, receive, send):
            await send(
                {"type": "http.response.start", "status": 200, "headers": []}
            )
            await send({"type": "http.response.body", "body": b'{"ok":true}'})

        server = self._server_with(monkeypatch, responds)
        send = _SendRecorder()
        await server._handle_http(_make_scope([]), _dummy_receive, send)

        assert len(send.messages) == 2
        assert json.loads(send.body) == {"ok": True}

    async def test_cancellation_is_not_swallowed(self, monkeypatch):
        """Shutdown must still unwind — `except Exception` must not catch it.

        `asyncio.CancelledError` derives from BaseException precisely so that a
        broad handler cannot turn a cancelled task into a completed one. If this
        ever starts passing by returning a 500, the worker has stopped shutting
        down correctly.
        """
        import asyncio

        async def cancelled(scope, receive, send):
            raise asyncio.CancelledError()

        server = self._server_with(monkeypatch, cancelled)
        with pytest.raises(asyncio.CancelledError):
            await server._handle_http(_make_scope([]), _dummy_receive, _SendRecorder())


class TestTheFinisherCannotMakeThingsWorse:
    """Found in review: both halves of the finisher could themselves fail.

    The peer being gone is one of the reasons a response gets abandoned, so the
    finishing send is exactly the send most likely to raise. If it escapes, the
    original failure is replaced by an opaque "Exception in ASGI application" —
    the outcome this whole mechanism exists to prevent.
    """

    @staticmethod
    def _server_with(monkeypatch, handler):
        server = JawafdehiMCPServer()
        monkeypatch.setattr(server.session_manager, "handle_request", handler)
        return server

    class _DisconnectedSend:
        """Raises like uvicorn does once the peer is gone."""

        def __init__(self, fail_after=0):
            self.calls = 0
            self.fail_after = fail_after

        async def __call__(self, message):
            self.calls += 1
            if self.calls > self.fail_after:
                raise OSError("Cannot write to a closing transport")

    async def test_a_dead_peer_during_the_framed_error_does_not_escape(
        self, monkeypatch
    ):
        async def sends_nothing(scope, receive, send):
            return

        server = self._server_with(monkeypatch, sends_nothing)
        await server._handle_http(
            _make_scope([]), _dummy_receive, self._DisconnectedSend()
        )

    async def test_a_dead_peer_during_the_body_close_does_not_escape(
        self, monkeypatch
    ):
        send = self._DisconnectedSend(fail_after=2)

        async def dies_mid_stream(scope, receive, send_):
            await send_({"type": "http.response.start", "status": 200, "headers": []})
            await send_({"type": "http.response.body", "body": b"x", "more_body": True})

        server = self._server_with(monkeypatch, dies_mid_stream)
        await server._handle_http(_make_scope([]), _dummy_receive, send)

    async def test_a_send_that_fails_does_not_count_as_a_completed_response(self):
        """The guard records what went out, not what was attempted.

        Flagging before the await marks a response complete that the send then
        failed to deliver, and the finisher's `if guard.complete` early-exit
        then skips precisely the case it exists to catch.
        """

        class _Failing:
            async def __call__(self, message):
                raise OSError("peer gone")

        guard = _ResponseGuard(_Failing())
        with pytest.raises(OSError):
            await guard({"type": "http.response.body", "body": b"{}"})
        assert not guard.complete


class TestTheConditionStaysVisible:
    """Found in review: WARNING would have hidden the issue, not fixed it.

    sentry_sdk's LoggingIntegration reports at `logging.ERROR` and turns
    anything lower into a breadcrumb. The dominant production signature takes
    the "handler returned early" path, where there is no exception to report —
    so this log IS the whole signal, and at WARNING a tool that kept dying
    would produce zero Sentry events.
    """

    @staticmethod
    def _server_with(monkeypatch, handler):
        server = JawafdehiMCPServer()
        monkeypatch.setattr(server.session_manager, "handle_request", handler)
        return server

    async def test_an_abandoned_response_logs_at_error(self, monkeypatch, caplog):
        import logging

        async def sends_nothing(scope, receive, send):
            return

        server = self._server_with(monkeypatch, sends_nothing)
        with caplog.at_level(logging.DEBUG):
            await server._handle_http(_make_scope([]), _dummy_receive, _SendRecorder())

        abandoned = [r for r in caplog.records if "mcp_response_abandoned" in r.getMessage()]
        assert abandoned, "the abandoned-response condition must be logged"
        assert all(r.levelno >= logging.ERROR for r in abandoned)

    async def test_the_log_names_the_endpoint(self, monkeypatch, caplog):
        import logging

        async def sends_nothing(scope, receive, send):
            return

        server = self._server_with(monkeypatch, sends_nothing)
        with caplog.at_level(logging.DEBUG):
            await server._handle_http(
                _make_scope([], path="/", method="POST"), _dummy_receive, _SendRecorder()
            )
        assert "POST" in caplog.text

    async def test_a_normal_response_logs_nothing(self, monkeypatch, caplog):
        import logging

        async def responds(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        server = self._server_with(monkeypatch, responds)
        with caplog.at_level(logging.DEBUG):
            await server._handle_http(_make_scope([]), _dummy_receive, _SendRecorder())
        assert "mcp_response_abandoned" not in caplog.text
