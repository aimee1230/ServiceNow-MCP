"""
ServiceNow MCP server for an Ejento demo (FastMCP, Streamable HTTP).

Tools: get_incidents, create_incident, update_incident, get_hr_cases, get_hr_services, get_hr_profiles
Run:   uvicorn server:app --host 0.0.0.0 --port 8000        (Render: --port $PORT)
MCP endpoint: /mcp      Health check: /health

Env: SERVICENOW_INSTANCE, SERVICENOW_AUTH_MODE (oauth|oauth_password|basic), SERVICENOW_CLIENT_ID,
     SERVICENOW_CLIENT_SECRET, SERVICENOW_USERNAME, SERVICENOW_PASSWORD, SERVICENOW_MAX_LIMIT
"""

import asyncio
import os
import re
import time
from typing import Annotated, Any, Optional

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field
from starlette.requests import Request
from starlette.responses import PlainTextResponse

load_dotenv(override=True)   # values in .env win over stale shell variables

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
INSTANCE = os.getenv("SERVICENOW_INSTANCE", "").rstrip("/")
AUTH_MODE = os.getenv("SERVICENOW_AUTH_MODE", "oauth").strip().lower()
CLIENT_ID = os.getenv("SERVICENOW_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("SERVICENOW_CLIENT_SECRET", "")
USERNAME = os.getenv("SERVICENOW_USERNAME", "")
PASSWORD = os.getenv("SERVICENOW_PASSWORD", "")
MAX_LIMIT = max(1, min(int(os.getenv("SERVICENOW_MAX_LIMIT", "25")), 100))
DEFAULT_LIMIT = 5
# "all" = {display_value, value} for every field | "true" = readable text only | "false" = raw values only
DISPLAY_VALUE = os.getenv("SERVICENOW_DISPLAY_VALUE", "all").strip().lower()

READ_TABLES = {"incident", "sn_hr_core_case", "sn_hr_core_service", "sn_hr_core_profile",
               "sys_user", "sys_user_group"}          # last two: internal name -> sys_id lookups only
WRITE_TABLES = {"incident"}

STATE_ALIASES = {"new": "1", "in_progress": "2", "on_hold": "3", "resolved": "6", "closed": "7",
                 "canceled": "8", "cancelled": "8"}
HOLD_ALIASES = {"awaiting_caller": "1", "awaiting_change": "3", "awaiting_problem": "4", "awaiting_vendor": "5"}

SYS_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
FIELDS_RE = re.compile(r"^[A-Za-z0-9_.,]+$")
JS_DATE_RE = re.compile(r"javascript:gs\.(?:days|months|years|beginning|end|dateGenerate)\w*\([^()]*\)", re.I)

if not INSTANCE.startswith("https://"):
    raise RuntimeError("SERVICENOW_INSTANCE must be an HTTPS URL, e.g. https://your-instance.service-now.com")
if AUTH_MODE in {"oauth", "oauth_password"} and not (CLIENT_ID and CLIENT_SECRET):
    raise RuntimeError("OAuth requires SERVICENOW_CLIENT_ID and SERVICENOW_CLIENT_SECRET.")
if AUTH_MODE in {"basic", "oauth_password"} and not (USERNAME and PASSWORD):
    raise RuntimeError("Basic authentication requires SERVICENOW_USERNAME and SERVICENOW_PASSWORD.")
if DISPLAY_VALUE not in {"all", "true", "false"}:
    raise RuntimeError("SERVICENOW_DISPLAY_VALUE must be all, true or false.")
if AUTH_MODE not in {"oauth", "oauth_password", "basic"}:
    raise RuntimeError("SERVICENOW_AUTH_MODE must be 'oauth', 'oauth_password' or 'basic'.")

# ---------------------------------------------------------------------------
# Shared tool-description text
# ---------------------------------------------------------------------------
QUERY_HELP = (
    "QUERY SYNTAX (ServiceNow encoded query): join conditions with ^ (AND) or ^OR (OR). Operators: "
    "= != > >= < <= LIKE (contains) STARTSWITH IN (comma list) ISEMPTY ISNOTEMPTY. Filter on stored "
    "values (state=2), not labels. Dot-walk into referenced records, e.g. assigned_to.name=Beth Anglin "
    "or caller_id.email=a@b.com. Date helpers are allowed, e.g. sys_created_on>=javascript:gs.daysAgoStart(7). "
    "Results are newest-updated first unless you add ORDERBY<field> or ORDERBYDESC<field>.")
FIELDS_HELP = ("FIELDS: every field of each record is returned by default, including empty ones"
               + (" (each value as {display_value, value}; references give the readable name and the sys_id)"
                  if DISPLAY_VALUE == "all" else "")
               + ". Optionally pass 'fields' as a comma-separated list (e.g. 'number,state') to return only those.")
IDENT_DESC = "Exact record number or 32-character sys_id for ONE record. Takes precedence over 'query'."
QUERY_DESC = "Encoded query to SEARCH several records. Ignored when 'identifier' is given."
LIMIT_DESC = f"Max records for a search (1-{MAX_LIMIT}, default {DEFAULT_LIMIT})."
FIELDS_DESC = "Optional comma-separated fields to return. Empty = all fields."

mcp = FastMCP(
    name="ServiceNow MCP Server",
    instructions=(
        "Tools for a ServiceNow instance: IT incidents (read, create, update) and HR data (cases, services, "
        "profiles; read-only). For one known record use 'identifier'; to search use 'query' with ServiceNow "
        "encoded-query syntax. Numbers: INC0010001 (incident), HRC0000184 (HR case), HRP0000350 (HR profile). "
        "When creating or updating incidents, people may be given as username, email or full name and groups "
        "by name; the server resolves them. ServiceNow permissions remain authoritative."),
)
http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=False)
_token: Optional[str] = None
_token_expiry = 0.0
_token_lock = asyncio.Lock()


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


