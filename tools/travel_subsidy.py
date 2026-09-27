# tools/travel_subsidy.py
import json
import logging
import uuid
from pathlib import Path

from fastmcp import FastMCP

from validation.schemas import validate_travel_subsidy, verify_readback

log = logging.getLogger("datev_mcp.tools.travel_subsidy")
# Outside the project folder on purpose — see tools/commute.py for why.
TRACE_DIR = Path.home() / ".datev_mcp_traces"


def register_travel_subsidy_tools(mcp: FastMCP):

    @mcp.tool(
        name="datev_set_travel_subsidy",
        annotations={"readOnlyHint": False, "destructiveHint": True},
    )
    async def datev_set_travel_subsidy(
        cash_allowance_eur: float = 0,
        public_transport_tax_free: bool = False,
        public_transport_flat_rate_cash_allowance: bool = False,
        partial_month_rule: str = "Reduce partial month/Reduce non-working month",
        job_ticket_value_eur: float = 0,
        tax_treatment: str = "benefit_in_kind",
        benefit_in_kind_exemption_exhausted: bool = False,
        public_transport_flat_rate_job_ticket: bool = False,
        flat_rate_taxation_method: str = "Distance allowance",
    ) -> str:
        """
        Set the commute allowance for the currently open employee in DATEV LODAS
        (Personnel Data > Compensation > Commute Allowance).
        Call datev_navigate_to_employee first.

        Args:
            cash_allowance_eur: Commute Allowance (Cash Benefit) amount in EUR
            public_transport_tax_free: "For trips on public transport (tax-free from 01/2019)"
            public_transport_flat_rate_cash_allowance: "For trips on public transport, 25% flat rate (from 01/2020)" under Cash Benefit
            partial_month_rule: "Reduction for partial month/non-working month" dropdown value
            job_ticket_value_eur: Job ticket (value of the non-cash benefit) in EUR
            tax_treatment: "benefit_in_kind" or "employer_discount" — "Tax exemption under"
            benefit_in_kind_exemption_exhausted: "Benefit-in-kind exemption threshold exhausted/not applicable"
            public_transport_flat_rate_job_ticket: "For trips on public transport, 25% flat rate (from 01/2020)" under Job Ticket/Benefit in Kind
            flat_rate_taxation_method: "Flat-rate taxation method" dropdown value
        """
        validation = validate_travel_subsidy(
            cash_allowance_eur=cash_allowance_eur,
            public_transport_tax_free=public_transport_tax_free,
            public_transport_flat_rate_cash_allowance=public_transport_flat_rate_cash_allowance,
            partial_month_rule=partial_month_rule,
            job_ticket_value_eur=job_ticket_value_eur,
            tax_treatment=tax_treatment,
            benefit_in_kind_exemption_exhausted=benefit_in_kind_exemption_exhausted,
            public_transport_flat_rate_job_ticket=public_transport_flat_rate_job_ticket,
            flat_rate_taxation_method=flat_rate_taxation_method,
        )
        if not validation.ok:
            return json.dumps({"success": False, "error": "validation", "details": validation.to_dict()})

        from computer_use.worker import DATEVWorker

        action_id = str(uuid.uuid4())[:8]
        worker = DATEVWorker(trace_dir=TRACE_DIR)

        expected = {
            "cash_allowance_eur": cash_allowance_eur,
            "public_transport_tax_free": public_transport_tax_free,
            "public_transport_flat_rate_cash_allowance": public_transport_flat_rate_cash_allowance,
            "partial_month_rule": partial_month_rule,
            "job_ticket_value_eur": job_ticket_value_eur,
            "tax_treatment": tax_treatment,
            "benefit_in_kind_exemption_exhausted": benefit_in_kind_exemption_exhausted,
            "public_transport_flat_rate_job_ticket": public_transport_flat_rate_job_ticket,
            "flat_rate_taxation_method": flat_rate_taxation_method,
        }

        tax_treatment_label = "Benefit in kind" if tax_treatment == "benefit_in_kind" else "Employer discount"

        goal = (
            f"In the currently open DATEV employee record, navigate to "
            f"'Compensation' > 'Commute Allowance' in the left-hand tree (under "
            f"Personnel Data).\n"
            f"In the 'Commute Allowance (Cash Benefit)' section set:\n"
            f"- Commute allowance: {cash_allowance_eur} EUR\n"
            f"- 'For trips on public transport (tax-free from 01/2019)': "
            f"{'checked' if public_transport_tax_free else 'unchecked'}\n"
            f"- 'For trips on public transport, 25% flat rate (from 01/2020)': "
            f"{'checked' if public_transport_flat_rate_cash_allowance else 'unchecked'}\n"
            f"- 'Reduction for partial month/non-working month': {partial_month_rule}\n"
            f"In the 'Job Ticket (Benefit in Kind)' section set:\n"
            f"- Job ticket (value of the non-cash benefit): {job_ticket_value_eur} EUR\n"
            f"- 'Tax exemption under': {tax_treatment_label}\n"
            f"- 'Benefit-in-kind exemption threshold exhausted/not applicable': "
            f"{'checked' if benefit_in_kind_exemption_exhausted else 'unchecked'}\n"
            f"- 'For trips on public transport, 25% flat rate (from 01/2020)': "
            f"{'checked' if public_transport_flat_rate_job_ticket else 'unchecked'}\n"
            f"In the 'Flat-Rate Taxation Details' section set:\n"
            f"- 'Flat-rate taxation method': {flat_rate_taxation_method}\n"
            f"Do NOT save yet. When all fields are filled, read back the values and "
            f"reply with a JSON object using exactly these keys: cash_allowance_eur, "
            f"public_transport_tax_free, public_transport_flat_rate_cash_allowance, "
            f"partial_month_rule, job_ticket_value_eur, tax_treatment, "
            f"benefit_in_kind_exemption_exhausted, public_transport_flat_rate_job_ticket, "
            f"flat_rate_taxation_method. For tax_treatment reply with exactly "
            f"'benefit_in_kind' or 'employer_discount'."
        )

        result = worker.run(goal=goal, action_id=action_id, tool_name="datev_set_travel_subsidy")

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
