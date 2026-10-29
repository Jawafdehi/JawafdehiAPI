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
        guard = _ResponseGuard(_SendRecorder(), _dummy_receive)
        assert not guard.started
        assert not guard.complete

    async def test_start_then_body_is_a_complete_response(self):
        recorder = _SendRecorder()
        guard = _ResponseGuard(recorder, _dummy_receive)
        await guard({"type": "http.response.start", "status": 200, "headers": []})
        assert guard.started and not guard.complete
        await guard({"type": "http.response.body", "body": b"{}"})
        assert guard.complete

    async def test_a_streamed_body_is_incomplete_until_the_last_chunk(self):
        guard = _ResponseGuard(_SendRecorder(), _dummy_receive)
        await guard({"type": "http.response.start", "status": 200, "headers": []})
        await guard({"type": "http.response.body", "body": b"a", "more_body": True})
        assert not guard.complete
        await guard({"type": "http.response.body", "body": b"b", "more_body": False})
        assert guard.complete

    async def test_it_forwards_every_message_untouched(self):
        """It observes the stream; it must not buffer or rewrite it."""
        recorder = _SendRecorder()
        guard = _ResponseGuard(recorder, _dummy_receive)
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

        guard = _ResponseGuard(_Failing(), _dummy_receive)
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


class TestAClientHangingUpIsNotADefect:
    """Found in the second review round, and it would have made things worse.

    The MCP transport answers over `EventSourceResponse`, whose
    `_listen_for_disconnect` cancels the streaming task when the peer goes
    away — skipping the closing `more_body: False`. Without disconnect
    awareness, every client-initiated SSE close and every open stream on a pod
    rollout looks exactly like an abandoned response.

    uvicorn draws the same line: its own "returned without completing
    response" is guarded by `and not self.disconnected`. A replacement that
    fired on a strictly larger set of conditions than the message it replaces
    would be a regression dressed as a fix.
    """

    @staticmethod
    def _server_with(monkeypatch, handler):
        server = JawafdehiMCPServer()
        monkeypatch.setattr(server.session_manager, "handle_request", handler)
        return server

    async def test_the_guard_notices_the_disconnect(self):
        async def receive():
            return {"type": "http.disconnect"}

        guard = _ResponseGuard(_SendRecorder(), receive)
        assert not guard.disconnected
        await guard.receive()
        assert guard.disconnected

    async def test_an_ordinary_message_is_not_a_disconnect(self):
        async def receive():
            return {"type": "http.request", "body": b"{}"}

        guard = _ResponseGuard(_SendRecorder(), receive)
        await guard.receive()
        assert not guard.disconnected

    async def test_receive_still_forwards_the_message(self):
        async def receive():
            return {"type": "http.request", "body": b"payload"}

        guard = _ResponseGuard(_SendRecorder(), receive)
        assert await guard.receive() == {"type": "http.request", "body": b"payload"}

    async def test_a_half_stream_after_a_disconnect_logs_no_error(
        self, monkeypatch, caplog
    ):
        import logging

        async def streams_then_peer_leaves(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"x", "more_body": True})
            await receive()  # the disconnect arrives here

        server = self._server_with(monkeypatch, streams_then_peer_leaves)
        with caplog.at_level(logging.DEBUG):
            await server._handle_http(
                _make_scope([]), _dummy_receive, _SendRecorder()
            )

        assert "mcp_response_abandoned" not in caplog.text
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not errors, "a client hanging up must not be reported as a defect"

    async def test_nothing_is_sent_to_a_departed_client(self, monkeypatch):
        async def streams_then_peer_leaves(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"x", "more_body": True})
            await receive()

        server = self._server_with(monkeypatch, streams_then_peer_leaves)
        send = _SendRecorder()
        await server._handle_http(_make_scope([]), _dummy_receive, send)
        # Two messages from the handler, and no finishing chunk appended.
        assert len(send.messages) == 2

    async def test_a_genuine_abandonment_is_still_reported(self, monkeypatch, caplog):
        """The disconnect check must not swallow the real case."""
        import logging

        async def dies_mid_stream(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"x", "more_body": True})

        server = self._server_with(monkeypatch, dies_mid_stream)
        with caplog.at_level(logging.DEBUG):
            await server._handle_http(_make_scope([]), _dummy_receive, _SendRecorder())
        assert "mcp_response_abandoned" in caplog.text


