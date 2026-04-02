"""CoWorker MCP Server — expose agent skills as MCP tools.

This allows Claude Code, Cursor, and other MCP-compatible clients
to directly call CoWorker agent skills via the standard MCP protocol.

Usage:
    coworker mcp serve              # Start MCP server (stdio)
    coworker mcp serve --http 8080  # Start MCP server (HTTP)

The agent's skills are automatically converted to MCP tools.
Skill implementation stays private (Skill-as-API).

Protocol: JSON-RPC 2.0 over stdio (default) or HTTP.
Spec: https://modelcontextprotocol.io/specification
"""

import json
import sys
import logging
from typing import Dict, List, Optional

logger = logging.getLogger("coworker.mcp")


class MCPServer:
    """Minimal MCP server that exposes CoWorker skills as tools."""

    def __init__(self, agent=None, skills: List[dict] = None,
                 skill_executor=None):
        """
        Args:
            agent: A CoWorker Agent instance (if running locally).
            skills: Pre-loaded skill dicts (if agent not available).
            skill_executor: Function(skill_name, input_data) -> result dict.
        """
        self._agent = agent
        self._skills = skills or []
        self._executor = skill_executor
        self._server_info = {
            "name": "coworker-mcp",
            "version": "0.6.0",
        }

        # If agent provided, extract skills
        if agent and not skills:
            self._skills = agent.executor.list_skills()

        if agent and not skill_executor:
            self._executor = lambda name, inp: agent.executor.execute(name, inp)

    def _skill_to_mcp_tool(self, skill: dict) -> dict:
        """Convert a CoWorker skill dict to MCP tool format."""
        # Build JSON Schema for input
        properties = {}
        required = []
        for param_name, param_type in skill.get("input_schema", {}).items():
            json_type = "string"  # default
            if param_type in ("int", "integer"):
                json_type = "integer"
            elif param_type in ("float", "number"):
                json_type = "number"
            elif param_type in ("bool", "boolean"):
                json_type = "boolean"
            elif param_type in ("list", "array"):
                json_type = "array"
            elif param_type in ("dict", "object"):
                json_type = "object"
            properties[param_name] = {"type": json_type}
            required.append(param_name)

        tool = {
            "name": skill["name"],
            "description": skill.get("description", ""),
            "inputSchema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        }

        # Add when_to_use to description if present
        wtu = skill.get("when_to_use", "")
        if wtu:
            tool["description"] += f"\n\nWhen to use: {wtu}"

        return tool

    def handle_request(self, request: dict) -> dict:
        """Handle a single JSON-RPC 2.0 request."""
        method = request.get("method", "")
        req_id = request.get("id")
        params = request.get("params", {})

        if method == "initialize":
            return self._respond(req_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {"listChanged": False},
                },
                "serverInfo": self._server_info,
            })

        elif method == "tools/list":
            tools = [self._skill_to_mcp_tool(s) for s in self._skills]
            return self._respond(req_id, {"tools": tools})

        elif method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            return self._handle_tool_call(req_id, tool_name, arguments)

        elif method == "notifications/initialized":
            # Client notification, no response needed
            return None

        elif method == "ping":
            return self._respond(req_id, {})

        else:
            return self._error(req_id, -32601, f"Method not found: {method}")

    def _handle_tool_call(self, req_id, tool_name: str, arguments: dict) -> dict:
        """Execute a skill and return MCP tool result."""
        if not self._executor:
            return self._error(req_id, -32603, "No skill executor configured")

        # Find skill
        skill_exists = any(s["name"] == tool_name for s in self._skills)
        if not skill_exists:
            return self._error(req_id, -32602, f"Unknown tool: {tool_name}")

        try:
            result = self._executor(tool_name, arguments)

            if result.get("success"):
                content = json.dumps(result.get("result", {}), ensure_ascii=False, default=str)
                return self._respond(req_id, {
                    "content": [{"type": "text", "text": content}],
                    "isError": False,
                })
            else:
                return self._respond(req_id, {
                    "content": [{"type": "text", "text": result.get("error", "Unknown error")}],
                    "isError": True,
                })
        except Exception as e:
            return self._respond(req_id, {
                "content": [{"type": "text", "text": str(e)}],
                "isError": True,
            })

    def _respond(self, req_id, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def _error(self, req_id, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    def serve_stdio(self):
        """Run MCP server over stdio (standard MCP transport)."""
        logger.info("MCP server starting on stdio...")
        print(f"CoWorker MCP Server v{self._server_info['version']}", file=sys.stderr)
        print(f"Tools: {len(self._skills)} skills exposed", file=sys.stderr)

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
                response = self.handle_request(request)
                if response is not None:
                    sys.stdout.write(json.dumps(response) + "\n")
                    sys.stdout.flush()
            except json.JSONDecodeError:
                err = self._error(None, -32700, "Parse error")
                sys.stdout.write(json.dumps(err) + "\n")
                sys.stdout.flush()
            except Exception as e:
                logger.error("MCP error: %s", e)
                err = self._error(None, -32603, str(e))
                sys.stdout.write(json.dumps(err) + "\n")
                sys.stdout.flush()

    def serve_http(self, port: int = 8080):
        """Run MCP server over HTTP."""
        from http.server import HTTPServer, BaseHTTPRequestHandler

        server_ref = self

        class MCPHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else b""
                try:
                    request = json.loads(body)
                    response = server_ref.handle_request(request)
                    if response:
                        resp_body = json.dumps(response).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(resp_body)))
                        self.end_headers()
                        self.wfile.write(resp_body)
                    else:
                        self.send_response(204)
                        self.end_headers()
                except Exception as e:
                    err = json.dumps(server_ref._error(None, -32603, str(e))).encode()
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(err)

            def log_message(self, fmt, *args):
                pass

        httpd = HTTPServer(("127.0.0.1", port), MCPHandler)
        print(f"CoWorker MCP Server on http://127.0.0.1:{port}")
        print(f"Tools: {len(self._skills)} skills")
        httpd.serve_forever()
