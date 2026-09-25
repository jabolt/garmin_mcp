"""HTTP serving for the self-hosted fork: stateless mode, both MCP protocol eras, tool-list caching.

Stateless mode (GARMIN_MCP_STATELESS): in the default stateful mode the MCP SDK keeps
each initialize-handshake client's session in this process's memory. Anything that
restarts the process (the gateway's idle reaper, a deploy) silently kills those
sessions: the client's next request carries a dead Mcp-Session-Id and gets HTTP 404,
and a request with no session id gets HTTP 400. Stateless mode has no sessions to lose.
No tool in this server uses a session-dependent feature.

Protocol eras: since 2026-09 Claude's platform speaks MCP 2026-07-28 first, opening
with `server/discover`. The mcp 1.x SDK answered that with a JSON-RPC error inside
HTTP 200, which a 2026-07-28 client neither accepts nor treats as "fall back to
initialize", so it never re-listed tools and newly deployed tools stayed invisible.
mcp 2.x serves 2026-07-28 natively and still serves initialize-handshake clients.

Tool-list caching: 2026-07-28 `tools/list` results carry `ttlMs`, telling the client how
long its copy stays fresh (GARMIN_MCP_TOOLS_LIST_TTL seconds, default one hour).
"""
import json
import os
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from garmin_mcp import _ToolFilter, _create_server, _parse_stateless, _parse_tools_list_ttl, _run_options

MODERN = "2026-07-28"
LEGACY = "2025-06-18"
BASE_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
STALE = "deadbeefdeadbeefdeadbeefdeadbeef"


@pytest.fixture(autouse=True)
def _fresh_sse_exit_event():
    """sse_starlette keeps one process-wide exit Event, bound to the first event loop
    that uses it. Each TestClient runs its own loop, so reset it per test. A real
    worker has a single loop for its whole life and never hits this."""
    from sse_starlette.sse import AppStatus
    AppStatus.should_exit_event = None
    yield
    AppStatus.should_exit_event = None


def _server(ttl_seconds=None):
    env = {} if ttl_seconds is None else {"GARMIN_MCP_TOOLS_LIST_TTL": str(ttl_seconds)}
    with patch.dict(os.environ, env):
        server = _create_server()

    @server.tool()
    async def ping() -> str:
        """Test tool."""
        return "pong"

    return server


def _client(stateless: bool, ttl_seconds=None) -> TestClient:
    app = _server(ttl_seconds).streamable_http_app(stateless_http=stateless, host="127.0.0.1")
    return TestClient(app, base_url="http://127.0.0.1:8000")


def _payload(response):
    """The JSON-RPC message in a JSON or single-event SSE response."""
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        data = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
        return json.loads(data[-1])
    return response.json()


def _legacy(client, method, params=None, session_id=None, id_=1):
    headers = dict(BASE_HEADERS)
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    body = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}
    return client.post("/mcp", json=body, headers=headers)


