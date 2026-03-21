"""Tests for MessageRouter — dispatch, middleware, handlers."""

import pytest
from unittest.mock import MagicMock, patch

from router import MessageRouter, RouterContext


class TestHandlerRegistration:
    """Test handler registration and lookup."""

    def test_register_with_decorator(self):
        router = MessageRouter()

        @router.handler("ping")
        def handle_ping(msg, ctx):
            return {"type": "pong", "payload": {}}

        assert "ping" in router.registered_types

    def test_register_with_method(self):
        router = MessageRouter()

        def handle_ping(msg, ctx):
            pass

        router.register("ping", handle_ping)
        assert "ping" in router.registered_types

    def test_fallback_handler(self):
        router = MessageRouter()
        called = {"value": False}

        def fallback(msg, ctx):
            called["value"] = True

        router.set_fallback(fallback)

        ctx = MagicMock(spec=RouterContext)
        router.dispatch({"type": "unknown_type"}, ctx)
        assert called["value"] is True


class TestDispatch:
    """Test message dispatching."""

    def test_dispatch_to_handler(self):
        router = MessageRouter()
        results = []

        @router.handler("echo")
        def handle_echo(msg, ctx):
            results.append(msg["payload"])
            return None

        ctx = MagicMock(spec=RouterContext)
        router.dispatch(
            {"type": "echo", "payload": {"text": "hello"}, "sender_id": "p", "correlation_id": "c"},
            ctx
        )
        assert results == [{"text": "hello"}]
        assert router.stats["handled"] == 1

    def test_unhandled_message(self):
        router = MessageRouter()
        ctx = MagicMock(spec=RouterContext)
        router.dispatch(
            {"type": "unknown", "sender_id": "p", "correlation_id": "c"},
            ctx
        )
        assert router.stats["unhandled"] == 1

    def test_auto_reply(self):
        router = MessageRouter()

        @router.handler("ping")
        def handle_ping(msg, ctx):
            return {"type": "pong", "payload": {"message": "pong!"}}

        ctx = MagicMock(spec=RouterContext)
        ctx.reply.return_value = {"status": "ok"}

        router.dispatch(
            {"type": "ping", "payload": {}, "sender_id": "p",
             "correlation_id": "c", "_xmtp_sender_wallet": "0x123"},
            ctx
        )
        ctx.reply.assert_called_once()

    def test_handler_exception_sends_error(self):
        router = MessageRouter()

        @router.handler("bad")
        def handle_bad(msg, ctx):
            raise ValueError("something broke")

        ctx = MagicMock(spec=RouterContext)
        router.dispatch(
            {"type": "bad", "sender_id": "p", "correlation_id": "c"},
            ctx
        )
        assert router.stats["errors"] == 1
        # Should try to send error response
        ctx.reply.assert_called_once()
        call_args = ctx.reply.call_args
        assert call_args[0][1] == "error"  # msg_type = "error"


class TestMiddleware:
    """Test middleware chain."""

    def test_middleware_blocks_message(self):
        router = MessageRouter()
        handled = {"value": False}

        def block_all(msg, ctx):
            return False  # Block

        router.add_middleware(block_all)

        @router.handler("ping")
        def handle_ping(msg, ctx):
            handled["value"] = True

        ctx = MagicMock(spec=RouterContext)
        router.dispatch(
            {"type": "ping", "sender_id": "p", "correlation_id": "c"},
            ctx
        )
        assert handled["value"] is False  # Handler not called

    def test_middleware_allows_message(self):
        router = MessageRouter()
        handled = {"value": False}

        def allow_all(msg, ctx):
            return True

        router.add_middleware(allow_all)

        @router.handler("ping")
        def handle_ping(msg, ctx):
            handled["value"] = True

        ctx = MagicMock(spec=RouterContext)
        router.dispatch(
            {"type": "ping", "sender_id": "p", "correlation_id": "c"},
            ctx
        )
        assert handled["value"] is True

    def test_middleware_chain_order(self):
        router = MessageRouter()
        order = []

        def mw1(msg, ctx):
            order.append("mw1")
            return True

        def mw2(msg, ctx):
            order.append("mw2")
            return True

        router.add_middleware(mw1)
        router.add_middleware(mw2)

        @router.handler("ping")
        def handle_ping(msg, ctx):
            order.append("handler")

        ctx = MagicMock(spec=RouterContext)
        router.dispatch(
            {"type": "ping", "sender_id": "p", "correlation_id": "c"},
            ctx
        )
        assert order == ["mw1", "mw2", "handler"]

    def test_middleware_exception_doesnt_crash(self):
        router = MessageRouter()

        def bad_mw(msg, ctx):
            raise RuntimeError("middleware error")

        router.add_middleware(bad_mw)

        @router.handler("ping")
        def handle_ping(msg, ctx):
            pass

        ctx = MagicMock(spec=RouterContext)
        # Should not raise
        router.dispatch(
            {"type": "ping", "sender_id": "p", "correlation_id": "c"},
            ctx
        )


class TestStats:
    """Test router statistics."""

    def test_stats_tracking(self):
        router = MessageRouter()

        @router.handler("ping")
        def handle_ping(msg, ctx):
            pass

        ctx = MagicMock(spec=RouterContext)
        router.dispatch({"type": "ping", "sender_id": "p", "correlation_id": "c"}, ctx)
        router.dispatch({"type": "unknown", "sender_id": "p", "correlation_id": "c"}, ctx)

        assert router.stats["dispatched"] == 2
        assert router.stats["handled"] == 1
        assert router.stats["unhandled"] == 1