# ---------------------------------------------------------------------------
# ServiceNow access
# ---------------------------------------------------------------------------
async def get_access_token() -> str:
    """OAuth client-credentials token, cached until shortly before expiry."""
    global _token, _token_expiry
    async with _token_lock:
        if _token and time.time() < _token_expiry - 60:
            return _token
        try:
            r = await http.post(f"{INSTANCE}/oauth_token.do", headers={"Accept": "application/json"},
                                data={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                                      **({"grant_type": "password", "username": USERNAME, "password": PASSWORD}
                                         if AUTH_MODE == "oauth_password" else {"grant_type": "client_credentials"})})
            if r.status_code != 200:
                raise ToolError(f"ServiceNow OAuth failed ({r.status_code}): {r.text[:300]}")
            body = r.json()
            _token = body["access_token"]
            _token_expiry = time.time() + int(body.get("expires_in", 1800))
            return _token
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            _token, _token_expiry = None, 0
            raise ToolError(f"ServiceNow OAuth authentication failed: {exc}") from None


async def api_request(method: str, table: str, *, sys_id: Optional[str] = None,
                      params: Optional[dict] = None, data: Optional[dict] = None) -> Any:
    """Table API call restricted to allow-listed tables; writes only on WRITE_TABLES."""
    global _token, _token_expiry
    if table not in READ_TABLES:
        raise ToolError(f"Access to table '{table}' is not allowed.")
    if method != "GET" and table not in WRITE_TABLES:
        raise ToolError(f"Write operations are not allowed for table '{table}'.")

    url = f"{INSTANCE}/api/now/table/{table}" + (f"/{sys_id}" if sys_id else "")
    auth = (USERNAME, PASSWORD) if AUTH_MODE == "basic" else None

    async def send():
        headers = {"Accept": "application/json"}
        if AUTH_MODE != "basic":
            headers["Authorization"] = f"Bearer {await get_access_token()}"
        return await http.request(method, url, headers=headers, auth=auth, params=params, json=data)

    try:
        r = await send()
        if r.status_code == 401 and AUTH_MODE != "basic":      # expired token: refresh once
            _token, _token_expiry = None, 0
            r = await send()
        if r.is_error:
            try:
                err = r.json().get("error", {})
                message = err.get("message") or err.get("detail") or r.text
            except ValueError:
                message = r.text
            raise ToolError(f"ServiceNow API error ({r.status_code}): {str(message)[:500]}")
        return r.json().get("result") if r.content else None
    except httpx.TimeoutException:
        raise ToolError("The ServiceNow request timed out.") from None
    except httpx.HTTPError as exc:
        raise ToolError(f"Could not connect to ServiceNow: {exc}") from None
    except ValueError as exc:
        raise ToolError(f"Invalid response from ServiceNow: {exc}") from None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def validate_query(query: str) -> str:
    q = query.strip()
    if not q or len(q) > 2000 or "\n" in q or "\r" in q:
        raise ToolError("Provide a non-empty query (max 2000 characters, no line breaks).")
    # javascript: is only allowed for gs date helpers (blocks arbitrary script in queries)
    if len(re.findall(r"javascript:", q, re.I)) != len(JS_DATE_RE.findall(q)):
        raise ToolError("javascript: is only allowed for date helpers such as javascript:gs.daysAgoStart(7).")
    return q


