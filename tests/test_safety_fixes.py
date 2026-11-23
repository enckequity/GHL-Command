"""Focused tests for the subject, approval, location and contactId fixes.

HTTP is mocked at urllib.request.urlopen and the secret scope is a fake
agent.secret_scope module, so nothing leaves the machine.
"""

import importlib.util
import io
import json
import sys
import types
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

LOC = "LocConfigured01"
OTHER = "LocOtherAgency9"


def _load():
    plugin_dir = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("px_ghl_tools_fixes", plugin_dir / "tools.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def secrets(monkeypatch):
    values = {"GHL_API_KEY": "pit-test", "GHL_LOCATION_ID": LOC}
    agent = types.ModuleType("agent")
    scope = types.ModuleType("agent.secret_scope")
    scope.get_secret = lambda name, default=None: values.get(name, default)
    agent.secret_scope = scope
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", scope)
    return values


class _Resp:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def http(monkeypatch):
    """Record every request; answer from a route table keyed by (method, path)."""
    calls = []
    routes = {}

    def fake_urlopen(req, timeout=None):
        url = urllib.parse.urlsplit(req.full_url)
        body = json.loads(req.data.decode("utf-8")) if req.data else None
        calls.append({"method": req.get_method(), "path": url.path,
                      "query": dict(urllib.parse.parse_qsl(url.query)), "body": body})
        key = (req.get_method(), url.path)
        if key not in routes:
            raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, io.BytesIO(b'{"msg":"nf"}'))
        payload = routes[key]
        return _Resp(payload(calls[-1]) if callable(payload) else payload)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return types.SimpleNamespace(calls=calls, routes=routes)


def _ok(out):
    data = json.loads(out)
    assert "error" not in data, data
    return data["result"]


def _err(out):
    data = json.loads(out)
    assert "error" in data, data
    return data["error"]


def _posts(http):
    return [c for c in http.calls if c["method"] == "POST" and c["path"] == "/conversations/messages"]


# --- 1. No engine name in the email subject ----------------------------------

def test_no_engine_default_subject_in_source():
    src = (Path(__file__).resolve().parent.parent / "tools.py").read_text(encoding="utf-8")
    assert "Message from Hermes" not in src
    assert "hermes" not in src.lower()


def test_no_engine_name_in_user_visible_text():
    root = Path(__file__).resolve().parent.parent
    assert "hermes" not in (root / "__init__.py").read_text(encoding="utf-8").lower()
    manifest = (root / "plugin.yaml").read_text(encoding="utf-8").lower()
    manifest = manifest.replace("requires_hermes", "")
    assert "hermes" not in manifest
    mod = _load()
    for schema, _h in mod.TOOLS:
        assert "hermes" not in json.dumps(schema).lower()


def test_email_without_subject_uses_business_name(secrets, http):
    secrets["GHL_BUSINESS_NAME"] = "Acme Plumbing"
    http.routes[("GET", "/contacts/c1")] = {"contact": {"id": "c1", "locationId": LOC}}
    http.routes[("POST", "/conversations/messages")] = {"messageId": "m1"}
    mod = _load()
    res = _ok(mod._handle_send_email({"contact_id": "c1", "html": "<p>Hi</p>"}))
    sent = _posts(http)[0]["body"]
    assert sent["subject"] == "Acme Plumbing" == res["subject"]
    assert sent["html"] == "<p>Hi</p>"
    assert "htmlBody" not in sent


def test_email_without_subject_or_business_name_is_refused(secrets, http):
    mod = _load()
    err = _err(mod._handle_send_email({"contact_id": "c1", "html": "Hi"}))
    assert "subject" in err
    assert http.calls == []
    verdict = mod.pre_tool_call(tool_name="ghl_send_email", args={"contact_id": "c1", "html": "Hi"})
    assert verdict["action"] == "block" and "subject" in verdict["message"]


def test_explicit_subject_wins(secrets, http):
    secrets["GHL_BUSINESS_NAME"] = "Acme Plumbing"
    http.routes[("GET", "/contacts/c1")] = {"contact": {"id": "c1", "locationId": LOC}}
    http.routes[("POST", "/conversations/messages")] = {"messageId": "m1"}
    mod = _load()
    _ok(mod._handle_send_email({"contact_id": "c1", "subject": "Your quote", "html": "Hi"}))
    assert _posts(http)[0]["body"]["subject"] == "Your quote"


# --- 2. Approval preview and per-send rule keys ------------------------------

def test_email_approval_shows_body_excerpt_and_subject(secrets):
    mod = _load()
    body = "<p>Hello Sam,</p><p>Your &amp; quote is " + "x" * 400 + "</p><script>evil()</script>"
    v = mod.pre_tool_call(tool_name="ghl_send_email",
                          args={"email": "sam@example.com", "subject": "Quote", "html": body})
    assert v["action"] == "approve"
    msg = v["message"]
    assert "sam@example.com" in msg and "Subject: Quote" in msg
    assert "Hello Sam," in msg and "Your & quote is" in msg
    assert "<p>" not in msg and "evil" not in msg
    excerpt = msg.split("\n\n", 1)[1]
    assert len(excerpt) <= mod.PREVIEW_CHARS + 3 and excerpt.endswith("...")


