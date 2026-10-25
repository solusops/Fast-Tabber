"""Read-only end-to-end check through the real MCP HTTP endpoint."""

import asyncio
import json

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from fast_tabber.server import STATE


async def main():
    connection = json.loads((STATE / "connection.json").read_text(encoding="utf-8"))
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {connection['token']}"}, trust_env=False, timeout=30) as http:
        async with streamable_http_client("http://127.0.0.1:8765/mcp", http_client=http) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                tools = await client.list_tools()
                print("Advertised tools:", ", ".join(tool.name for tool in tools.tools))
                result = await client.call_tool("browser_status", {})
                if result.is_error:
                    raise RuntimeError("Chrome connection probe failed; setup is not complete")
                status = json.loads(result.content[0].text)
                tabs_result = await client.call_tool("list_tabs", {"limit": 100})
                if tabs_result.is_error:
                    raise RuntimeError("Chrome tab listing failed")
                tabs = json.loads(tabs_result.content[0].text)
                if any("?" in tab["url"] or "#" in tab["url"] for tab in tabs["tabs"]):
                    raise RuntimeError("Tab URL query or fragment escaped the privacy filter")
                print(f"Chrome connected: {status['tabCount']} tabs in {status['windowCount']} windows")
                print(f"Read {len(tabs['tabs'])} bounded tab records; all URL queries and fragments withheld")
                print("Tab states (id, index, active):", [(tab["id"], tab["index"], tab["active"]) for tab in tabs["tabs"]])


if __name__ == "__main__":
    asyncio.run(main())