def identifier_query(identifier: str, prefix: Optional[str] = None) -> str:
    v = identifier.strip()
    if SYS_ID_RE.fullmatch(v):
        return f"sys_id={v}"
    if not v or len(v) > 100 or any(c in v for c in "^=<>@\r\n"):
        raise ToolError("Provide a valid record number or 32-character sys_id.")
    if prefix and not v.upper().startswith(prefix):
        raise ToolError(f"Record number must start with {prefix}.")
    return f"number={v.upper()}"


def clean_value(value: Any, label: str) -> str:
    v = str(value).strip()
    if not v or len(v) > 150 or any(c in v for c in "^\r\n"):
        raise ToolError(f"Invalid {label}.")
    return v


async def fetch_rows(table: str, query: str, limit: int, fields: str = "") -> list[dict]:
    """Return every field of each matching record (or only 'fields' if the caller narrows it)."""
    params: dict[str, Any] = {"sysparm_query": query, "sysparm_limit": limit,
                              "sysparm_display_value": DISPLAY_VALUE, "sysparm_exclude_reference_link": "true"}
    f = (fields or "").strip()
    if f and f.lower() != "all":
        if not FIELDS_RE.fullmatch(f):
            raise ToolError("fields must be a comma-separated list of field names.")
        params["sysparm_fields"] = f
    return await api_request("GET", table, params=params) or []


async def retrieve(table: str, label: str, identifier: str, query: str, limit: int, fields: str,
                   prefix: Optional[str] = None) -> Any:
    """identifier -> one record (dict); query -> list of records."""
    if identifier.strip():
        rows = await fetch_rows(table, identifier_query(identifier, prefix), 1, fields)
        return rows[0] if rows else {"message": f"{label} not found."}
    if query.strip():
        q = validate_query(query)
        if "ORDERBY" not in q.upper():
            q += "^ORDERBYDESCsys_updated_on"
        try:
            n = max(1, min(int(limit), MAX_LIMIT))
        except (TypeError, ValueError):
            raise ToolError("limit must be an integer.") from None
        return await fetch_rows(table, q, n, fields) or {"message": f"No {label.lower()}s found."}
    raise ToolError("Provide either 'identifier' to retrieve one record or 'query' to search records.")


async def find_sys_id(table: str, query: str, label: str, shown: str) -> str:
    rows = await api_request("GET", table, params={
        "sysparm_query": query, "sysparm_limit": 2, "sysparm_fields": "sys_id",
        "sysparm_display_value": "false", "sysparm_exclude_reference_link": "true"})
    if not rows:
        raise ToolError(f"{label} '{shown}' was not found.")
    if len(rows) > 1:
        raise ToolError(f"More than one {label.lower()} matches '{shown}'. Use a unique username/email or the sys_id.")
    return rows[0]["sys_id"]


async def resolve_user(value: Any) -> str:
    v = clean_value(value, "user")
    return v if SYS_ID_RE.fullmatch(v) else await find_sys_id(
        "sys_user", f"user_name={v}^ORemail={v}^ORname={v}", "User", v)


