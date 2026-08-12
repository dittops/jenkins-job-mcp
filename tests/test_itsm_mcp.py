"""Tests for the ITSM policy, payload construction, and the update flow."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from itsm_mcp import server
from itsm_mcp.client import (
    ItsmClient,
    build_update_payload,
    redact,
    summarise_request,
)
from itsm_mcp.config import (
    NotePolicy,
    Policy,
    Settings,
    load_policy,
    load_settings,
)
from itsm_mcp.errors import (
    ConfigError,
    ItsmAPIError,
    ItsmAuthError,
    ItsmConnectionError,
    ParameterError,
    TicketNotFound,
)

POLICY = {
    "groups": [
        {"name": "ICCM Tools", "description": "ICCM tooling queue"},
        "Network Ops",
    ],
    "notes": {
        "max_length": 100,
        # Explicitly all-off, so fixture-based tests also prove a policy file
        "defaults": {
            "show_to_requester": False,
            "mark_first_response": False,
            "add_to_linked_requests": False,
        },
        "allowed": {"show_to_requester": True, "add_to_linked_requests": True},
    },
}

ALL_OFF = {
    "show_to_requester": False,
    "mark_first_response": False,
    "add_to_linked_requests": False,
}

# What an unconfigured policy applies: customer-visible and propagated to
# linked tickets, but not claiming the SLA first-response.
STOCK_DEFAULTS = {
    "show_to_requester": True,
    "mark_first_response": False,
    "add_to_linked_requests": True,
}


def write_policy(tmp_path: Path, data=None) -> Policy:
    path = tmp_path / "itsm.json"
    path.write_text(json.dumps(data if data is not None else POLICY))
    return load_policy(path)


def make_settings(policy: Policy | None = None) -> Settings:
    return Settings(
        base_url="https://itsm.test",
        authtoken="secret-token",
        policy=policy if policy is not None else Policy(),
        portal_id="1",
    )


def make_client(cfg: Settings, handler) -> ItsmClient:
    api = ItsmClient(cfg)
    api._client = httpx.AsyncClient(
        base_url=cfg.base_url,
        headers=cfg.headers,
        transport=httpx.MockTransport(handler),
    )
    return api


def ok_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "response_status": {"status_code": 2000, "status": "success"},
            "request": {
                "id": "2977634",
                "subject": "Printer offline",
                "status": {"name": "Open"},
                "group": {"name": "ICCM Tools"},
                "technician": {"name": "A Tech"},
            },
        },
    )


# --- policy ---------------------------------------------------------------


def test_unlisted_group_passes_through_verbatim(tmp_path):
    """The configured list is a hint, not a gate — ITSM decides what exists."""
    policy = write_policy(tmp_path)
    assert policy.group("ICCM Database").name == "ICCM Database"
    assert policy.group("Payroll").name == "Payroll"


def test_group_match_is_case_insensitive_but_sends_configured_spelling(tmp_path):
    policy = write_policy(tmp_path)
    assert policy.group("iccm tools").name == "ICCM Tools"
    assert policy.group("  NETWORK OPS ").name == "Network Ops"


def test_unconfigured_policy_accepts_any_group():
    assert Policy().group("Anything").name == "Anything"


def test_unlisted_group_keeps_the_caller_s_spelling(tmp_path):
    assert write_policy(tmp_path).group("  iccm database  ").name == "iccm database"


def test_blank_group_is_rejected(tmp_path):
    with pytest.raises(ParameterError):
        write_policy(tmp_path).group("   ")


def test_note_must_be_non_empty_and_within_limit(tmp_path):
    policy = write_policy(tmp_path)
    assert policy.validate_note("  hello  ") == "hello"
    with pytest.raises(ParameterError):
        policy.validate_note("   ")
    with pytest.raises(ParameterError) as exc:
        policy.validate_note("x" * 101)
    assert "100" in str(exc.value)


def test_note_flags_default_to_customer_visible_and_propagated():
    assert Policy().resolve_note_flags(None, None, None) == STOCK_DEFAULTS


def test_policy_file_overrides_the_stock_defaults(tmp_path):
    assert write_policy(tmp_path).resolve_note_flags(None, None, None) == ALL_OFF


def test_note_flags_honour_explicit_values():
    resolved = Policy().resolve_note_flags(True, False, True)
    assert resolved == {
        "show_to_requester": True,
        "mark_first_response": False,
        "add_to_linked_requests": True,
    }


def test_policy_can_forbid_customer_visible_notes():
    policy = Policy(
        notes=NotePolicy(
            allow_show_to_requester=False, default_show_to_requester=False
        )
    )
    with pytest.raises(ParameterError) as exc:
        policy.resolve_note_flags(True, None, None)
    assert "show_to_requester" in str(exc.value)


def test_policy_can_forbid_linked_request_fanout():
    policy = Policy(
        notes=NotePolicy(
            allow_add_to_linked_requests=False, default_add_to_linked_requests=False
        )
    )
    with pytest.raises(ParameterError):
        policy.resolve_note_flags(None, None, True)


def test_configured_default_applies_when_caller_is_silent():
    policy = Policy(notes=NotePolicy(default_show_to_requester=False))
    assert policy.resolve_note_flags(None, None, None)["show_to_requester"] is False


def test_caller_can_make_a_note_internal():
    assert Policy().resolve_note_flags(False, None, False) == ALL_OFF


# --- policy file parsing --------------------------------------------------


def test_omitted_notes_block_uses_the_stock_defaults(tmp_path):
    policy = write_policy(tmp_path, {"groups": ["Ops"]})
    assert policy.resolve_note_flags(None, None, None) == STOCK_DEFAULTS


def test_closing_a_gate_also_turns_its_default_off(tmp_path):
    """Otherwise the default would be one the gate rejects on every call."""
    policy = write_policy(
        tmp_path, {"notes": {"allowed": {"show_to_requester": False}}}
    )
    flags = policy.resolve_note_flags(None, None, None)
    assert flags["show_to_requester"] is False
    assert flags["add_to_linked_requests"] is True  # untouched gate stays open
    with pytest.raises(ParameterError):
        policy.resolve_note_flags(True, None, None)


def test_a_default_its_gate_forbids_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError) as exc:
        write_policy(
            tmp_path,
            {
                "notes": {
                    "defaults": {"show_to_requester": True},
                    "allowed": {"show_to_requester": False},
                }
            },
        )
    assert "every note would be rejected" in str(exc.value)


def test_duplicate_group_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError) as exc:
        write_policy(tmp_path, {"groups": ["Ops", "ops"]})
    assert "more than once" in str(exc.value)


def test_bad_policy_shapes_are_rejected(tmp_path):
    with pytest.raises(ConfigError):
        write_policy(tmp_path, {"groups": "Ops"})
    with pytest.raises(ConfigError):
        write_policy(tmp_path, {"groups": [{"description": "no name"}]})
    with pytest.raises(ConfigError):
        write_policy(tmp_path, {"notes": {"max_length": 0}})
    with pytest.raises(ConfigError):
        write_policy(tmp_path, {"notes": {"allowed": {"show_to_requester": "yes"}}})


def test_invalid_json_names_the_file(tmp_path):
    path = tmp_path / "itsm.json"
    path.write_text("{not json")
    with pytest.raises(ConfigError) as exc:
        load_policy(path)
    assert str(path) in str(exc.value)


def test_missing_policy_defaults_to_unrestricted(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ITSM_CONFIG", raising=False)
    monkeypatch.setenv("ITSM_URL", "https://itsm.test/")
    monkeypatch.setenv("ITSM_AUTHTOKEN", "t")
    cfg = load_settings()
    assert cfg.policy.groups == ()
    assert cfg.base_url == "https://itsm.test"  # trailing slash trimmed
    assert cfg.portal_id == "1"


def test_explicitly_configured_missing_policy_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ITSM_URL", "https://itsm.test")
    monkeypatch.setenv("ITSM_AUTHTOKEN", "t")
    monkeypatch.setenv("ITSM_CONFIG", str(tmp_path / "absent.json"))
    with pytest.raises(ConfigError) as exc:
        load_settings()
    assert "absent.json" in str(exc.value)


def test_required_env_is_checked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ITSM_URL", raising=False)
    monkeypatch.delenv("ITSM_AUTHTOKEN", raising=False)
    with pytest.raises(ConfigError) as exc:
        load_settings()
    assert "ITSM_URL" in str(exc.value)

    monkeypatch.setenv("ITSM_URL", "https://itsm.test")
    with pytest.raises(ConfigError) as exc:
        load_settings()
    assert "ITSM_AUTHTOKEN" in str(exc.value)


def test_dotenv_loads_but_real_env_wins(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "ITSM_URL=https://file.test\nITSM_AUTHTOKEN=file-token\n"
    )
    monkeypatch.delenv("ITSM_URL", raising=False)
    monkeypatch.setenv("ITSM_AUTHTOKEN", "env-token")
    cfg = load_settings()
    assert cfg.base_url == "https://file.test"
    assert cfg.authtoken == "env-token"


# --- payload --------------------------------------------------------------


def test_payload_matches_the_documented_shape():
    payload = build_update_payload(
        "ICCM Tools",
        "Stop changing the status to pending in ICCMFDM",
        {
            "show_to_requester": True,
            "mark_first_response": False,
            "add_to_linked_requests": True,
        },
    )
    assert payload == {
        "request": {
            "group": {"name": "ICCM Tools"},
            "note_comments": {
                "description": "Stop changing the status to pending in ICCMFDM",
                "show_to_requester": True,
                "mark_first_response": False,
                "add_to_linked_requests": True,
            },
        }
    }


def test_payload_omits_the_half_that_was_not_asked_for():
    assert build_update_payload("Network Ops", None, ALL_OFF) == {
        "request": {"group": {"name": "Network Ops"}}
    }
    assert "group" not in build_update_payload(None, "just a note", ALL_OFF)["request"]


def test_redact_strips_credentials_and_query():
    assert redact("https://u:p@10.10.146.120/api/v3/requests/1?authtoken=x") == (
        "https://10.10.146.120/api/v3/requests/1"
    )


def test_summarise_tolerates_a_bodyless_response():
    summary = summarise_request(42, None)
    assert summary.request_id == 42
    assert summary.subject is None and summary.group is None


# --- client ---------------------------------------------------------------


async def test_update_sends_form_encoded_input_data_and_headers():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["authtoken"] = request.headers.get("authtoken")
        seen["portal"] = request.headers.get("PORTALID")
        seen["content_type"] = request.headers.get("content-type")
        seen["body"] = parse_qs(request.content.decode())
        return ok_response(request)

    cfg = make_settings()
    api = make_client(cfg, handler)
    payload = build_update_payload("ICCM Tools", "note text", ALL_OFF)
    summary, messages = await api.update_request(2977634, payload)

    assert seen["method"] == "PUT"
    assert seen["path"] == "/api/v3/requests/2977634"
    assert seen["authtoken"] == "secret-token"
    assert seen["portal"] == "1"
    assert "application/x-www-form-urlencoded" in seen["content_type"]
    assert json.loads(seen["body"]["input_data"][0]) == payload
    assert summary.subject == "Printer offline"
    assert summary.group == "ICCM Tools"
    assert messages == []


async def test_http_401_becomes_an_auth_error():
    def handler(request):
        return httpx.Response(401, json={})

    api = make_client(make_settings(), handler)
    with pytest.raises(ItsmAuthError):
        await api.update_request(1, {"request": {}})


async def test_http_404_becomes_ticket_not_found():
    def handler(request):
        return httpx.Response(404, json={})

    api = make_client(make_settings(), handler)
    with pytest.raises(TicketNotFound) as exc:
        await api.update_request(999, {"request": {}})
    assert "999" in str(exc.value)


async def test_http_400_surfaces_the_server_messages():
    def handler(request):
        return httpx.Response(
            400,
            json={
                "response_status": {
                    "status_code": 4000,
                    "status": "failed",
                    "messages": [{"field": "group", "message": "Invalid group"}],
                }
            },
        )

    api = make_client(make_settings(), handler)
    with pytest.raises(ItsmAPIError) as exc:
        await api.update_request(1, {"request": {}})
    assert "group: Invalid group" in str(exc.value)


async def test_200_with_a_failure_envelope_is_not_success():
    """The v3 API reports rejected field values inside an HTTP 200."""

    def handler(request):
        return httpx.Response(
            200,
            json={
                "response_status": [
                    {
                        "status_code": 4000,
                        "status": "failed",
                        "messages": [{"message": "Request is closed"}],
                    }
                ]
            },
        )

    api = make_client(make_settings(), handler)
    with pytest.raises(ItsmAPIError) as exc:
        await api.update_request(1, {"request": {}})
    assert "Request is closed" in str(exc.value)


async def test_200_without_an_envelope_is_taken_at_its_word():
    def handler(request):
        return httpx.Response(200, text="OK")

    api = make_client(make_settings(), handler)
    summary, messages = await api.update_request(7, {"request": {}})
    assert summary.request_id == 7 and messages == []


async def test_unreachable_host_becomes_a_typed_error():
    def handler(request):
        raise httpx.ConnectTimeout("")

    api = make_client(make_settings(), handler)
    with pytest.raises(ItsmConnectionError) as exc:
        await api.update_request(1, {"request": {}})
    assert "itsm.test" in str(exc.value)


# --- tools ----------------------------------------------------------------


def bind(monkeypatch, cfg: Settings, api: ItsmClient | None = None) -> None:
    monkeypatch.setattr(server, "_settings", cfg)
    monkeypatch.setattr(server, "_client", api)


def test_list_groups_reports_the_allowlist_and_policy(tmp_path, monkeypatch):
    cfg = make_settings(write_policy(tmp_path))
    bind(monkeypatch, cfg)
    result = server.list_itsm_groups()
    assert result["ok"] is True
    assert [g["name"] for g in result["groups"]] == ["ICCM Tools", "Network Ops"]
    assert result["note_policy"]["max_length"] == 100
    assert result["itsm_url"] == "https://itsm.test"


async def test_update_applies_group_and_note(tmp_path, monkeypatch):
    seen: dict = {}

    def handler(request):
        seen["body"] = json.loads(parse_qs(request.content.decode())["input_data"][0])
        return ok_response(request)

    cfg = make_settings(write_policy(tmp_path))
    bind(monkeypatch, cfg, make_client(cfg, handler))

    result = await server.update_itsm_ticket(
        request_id=2977634, group="iccm tools", note="Reassigning for triage"
    )

    assert result["ok"] is True
    assert result["applied"]["group"] == "ICCM Tools"
    assert result["applied"]["note_added"] is True
    assert result["applied"]["note_flags"] == ALL_OFF
    assert result["ticket"]["subject"] == "Printer offline"
    assert "2977634" in result["url"]
    assert seen["body"]["request"]["group"] == {"name": "ICCM Tools"}


async def test_update_needs_something_to_write(tmp_path, monkeypatch):
    cfg = make_settings(write_policy(tmp_path))
    bind(monkeypatch, cfg)
    result = await server.update_itsm_ticket(request_id=1)
    assert result["ok"] is False
    assert result["error_type"] == "ParameterError"


async def test_note_flags_without_a_note_are_refused(tmp_path, monkeypatch):
    cfg = make_settings(write_policy(tmp_path))
    bind(monkeypatch, cfg)
    result = await server.update_itsm_ticket(
        request_id=1, group="ICCM Tools", show_to_requester=True
    )
    assert result["ok"] is False
    assert "only apply to a note" in result["error"]


@pytest.mark.parametrize("bad", [0, -5, "abc", None, 1.5])
async def test_bad_request_ids_are_refused_before_any_write(bad, tmp_path, monkeypatch):
    def handler(request):  # pragma: no cover - must never be reached
        raise AssertionError("a write was attempted with an invalid id")

    cfg = make_settings(write_policy(tmp_path))
    bind(monkeypatch, cfg, make_client(cfg, handler))
    result = await server.update_itsm_ticket(request_id=bad, note="x")
    assert result["ok"] is False
    assert result["error_type"] == "ParameterError"


async def test_unlisted_group_is_forwarded_to_itsm(tmp_path, monkeypatch):
    """Groups the policy has never heard of must still reach the wire."""
    seen: dict = {}

    def handler(request):
        seen["body"] = json.loads(parse_qs(request.content.decode())["input_data"][0])
        return ok_response(request)

    cfg = make_settings(write_policy(tmp_path))
    bind(monkeypatch, cfg, make_client(cfg, handler))

    result = await server.update_itsm_ticket(request_id=1, group="ICCM Database")

    assert result["ok"] is True
    assert result["applied"]["group"] == "ICCM Database"
    assert seen["body"]["request"]["group"] == {"name": "ICCM Database"}


async def test_blank_group_is_still_refused(tmp_path, monkeypatch):
    """Dropping the allowlist does not mean sending a malformed assignment."""

    def handler(request):  # pragma: no cover - must never be reached
        raise AssertionError("a write was attempted with a blank group")

    cfg = make_settings(write_policy(tmp_path))
    bind(monkeypatch, cfg, make_client(cfg, handler))
    result = await server.update_itsm_ticket(request_id=1, group="   ")
    assert result["ok"] is False
    assert result["error_type"] == "ParameterError"


async def test_api_failure_is_returned_not_raised(tmp_path, monkeypatch):
    def handler(request):
        return httpx.Response(404, json={})

    cfg = make_settings(write_policy(tmp_path))
    bind(monkeypatch, cfg, make_client(cfg, handler))
    result = await server.update_itsm_ticket(request_id=42, note="hello")
    assert result["ok"] is False
    assert result["error_type"] == "TicketNotFound"


async def test_unexpected_exception_is_never_blank(tmp_path, monkeypatch):
    def handler(request):
        raise RuntimeError("")

    cfg = make_settings(write_policy(tmp_path))
    bind(monkeypatch, cfg, make_client(cfg, handler))
    result = await server.update_itsm_ticket(request_id=1, note="hello")
    assert result["ok"] is False
    assert result["error"].strip() != "Unexpected error:"
    assert "RuntimeError" in result["error"]
