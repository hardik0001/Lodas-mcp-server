# tools/commute.py
import json
import logging
import uuid
from pathlib import Path

from fastmcp import FastMCP

from validation.schemas import validate_commute, verify_readback

log = logging.getLogger("datev_mcp.tools.commute")
# Outside the project folder on purpose: mock_datev/ is served by VS Code Live
# Server (or similar), which watches its whole root recursively. Writing trace
# screenshots under datev_mcp/traces triggered a full page reload after every
# single computer-use action — wiping whatever had just been typed into the
# mock page before the next click could register.
TRACE_DIR = Path.home() / ".datev_mcp_traces"


def register_commute_tools(mcp: FastMCP):

    @mcp.tool(
        name="datev_set_commute_parameters",
        annotations={"readOnlyHint": False, "destructiveHint": True},
    )
    async def datev_set_commute_parameters(
        distance_km: int,
        work_days_per_month: int,
        usage_frequency: str = "regularly",
        usage_end: str = "",
        flat_rate_tax: bool = False,
    ) -> str:
        """
        Set commute parameters for the currently open employee in DATEV LODAS.
        Call datev_navigate_to_employee first.

        Args:
            distance_km: One-way commute distance in km (1–999)
            work_days_per_month: Working days per month (1–31; >23 is flagged as unusual)
            usage_frequency: "regularly" or "occasionally"
            usage_end: End date in MM/YYYY format, e.g. "12/2026". Empty = no end date.
            flat_rate_tax: Whether flat-rate taxation applies
        """
        validation = validate_commute(
            distance_km=distance_km,
            work_days_per_month=work_days_per_month,
            usage_frequency=usage_frequency,
            usage_end=usage_end,
            flat_rate_tax=flat_rate_tax,
        )
        if not validation.ok:
            return json.dumps({"success": False, "error": "validation", "details": validation.to_dict()})

        from computer_use.worker import DATEVWorker

        action_id = str(uuid.uuid4())[:8]
        worker = DATEVWorker(trace_dir=TRACE_DIR)

        expected = {
            "distance_km": distance_km,
            "work_days_per_month": work_days_per_month,
            "usage_frequency": usage_frequency,
            "usage_end": usage_end,
            "flat_rate_tax": flat_rate_tax,
        }

        goal = (
            f"In the currently open DATEV employee record, navigate to the "
            f"'Home/Workplace' section (in the left-hand tree under Personnel Data). "
            f"Set the following values:\n"
            f"- One-way distance: {distance_km} km\n"
            f"- Working days per month: {work_days_per_month}\n"
            f"- Usage frequency: {usage_frequency}\n"
            f"- Usage end date: {usage_end if usage_end else 'leave empty'}\n"
            f"- Flat-rate taxation: {'checked' if flat_rate_tax else 'unchecked'}\n"
            f"Do NOT save yet. When all fields are filled, read back the values and "
            f"reply with a JSON object using exactly these keys: "
            f"distance_km, work_days_per_month, usage_frequency, usage_end, flat_rate_tax."
        )

        result = worker.run(goal=goal, action_id=action_id, tool_name="datev_set_commute_parameters")

        if not result.success:
            return json.dumps({
                "success": False,
                "error": result.error,
                "screenshot": result.final_screenshot,
                "trace_id": result.trace_id,
            }, indent=2)

        verification = verify_readback(expected, result.values)

        return json.dumps({
            "success": verification.all_match,
            "values_after": result.values,
            "verification": verification.to_dict(),
            "warnings": validation.warnings,
            "screenshot": result.final_screenshot,
            "trace_id": result.trace_id,
        }, indent=2)
