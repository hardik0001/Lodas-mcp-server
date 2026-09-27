import sys
import logging
from pathlib import Path
from dotenv import load_dotenv
from fastmcp import FastMCP
from tools.navigate import register_navigate_tools
from tools.commute import register_commute_tools
from tools.travel_subsidy import register_travel_subsidy_tools
from tools.read_values import register_read_tools
from tools.salary import register_salary_tools
from tools.company_car import register_company_car_tools
from tools.save import register_save_tools
from tools.status import register_status_tools

# load_dotenv() with no args searches upward from the current working
# directory — which is whatever launched this process (e.g. Claude
# Desktop's own app directory), not necessarily this file's location. Point
# it at the .env next to main.py explicitly so API keys load regardless of
# how/where this server is started from.
load_dotenv(Path(__file__).resolve().parent / ".env")


mcp = FastMCP(
    name="datev_mcp",
    instructions=(
        "Automates data entry in DATEV LODAS via computer-use. "
        "Always call datev_navigate_to_employee before any data-entry tool."
    ),
)

register_navigate_tools(mcp)
register_commute_tools(mcp)
register_travel_subsidy_tools(mcp)
register_read_tools(mcp)
register_salary_tools(mcp)
register_company_car_tools(mcp)
register_save_tools(mcp)
register_status_tools(mcp)

if __name__ == "__main__":
    mcp.run(transport="stdio")
    # mcp.run(transport="http", host="127.0.0.1", port=8000)