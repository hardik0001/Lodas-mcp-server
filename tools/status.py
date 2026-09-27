# tools/status.py
import json
import logging
import os
import uuid
from pathlib import Path

from fastmcp import FastMCP

log = logging.getLogger("datev_mcp.tools.status")
# Outside the project folder on purpose — see tools/commute.py for why.
TRACE_DIR = Path.home() / ".datev_mcp_traces"


def register_status_tools(mcp: FastMCP):

    @mcp.tool(
        name="datev_get_session_status",
        annotations={"readOnlyHint": True, "destructiveHint": False},
    )
    async def datev_get_session_status() -> str:
        """
        Check whether DATEV LODAS is reachable and describe the current screen.
        Cheap pre-flight check — call this before a batch of operations, or
        after an error, to confirm the session is still usable. Does not
        change anything.
        """
        from computer_use import actions

        target_window = os.environ.get("DATEV_TARGET_WINDOW_TITLE", "")
        if target_window and not actions.window_exists(target_window):
            # Deterministic check, no computer-use API call spent: if the
            # configured DATEV window isn't even open, there's nothing on
            # screen worth asking the model to describe.
            return json.dumps({
                "success": True,
                "datev_running": False,
                "current_screen": None,
                "screenshot": None,
                "error": f"no window matching {target_window!r} found",
            }, indent=2)

        from computer_use.worker import DATEVWorker

        action_id = str(uuid.uuid4())[:8]
        worker = DATEVWorker(trace_dir=TRACE_DIR)

        goal = (
            "Take a screenshot of the current screen and describe it in one "
            "short sentence: which application is shown, and if it is DATEV "
            "LODAS, which screen/section is currently open (e.g. login "
            "screen, main menu, employee list, or a specific employee "
            "record and tab). Do not click or type anything."
        )

        result = worker.run(goal=goal, action_id=action_id, tool_name="datev_get_session_status")

        return json.dumps({
            "success": result.success,
            "datev_running": result.success,
            "current_screen": result.description or None,
            "screenshot": result.final_screenshot,
            "error": result.error,
            "trace_id": result.trace_id,
        }, indent=2)
