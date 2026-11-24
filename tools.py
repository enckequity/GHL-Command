"""GHL Command: GoHighLevel tools for an agent.

Native tools for GoHighLevel CRM operations: contacts, pipelines, calendars,
workflows, email and SMS sends directly from chat.

API host is services.leadconnectorhq.com (the only host that accepts pit-
Private Integration Tokens) with Version header 2021-07-28. Auth goes
through the agent's secret scope (agent.secret_scope.get_secret), never os.environ
directly, so profile multiplexing is respected (catalog rule 11).

There is no baked-in default location. A public plugin must never target the
author's own account, so both GHL_API_KEY and GHL_LOCATION_ID are required and
a missing one fails with a clear message instead of writing somewhere the user
did not choose. Every call is held to that configured location: a location_id
argument that names any other location is refused, and a record fetched by id
must belong to it before it is changed or messaged (an agency-wide token can
reach every sub-account, so the token alone does not confine it).

Outbound text never names the engine. An email without a subject takes the
configured business name (GHL_BUSINESS_NAME); with neither, it is refused.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

TOOLSET = "ghl-command"
PLUGIN_VERSION = "0.1.0"
USER_AGENT = "ghl-command/" + PLUGIN_VERSION + " (+https://github.com/PXMichael/GHL-Command)"
GHL_BASE = "https://services.leadconnectorhq.com"
LOCATION_HELP = (
    "GHL_LOCATION_ID is not set. Add your GoHighLevel Location ID to the "
    "assistant's secrets. Find it in the GHL URL bar inside the sub-account."
)
SUBJECT_HELP = (
    "Missing required parameter: subject. Give the email a subject line "
    "(or set GHL_BUSINESS_NAME so a missing subject uses the business name)."
)
PREVIEW_CHARS = 300


def tool_error(message: str) -> str:
    """Tool error envelope (same JSON shape as core)."""
    return json.dumps({"error": str(message)}, ensure_ascii=False)


def _business_name() -> str:
    """The business name a subject-less email goes out under, or "" when unset."""
    from agent.secret_scope import get_secret

    return (get_secret("GHL_BUSINESS_NAME", "") or "").strip()


def _get_config() -> tuple[str, str]:
    """Return (token, location) via the agent's secret scope.

    Both values are required. There is no default location, so an unset
    GHL_LOCATION_ID fails loudly rather than targeting an account the user
    never chose.
    """
    from agent.secret_scope import get_secret

    token = (get_secret("GHL_API_KEY", "") or "").strip()
    location = (get_secret("GHL_LOCATION_ID", "") or "").strip()
    return token, location


def _get_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Version": "2021-07-28",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }


def _api(
    method: str,
    path: str,
    location_id: Optional[str] = None,
    query: Optional[Dict[str, str]] = None,
    body: Optional[Any] = None,
    timeout: float = 20,
):
    """One GHL REST call. Raises on HTTP error, returns parsed JSON.

    The location is always the configured one; a different location_id raises.
    """
    token, configured = _get_config()
    if not token:
        raise RuntimeError(
            "GHL_API_KEY is not set. Add it to the assistant's secrets. "
            "Private Integration Token (pit-...) is required."
        )
    if not configured:
        raise RuntimeError(LOCATION_HELP)
    if location_id and str(location_id).strip() != configured:
        raise ValueError(_other_location_message(location_id))
    loc = configured
    url = GHL_BASE + path
    # Always include locationId as query for list endpoints when not in body
    params = dict(query or {})
    if loc and "locationId" not in params and "location_id" not in params:
        # Only add for endpoints that accept it; safe to pass always, GHL ignores extra on some routes
        # but we keep it for clarity. POST /contacts/search needs it in body instead.
        if path.startswith("/contacts/search"):
            pass
        else:
            params["locationId"] = loc
    if params:
        qs = urllib.parse.urlencode(params, doseq=True)
        url = url + ("&" if "?" in url else "?") + qs
    data_bytes = None
    if body is not None:
        data_bytes = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data_bytes, method=method.upper(), headers=_get_headers(token)
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                return {"raw": raw.decode("utf-8", errors="ignore")}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="ignore") if hasattr(e, "read") else ""
        # Keep GHL error body visible for debugging (token vs location vs scope)
        raise RuntimeError(f"GHL API {method} {path} failed {e.code}: {raw[:2000]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"GHL network error for {path}: {e.reason}")


def _other_location_message(asked: Any) -> str:
    return (
        "location_id " + repr(str(asked)[:40]) + " is not the configured GoHighLevel "
        "location. This connection only works on the location in GHL_LOCATION_ID; "
        "leave location_id out."
    )


def _asked_location(args: dict) -> str:
    return str(args.get("location_id") or args.get("locationId") or "").strip()


def _resolve_location(args: dict) -> str:
    """The configured location. A location_id argument may only repeat it."""
    _token, configured = _get_config()
    if not configured:
        raise RuntimeError(LOCATION_HELP)
    asked = _asked_location(args)
    if asked and asked != configured:
        raise ValueError(_other_location_message(asked))
    return configured


def _require_in_location(kind: str, record: Any, loc: str, label: str) -> None:
    """Refuse a record that is not in the configured location (fail closed)."""
    found = record.get("locationId") if isinstance(record, dict) else None
    if found != loc:
        raise ValueError(
            kind + " " + repr(label[:40]) + " is not in the configured GoHighLevel "
            "location, so nothing was changed or sent."
        )


def _contact_in_location(cid: str, raw_id: Any, loc: str) -> Dict[str, Any]:
    """Fetch a contact by (escaped) id and check it belongs to the configured location."""
    res = _api("GET", f"/contacts/{cid}", location_id=loc)
    contact = res.get("contact") if isinstance(res, dict) else None
    _require_in_location("Contact", contact, loc, str(raw_id))
    return contact


def _find_contact_id(loc: str, phone: str = "", email: str = "") -> str:
    """Resolve the contactId for a phone number or email in the configured location.

    POST /conversations/messages requires contactId, so a send by phone or email
    looks the contact up first (GET /contacts/search/duplicate). No match is an
    error: the contact is created on purpose, not as a side effect of a send.
    """
    query = {"locationId": loc}
    if phone:
        query["number"] = phone
        what = "phone " + phone
    else:
        query["email"] = email
        what = "email " + email
    res = _api("GET", "/contacts/search/duplicate", location_id=loc, query=query)
    contact = res.get("contact") if isinstance(res, dict) else None
    if not isinstance(contact, dict) or not contact.get("id"):
        raise ValueError(
            "No GoHighLevel contact has " + what + ". Create the contact first "
            "(ghl_create_contact), then send to its contact_id."
        )
    _require_in_location("Contact", contact, loc, str(contact.get("id")))
    return str(contact["id"])


def _email_subject(args: dict) -> str:
    """The subject the email goes out with: the given one, else the business name, else ""."""
    subject = str(args.get("subject") or "").strip()
    return subject or _business_name()


def _plain_excerpt(text: Any, limit: int = PREVIEW_CHARS) -> str:
    """A readable excerpt of an (HTML or plain) body for the approval prompt."""
    raw = str(text or "")
    raw = re.sub(r"(?is)<(script|style)\b.*?</\1\s*>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p\s*>|</div\s*>|</li\s*>", "\n", raw)
    raw = re.sub(r"<[^>]*>", " ", raw)
    raw = html_lib.unescape(raw)
    lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in raw.split("\n")]
    out = "\n".join(ln for ln in lines if ln)
    if len(out) > limit:
        out = out[:limit].rstrip() + "..."
    return out


def _compact_contact(c: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": c.get("id"),
        "firstName": c.get("firstName"),
        "lastName": c.get("lastName"),
        "email": c.get("email"),
        "phone": c.get("phone"),
        "tags": c.get("tags"),
        "companyName": c.get("companyName"),
        "dateAdded": c.get("dateAdded"),
    }


def _dispatch(fn, log_name: str, fail_msg: str) -> str:
    try:
        result = fn()
        # fn already returns dict; wrap as {"result": ...}
        return json.dumps({"result": result}, ensure_ascii=False)
    except Exception as e:
        logger.error("%s error: %s", log_name, e)
        return tool_error(f"{fail_msg}: {e}")


def _check_ghl_available() -> bool:
    """Tool is only available when both the token and the location are set."""
    try:
        token, location = _get_config()
        return bool(token and location)
    except Exception:
        return False


# --- Approval gate -------------------------------------------------------

# These four reach outside the conversation: they delete a record or put a
# message in front of a real person. A CRM is full of text other people wrote
# (notes, inbound messages, form answers), so any of it can carry instructions
# aimed at the model. They run only after the engine's approval prompt.
APPROVAL_TOOLS = {
    "ghl_delete_contact",
    "ghl_send_sms",
    "ghl_send_email",
    "ghl_enroll_workflow",
}

_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _safe_id(value: Any, field: str) -> str:
    """Validate and escape a model-supplied id before it enters a URL path.

    An id carrying '/', '?', '#' or '..' can retarget the request at a
    different endpoint while still presenting the same token, so ids are held
    to a strict charset and percent-escaped as well.
    """
    text = str(value if value is not None else "").strip()
    if not text or not _ID_RE.match(text):
        raise ValueError(
            "Invalid " + field + ": expected [A-Za-z0-9_-] only, got " + repr(text[:40])
        )
    return urllib.parse.quote(text, safe="")


def _arg(args: dict, *keys: str) -> str:
    for key in keys:
        v = args.get(key)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _approval_preview(tool_name: str, args: dict) -> str:
    """The approval prompt: who, and exactly what they will receive."""
    if tool_name == "ghl_delete_contact":
        return "Delete GoHighLevel contact " + _arg(args, "contact_id", "contactId") + "? This cannot be undone."
    if tool_name == "ghl_send_sms":
        who = _arg(args, "contact_id", "contactId") or _arg(args, "phone", "to")
        body = _plain_excerpt(_arg(args, "message", "body"))
        return "Text " + who + " from GoHighLevel?\n\n" + body
    if tool_name == "ghl_send_email":
        who = _arg(args, "contact_id", "contactId") or _arg(args, "email", "to")
        body = _plain_excerpt(_arg(args, "html", "htmlBody", "body", "message"))
        return ("Email " + who + " from GoHighLevel?\nSubject: " + _email_subject(args)
                + "\n\n" + body)
    if tool_name == "ghl_enroll_workflow":
        return ("Enroll GoHighLevel contact " + _arg(args, "contact_id", "contactId")
                + " in workflow " + _arg(args, "workflow_id", "workflowId")
                + "? The workflow may message them.")
    return tool_name


def _approval_rule_key(tool_name: str, args: dict) -> str:
    """One rule per distinct action, so an "always" never covers a different send.

    The key hashes the recipient and the exact content; approving one message
    "always" only lets that same message to that same recipient through again.
    """
    if tool_name == "ghl_send_sms":
        parts = [_arg(args, "contact_id", "contactId"), _arg(args, "phone", "to"), _arg(args, "message", "body")]
    elif tool_name == "ghl_send_email":
        parts = [_arg(args, "contact_id", "contactId"), _arg(args, "email", "to"), _email_subject(args),
                 _arg(args, "html", "htmlBody", "body", "message")]
    elif tool_name == "ghl_delete_contact":
        parts = [_arg(args, "contact_id", "contactId")]
    elif tool_name == "ghl_enroll_workflow":
        parts = [_arg(args, "contact_id", "contactId"), _arg(args, "workflow_id", "workflowId")]
    else:
        parts = [json.dumps(args, sort_keys=True, default=str)]
    digest = hashlib.sha256(json.dumps(parts).encode("utf-8")).hexdigest()[:16]
    return "ghl-command:" + tool_name + ":" + digest


def pre_tool_call(tool_name: str = "", args=None, **kwargs):
    """Route outbound and destructive calls through the engine's approval gate.

    Returns the native ``{"action": "approve", "message", "rule_key"}`` directive,
    so the engine's own once/session/always/deny prompt (and whatever approval
    surface and ledger it reports to) decides. A call that cannot run anyway
    (another location, an email without a subject) is blocked before anyone is
    asked. Returns None for everything else.
    """
    if not str(tool_name).startswith("ghl_"):
        return None
    args = args if isinstance(args, dict) else {}
    asked = _asked_location(args)
    if asked:
        try:
            _token, configured = _get_config()
        except Exception:
            configured = ""
        if asked != configured:
            return {"action": "block", "message": "BLOCKED: nothing was read, changed or sent. "
                    + _other_location_message(asked)}
    if tool_name not in APPROVAL_TOOLS:
        return None
    try:
        if tool_name == "ghl_send_email" and not _email_subject(args):
            return {"action": "block", "message": "BLOCKED: the email was not sent. " + SUBJECT_HELP}
    except Exception:
        pass
    try:
        detail = _approval_preview(tool_name, args)
    except Exception:
        detail = "Run " + tool_name + "?"
    try:
        rule_key = _approval_rule_key(tool_name, args)
    except Exception:
        digest = hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
        rule_key = "ghl-command:" + tool_name + ":" + digest
    return {"action": "approve", "message": detail, "rule_key": rule_key}



# --- Handlers ---
# Each handler resolves the location inside run(), so a refused location_id
# comes back as a tool error rather than an exception.

def _handle_list_contacts(args: dict, **kw) -> str:
    limit = args.get("limit", 20)
    def run():
        loc = _resolve_location(args)
        res = _api("GET", "/contacts/", location_id=loc, query={"limit": str(limit), "page": str(args.get("page", 1))})
        contacts = res.get("contacts") or res.get("data") or []
        return {"location_id": loc, "count": len(contacts), "contacts": [_compact_contact(c) for c in contacts], "meta": res.get("meta")}
    return _dispatch(run, "ghl_list_contacts", "Failed to list contacts")


def _handle_search_contacts(args: dict, **kw) -> str:
    query = (args.get("query") or args.get("email") or "").strip()
    if not query:
        return tool_error("Missing required parameter: query (email or name)")
    page_limit = args.get("pageLimit") or args.get("limit", 20)
    def run():
        loc = _resolve_location(args)
        body = {"locationId": loc, "query": query, "pageLimit": int(page_limit)}
        # GHL search supports POST with query and pageLimit
        res = _api("POST", "/contacts/search", location_id=loc, body=body)
        contacts = res.get("contacts") or res.get("data") or []
        return {"location_id": loc, "count": len(contacts), "contacts": [_compact_contact(c) for c in contacts]}
    return _dispatch(run, "ghl_search_contacts", "Failed to search contacts")


def _handle_create_contact(args: dict, **kw) -> str:
    first = args.get("firstName") or args.get("first_name")
    email = args.get("email")
    phone = args.get("phone")
    if not first and not email and not phone:
        return tool_error("Missing required parameters: firstName, email or phone")
    def run():
        loc = _resolve_location(args)
        body = {"locationId": loc}
        if first:
            body["firstName"] = first
        for key in ["lastName", "email", "phone", "tags", "companyName", "address", "source"]:
            if args.get(key):
                body[key] = args[key]
        # Accept tags as string or list
        if isinstance(body.get("tags"), str):
            body["tags"] = [t.strip() for t in body["tags"].split(",") if t.strip()]
        res = _api("POST", "/contacts/", location_id=loc, body=body)
        contact = res.get("contact") or res.get("data") or res
        return {"location_id": loc, "contact": _compact_contact(contact) if isinstance(contact, dict) else contact}
    return _dispatch(run, "ghl_create_contact", "Failed to create contact")


def _handle_update_contact(args: dict, **kw) -> str:
    contact_id = args.get("contact_id") or args.get("contactId")
    if not contact_id:
        return tool_error("Missing required parameter: contact_id")
    def run():
        loc = _resolve_location(args)
        body = {}
        for key in ["firstName", "lastName", "email", "phone", "tags", "companyName", "address"]:
            if args.get(key) is not None:
                body[key] = args[key]
        if isinstance(body.get("tags"), str):
            body["tags"] = [t.strip() for t in body["tags"].split(",") if t.strip()]
        if not body:
            return {"error": "No fields to update", "contact_id": contact_id}
        cid = _safe_id(contact_id, "contact_id")
        _contact_in_location(cid, contact_id, loc)
        # IMPORTANT: contact update must NOT include locationId (returns 422)
        res = _api("PUT", f"/contacts/{cid}", body=body)
        contact = res.get("contact") or res
        return {"contact_id": contact_id, "contact": _compact_contact(contact) if isinstance(contact, dict) else contact}
    return _dispatch(run, "ghl_update_contact", "Failed to update contact")


def _handle_delete_contact(args: dict, **kw) -> str:
    contact_id = args.get("contact_id") or args.get("contactId")
    if not contact_id:
        return tool_error("Missing required parameter: contact_id")
    def run():
        loc = _resolve_location(args)
        cid = _safe_id(contact_id, "contact_id")
        _contact_in_location(cid, contact_id, loc)
        res = _api("DELETE", f"/contacts/{cid}", location_id=loc)
        return {"contact_id": contact_id, "location_id": loc, "result": res}
    return _dispatch(run, "ghl_delete_contact", "Failed to delete contact")


def _send_target(loc: str, contact_id: Any, phone: str = "", email: str = "") -> str:
    """The contactId a message goes to: a given id checked against the location, else looked up."""
    if contact_id:
        cid = _safe_id(contact_id, "contact_id")
        _contact_in_location(cid, contact_id, loc)
        return str(contact_id).strip()
    return _find_contact_id(loc, phone=phone, email=email)


def _handle_send_sms(args: dict, **kw) -> str:
    contact_id = args.get("contact_id") or args.get("contactId")
    message = args.get("message") or args.get("body")
    phone = str(args.get("phone") or args.get("to") or "").strip()
    if not message:
        return tool_error("Missing required parameter: message")
    if not contact_id and not phone:
        return tool_error("Missing required parameter: contact_id or phone")
    def run():
        loc = _resolve_location(args)
        cid = _send_target(loc, contact_id, phone=phone)
        body = {"locationId": loc, "contactId": cid, "type": "SMS", "message": message}
        # GHL conversations/messages POST (contactId is required)
        res = _api("POST", "/conversations/messages", location_id=loc, body=body)
        return {"location_id": loc, "contact_id": cid, "phone": phone or None, "message_id": res.get("messageId") or res.get("id"), "raw": res}
    return _dispatch(run, "ghl_send_sms", "Failed to send SMS")


def _handle_send_email(args: dict, **kw) -> str:
    contact_id = args.get("contact_id") or args.get("contactId")
    email = str(args.get("email") or args.get("to") or "").strip()
    html = args.get("html") or args.get("htmlBody") or args.get("body") or args.get("message")
    if not html:
        return tool_error("Missing required parameter: html (email body)")
    if not contact_id and not email:
        return tool_error("Missing required parameter: contact_id or email")
    def run():
        subject = _email_subject(args)
        if not subject:
            raise ValueError(SUBJECT_HELP)
        loc = _resolve_location(args)
        cid = _send_target(loc, contact_id, email=email)
        body = {"locationId": loc, "contactId": cid, "type": "Email", "subject": subject, "html": html}
        res = _api("POST", "/conversations/messages", location_id=loc, body=body)
        return {"location_id": loc, "contact_id": cid, "email": email or None, "subject": subject, "message_id": res.get("messageId") or res.get("id"), "raw": res}
    return _dispatch(run, "ghl_send_email", "Failed to send email")


def _handle_list_pipelines(args: dict, **kw) -> str:
    def run():
        loc = _resolve_location(args)
        res = _api("GET", "/opportunities/pipelines", location_id=loc)
        pipelines = res.get("pipelines") or res.get("data") or res
        # Normalize pipeline summaries
        summaries = []
        if isinstance(pipelines, list):
            for p in pipelines:
                summaries.append({"id": p.get("id"), "name": p.get("name"), "stages": p.get("stages")})
        else:
            summaries = pipelines
        return {"location_id": loc, "pipelines": summaries, "raw_count": len(pipelines) if isinstance(pipelines, list) else None}
    return _dispatch(run, "ghl_list_pipelines", "Failed to list pipelines")


def _handle_search_opportunities(args: dict, **kw) -> str:
    def run():
        loc = _resolve_location(args)
        # GET /opportunities/search takes snake_case location_id
        query = {"location_id": loc, "limit": str(args.get("limit", 20))}
        if args.get("pipeline_id"):
            query["pipeline_id"] = args["pipeline_id"]
        if args.get("contact_id"):
            query["contact_id"] = args["contact_id"]
        res = _api("GET", "/opportunities/search", location_id=loc, query=query)
        opps = res.get("opportunities") or res.get("data") or []
        return {"location_id": loc, "count": len(opps), "opportunities": opps}
    return _dispatch(run, "ghl_search_opportunities", "Failed to search opportunities")


def _handle_move_opportunity(args: dict, **kw) -> str:
    opp_id = args.get("opportunity_id") or args.get("opportunityId")
    if not opp_id:
        return tool_error("Missing required parameter: opportunity_id")
    def run():
        loc = _resolve_location(args)
        body = {}
        # Accept stage_id or status; the API field is pipelineStageId
        if args.get("stage_id"):
            body["pipelineStageId"] = args["stage_id"]
        if args.get("stage"):
            body["pipelineStageId"] = args["stage"]
        if args.get("status"):
            body["status"] = args["status"]
        if not body:
            return {"error": "Provide stage_id or status", "opportunity_id": opp_id}
        oid = _safe_id(opp_id, "opportunity_id")
        current = _api("GET", f"/opportunities/{oid}", location_id=loc)
        _require_in_location("Opportunity", current.get("opportunity") if isinstance(current, dict) else None, loc, str(opp_id))
        res = _api("PUT", f"/opportunities/{oid}", body=body)
        return {"opportunity_id": opp_id, "result": res}
    return _dispatch(run, "ghl_move_opportunity", "Failed to move opportunity")


def _handle_list_workflows(args: dict, **kw) -> str:
    def run():
        loc = _resolve_location(args)
        res = _api("GET", "/workflows/", location_id=loc)
        workflows = res.get("workflows") or res.get("data") or res
        if isinstance(workflows, list):
            summarized = [{"id": w.get("id"), "name": w.get("name"), "isActive": w.get("isActive")} for w in workflows]
        else:
            summarized = workflows
        return {"location_id": loc, "workflows": summarized}
    return _dispatch(run, "ghl_list_workflows", "Failed to list workflows")


def _handle_enroll_workflow(args: dict, **kw) -> str:
    contact_id = args.get("contact_id") or args.get("contactId")
    workflow_id = args.get("workflow_id") or args.get("workflowId")
    if not contact_id or not workflow_id:
        return tool_error("Missing required parameters: contact_id and workflow_id")
    def run():
        loc = _resolve_location(args)
        # Exact singular endpoint: POST /contacts/{contactId}/workflow/{workflowId} with {}
        cid = _safe_id(contact_id, "contact_id")
        wid = _safe_id(workflow_id, "workflow_id")
        _contact_in_location(cid, contact_id, loc)
        res = _api("POST", f"/contacts/{cid}/workflow/{wid}", body={})
        return {"contact_id": contact_id, "workflow_id": workflow_id, "result": res}
    return _dispatch(run, "ghl_enroll_workflow", "Failed to enroll in workflow")


# --- Schemas ---

GHL_LIST_CONTACTS_SCHEMA = {
    "name": "ghl_list_contacts",
    "description": "List GoHighLevel contacts for a location.",
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "How many to return. Default 20."},
            "page": {"type": "integer", "description": "Page number. Default 1."},
        },
        "required": [],
    },
}

GHL_SEARCH_CONTACTS_SCHEMA = {
    "name": "ghl_search_contacts",
    "description": "Search GoHighLevel contacts by email or name. Use POST /contacts/search with query and pageLimit.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Email or name to search for."},
            "pageLimit": {"type": "integer", "description": "How many to return. Default 20."},
        },
        "required": ["query"],
    },
}

GHL_CREATE_CONTACT_SCHEMA = {
    "name": "ghl_create_contact",
    "description": "Create a GoHighLevel contact. Provide firstName, email or phone.",
    "parameters": {
        "type": "object",
        "properties": {
            "firstName": {"type": "string"},
            "lastName": {"type": "string"},
            "email": {"type": "string"},
            "phone": {"type": "string"},
            "tags": {"type": "string", "description": "Comma separated tags."},
            "companyName": {"type": "string"},
        },
        "required": [],
    },
}

GHL_UPDATE_CONTACT_SCHEMA = {
    "name": "ghl_update_contact",
    "description": "Update a GoHighLevel contact. Do not include locationId in the body (returns 422).",
    "parameters": {
        "type": "object",
        "properties": {
            "contact_id": {"type": "string"},
            "firstName": {"type": "string"},
            "lastName": {"type": "string"},
            "email": {"type": "string"},
            "phone": {"type": "string"},
            "tags": {"type": "string", "description": "Comma separated tags."},
            "companyName": {"type": "string"},
        },
        "required": ["contact_id"],
    },
}

GHL_DELETE_CONTACT_SCHEMA = {
    "name": "ghl_delete_contact",
    "description": "Delete a GoHighLevel contact by ID.",
    "parameters": {
        "type": "object",
        "properties": {
            "contact_id": {"type": "string"},
        },
        "required": ["contact_id"],
    },
}

GHL_SEND_SMS_SCHEMA = {
    "name": "ghl_send_sms",
    "description": "Send an SMS via GoHighLevel conversations. Provide contact_id, or the phone number of an existing contact, plus message. Asks the person for approval first.",
    "parameters": {
        "type": "object",
        "properties": {
            "contact_id": {"type": "string"},
            "phone": {"type": "string"},
            "message": {"type": "string"},
        },
        "required": ["message"],
    },
}

GHL_SEND_EMAIL_SCHEMA = {
    "name": "ghl_send_email",
    "description": "Send an email via GoHighLevel conversations. Provide contact_id, or the email of an existing contact, plus subject and html body. Asks the person for approval first.",
    "parameters": {
        "type": "object",
        "properties": {
            "contact_id": {"type": "string"},
            "email": {"type": "string"},
            "subject": {"type": "string", "description": "Subject line. Defaults to the business name when one is configured."},
            "html": {"type": "string", "description": "HTML email body or plain text."},
        },
        "required": ["html"],
    },
}

GHL_LIST_PIPELINES_SCHEMA = {
    "name": "ghl_list_pipelines",
    "description": "List GoHighLevel pipelines for a location.",
    "parameters": {
        "type": "object",
        "properties": {
        },
        "required": [],
    },
}

GHL_SEARCH_OPPORTUNITIES_SCHEMA = {
    "name": "ghl_search_opportunities",
    "description": "Search GoHighLevel opportunities or pipeline deals.",
    "parameters": {
        "type": "object",
        "properties": {
            "pipeline_id": {"type": "string"},
            "contact_id": {"type": "string"},
            "limit": {"type": "integer"},
        },
        "required": [],
    },
}

GHL_MOVE_OPPORTUNITY_SCHEMA = {
    "name": "ghl_move_opportunity",
    "description": "Move an opportunity to a new pipeline stage. Provide opportunity_id plus stage_id (a pipeline stage id) or status.",
    "parameters": {
        "type": "object",
        "properties": {
            "opportunity_id": {"type": "string"},
            "stage_id": {"type": "string"},
            "status": {"type": "string"},
        },
        "required": ["opportunity_id"],
    },
}

GHL_LIST_WORKFLOWS_SCHEMA = {
    "name": "ghl_list_workflows",
    "description": "List GoHighLevel workflows for a location.",
    "parameters": {
        "type": "object",
        "properties": {
        },
        "required": [],
    },
}

GHL_ENROLL_WORKFLOW_SCHEMA = {
    "name": "ghl_enroll_workflow",
    "description": "Enroll a contact into a GoHighLevel workflow. Uses POST /contacts/{contactId}/workflow/{workflowId} with {}.",
    "parameters": {
        "type": "object",
        "properties": {
            "contact_id": {"type": "string"},
            "workflow_id": {"type": "string"},
        },
        "required": ["contact_id", "workflow_id"],
    },
}


TOOLS = (
    (GHL_LIST_CONTACTS_SCHEMA, _handle_list_contacts),
    (GHL_SEARCH_CONTACTS_SCHEMA, _handle_search_contacts),
    (GHL_CREATE_CONTACT_SCHEMA, _handle_create_contact),
    (GHL_UPDATE_CONTACT_SCHEMA, _handle_update_contact),
    (GHL_DELETE_CONTACT_SCHEMA, _handle_delete_contact),
    (GHL_SEND_SMS_SCHEMA, _handle_send_sms),
    (GHL_SEND_EMAIL_SCHEMA, _handle_send_email),
    (GHL_LIST_PIPELINES_SCHEMA, _handle_list_pipelines),
    (GHL_SEARCH_OPPORTUNITIES_SCHEMA, _handle_search_opportunities),
    (GHL_MOVE_OPPORTUNITY_SCHEMA, _handle_move_opportunity),
    (GHL_LIST_WORKFLOWS_SCHEMA, _handle_list_workflows),
    (GHL_ENROLL_WORKFLOW_SCHEMA, _handle_enroll_workflow),
)


def register_tools(ctx) -> None:
    """Register the ghl-command tools and the approval hook via the plugin API."""
    ctx.register_hook("pre_tool_call", pre_tool_call)
    for schema, handler in TOOLS:
        ctx.register_tool(
            name=schema["name"],
            toolset=TOOLSET,
            schema=schema,
            handler=handler,
            check_fn=_check_ghl_available,
            emoji="📈",
        )
