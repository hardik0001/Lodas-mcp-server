# tools/read_values.py
import json
import logging
import uuid
from pathlib import Path

from fastmcp import FastMCP

log = logging.getLogger("datev_mcp.tools.read")
# Outside the project folder on purpose — see tools/commute.py for why.
TRACE_DIR = Path.home() / ".datev_mcp_traces"

VALID_SECTIONS = {"commute", "travel_subsidy", "company_car", "all"}


def register_read_tools(mcp: FastMCP):

    @mcp.tool(
        name="datev_read_current_values",
        annotations={"readOnlyHint": True, "destructiveHint": False},
    )
    async def datev_read_current_values(section: str = "all") -> str:
        """
        Read current field values from the open DATEV form. Does not change anything.

        Args:
            section: "commute", "travel_subsidy", "company_car", or "all"
        """
        if section not in VALID_SECTIONS:
            return json.dumps({
                "success": False,
                "error": f"Invalid section '{section}'. Must be one of: {sorted(VALID_SECTIONS)}",
            })

        from computer_use.worker import DATEVWorker

        action_id = str(uuid.uuid4())[:8]
        worker = DATEVWorker(trace_dir=TRACE_DIR)

        goal = (
            f"Read all visible field values from the '{section}' section of the "
            f"currently open DATEV form. "
            f"Reply with a single JSON object with field names as keys and their "
            f"current values as values. "
            f"Do not click anything. Do not change any values."
        )

        result = worker.run(goal=goal, action_id=action_id, tool_name="datev_read_current_values")

        return json.dumps({
            "success": result.success,
            "section": section,
            "values": result.values,
            "screenshot": result.final_screenshot,
            "error": result.error,
            "trace_id": result.trace_id,
        }, indent=2)
