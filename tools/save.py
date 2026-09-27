# tools/save.py
import json
import logging
import uuid
from pathlib import Path

from fastmcp import FastMCP

log = logging.getLogger("datev_mcp.tools.save")
# Outside the project folder on purpose — see tools/commute.py for why.
TRACE_DIR = Path.home() / ".datev_mcp_traces"


def register_save_tools(mcp: FastMCP):

    @mcp.tool(
        name="datev_save_and_confirm",
        annotations={"readOnlyHint": False, "destructiveHint": True},
    )
    async def datev_save_and_confirm() -> str:
        """
        Save the currently open DATEV form (clicks 'Speichern'/Save) and confirm
        any resulting dialog. The data-entry tools deliberately leave fields
        unsaved so multiple sections can be filled and read-back-verified first;
        call this once you're ready to actually commit those changes.
        """
        from computer_use.worker import DATEVWorker

        action_id = str(uuid.uuid4())[:8]
        worker = DATEVWorker(trace_dir=TRACE_DIR)

        goal = (
            "In the currently open DATEV form, click the 'Save' (Speichern) "
            "button to commit the changes just made. If a confirmation dialog "
            "or error dialog appears, read its full text, then dismiss it by "
            "clicking its 'OK' (or equivalent) button. Reply with a JSON "
            "object with exactly these keys: confirmation_dialog (the dialog's "
            "text, or null if no dialog appeared) and error_dialog (true if "
            "the dialog indicated a problem rather than a normal save "
            "confirmation, false otherwise)."
        )

        result = worker.run(goal=goal, action_id=action_id, tool_name="datev_save_and_confirm")

        if not result.success:
            return json.dumps({
                "success": False,
                "error": result.error,
                "screenshot": result.final_screenshot,
                "trace_id": result.trace_id,
            }, indent=2)

        values = result.values or {}
        error_dialog = bool(values.get("error_dialog"))

        return json.dumps({
            "success": not error_dialog,
            "confirmation_dialog": values.get("confirmation_dialog"),
            "screenshot": result.final_screenshot,
            "trace_id": result.trace_id,
        }, indent=2)