class TestOneFailureIsOneIssue:
    """Found in review: the raised path logged two ERRORs for one event."""

    @staticmethod
    def _server_with(monkeypatch, handler):
        server = JawafdehiMCPServer()
        monkeypatch.setattr(server.session_manager, "handle_request", handler)
        return server

    async def test_a_raised_handler_produces_exactly_one_error_record(
        self, monkeypatch, caplog
    ):
        import logging

        async def explodes(scope, receive, send):
            raise RuntimeError("tool blew up")

        server = self._server_with(monkeypatch, explodes)
        with caplog.at_level(logging.DEBUG):
            await server._handle_http(_make_scope([]), _dummy_receive, _SendRecorder())

        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1
        assert "mcp_request_failed" in errors[0].getMessage()

    async def test_that_record_carries_the_exception_for_sentry(
        self, monkeypatch, caplog
    ):
        """Sentry builds an exception event ONLY from record.exc_info.

        structlog's chain runs `format_exc_info`, which renders the traceback
        to a string and nulls `exc_info` before the LogRecord exists — so a
        structlog call here would produce a message-only issue with no type
        and no frames. MCP traffic bypasses DjangoIntegration entirely, so
        this is the only chance to report it properly.
        """
        import logging

        async def explodes(scope, receive, send):
            raise RuntimeError("tool blew up")

        server = self._server_with(monkeypatch, explodes)
        with caplog.at_level(logging.DEBUG):
            await server._handle_http(_make_scope([]), _dummy_receive, _SendRecorder())

        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors[0].exc_info is not None
        assert errors[0].exc_info[0] is RuntimeError

    async def test_the_client_still_gets_its_framed_error(self, monkeypatch):
        """Quieter logging must not change what the caller receives."""

        async def explodes(scope, receive, send):
            raise RuntimeError("tool blew up")

        server = self._server_with(monkeypatch, explodes)
        send = _SendRecorder()
        await server._handle_http(_make_scope([]), _dummy_receive, send)
        assert send.status == 500


class TestADeadPeerIsNeverADefect:
    """Round three: two paths still reported a client hang-up as an error.

    1. `mcp_request_failed` was logged at ERROR *before* consulting
       `guard.disconnected`, so a disconnect surfacing as an exception on the
       next write (anyio ClosedResourceError, uvicorn's OSError) opened a
       Sentry issue. None of those types are in the noise filter either.
    2. Disconnect detection only fired if something called `receive()` again —
       which the json_response POST path never does after reading the body. A
       client that walks away mid-POST was therefore an "abandoned response".

    Both matter more than usual because the PR asks operators to alert on
    `mcp_response_abandoned` instead of the /mcp 5xx rate.
    """

    @staticmethod
    def _server_with(monkeypatch, handler):
        server = JawafdehiMCPServer()
        monkeypatch.setattr(server.session_manager, "handle_request", handler)
        return server

    class _DeadPeer:
        async def __call__(self, message):
            raise OSError("Cannot write to a closing transport")

    async def test_a_failed_write_marks_the_peer_as_gone(self):
        guard = _ResponseGuard(self._DeadPeer(), _dummy_receive)
        with pytest.raises(OSError):
            await guard({"type": "http.response.start", "status": 200, "headers": []})
        assert guard.disconnected

    async def test_a_post_whose_client_walked_away_is_not_an_error(
        self, monkeypatch, caplog
    ):
        """The json_response path, which never polls receive again."""
        import logging

        async def writes_to_a_dead_peer(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})

        server = self._server_with(monkeypatch, writes_to_a_dead_peer)
        with caplog.at_level(logging.DEBUG):
            await server._handle_http(
                _make_scope([]), _dummy_receive, self._DeadPeer()
            )

        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not errors, "a client hanging up mid-POST is not a defect"

    async def test_a_disconnect_that_surfaces_as_an_exception_is_not_an_error(
        self, monkeypatch, caplog
    ):
        import logging

        async def dies_writing(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"x", "more_body": True})

        server = self._server_with(monkeypatch, dies_writing)
        with caplog.at_level(logging.DEBUG):
            await server._handle_http(
                _make_scope([]), _dummy_receive, self._DeadPeer()
            )

        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not errors

    async def test_a_genuine_crash_is_still_an_error(self, monkeypatch, caplog):
        """The disconnect checks must not swallow a real failure."""
        import logging

        async def explodes(scope, receive, send):
            raise RuntimeError("the tool itself broke")

        server = self._server_with(monkeypatch, explodes)
        with caplog.at_level(logging.DEBUG):
            await server._handle_http(_make_scope([]), _dummy_receive, _SendRecorder())

        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1