async def resolve_group(value: Any) -> str:
    v = clean_value(value, "assignment group")
    return v if SYS_ID_RE.fullmatch(v) else await find_sys_id("sys_user_group", f"name={v}", "Assignment group", v)


# ---------------------------------------------------------------------------
# INCIDENT TOOLS
# ---------------------------------------------------------------------------
@mcp.tool(description=(
    "Retrieve IT incidents. Use identifier for ONE incident (INC number such as INC0010001, or sys_id): "
    "returns all of its fields. Use query to SEARCH "
    "incidents: returns all fields of each matching incident. "
    "Values: state 1=New, 2=In Progress, 3=On Hold, 6=Resolved, 7=Closed, 8=Canceled; priority 1=Critical, "
    "2=High, 3=Moderate, 4=Low, 5=Planning; urgency/impact 1=High, 2=Medium, 3=Low. "
    "Examples: query='active=true^priority=1' (open critical); query='assigned_to.name=Beth Anglin^stateIN1,2,3'; "
    "query='short_descriptionLIKEvpn^sys_created_on>=javascript:gs.daysAgoStart(7)' (VPN incidents this week); "
    "query='caller_id.email=a@b.com^active=true' (a person's open tickets). " + QUERY_HELP + " " + FIELDS_HELP))
async def get_incidents(
    identifier: Annotated[str, Field(description=IDENT_DESC)] = "",
    query: Annotated[str, Field(description=QUERY_DESC)] = "",
    limit: Annotated[int, Field(description=LIMIT_DESC)] = DEFAULT_LIMIT,
    fields: Annotated[str, Field(description=FIELDS_DESC)] = "",
) -> Any:
    return await retrieve("incident", "Incident", identifier, query, limit, fields, "INC")


@mcp.tool(description=(
    "Create a NEW IT incident (write action: only when the user clearly asks to log or raise a ticket). "
    "Returns the new incident number, state and priority. Priority is calculated from urgency and impact "
    "(1=High, 2=Medium, 3=Low; default 3/3). caller and assigned_to accept a username, email or exact full "
    "name; assignment_group accepts the exact group name. If a name matches several people the call fails: "
    "ask the user for the username or email. category/subcategory must match your instance's choice lists "
    "(e.g. software, hardware, network, inquiry)."))
async def create_incident(
    short_description: Annotated[str, Field(description="One-line summary of the problem (required).")],
    description: Annotated[str, Field(description="Detailed description: what happened, impact, steps to reproduce.")] = "",
    caller: Annotated[str, Field(description="Person reporting the issue: username, email, full name or sys_id.")] = "",
    urgency: Annotated[int, Field(description="1=High, 2=Medium, 3=Low.")] = 3,
    impact: Annotated[int, Field(description="1=High, 2=Medium, 3=Low.")] = 3,
    category: Annotated[str, Field(description="e.g. software, hardware, network, inquiry.")] = "",
    subcategory: Annotated[str, Field(description="e.g. email, vpn, os.")] = "",
    assigned_to: Annotated[str, Field(description="Person to assign: username, email, full name or sys_id.")] = "",
    assignment_group: Annotated[str, Field(description="Exact group name or sys_id, e.g. 'Service Desk'.")] = "",
) -> Any:
    if not short_description.strip():
        raise ToolError("short_description cannot be empty.")
    if urgency not in {1, 2, 3} or impact not in {1, 2, 3}:
        raise ToolError("urgency and impact must each be 1, 2, or 3.")
    body: dict[str, Any] = {"short_description": short_description.strip(),
                            "urgency": str(urgency), "impact": str(impact)}
    for key, val in (("description", description), ("category", category), ("subcategory", subcategory)):
        if val.strip():
            body[key] = val.strip()
    if caller.strip():
        body["caller_id"] = await resolve_user(caller)
    if assigned_to.strip():
        body["assigned_to"] = await resolve_user(assigned_to)
    if assignment_group.strip():
        body["assignment_group"] = await resolve_group(assignment_group)
    res = await api_request("POST", "incident", data=body, params={
        "sysparm_display_value": DISPLAY_VALUE, "sysparm_exclude_reference_link": "true"})
    return res


