"""
Dataset Agent — Phase 2: Schema discovery via MCP.
Discovers datasets, matches them to the plan, and extracts column schemas.
"""
 
import logging
from typing import Optional
 
from mcp_client import MCPClient
from models import AgentResult, DatasetSchema
 
log = logging.getLogger(__name__)
 
 
class DatasetAgent:
    """Discovers and validates datasets via MCP tools."""
 
    def __init__(self, mcp: MCPClient):
        self.mcp = mcp
 
    def discover(self, dataset_names: list[str]) -> AgentResult:
        """
        Find datasets matching the plan's requested names.
        Returns AgentResult with a list of DatasetSchema objects.
        """
        # Step 1: List all datasets from Superset via MCP
        list_result = self.mcp.list_datasets()
        if not list_result.success:
            return AgentResult.fail(
                f"Failed to list datasets: {list_result.error}"
            )
 
        datasets_raw = list_result.data
 
        # The Superset MCP returns {"datasets": [...], "count": N}
        # Fall back to other known shapes just in case
        if isinstance(datasets_raw, dict):
            datasets_list = (
                datasets_raw.get("datasets")
                or datasets_raw.get("result")
                or datasets_raw.get("data")
                or []
            )
        elif isinstance(datasets_raw, list):
            datasets_list = datasets_raw
        else:
            datasets_list = []
 
        log.info("Found %d datasets in Superset", len(datasets_list))
 
        # Step 2: Match requested names against available datasets
        matched_schemas = []
        for requested_name in dataset_names:
            schema = self._find_dataset(requested_name, datasets_list)
            if schema:
                # Step 3: Get full column info for each matched dataset
                enriched = self._enrich_schema(schema)
                if enriched:
                    matched_schemas.append(enriched)
                    log.info("Matched dataset '%s' → id=%d, %d columns",
                             enriched.name, enriched.id, len(enriched.columns))
                else:
                    log.warning("Found dataset '%s' but couldn't get column info", requested_name)
            else:
                log.warning("No dataset matching '%s' found", requested_name)
 
        if not matched_schemas:
            available = [d.get("table_name", d.get("name", "?")) for d in datasets_list[:10]]
            return AgentResult.fail(
                f"No datasets found matching: {dataset_names}. "
                f"Available: {available}"
            )
 
        return AgentResult.ok(matched_schemas)
 
    def _find_dataset(self, name: str, datasets: list[dict]) -> Optional[DatasetSchema]:
        """
        Fuzzy-match a dataset name against the available list.
        The Superset MCP list_datasets response has:
          {"id": 22, "table_name": "sqllab_agent", "database_name": "examples", ...}
        There is no "name" field — only "table_name".
        """
        name_lower = name.lower().strip()
 
        # Exact match on table_name
        for ds in datasets:
            table_name = str(ds.get("table_name", "")).lower()
            if name_lower == table_name:
                return DatasetSchema(
                    id=ds["id"],
                    name=ds["table_name"],
                    database_id=0,  # filled in by _enrich_schema
                    table_name=ds["table_name"],
                )
 
        # Partial match fallback
        for ds in datasets:
            table_name = str(ds.get("table_name", "")).lower()
            if name_lower in table_name or table_name in name_lower:
                return DatasetSchema(
                    id=ds["id"],
                    name=ds["table_name"],
                    database_id=0,
                    table_name=ds["table_name"],
                )
 
        return None
 
    def _enrich_schema(self, schema: DatasetSchema) -> Optional[DatasetSchema]:
        """Fetch full column info for a dataset via get_dataset_info."""
        info_result = self.mcp.get_dataset_info(schema.id)
        if not info_result.success:
            log.error("get_dataset_info failed for id=%d: %s", schema.id, info_result.error)
            return schema  # Return partial schema rather than failing
 
        data = info_result.data
 
        # get_dataset_info returns the dataset dict directly or nested under "result"
        if isinstance(data, dict):
            result = data.get("result", data)
        else:
            result = {}
 
        # Extract columns — MCP returns them as a list of column dicts
        columns = {}
        for col in result.get("columns", []):
            col_name = col.get("column_name") or col.get("name", "")
            col_type = col.get("type") or col.get("type_generic", "UNKNOWN")
            if col_name:
                columns[col_name] = str(col_type)
 
        schema.columns = columns
 
        # Extract database_id from the nested database object
        db_info = result.get("database", {})
        if isinstance(db_info, dict) and db_info.get("id"):
            schema.database_id = int(db_info["id"])
        elif result.get("database_id"):
            schema.database_id = int(result["database_id"])
 
        # Refresh table_name from the full response
        if result.get("table_name"):
            schema.table_name = result["table_name"]
 
        # Warn if timestamp is stored as VARCHAR — temporal charts need CAST
        ts_type = columns.get("timestamp", "")
        if ts_type and "VARCHAR" in ts_type.upper():
            log.warning(
                "⚠ 'timestamp' column is stored as %s — temporal charts may need casting",
                ts_type,
            )
 
        log.info(
            "Enriched schema for '%s': database_id=%d, %d columns",
            schema.name, schema.database_id, len(columns),
        )
        return schema