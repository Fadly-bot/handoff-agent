"""Allow running as: python -m handoff_agent.mcp"""

import sys

from handoff_agent.mcp.server import MCPServer
from handoff_agent.mcp.adapter import HandoffMCPAdapter


def main() -> None:
    adapter = HandoffMCPAdapter()
    server = MCPServer(adapter)
    server.serve()


if __name__ == "__main__":
    sys.exit(main() or 0)
