# tools/salary.py
import json
import logging
import uuid
from pathlib import Path

from fastmcp import FastMCP

from validation.schemas import validate_salary_details, verify_readback

log = logging.getLogger("datev_mcp.tools.salary")
# Outside the project folder on purpose — see tools/commute.py for why.
TRACE_DIR = Path.home() / ".datev_mcp_traces"


def register_salary_tools(mcp: FastMCP):

    @mcp.tool(
        name="datev_set_salary_details",
        annotations={"readOnlyHint": False, "destructiveHint": True},
    )
    async def datev_set_salary_details(
        iban: str,
        amount_eur: float,
        cycle: str = "monthly",
        valid_from: str = "",
        valid_until: str = "",
        reduce_for_partial_month: bool = False,
    ) -> str:
        """
        Set a fixed-pay entry for the currently open employee in DATEV LODAS
        (Personnel Data > Compensation > Salary).
        Call datev_navigate_to_employee first.

        Args:
            iban: IBAN the salary is paid to, e.g. "DE89 3704 0044 0532 0130 00"
            amount_eur: Amount in EUR (>= 0)
            cycle: "monthly", "one_time", "quarterly", or "yearly"
            valid_from: Start date in MM/YYYY format, e.g. "01/2026"
            valid_until: End date in MM/YYYY format. Empty = no end date.
            reduce_for_partial_month: Whether to prorate for a partial month
        """
        validation = validate_salary_details(
            iban=iban,
            amount_eur=amount_eur,
            cycle=cycle,
            valid_from=valid_from,
            valid_until=valid_until,
            reduce_for_partial_month=reduce_for_partial_month,
        )
        if not validation.ok:
            return json.dumps({"success": False, "error": "validation", "details": validation.to_dict()})

        from computer_use.worker import DATEVWorker

        action_id = str(uuid.uuid4())[:8]
        worker = DATEVWorker(trace_dir=TRACE_DIR)

        expected = {
            "iban": iban,
            "amount_eur": amount_eur,
            "cycle": cycle,
            "valid_from": valid_from,
            "valid_until": valid_until,
            "reduce_for_partial_month": reduce_for_partial_month,
        }

        goal = (
            f"In the currently open DATEV employee record, navigate to "
            f"'Compensation' > 'Salary' in the left-hand tree (under Personnel "
            f"Data).\n"
            f"Set the following values:\n"
            f"- IBAN: {iban}\n"
            f"- Amount: {amount_eur} EUR\n"
            f"- Payment cycle: {cycle}\n"
            f"- Valid from: {valid_from}\n"
            f"- Valid until: {valid_until if valid_until else 'leave empty'}\n"
            f"- Reduce for partial month: {'checked' if reduce_for_partial_month else 'unchecked'}\n"
            f"Do NOT save yet. When all fields are filled, read back the values and "
            f"reply with a JSON object using exactly these keys: iban, "
            f"amount_eur, cycle, valid_from, valid_until, reduce_for_partial_month."
        )

        result = worker.run(goal=goal, action_id=action_id, tool_name="datev_set_salary_details")

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