def test_email_approval_shows_business_name_subject(secrets):
    secrets["GHL_BUSINESS_NAME"] = "Acme Plumbing"
    mod = _load()
    v = mod.pre_tool_call(tool_name="ghl_send_email", args={"contact_id": "c1", "html": "Hi"})
    assert v["action"] == "approve" and "Subject: Acme Plumbing" in v["message"]


def test_sms_approval_shows_message(secrets):
    mod = _load()
    v = mod.pre_tool_call(tool_name="ghl_send_sms", args={"phone": "+14155550123", "message": "See you at 3"})
    assert "+14155550123" in v["message"] and "See you at 3" in v["message"]


@pytest.mark.parametrize(
    "tool_name,a,b",
    [
        ("ghl_send_email", {"contact_id": "c1", "subject": "S", "html": "one"},
         {"contact_id": "c1", "subject": "S", "html": "two"}),
        ("ghl_send_email", {"contact_id": "c1", "subject": "S", "html": "one"},
         {"contact_id": "c2", "subject": "S", "html": "one"}),
        ("ghl_send_email", {"contact_id": "c1", "subject": "S", "html": "one"},
         {"contact_id": "c1", "subject": "T", "html": "one"}),
        ("ghl_send_sms", {"contact_id": "c1", "message": "one"}, {"contact_id": "c1", "message": "two"}),
        ("ghl_send_sms", {"phone": "+1555", "message": "one"}, {"phone": "+1556", "message": "one"}),
        ("ghl_delete_contact", {"contact_id": "c1"}, {"contact_id": "c2"}),
        ("ghl_enroll_workflow", {"contact_id": "c1", "workflow_id": "w1"},
         {"contact_id": "c1", "workflow_id": "w2"}),
    ],
)
def test_each_distinct_action_gets_its_own_rule_key(secrets, tool_name, a, b):
    mod = _load()
    ka = mod.pre_tool_call(tool_name=tool_name, args=a)["rule_key"]
    kb = mod.pre_tool_call(tool_name=tool_name, args=b)["rule_key"]
    assert ka != kb
    assert ka.startswith("ghl-command:" + tool_name + ":")
    assert ka == mod.pre_tool_call(tool_name=tool_name, args=dict(a))["rule_key"]


def test_engine_directive_shape_is_native(secrets):
    """The engine reads action/message/rule_key only (hermes_cli.plugins, v0.23.3)."""
    mod = _load()
    v = mod.pre_tool_call(tool_name="ghl_send_sms", args={"contact_id": "c1", "message": "hi"})
    assert set(v) == {"action", "message", "rule_key"}
    assert all(isinstance(x, str) and x for x in v.values())


# --- 3. Location limited to the configured one -------------------------------

def test_other_location_is_refused_by_handler(secrets, http):
    mod = _load()
    for handler, args in [
        (mod._handle_list_contacts, {"location_id": OTHER}),
        (mod._handle_search_contacts, {"query": "a", "locationId": OTHER}),
        (mod._handle_create_contact, {"firstName": "A", "location_id": OTHER}),
        (mod._handle_delete_contact, {"contact_id": "c1", "location_id": OTHER}),
        (mod._handle_send_sms, {"contact_id": "c1", "message": "hi", "location_id": OTHER}),
        (mod._handle_send_email, {"contact_id": "c1", "subject": "s", "html": "hi", "location_id": OTHER}),
        (mod._handle_list_pipelines, {"location_id": OTHER}),
        (mod._handle_search_opportunities, {"location_id": OTHER}),
        (mod._handle_list_workflows, {"location_id": OTHER}),
    ]:
        assert "not the configured" in _err(handler(args))
    assert http.calls == []


def test_other_location_is_blocked_by_hook_before_approval(secrets):
    mod = _load()
    for tool in ("ghl_list_contacts", "ghl_send_sms"):
        v = mod.pre_tool_call(tool_name=tool, args={"location_id": OTHER, "contact_id": "c1", "message": "x"})
        assert v["action"] == "block" and "not the configured" in v["message"]


def test_same_location_is_allowed(secrets, http):
    http.routes[("GET", "/contacts/")] = {"contacts": []}
    mod = _load()
    assert mod.pre_tool_call(tool_name="ghl_list_contacts", args={"location_id": LOC}) is None
    _ok(mod._handle_list_contacts({"location_id": LOC}))
    assert http.calls[0]["query"]["locationId"] == LOC


