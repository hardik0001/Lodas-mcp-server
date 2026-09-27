# tools/navigate.py
import json
import logging
import uuid
from pathlib import Path

from fastmcp import FastMCP

log = logging.getLogger("datev_mcp.tools.navigate")
# Outside the project folder on purpose — see tools/commute.py for why.
TRACE_DIR = Path.home() / ".datev_mcp_traces"


def register_navigate_tools(mcp: FastMCP):

    @mcp.tool(
        name="datev_navigate_to_employee",
        annotations={"readOnlyHint": False, "destructiveHint": False},
    )
    async def datev_navigate_to_employee(personnel_number: str) -> str:
        """
        Navigate to an employee's record in DATEV LODAS by personnel number.
        Call this before any data-entry or read tool.

        Args:
            personnel_number: The employee's personnel number in DATEV.
        """ 
        if not personnel_number.strip():
            return json.dumps({
                "success": False,
                "error": "personnel_number cannot be empty",
            })

        # Lazy import — worker not needed until tool is actually called
        from computer_use.worker import DATEVWorker

        action_id = str(uuid.uuid4())[:8]
        worker = DATEVWorker(trace_dir=TRACE_DIR)

        goal = (
            f"Navigate to employee with personnel number {personnel_number} in DATEV LODAS. "
            f"Use the employee search or navigation to open that employee's record. "
            f"When the employee record is open, stop and describe the current screen."
        )

        result = worker.run(goal=goal, action_id=action_id, tool_name="datev_navigate_to_employee")

        return json.dumps({
            "success": result.success,
            "description": result.description,
            "error": result.error,
            "trace_id": result.trace_id,
        })
