"""
debug_mcp.py — full chain verification.
Run from project root: python debug_mcp.py
"""
import json
import sys
import os
import importlib.util

base = os.path.dirname(os.path.abspath(__file__))

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

load("config", f"{base}/config.py")
load("models", f"{base}/models.py")
mcp_mod = load("mcp_client", f"{base}/mcp_client.py")
MCPClient = mcp_mod.MCPClient

mcp = MCPClient()
mcp.initialize()

print("=== health_check ===")
r = mcp.health_check()
print(f"success: {r.success}")

print("\n=== list_datasets (all) ===")
r = mcp.list_datasets(page_size=100)
print(f"success: {r.success}  error: {r.error}")
if isinstance(r.data, dict):
    datasets = r.data.get("datasets", [])
    print(f"total: {len(datasets)}")
    for d in datasets:
        print(f"  id={d.get('id')}  table_name={d.get('table_name')}")
    sqllab = next((d for d in datasets if "sqllab" in d.get("table_name", "").lower()), None)
    print(f"\nsqllab_agent: {sqllab}")
    sqllab_id = sqllab["id"] if sqllab else 22
else:
    print("raw:", str(r.data)[:300])
    sqllab_id = 22

print(f"\n=== get_dataset_info({sqllab_id}) ===")
r = mcp.get_dataset_info(sqllab_id)
print(f"success: {r.success}  error: {r.error}")
print("raw data:", json.dumps(r.data, default=str)[:600])

print("\n=== execute_sql ===")
r = mcp.execute_sql(1, "SELECT state, COUNT(*) as cnt FROM sqllab_agent GROUP BY state LIMIT 3")
print(f"success: {r.success}  error: {r.error}")
print("raw data:", json.dumps(r.data, default=str)[:400])

print("\n=== generate_chart (bar) ===")
r = mcp.generate_chart({
    "dataset_id": sqllab_id,
    "chart_name": "DEBUG Test Bar",
    "save_chart": True,
    "generate_preview": False,
    "config": {
        "chart_type": "xy",
        "kind": "bar",
        "x": {"name": "state"},
        "y": [{"name": "amount", "aggregate": "SUM", "label": "SUM(amount)"}],
    }
})
print(f"success: {r.success}  error: {r.error}")
print("data:", json.dumps(r.data, default=str)[:400])

mcp.close()
print("\n✅ Done")