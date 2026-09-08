"""Handoff MCP Server — JSON-RPC 2.0 transport over stdio.

Implements the Model Context Protocol (MCP) layer that exposes Handoff
Agent's capabilities (checkpoint management, project inspection, validation)
as resources and tools that any MCP-compatible client (LLM agent, IDE, etc.)
can consume.

This module owns only the JSON-RPC 2.0 transport. All business logic lives in
``handoff_agent.mcp.adapter``. Security enforcement (project containment,
symlink safety, secret filtering, read/write boundaries) is handled by the
adapter.

MCP Lifecycle:
  1. Client sends ``initialize`` → Server responds with capabilities.
  2. Client may send ``notifications/initialized`` (notification, no response).
  3. Client calls ``tools/list``, ``tools/call``, ``resources/list``,
     ``resources/read``, ``ping``.
  4. Server processes each request synchronously.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Callable, TextIO

from handoff_agent.mcp.adapter import HandoffMCPAdapter, MCPError

logger = logging.getLogger(__name__)

# JSON-RPC 2.0 error codes (standard)
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# MCP protocol version
MCP_PROTOCOL_VERSION = "2024-11-05"


def _jsonrpc_response(
    result: Any,
    request_id: Any,
) -> str:
    """Build a JSON-RPC 2.0 success response."""
    return json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "result": result},
        ensure_ascii=False,
    )


def _jsonrpc_error(
    code: int,
    message: str,
    request_id: Any,
    data: Any = None,
) -> str:
    """Build a JSON-RPC 2.0 error response."""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "error": error},
        ensure_ascii=False,
    )


class MCPServer:
    """JSON-RPC 2.0 MCP server over stdio.

    Usage::

        adapter = HandoffMCPAdapter(project_root="/path/to/repo")
        server = MCPServer(adapter)
        server.serve()  # blocks on stdin

    For testing, use ``handle_line`` directly.
    """

    def __init__(self, adapter: HandoffMCPAdapter) -> None:
        self._adapter = adapter
        self._initialized = False

    def serve(
        self,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
    ) -> None:
        """Run the MCP server loop, reading JSON-RPC messages from stdin."""
        inp = stdin or sys.stdin
        out = stdout or sys.stdout
        for line in inp:
            line = line.strip()
            if not line:
                continue
            response = self.handle_line(line)
            if response is not None:
                out.write(response + "\n")
                out.flush()

    def handle_line(self, line: str) -> str | None:
        """Parse a JSON-RPC message and return the response (or None for notifications)."""
        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, ValueError) as exc:
            return _jsonrpc_error(PARSE_ERROR, f"Parse error: {exc}", None)

        if not isinstance(msg, dict):
            return _jsonrpc_error(INVALID_REQUEST, "Request must be a JSON object", None)

        request_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params", {})

        if not isinstance(method, str) or not method:
            return _jsonrpc_error(
                INVALID_REQUEST, "Missing or invalid 'method'", request_id
            )

        # Notifications have no id — we don't respond
        if request_id is None and method == "notifications/initialized":
            self._initialized = True
            logger.debug("Client initialized")
            return None

        return self._dispatch(method, params, request_id)

    def _dispatch(
        self, method: str, params: dict[str, Any], request_id: Any
    ) -> str:
        """Route a method call to the appropriate handler."""
        handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "initialize": self._handle_initialize,
            "ping": self._handle_ping,
            "resources/list": self._handle_resources_list,
            "resources/read": self._handle_resources_read,
            "tools/list": self._handle_tools_list,
            "tools/call": self._handle_tools_call,
        }

        handler = handlers.get(method)
        if handler is None:
            return _jsonrpc_error(
                METHOD_NOT_FOUND, f"Unknown method: {method}", request_id
            )

        try:
            result = handler(params)
            return _jsonrpc_response(result, request_id)
        except MCPError as exc:
            return _jsonrpc_error(
                exc.code, exc.message, request_id, exc.data
            )
        except Exception as exc:
            logger.exception("Unhandled error in %s", method)
            return _jsonrpc_error(
                INTERNAL_ERROR, f"Internal error: {exc}", request_id
            )

    # ------------------------------------------------------------------
    # Method handlers
    # ------------------------------------------------------------------

    def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle the ``initialize`` request (MCP lifecycle step 1)."""
        self._initialized = True
        return {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {
                "resources": {"listChanged": False},
                "tools": {"listChanged": False},
            },
            "serverInfo": {
                "name": "handoff-mcp-server",
                "version": "0.4.0",
            },
        }

    def _handle_ping(self, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    def _handle_resources_list(self, params: dict[str, Any]) -> dict[str, Any]:
        resources = self._adapter.list_resources()
        return {"resources": resources}

    def _handle_resources_read(self, params: dict[str, Any]) -> dict[str, Any]:
        uri = params.get("uri")
        if not uri or not isinstance(uri, str):
            raise MCPError(INVALID_PARAMS, "Missing 'uri' parameter")
        contents = self._adapter.read_resource(uri)
        return {"contents": contents}

    def _handle_tools_list(self, params: dict[str, Any]) -> dict[str, Any]:
        tools = self._adapter.list_tools()
        return {"tools": tools}

    def _handle_tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not name or not isinstance(name, str):
            raise MCPError(INVALID_PARAMS, "Missing 'name' parameter")
        if not isinstance(arguments, dict):
            raise MCPError(INVALID_PARAMS, "'arguments' must be an object")
        result = self._adapter.call_tool(name, arguments)
        return result
