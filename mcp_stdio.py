#!/usr/bin/env python3
"""Standalone MCP stdio server for Claude Desktop.

Claude Desktop launches this as a subprocess and communicates via stdin/stdout.
This script imports the MCP server from the app and runs it with stdio transport.

Usage in claude_desktop_config.json:
{
  "mcpServers": {
    "pggather": {
      "command": "/path/to/PgGatherApp/.venv/bin/python",
      "args": ["/path/to/PgGatherApp/mcp_stdio.py"]
    }
  }
}
"""

import sys
from pathlib import Path

# Add the project root to sys.path so app modules can be imported
project_root = Path(__file__).resolve().parent
sys.path.insert(0, str(project_root))

# Initialize the database before MCP tools can access it
from app.db import init_db
init_db()

# Import and run the MCP server with stdio transport
from app.mcp_server import mcp

if __name__ == "__main__":
    mcp.run(transport="stdio")
