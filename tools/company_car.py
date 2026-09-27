# tools/company_car.py
import json
import logging
import uuid
from pathlib import Path

from fastmcp import FastMCP

from validation.schemas import validate_company_car, verify_readback

log = logging.getLogger("datev_mcp.tools.company_car")
# Outside the project folder on purpose — see tools/commute.py for why.
TRACE_DIR = Path.home() / ".datev_mcp_traces"


def register_company_car_tools(mcp: FastMCP):

    @mcp.tool(
        name="datev_set_company_car",
        annotations={"readOnlyHint": False, "destructiveHint": True},
    )
    async def datev_set_company_car(
        usage_frequency: str,
        usage_end: str = "",
        private_use: bool = False,
        commute_billing: bool = False,
        distance_km: int | None = None,
        work_days_per_month: int | None = None,
        trips_per_month: int | None = None,
    ) -> str:
        """
        Set company car parameters for the currently open employee in DATEV LODAS
        (Personnel Data > Compensation > Company Car/Company Car Provision).
        Call datev_navigate_to_employee first.

        Args:
            usage_frequency: "regularly" or "occasionally"
            usage_end: End date in MM/YYYY format, e.g. "12/2026". Empty = no end date.
            private_use: Whether the car may be used privately
            commute_billing: Whether home-to-work trips are billed via this record
            distance_km: One-way commute distance in km — required if commute_billing is true
            work_days_per_month: Working days per month — required if commute_billing is true
            trips_per_month: Trips per month — required if usage_frequency is "occasionally"
        """
        validation = validate_company_car(
            usage_frequency=usage_frequency,
            usage_end=usage_end,
            private_use=private_use,
            commute_billing=commute_billing,
            distance_km=distance_km,
            work_days_per_month=work_days_per_month,
            trips_per_month=trips_per_month,
        )
        if not validation.ok:
            return json.dumps({"success": False, "error": "validation", "details": validation.to_dict()})

        from computer_use.worker import DATEVWorker

        action_id = str(uuid.uuid4())[:8]
        worker = DATEVWorker(trace_dir=TRACE_DIR)

        expected = {
            "usage_frequency": usage_frequency,
            "usage_end": usage_end,
            "private_use": private_use,
            "commute_billing": commute_billing,
            "distance_km": distance_km,
            "work_days_per_month": work_days_per_month,
            "trips_per_month": trips_per_month,
        }

        optional_lines = "".join((
            f"- Distance: {distance_km} km\n" if distance_km is not None else "",
            f"- Working days per month: {work_days_per_month}\n" if work_days_per_month is not None else "",
            f"- Trips per month (individual valuation): {trips_per_month}\n" if trips_per_month is not None else "",
        ))

        goal = (
            f"In the currently open DATEV employee record, navigate to "
            f"'Compensation' > 'Company Car/Company Car Provision' in the "
            f"left-hand tree (under Personnel Data).\n"
            f"Set the following values:\n"
            f"- Usage frequency: {usage_frequency}\n"
            f"- Usage end date: {usage_end if usage_end else 'leave empty'}\n"
            f"- Private use: {'checked' if private_use else 'unchecked'}\n"
            f"- Bill home-to-work trips: {'checked' if commute_billing else 'unchecked'}\n"
            f"{optional_lines}"
            f"Do NOT save yet. When all fields are filled, read back the values and "
            f"reply with a JSON object using exactly these keys: usage_frequency, "
            f"usage_end, private_use, commute_billing, distance_km, "
            f"work_days_per_month, trips_per_month."
        )

        result = worker.run(goal=goal, action_id=action_id, tool_name="datev_set_company_car")

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