@mcp.tool(description=(
    "Update an EXISTING incident (write action). identifier = INC number or sys_id. fields = an object with "
    "only the fields to change. Allowed: short_description, description, state, hold_reason, priority, "
    "urgency, impact, assigned_to, assignment_group, caller_id, category, subcategory, work_notes, comments, "
    "close_code, close_notes. "
    "state accepts 1=New, 2=In Progress, 3=On Hold, 6=Resolved, 7=Closed, 8=Canceled (or the words new, "
    "in_progress, on_hold, resolved, closed, canceled). Resolving or closing (6 or 7) REQUIRES close_code "
    "(e.g. 'Solved (Permanently)', 'Solved (Work Around)', 'Not Solved (Not Reproducible)') and close_notes "
    "in the same call. On Hold (3) normally needs hold_reason (awaiting_caller, awaiting_change, "
    "awaiting_problem or awaiting_vendor) and a comment. work_notes are internal (IT staff only); comments "
    "are visible to the caller. Prefer urgency/impact over priority (priority is recalculated). assigned_to / "
    "caller_id accept username, email, full name or sys_id; assignment_group accepts the exact group name. "
    "Example: identifier='INC0010001', fields={\"state\":\"in_progress\",\"assigned_to\":\"beth.anglin\","
    "\"work_notes\":\"Investigating\"}. Incidents cannot be deleted."))
async def update_incident(
    identifier: Annotated[str, Field(description="INC number (e.g. INC0010001) or sys_id of the incident.")],
    fields: Annotated[dict[str, Any], Field(description="Field names mapped to new values; only include fields that change.")],
) -> Any:
    allowed = {"short_description", "description", "state", "hold_reason", "priority", "urgency", "impact",
               "assigned_to", "assignment_group", "caller_id", "category", "subcategory",
               "work_notes", "comments", "close_code", "close_notes"}
    if not isinstance(fields, dict) or not fields:
        raise ToolError("Provide a non-empty fields object.")
    data = dict(fields)
    bad = set(data) - allowed
    if bad:
        raise ToolError(f"Unsupported update fields: {', '.join(sorted(bad))}. Allowed: {', '.join(sorted(allowed))}")

    for f in ("priority", "urgency", "impact"):
        if f in data:
            try:
                n = int(data[f])
            except (TypeError, ValueError):
                raise ToolError(f"{f} must be a number.") from None
            if n not in (range(1, 6) if f == "priority" else range(1, 4)):
                raise ToolError(f"Invalid value for {f}.")
            data[f] = str(n)

    if "state" in data:
        s = str(data["state"]).strip().lower().replace(" ", "_")
        s = STATE_ALIASES.get(s, s)
        if s not in {"1", "2", "3", "6", "7", "8"}:
            raise ToolError("state must be 1 (New), 2 (In Progress), 3 (On Hold), 6 (Resolved), 7 (Closed) or 8 (Canceled).")
        data["state"] = s
        if s in {"6", "7"} and not (str(data.get("close_code", "")).strip() and str(data.get("close_notes", "")).strip()):
            raise ToolError("Resolving or closing an incident requires both close_code and close_notes.")

    if "hold_reason" in data:
        h = str(data["hold_reason"]).strip().lower().replace(" ", "_")
        data["hold_reason"] = HOLD_ALIASES.get(h, h)

    for f in ("assigned_to", "caller_id"):
        if data.get(f):
            data[f] = await resolve_user(data[f])
    if data.get("assignment_group"):
        data["assignment_group"] = await resolve_group(data["assignment_group"])

    sys_id = await find_sys_id("incident", identifier_query(identifier, "INC"), "Incident", identifier)
    res = await api_request("PATCH", "incident", sys_id=sys_id, data=data, params={
        "sysparm_display_value": DISPLAY_VALUE, "sysparm_exclude_reference_link": "true"})
    return res


# ---------------------------------------------------------------------------
# HR TOOLS (read-only; need the HR Service Delivery plugin)
# ---------------------------------------------------------------------------
HR_NOTE = "Requires the HR Service Delivery plugin and HR read access for the integration account."