def test_api_refuses_other_location(secrets, http):
    mod = _load()
    with pytest.raises(ValueError):
        mod._api("GET", "/contacts/", location_id=OTHER)
    assert http.calls == []


@pytest.mark.parametrize("handler_name,args", [
    ("_handle_update_contact", {"contact_id": "c9", "firstName": "X"}),
    ("_handle_delete_contact", {"contact_id": "c9"}),
    ("_handle_send_sms", {"contact_id": "c9", "message": "hi"}),
    ("_handle_send_email", {"contact_id": "c9", "subject": "s", "html": "hi"}),
    ("_handle_enroll_workflow", {"contact_id": "c9", "workflow_id": "w1"}),
])
def test_contact_from_another_location_is_not_touched(secrets, http, handler_name, args):
    http.routes[("GET", "/contacts/c9")] = {"contact": {"id": "c9", "locationId": OTHER}}
    mod = _load()
    assert "not in the configured" in _err(getattr(mod, handler_name)(args))
    assert [c["method"] for c in http.calls] == ["GET"]


def test_opportunity_from_another_location_is_not_moved(secrets, http):
    http.routes[("GET", "/opportunities/o9")] = {"opportunity": {"id": "o9", "locationId": OTHER}}
    mod = _load()
    assert "not in the configured" in _err(mod._handle_move_opportunity({"opportunity_id": "o9", "stage_id": "s1"}))
    assert [c["method"] for c in http.calls] == ["GET"]


def test_opportunity_in_location_moves_with_pipeline_stage_id(secrets, http):
    http.routes[("GET", "/opportunities/o1")] = {"opportunity": {"id": "o1", "locationId": LOC}}
    http.routes[("PUT", "/opportunities/o1")] = {"opportunity": {"id": "o1"}}
    mod = _load()
    _ok(mod._handle_move_opportunity({"opportunity_id": "o1", "stage_id": "s1"}))
    assert http.calls[-1]["body"] == {"pipelineStageId": "s1"}


def test_opportunity_search_sends_snake_case_location(secrets, http):
    http.routes[("GET", "/opportunities/search")] = {"opportunities": []}
    mod = _load()
    _ok(mod._handle_search_opportunities({}))
    q = http.calls[0]["query"]
    assert q["location_id"] == LOC and "locationId" not in q


# --- 4. SMS by phone sends the resolved contactId ----------------------------

def test_sms_by_phone_resolves_contact_id(secrets, http):
    http.routes[("GET", "/contacts/search/duplicate")] = {"contact": {"id": "c42", "locationId": LOC}}
    http.routes[("POST", "/conversations/messages")] = {"messageId": "m1"}
    mod = _load()
    res = _ok(mod._handle_send_sms({"phone": "+14155550123", "message": "On my way"}))
    lookup = http.calls[0]
    assert lookup["path"] == "/contacts/search/duplicate"
    assert lookup["query"] == {"locationId": LOC, "number": "+14155550123"}
    sent = _posts(http)[0]["body"]
    assert sent["contactId"] == "c42" and sent["type"] == "SMS" and sent["message"] == "On my way"
    assert "phone" not in sent
    assert res["contact_id"] == "c42" and res["message_id"] == "m1"


def test_sms_by_unknown_phone_sends_nothing(secrets, http):
    http.routes[("GET", "/contacts/search/duplicate")] = {"contact": None}
    mod = _load()
    assert "No GoHighLevel contact" in _err(mod._handle_send_sms({"phone": "+14155550123", "message": "hi"}))
    assert _posts(http) == []


def test_sms_by_phone_matching_contact_in_other_location_sends_nothing(secrets, http):
    http.routes[("GET", "/contacts/search/duplicate")] = {"contact": {"id": "c42", "locationId": OTHER}}
    mod = _load()
    assert "not in the configured" in _err(mod._handle_send_sms({"phone": "+14155550123", "message": "hi"}))
    assert _posts(http) == []


def test_email_by_address_resolves_contact_id(secrets, http):
    http.routes[("GET", "/contacts/search/duplicate")] = {"contact": {"id": "c7", "locationId": LOC}}
    http.routes[("POST", "/conversations/messages")] = {"messageId": "m2"}
    mod = _load()
    _ok(mod._handle_send_email({"email": "sam@example.com", "subject": "s", "html": "hi"}))
    assert http.calls[0]["query"] == {"locationId": LOC, "email": "sam@example.com"}
    assert _posts(http)[0]["body"]["contactId"] == "c7"


def test_sms_by_contact_id_checks_location_then_sends(secrets, http):
    http.routes[("GET", "/contacts/c1")] = {"contact": {"id": "c1", "locationId": LOC}}
    http.routes[("POST", "/conversations/messages")] = {"messageId": "m3"}
    mod = _load()
    _ok(mod._handle_send_sms({"contact_id": "c1", "message": "hi"}))
    assert _posts(http)[0]["body"]["contactId"] == "c1"
