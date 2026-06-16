import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_client import MCPClient

mcp = MCPClient()
mcp.initialize()

r = mcp.call_tool("search_tools", {"query": "generate_chart create chart"})
if isinstance(r.data, list):
    for t in r.data:
        if t.get("name") == "generate_chart":
            print(json.dumps(t.get("inputSchema", {}), indent=2))
            break
    else:
        print("not found in results, showing all:")
        for t in r.data:
            print(" -", t.get("name"))
mcp.close()