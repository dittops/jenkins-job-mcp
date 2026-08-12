"""Thin async wrapper over the ServiceDesk Plus v3 request API.

Knows nothing about MCP. One endpoint is implemented — ``PUT /api/v3/requests/
{id}`` — because that is the whole of the API surface this server was given.

The v3 API is form-encoded with the real payload as a JSON string in a single
``input_data`` field, and reports application-level failures inside a
``response_status`` envelope that can accompany an HTTP 200. Both quirks are
handled here so callers see either a result or a typed error.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import Settings
from .errors import (
    ItsmAPIError,
    ItsmAuthError,
    ItsmConnectionError,
    TicketNotFound,
)

REQUESTS_PATH = "/api/v3/requests"

# ManageEngine's success code. The textual `status` is checked first; this is
# the fallback for responses that carry only the numeric form.
STATUS_CODE_SUCCESS = 2000


def redact(url: str) -> str:
    """Strip any userinfo and query string so URLs are safe to return."""
    parts = urlsplit(url)
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def build_update_payload(
    group: str | None,
    note: str | None,
    note_flags: dict[str, bool],
) -> dict[str, Any]:
    """Assemble the ``input_data`` body for an update.

    Deliberately constructive rather than pass-through: the only keys that can
    ever appear under ``request`` are the ones this function writes, so no
    caller can reach ticket fields the server does not expose (status,
    requester, priority, technician...).
    """
    request: dict[str, Any] = {}

    if group is not None:
        request["group"] = {"name": group}

    if note is not None:
        request["note_comments"] = {
            "description": note,
            "show_to_requester": note_flags["show_to_requester"],
            "mark_first_response": note_flags["mark_first_response"],
            "add_to_linked_requests": note_flags["add_to_linked_requests"],
        }

    return {"request": request}


def _envelope(payload: object) -> tuple[str | None, int | None, list[str]]:
    """Pull (status, status_code, messages) out of ``response_status``.

    The field is a dict on some endpoints and a single-element list on others,
    so both shapes are accepted.
    """
    if not isinstance(payload, dict):
        return None, None, []

    raw = payload.get("response_status")
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if not isinstance(raw, dict):
        return None, None, []

    status = str(raw.get("status") or "").strip().lower() or None

    code = raw.get("status_code")
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None

    messages: list[str] = []
    for item in raw.get("messages") or []:
        if isinstance(item, str):
            text, field = item.strip(), None
        elif isinstance(item, dict):
            text = str(item.get("message") or "").strip()
            field = item.get("field")
        else:
            continue
        if text:
            messages.append(f"{field}: {text}" if field else text)

    return status, code, messages


def _succeeded(status: str | None, code: int | None) -> bool:
    """A response with no envelope at all is taken at its HTTP word."""
    if status is not None:
        return status == "success"
    if code is not None:
        return code == STATUS_CODE_SUCCESS
    return True


@dataclass
class TicketSummary:
    """The handful of fields worth echoing back after a write."""

    request_id: int
    subject: str | None
    status: str | None
    group: str | None
    technician: str | None


def _name_of(value: object) -> str | None:
    """ServiceDesk Plus nests display names one level down: {"name": "..."}."""
    if isinstance(value, dict):
        name = value.get("name")
        return str(name) if name else None
    return str(value) if value else None


def summarise_request(request_id: int, payload: object) -> TicketSummary:
    data = payload.get("request") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        data = {}

    subject = data.get("subject")
    return TicketSummary(
        request_id=request_id,
        subject=str(subject) if subject else None,
        status=_name_of(data.get("status")),
        group=_name_of(data.get("group")),
        technician=_name_of(data.get("technician")),
    )


class ItsmClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.base_url,
            headers=settings.headers,
            verify=settings.verify_ssl,
            trust_env=settings.trust_env,
            timeout=httpx.Timeout(float(settings.timeout_seconds)),
            # The authtoken rides on every request as a header, so a followed
            # redirect would hand it to whatever host the redirect names.
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Single choke point for HTTP, so transport failures become typed
        errors instead of escaping as bare httpx exceptions (several of which,
        like ConnectTimeout, carry an empty message)."""
        try:
            return await self._client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise ItsmConnectionError(
                f"Timed out connecting to ITSM at {redact(self._settings.base_url)}. "
                f"Check ITSM_URL and that the host is reachable from this machine "
                f"(VPN?)."
            ) from exc
        except httpx.HTTPError as exc:
            detail = str(exc) or type(exc).__name__
            raise ItsmConnectionError(
                f"Could not reach ITSM at {redact(self._settings.base_url)}: {detail}"
            ) from exc

    async def update_request(
        self, request_id: int, payload: dict[str, Any]
    ) -> tuple[TicketSummary, list[str]]:
        """PUT an update onto a ticket. Returns (summary, server messages)."""
        resp = await self._request(
            "PUT",
            f"{REQUESTS_PATH}/{request_id}",
            data={"input_data": json.dumps(payload, separators=(",", ":"))},
        )

        try:
            body = resp.json()
        except ValueError:
            body = None
        status, code, messages = _envelope(body)
        detail = f" ({'; '.join(messages)})" if messages else ""

        if resp.status_code in (401, 403):
            raise ItsmAuthError(
                f"ITSM rejected the credentials (HTTP {resp.status_code}){detail}. "
                f"Check ITSM_AUTHTOKEN and that the technician it belongs to may "
                f"edit this request."
            )
        if resp.status_code == 404:
            raise TicketNotFound(f"No ITSM request found with id {request_id}.")
        if resp.status_code >= 400:
            raise ItsmAPIError(
                f"Updating request {request_id} failed with HTTP "
                f"{resp.status_code}{detail}."
            )

        # A 200 with a failure envelope is the common shape for a rejected
        # field value, so an HTTP-only check would report success wrongly.
        if not _succeeded(status, code):
            raise ItsmAPIError(
                f"ITSM rejected the update to request {request_id}"
                f"{detail or f' (status: {status or code})'}."
            )

        return summarise_request(request_id, body), messages
