"""Helpers used by app.py for the publication-review requirements."""
import re
from copy import deepcopy
from datetime import date, datetime, timedelta

from remedy.sample_data import DEMO_TODAY

MADE_UP_NOTICE = "Made-up sample data — these orders, merchants, dates, charges and contacts are fictional. You did not enter them."

_TOOL_DEADLINE = re.compile(r"(return deadline is [0-9-]+\.) \[you entered\]")


def relabel_tool_dates(text):
    """A deadline the tool computed is not a user-entered fact."""
    return _TOOL_DEADLINE.sub(r"\1 [calculated by this tool from your dates]", text)


def sample_for_today(source, today):
    """Move the fixed fixture timeline with today while preserving intervals."""
    delta = today - DEMO_TODAY
    value = deepcopy(source)

    def shift(current):
        if isinstance(current, dict):
            return {key: shift(child) for key, child in current.items()}
        if isinstance(current, list):
            return [shift(child) for child in current]
        if not isinstance(current, str):
            return current
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", current):
            return (date.fromisoformat(current) + delta).isoformat()
        if re.match(r"^\d{4}-\d{2}-\d{2}T", current):
            parsed = datetime.fromisoformat(current.replace("Z", "+00:00")) + delta
            return parsed.isoformat()
        return current

    value = shift(value)
    value["selfserve"] = {
        "return_days": 30,
        "final_sale": False,
        "charges": [],
        "sample": True,
    }
    return value


def workspace_actions(token):
    from urllib.parse import quote
    base = "/w/" + quote(token, safe="")
    return (
        "<nav class='workspace-actions'>"
        f"<a class='button' href='{base}'>Workspace</a>"
        f"<a class='button' href='{base}/export'>Export everything</a>"
        f"<form class='inline' method='post' action='{base}/delete'>"
        "<button class='danger'>Delete this workspace now</button></form></nav>"
    )
