"""Environment settings and the action registry.

Everything the original script.py hardcoded (server URL, credentials, job name,
build token, action name) is loaded here instead.

The deployment has a *single* Jenkins job that dispatches on its ``action_name``
build parameter. So the registry describes one job plus the set of actions it
supports; the registry is also the allowlist — an action that is not listed
cannot be triggered.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError, ParameterError, UnknownActionError

# Marker the job is expected to print. Everything after it is the result.
DEFAULT_OUTPUT_MARKER = "job_output:"

# Build parameter used to select the action. Jenkins-side name, not ours.
ACTION_PARAMETER = "action_name"


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
class JobParameter:
    name: str
    required: bool = True
    description: str = ""
    default: str | None = None


@dataclass(frozen=True)
class ActionConfig:
    """One supported value of the job's ``action_name`` parameter."""

    name: str
    description: str = ""
    # Extra parameters this action needs beyond the job-wide ones.
    parameters: tuple[JobParameter, ...] = ()
    # Value actually sent as action_name, when it differs from the alias.
    action_value: str | None = None

    @property
    def value(self) -> str:
        return self.action_value or self.name


@dataclass(frozen=True)
class JobRegistry:
    """The single Jenkins job plus every action it supports."""

    job: str
    description: str = ""
    parameters: tuple[JobParameter, ...] = ()
    actions: dict[str, ActionConfig] = field(default_factory=dict)
    build_token_env: str = "JENKINS_BUILD_TOKEN"
    output_marker: str = DEFAULT_OUTPUT_MARKER
    # Opt-in: capture past the marker's line, for jobs printing block output.
    output_multiline: bool = False

    def build_token(self) -> str | None:
        return os.environ.get(self.build_token_env) or None

    def action(self, name: str) -> ActionConfig:
        try:
            return self.actions[name]
        except KeyError:
            known = ", ".join(sorted(self.actions)) or "(registry is empty)"
            raise UnknownActionError(
                f"Unknown action '{name}'. Configured actions: {known}"
            ) from None

    def parameters_for(self, action: ActionConfig) -> tuple[JobParameter, ...]:
        """Job-wide parameters plus any the action adds (action wins on name clash)."""
        merged: dict[str, JobParameter] = {p.name: p for p in self.parameters}
        for p in action.parameters:
            merged[p.name] = p
        return tuple(merged.values())

    def validate_parameters(
        self, action: ActionConfig, supplied: dict[str, object]
    ) -> dict[str, str]:
        """Check supplied params against the registry and coerce them to strings.

        Rejects unknown keys rather than forwarding them, so a caller cannot
        smuggle extra build parameters into the job. ``action_name`` is set by
        the server from the registry, never by the caller.
        """
        declared = {p.name: p for p in self.parameters_for(action)}

        if ACTION_PARAMETER in supplied:
            raise ParameterError(
                f"'{ACTION_PARAMETER}' is set by the server from the chosen action "
                f"and must not be supplied directly."
            )

        unknown = sorted(set(supplied) - set(declared))
        if unknown:
            raise ParameterError(
                f"Unknown parameter(s) for action '{action.name}': {', '.join(unknown)}. "
                f"Allowed: {', '.join(sorted(declared)) or '(none)'}"
            )

        resolved: dict[str, str] = {}
        missing: list[str] = []
        for name, spec in declared.items():
            if name in supplied and supplied[name] is not None:
                resolved[name] = str(supplied[name])
            elif spec.default is not None:
                resolved[name] = spec.default
            elif spec.required:
                missing.append(name)

        if missing:
            raise ParameterError(
                f"Missing required parameter(s) for action '{action.name}': "
                f"{', '.join(missing)}"
            )

        resolved[ACTION_PARAMETER] = action.value
        return resolved

    def describe_action(self, action: ActionConfig) -> dict[str, object]:
        return {
            "action": action.name,
            "description": action.description,
            "parameters": [
                {
                    "name": p.name,
                    "required": p.required,
                    "description": p.description,
                    "default": p.default,
                }
                for p in self.parameters_for(action)
            ],
        }


