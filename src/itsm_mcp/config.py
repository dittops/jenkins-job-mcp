"""Environment settings and the write policy.

The ServiceDesk Plus v3 API takes its entire payload as one ``input_data`` JSON
blob, so anything that reaches that blob lands on the ticket — status, priority,
requester, technician, whatever. This server therefore never forwards a
caller-supplied blob: ``client.build_update_payload`` assembles ``input_data``
itself from typed arguments, and this module is what bounds those arguments.

The ``groups`` list is the allowlist, mirroring how the Jenkins server's action
registry works: a group that is not listed cannot be assigned.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError, ParameterError, UnknownGroupError

DEFAULT_PORTAL_ID = "1"

# Long enough for a real hand-off note, short enough that a runaway generation
# cannot paste a novel into a ticket every other operator has to scroll past.
DEFAULT_MAX_NOTE_LENGTH = 5000


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class GroupConfig:
    """One support group a ticket may be assigned to."""

    name: str
    description: str = ""


@dataclass(frozen=True)
class NotePolicy:
    """Bounds on the note this server may append to a ticket.

    The two ``allow_*`` switches gate the flags whose blast radius reaches
    beyond the one ticket: ``show_to_requester`` publishes the note to the
    customer, and ``add_to_linked_requests`` copies it onto every linked
    ticket. Both default to permitted but *off*, so reaching a customer is
    always a deliberate argument rather than a default the model inherits.
    """

    max_length: int = DEFAULT_MAX_NOTE_LENGTH
    default_show_to_requester: bool = False
    default_mark_first_response: bool = False
    default_add_to_linked_requests: bool = False
    allow_show_to_requester: bool = True
    allow_add_to_linked_requests: bool = True


@dataclass(frozen=True)
class Policy:
    """What this server is permitted to write."""

    groups: tuple[GroupConfig, ...] = ()
    notes: NotePolicy = field(default_factory=NotePolicy)

    @property
    def restricts_groups(self) -> bool:
        return bool(self.groups)

    def group(self, name: str) -> GroupConfig:
        """Resolve a group name to its configured entry.

        Matching is case-insensitive and the *configured* spelling is what gets
        sent, so a model that says "iccm tools" still assigns "ICCM Tools". An
        empty allowlist means the policy does not restrict groups, and the name
        passes through as supplied.
        """
        wanted = (name or "").strip()
        if not wanted:
            raise ParameterError("group must be a non-empty group name.")

        if not self.groups:
            return GroupConfig(name=wanted)

        for group in self.groups:
            if group.name.lower() == wanted.lower():
                return group

        known = ", ".join(g.name for g in self.groups)
        raise UnknownGroupError(
            f"Group '{wanted}' is not allowed. Configured groups: {known}. "
            f"Add it to the ITSM policy file if this assignment is intended."
        )

    def validate_note(self, note: str) -> str:
        text = (note or "").strip()
        if not text:
            raise ParameterError("note must be non-empty text.")
        if len(text) > self.notes.max_length:
            raise ParameterError(
                f"note is {len(text)} characters; the configured limit is "
                f"{self.notes.max_length}."
            )
        return text

    def resolve_note_flags(
        self,
        show_to_requester: bool | None,
        mark_first_response: bool | None,
        add_to_linked_requests: bool | None,
    ) -> dict[str, bool]:
        """Apply the configured defaults, then enforce the allow switches."""
        notes = self.notes

        resolved = {
            "show_to_requester": (
                notes.default_show_to_requester
                if show_to_requester is None
                else bool(show_to_requester)
            ),
            "mark_first_response": (
                notes.default_mark_first_response
                if mark_first_response is None
                else bool(mark_first_response)
            ),
            "add_to_linked_requests": (
                notes.default_add_to_linked_requests
                if add_to_linked_requests is None
                else bool(add_to_linked_requests)
            ),
        }

        if resolved["show_to_requester"] and not notes.allow_show_to_requester:
            raise ParameterError(
                "show_to_requester is disabled by policy: this server may not "
                "publish notes to the requester."
            )
        if resolved["add_to_linked_requests"] and not notes.allow_add_to_linked_requests:
            raise ParameterError(
                "add_to_linked_requests is disabled by policy: this server may "
                "not copy notes onto linked tickets."
            )
        return resolved

    def describe(self) -> dict[str, object]:
        return {
            "groups": [
                {"name": g.name, "description": g.description} for g in self.groups
            ],
            "groups_restricted": self.restricts_groups,
            "note_policy": {
                "max_length": self.notes.max_length,
                "defaults": {
                    "show_to_requester": self.notes.default_show_to_requester,
                    "mark_first_response": self.notes.default_mark_first_response,
                    "add_to_linked_requests": self.notes.default_add_to_linked_requests,
                },
                "allowed": {
                    "show_to_requester": self.notes.allow_show_to_requester,
                    "add_to_linked_requests": self.notes.allow_add_to_linked_requests,
                },
            },
        }


@dataclass
class Settings:
    base_url: str
    authtoken: str
    policy: Policy
    portal_id: str = DEFAULT_PORTAL_ID
    verify_ssl: bool = True
    trust_env: bool = False
    timeout_seconds: int = 30

    @property
    def headers(self) -> dict[str, str]:
        return {
            "authtoken": self.authtoken,
            "PORTALID": self.portal_id,
            "Accept": "application/json",
        }


def _parse_groups(raw: object) -> tuple[GroupConfig, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("'groups' must be a list")

    groups: list[GroupConfig] = []
    seen: set[str] = set()
    for item in raw:
        # Shorthand: a bare string is a group name with no description.
        if isinstance(item, str):
            entry = GroupConfig(name=item)
        elif isinstance(item, dict) and "name" in item:
            entry = GroupConfig(
                name=str(item["name"]),
                description=str(item.get("description", "")),
            )
        else:
            raise ConfigError(
                "each group must be a string or an object with a 'name' field"
            )

        if not entry.name.strip():
            raise ConfigError("group names cannot be empty")
        # A duplicate would make the allowlist ambiguous about which spelling wins.
        if entry.name.lower() in seen:
            raise ConfigError(f"group '{entry.name}' is listed more than once")
        seen.add(entry.name.lower())
        groups.append(entry)
    return tuple(groups)


def _parse_notes(raw: object) -> NotePolicy:
    if raw is None:
        return NotePolicy()
    if not isinstance(raw, dict):
        raise ConfigError("'notes' must be an object")

    defaults = raw.get("defaults") or {}
    allowed = raw.get("allowed") or {}
    if not isinstance(defaults, dict) or not isinstance(allowed, dict):
        raise ConfigError("'notes.defaults' and 'notes.allowed' must be objects")

    max_length = raw.get("max_length", DEFAULT_MAX_NOTE_LENGTH)
    try:
        max_length = int(max_length)
    except (TypeError, ValueError) as exc:
        raise ConfigError("'notes.max_length' must be an integer") from exc
    if max_length < 1:
        raise ConfigError("'notes.max_length' must be at least 1")

    def flag(source: dict, key: str, default: bool) -> bool:
        value = source.get(key, default)
        if not isinstance(value, bool):
            raise ConfigError(f"'{key}' must be true or false, got {value!r}")
        return value

    return NotePolicy(
        max_length=max_length,
        default_show_to_requester=flag(defaults, "show_to_requester", False),
        default_mark_first_response=flag(defaults, "mark_first_response", False),
        default_add_to_linked_requests=flag(defaults, "add_to_linked_requests", False),
        allow_show_to_requester=flag(allowed, "show_to_requester", True),
        allow_add_to_linked_requests=flag(allowed, "add_to_linked_requests", True),
    )


def load_policy(path: Path) -> Policy:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"ITSM policy {path} is not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"ITSM policy {path} must be a JSON object")

    return Policy(groups=_parse_groups(raw.get("groups")), notes=_parse_notes(raw.get("notes")))


def load_dotenv(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from a .env file into os.environ.

    Real environment variables always win, so an MCP client's `env` block
    overrides the file.
    """
    env_path = path or Path(os.environ.get("ITSM_ENV_FILE", ".env")).expanduser()
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_settings() -> Settings:
    load_dotenv()

    base_url = os.environ.get("ITSM_URL", "").strip().rstrip("/")
    if not base_url:
        raise ConfigError("ITSM_URL is not set")

    authtoken = os.environ.get("ITSM_AUTHTOKEN", "").strip()
    if not authtoken:
        raise ConfigError("ITSM_AUTHTOKEN is not set")

    portal_id = os.environ.get("ITSM_PORTAL_ID", DEFAULT_PORTAL_ID).strip()
    if not portal_id:
        raise ConfigError("ITSM_PORTAL_ID cannot be empty")

    # An explicitly configured path that does not exist is a mistake worth
    # failing on. An absent default file just means "no extra restrictions",
    # so a stdio user can run the server without writing a policy first.
    configured = os.environ.get("ITSM_CONFIG", "").strip()
    if configured:
        policy_path = Path(configured).expanduser()
        if not policy_path.exists():
            raise ConfigError(f"ITSM policy file not found at {policy_path}")
        policy = load_policy(policy_path)
    else:
        default_path = Path("itsm.json")
        policy = load_policy(default_path) if default_path.exists() else Policy()

    return Settings(
        base_url=base_url,
        authtoken=authtoken,
        policy=policy,
        portal_id=portal_id,
        verify_ssl=_env_bool("ITSM_VERIFY_SSL", True),
        trust_env=_env_bool("ITSM_TRUST_ENV", False),
        timeout_seconds=_env_int("ITSM_TIMEOUT_SECONDS", 30),
    )
