"""Stateless HTTP mode (GARMIN_MCP_STATELESS) for the self-hosted fork.

In the default stateful mode the MCP SDK keeps each client's session in this
process's memory. Anything that restarts the process (the gateway's idle reaper, a
deploy) silently kills those sessions: the client's next request carries a dead
Mcp-Session-Id and gets HTTP 404, and a request with no session id gets HTTP 400.
Stateless mode has no sessions to lose. No tool in this server uses a
session-dependent feature (progress, sampling, elicitation, client logging).
"""
import os
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from garmin_mcp import _create_fastmcp, _parse_stateless

HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


@pytest.fixture(autouse=True)
def _fresh_sse_exit_event():
    """sse_starlette keeps one process-wide exit Event, bound to the first event loop
    that uses it. Each TestClient runs its own loop, so reset it per test. A real
    worker has a single loop for its whole life and never hits this."""
    from sse_starlette.sse import AppStatus
    AppStatus.should_exit_event = None
    yield
    AppStatus.should_exit_event = None
STALE = "deadbeefdeadbeefdeadbeefdeadbeef"


def _call(client, body, session_id=None):
    headers = dict(HEADERS)
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return client.post("/mcp", json=body, headers=headers)


def _app(stateless: bool):
    with patch.dict(os.environ, {"GARMIN_MCP_STATELESS": "true" if stateless else "false"}):
        app = _create_fastmcp("127.0.0.1", 8000)

    @app.tool()
    async def ping() -> str:
        """Test tool."""
        return "pong"

    return app


class TestParseStateless:
    def test_default_is_stateful(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GARMIN_MCP_STATELESS", None)
            assert _parse_stateless() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes ", "on"])
    def test_truthy_values(self, value):
        with patch.dict(os.environ, {"GARMIN_MCP_STATELESS": value}):
            assert _parse_stateless() is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "banana"])
    def test_everything_else_is_stateful(self, value):
        with patch.dict(os.environ, {"GARMIN_MCP_STATELESS": value}):
            assert _parse_stateless() is False


class TestCreateFastMCP:
    def test_flag_reaches_the_sdk_settings(self):
        assert _app(stateless=True).settings.stateless_http is True
        assert _app(stateless=False).settings.stateless_http is False

    def test_host_and_port_are_passed_through(self):
        app = _create_fastmcp("127.0.0.1", 9123)
        assert (app.settings.host, app.settings.port) == ("127.0.0.1", 9123)


class TestBehaviourAgainstTheRealSdk:
    """The reason the option exists, shown end to end (no Garmin involved)."""

    TOOL_CALL = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "ping", "arguments": {}}}

    def test_stateless_tool_call_needs_no_session(self):
        with TestClient(_app(True).streamable_http_app(), base_url="http://127.0.0.1:8000") as c:
            r = _call(c, self.TOOL_CALL)
            assert r.status_code == 200
            assert "pong" in r.text
            assert "mcp-session-id" not in {k.lower() for k in r.headers}

    def test_stateless_ignores_a_stale_session_id(self):
        with TestClient(_app(True).streamable_http_app(), base_url="http://127.0.0.1:8000") as c:
            r = _call(c, self.TOOL_CALL, session_id=STALE)
            assert r.status_code == 200
            assert "pong" in r.text

    def test_stateful_control_reproduces_the_production_failures(self):
        """What production did on 2026-09-21: 400 without a session, 404 for a dead one."""
        with TestClient(_app(False).streamable_http_app(), base_url="http://127.0.0.1:8000") as c:
            assert _call(c, self.TOOL_CALL).status_code == 400
            assert _call(c, self.TOOL_CALL, session_id=STALE).status_code == 404