def _modern(client, method, params=None, name=None, id_=1):
    headers = dict(BASE_HEADERS, **{"MCP-Protocol-Version": MODERN, "Mcp-Method": method})
    if name:
        headers["Mcp-Name"] = name
    params = dict(params or {})
    params["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": MODERN,
        "io.modelcontextprotocol/clientInfo": {"name": "test-client", "version": "1.0"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    body = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params}
    return client.post("/mcp", json=body, headers=headers)


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


class TestParseToolsListTtl:
    def test_default_is_one_hour(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GARMIN_MCP_TOOLS_LIST_TTL", None)
            assert _parse_tools_list_ttl() == 3600

    def test_override(self):
        with patch.dict(os.environ, {"GARMIN_MCP_TOOLS_LIST_TTL": " 300 "}):
            assert _parse_tools_list_ttl() == 300

    @pytest.mark.parametrize("value", ["-1", "soon", "1.5"])
    def test_rejects_anything_but_a_non_negative_integer(self, value):
        with patch.dict(os.environ, {"GARMIN_MCP_TOOLS_LIST_TTL": value}):
            with pytest.raises(ValueError, match="GARMIN_MCP_TOOLS_LIST_TTL"):
                _parse_tools_list_ttl()


class TestRunOptions:
    def test_streamable_http_gets_host_port_and_the_stateless_flag(self):
        with patch.dict(os.environ, {"GARMIN_MCP_STATELESS": "true"}):
            assert _run_options("streamable-http", "127.0.0.1", 9123) == {
                "host": "127.0.0.1", "port": 9123, "stateless_http": True,
            }
        with patch.dict(os.environ, {"GARMIN_MCP_STATELESS": "false"}):
            assert _run_options("streamable-http", "127.0.0.1", 9123)["stateless_http"] is False

    def test_sse_gets_host_and_port_only(self):
        assert _run_options("sse", "127.0.0.1", 9123) == {"host": "127.0.0.1", "port": 9123}

    def test_stdio_takes_no_options(self):
        assert _run_options("stdio", "127.0.0.1", 9123) == {}


class TestModernProtocol:
    """What Claude's platform sends first since the 2026-07-28 spec."""

    def test_server_discover_is_answered(self):
        with _client(stateless=True) as c:
            r = _modern(c, "server/discover")
            assert r.status_code == 200
            result = _payload(r)["result"]
            assert MODERN in result["supportedVersions"]
            assert "tools" in result["capabilities"]

    def test_tools_list_carries_the_cache_hint(self):
        with _client(stateless=True) as c:
            result = _payload(_modern(c, "tools/list"))["result"]
            assert [t["name"] for t in result["tools"]] == ["ping"]
            assert result["ttlMs"] == 3_600_000

    def test_cache_hint_follows_the_setting(self):
        with _client(stateless=True, ttl_seconds=60) as c:
            assert _payload(_modern(c, "tools/list"))["result"]["ttlMs"] == 60_000

    def test_tool_call(self):
        with _client(stateless=True) as c:
            r = _modern(c, "tools/call", {"name": "ping", "arguments": {}}, name="ping")
            assert r.status_code == 200
            assert _payload(r)["result"]["content"][0]["text"] == "pong"


class TestLegacyClients:
    """Initialize-handshake clients (e.g. Claude Code, Claude Desktop) keep working."""

    def test_handshake_then_tools_list(self):
        with _client(stateless=True) as c:
            init = _legacy(c, "initialize", {
                "protocolVersion": LEGACY, "capabilities": {},
                "clientInfo": {"name": "legacy-client", "version": "1.0"},
            })
            assert init.status_code == 200
            assert _payload(init)["result"]["protocolVersion"] == LEGACY
            tools = _payload(_legacy(c, "tools/list", id_=2))["result"]["tools"]
            assert [t["name"] for t in tools] == ["ping"]

    def test_stateless_tool_call_needs_no_session(self):
        with _client(stateless=True) as c:
            r = _legacy(c, "tools/call", {"name": "ping", "arguments": {}})
            assert r.status_code == 200
            assert "pong" in r.text
            assert "mcp-session-id" not in {k.lower() for k in r.headers}

    def test_stateless_ignores_a_stale_session_id(self):
        with _client(stateless=True) as c:
            r = _legacy(c, "tools/call", {"name": "ping", "arguments": {}}, session_id=STALE)
            assert r.status_code == 200
            assert "pong" in r.text

    def test_stateful_control_reproduces_the_production_failures(self):
        """What production did on 2026-09-21: 400 without a session, 404 for a dead one."""
        with _client(stateless=False) as c:
            assert _legacy(c, "tools/call", {"name": "ping", "arguments": {}}).status_code == 400
            assert _legacy(c, "tools/call", {"name": "ping", "arguments": {}},
                           session_id=STALE).status_code == 404


class TestToolResultsAreSentOnce:
    """Tools return json.dumps(...) text; the SDK must not repeat it as structuredContent (#331)."""

    def test_no_output_schema_and_no_structured_content(self):
        server = _create_server()

        @_ToolFilter(server, set(), set()).tool()
        async def get_thing() -> str:
            """Test tool returning JSON text, like every Garmin tool."""
            return json.dumps({"a": 1})

        app = server.streamable_http_app(stateless_http=True, host="127.0.0.1")
        with TestClient(app, base_url="http://127.0.0.1:8000") as c:
            tools = _payload(_modern(c, "tools/list"))["result"]["tools"]
            assert "outputSchema" not in tools[0]
            result = _payload(_modern(c, "tools/call", {"name": "get_thing", "arguments": {}},
                                      name="get_thing"))["result"]
            assert result["content"][0]["text"] == '{"a": 1}'
            assert "structuredContent" not in result
