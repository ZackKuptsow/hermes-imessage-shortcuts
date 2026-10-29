"""The household_forward tool: one of Zack's household commands, passed to the household
assistant as him. Its answer goes to his phone; the tool returns only whether it did."""

from __future__ import annotations

import json
from typing import Any

from gateway.platforms._shared import get_scoped_secret
from hermes_constants import get_hermes_home

from . import household, imsg
from .handles import ALLOW_LIST_RELATIVE_PATH
from .household import FUNCTION_URL_RELATIVE_PATH

TOOL_NAME = "household_forward"
TOOLSET = "imessage_shortcuts"
DESCRIPTION = (
    "Forward one of Zack's household commands (the shared lists, reminders, chores, OTIS, STOP, help) "
    "to the household assistant, as Zack. Pass his words unchanged. The household assistant's answer "
    "is texted straight to his phone and is never returned here: the result says only whether it was "
    "delivered. Use it only for a command Zack sent; never to message anyone."
)
PARAMETERS = {
    "properties": {"command": {"description": "Zack's household command, word for word.", "type": "string"}},
    "required": ["command"],
    "type": "object",
}


def forward_settings() -> household.ForwardSettings:
    """Where the forward reads the allow list, the URL and imsg, under this Hermes home."""
    hermes_home = get_hermes_home()
    return household.ForwardSettings(
        allow_list_path=hermes_home / ALLOW_LIST_RELATIVE_PATH,
        function_url_path=hermes_home / FUNCTION_URL_RELATIVE_PATH,
        imsg_path=imsg.find_imsg(get_scoped_secret("IMESSAGE_SHORTCUTS_IMSG", "").strip() or None),
    )


def household_forward(args: dict, **_: Any) -> str:
    """Tool handler: forward `command` and report, as JSON, whether the answer was delivered."""
    command = str(args.get("command", ""))
    try:
        outcome = household.forward_household_command(command, forward_settings())
    except (household.ForwardError, imsg.ImsgMissingError) as error:
        return json.dumps({"delivered": False, "summary": f"Not forwarded: {error}"})
    return json.dumps({"delivered": outcome.delivered, "status": outcome.status, "summary": outcome.summary})


def forward_is_configured() -> bool:
    """check_fn: offer the tool only on a machine that has the function's URL."""
    return (get_hermes_home() / FUNCTION_URL_RELATIVE_PATH).is_file()


def register_tools(ctx) -> None:
    """Register household_forward in the imessage_shortcuts toolset."""
    ctx.register_tool(
        check_fn=forward_is_configured,
        description=DESCRIPTION,
        emoji="\U0001f3e0",  # house
        handler=household_forward,
        name=TOOL_NAME,
        schema={"description": DESCRIPTION, "name": TOOL_NAME, "parameters": PARAMETERS},
        toolset=TOOLSET,
    )