@dataclass
class Settings:
    base_url: str
    user: str
    api_token: str
    registry: JobRegistry
    verify_ssl: bool = True
    trust_env: bool = False
    timeout_seconds: int = 600
    poll_interval: int = 2
    poll_max_interval: int = 10
    console_tail_lines: int = 200

    @property
    def auth(self) -> tuple[str, str]:
        return (self.user, self.api_token)


def _parse_parameters(raw: object, context: str) -> tuple[JobParameter, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"{context}: 'parameters' must be a list")

    params: list[JobParameter] = []
    for item in raw:
        # Shorthand: a bare string means a required parameter with no docs.
        if isinstance(item, str):
            params.append(JobParameter(name=item))
            continue
        if not isinstance(item, dict) or "name" not in item:
            raise ConfigError(
                f"{context}: each parameter must be a string or an object with 'name'"
            )
        default = item.get("default")
        params.append(
            JobParameter(
                name=str(item["name"]),
                required=bool(item.get("required", True)),
                description=str(item.get("description", "")),
                default=None if default is None else str(default),
            )
        )
    return tuple(params)


def _parse_actions(raw: object) -> dict[str, ActionConfig]:
    if not isinstance(raw, dict) or not raw:
        raise ConfigError("Registry must contain a non-empty 'actions' object")

    actions: dict[str, ActionConfig] = {}
    for name, entry in raw.items():
        # Shorthand: "fetchos": "Fetch the OS version"
        if isinstance(entry, str):
            actions[name] = ActionConfig(name=name, description=entry)
            continue
        if not isinstance(entry, dict):
            raise ConfigError(
                f"Action '{name}' must be a description string or an object"
            )
        actions[name] = ActionConfig(
            name=name,
            description=str(entry.get("description", "")),
            parameters=_parse_parameters(
                entry.get("parameters"), f"Action '{name}'"
            ),
            action_value=(
                str(entry["action_value"]) if entry.get("action_value") else None
            ),
        )
    return actions


def load_registry(path: Path) -> JobRegistry:
    if not path.exists():
        raise ConfigError(
            f"Action registry not found at {path}. "
            f"Set JENKINS_ACTIONS_CONFIG or create actions.json."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Action registry {path} is not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"Action registry {path} must be a JSON object")
    if "job" not in raw:
        raise ConfigError(f"Action registry {path} must contain a 'job' field")

    return JobRegistry(
        job=str(raw["job"]),
        description=str(raw.get("description", "")),
        parameters=_parse_parameters(raw.get("parameters"), "Registry"),
        actions=_parse_actions(raw.get("actions")),
        build_token_env=str(raw.get("build_token_env", "JENKINS_BUILD_TOKEN")),
        output_marker=str(raw.get("output_marker", DEFAULT_OUTPUT_MARKER)),
        output_multiline=bool(raw.get("output_multiline", False)),
    )


def load_dotenv(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from a .env file into os.environ.

    Real environment variables always win, so an MCP client's `env` block
    overrides the file. Kept dependency-free — the format we document is a
    handful of plain assignments.
    """
    env_path = path or Path(os.environ.get("JENKINS_ENV_FILE", ".env")).expanduser()
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

    base_url = os.environ.get("JENKINS_URL", "").strip().rstrip("/")
    if not base_url:
        raise ConfigError("JENKINS_URL is not set")

    user = os.environ.get("JENKINS_USER", "").strip()
    api_token = os.environ.get("JENKINS_API_TOKEN", "").strip()
    if not user or not api_token:
        raise ConfigError("JENKINS_USER and JENKINS_API_TOKEN must both be set")

    registry_path = Path(
        os.environ.get("JENKINS_ACTIONS_CONFIG", "actions.json")
    ).expanduser()

    return Settings(
        base_url=base_url,
        user=user,
        api_token=api_token,
        registry=load_registry(registry_path),
        verify_ssl=_env_bool("JENKINS_VERIFY_SSL", True),
        trust_env=_env_bool("JENKINS_TRUST_ENV", False),
        timeout_seconds=_env_int("JENKINS_TIMEOUT_SECONDS", 600),
        poll_interval=_env_int("JENKINS_POLL_INTERVAL", 2),
        poll_max_interval=_env_int("JENKINS_POLL_MAX_INTERVAL", 10),
        console_tail_lines=_env_int("JENKINS_CONSOLE_TAIL_LINES", 200),
    )