@mcp.tool(description=(
    "Retrieve HR cases (employee requests such as leave, benefits, payroll or onboarding questions). "
    "Use identifier for ONE case (HRC number such as HRC0000184, or sys_id). Use query to SEARCH cases. "
    "Both return all fields of the case(s). Examples: query='active=true'; query='opened_for.email=a@b.com^active=true' (a person's open "
    "cases); query='hr_service.nameLIKEbenefits'; query='opened_at>=javascript:gs.daysAgoStart(30)'. "
    + HR_NOTE + " " + QUERY_HELP + " " + FIELDS_HELP))
async def get_hr_cases(
    identifier: Annotated[str, Field(description=IDENT_DESC)] = "",
    query: Annotated[str, Field(description=QUERY_DESC)] = "",
    limit: Annotated[int, Field(description=LIMIT_DESC)] = DEFAULT_LIMIT,
    fields: Annotated[str, Field(description=FIELDS_DESC)] = "",
) -> Any:
    return await retrieve("sn_hr_core_case", "HR case", identifier, query, limit, fields, "HRC")


@mcp.tool(description=(
    "Retrieve the catalog of HR services employees can request (e.g. 'Vision Benefits Enrollment/"
    "Modification', leave of absence, payroll inquiry). Use identifier for ONE service: its exact name, "
    "internal value or sys_id. Use query to SEARCH services. Both return all fields of the service(s). Good for 'what HR services exist for X?'. Examples: query='active=true^nameLIKEbenefits'; "
    "query='active=true^descriptionLIKEleave'. " + HR_NOTE + " " + QUERY_HELP + " " + FIELDS_HELP))
async def get_hr_services(
    identifier: Annotated[str, Field(description="Exact service name, internal value or sys_id for ONE service. Takes precedence over 'query'.")] = "",
    query: Annotated[str, Field(description=QUERY_DESC)] = "",
    limit: Annotated[int, Field(description=LIMIT_DESC)] = DEFAULT_LIMIT,
    fields: Annotated[str, Field(description=FIELDS_DESC)] = "",
) -> Any:
    ident = identifier.strip()
    if not ident:
        return await retrieve("sn_hr_core_service", "HR service", "", query, limit, fields)
    if SYS_ID_RE.fullmatch(ident):
        q = f"sys_id={ident}"
    else:
        v = clean_value(ident, "HR service identifier")
        if any(c in v for c in "=<>"):
            raise ToolError("Invalid HR service identifier.")
        q = f"name={v}^ORvalue={v}"
    rows = await fetch_rows("sn_hr_core_service", q, 2, fields)
    if not rows:
        return {"message": "HR service not found."}
    return rows[0] if len(rows) == 1 else {"message": "Multiple HR services matched. Use the service sys_id."}


@mcp.tool(description=(
    "Retrieve employee HR profiles (employment details such as employment type, start date, department, "
    "location and manager). Use identifier for ONE profile (HRP number such as HRP0000350, or sys_id). Use "
    "query to SEARCH profiles. Returns all fields visible to the integration account. Examples: query='user.email=a@b.com'; query='employment_type=full_time_employee' (verify the "
    "internal value on your instance). " + HR_NOTE + " " + QUERY_HELP + " " + FIELDS_HELP))
async def get_hr_profiles(
    identifier: Annotated[str, Field(description=IDENT_DESC)] = "",
    query: Annotated[str, Field(description=QUERY_DESC)] = "",
    limit: Annotated[int, Field(description=LIMIT_DESC)] = DEFAULT_LIMIT,
    fields: Annotated[str, Field(description=FIELDS_DESC)] = "",
) -> Any:
    return await retrieve("sn_hr_core_profile", "HR profile", identifier, query, limit, fields, "HRP")


# ---------------------------------------------------------------------------
# ASGI app (uvicorn / Render)
# ---------------------------------------------------------------------------
app = mcp.http_app(path=os.getenv("MCP_PATH", "/mcp"), stateless_http=True)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))