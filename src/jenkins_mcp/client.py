"""Thin async wrapper over the Jenkins remote API.

Knows nothing about MCP. Mirrors the flow the original script.py performed:
trigger -> resolve queue item to a build number -> wait for completion ->
read the console. Unlike the script, every wait is bounded by a deadline and
every response is status-checked.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import Settings
from .errors import (
    BuildCancelled,
    JenkinsAPIError,
    JenkinsConnectionError,
    JenkinsTimeout,
)


# Lines Jenkins itself emits after a build step finishes. Used only to bound
# multiline capture; single-line extraction stops at the newline regardless.
_JENKINS_EPILOGUE = re.compile(
    r"^(?:Finished:|Build step |Archiving artifacts|Recording |Notifying |"
    r"\[Pipeline\]|ERROR:|Started by |Setting status)",
)


def redact(url: str) -> str:
    """Strip any userinfo and query string so URLs are safe to return."""
    parts = urlsplit(url)
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


# A mapping big enough to be pathological is not a job result; refuse to parse it.
_MAX_LITERAL_CHARS = 200_000

# Matches a quoted string at the start of a value, honouring backslash escapes.
_QUOTED_VALUE = re.compile(r"""^(['"])(?:\\.|(?!\1).)*\1""", re.DOTALL)


def _marker_key(marker: str) -> str | None:
    """The marker's bare name, when it has the ``name:`` shape.

    Returns None for markers that are not name/colon pairs (``>>> RESULT``),
    which are only ever matched literally.
    """
    stripped = marker.strip()
    if not stripped.endswith(":"):
        return None
    return stripped[:-1].strip().strip("\"'") or None


def _marker_pattern(marker: str) -> re.Pattern[str]:
    """Match the marker whether the job printed it bare or as a mapping key.

    A job that prints a dict — ``{'job_output': "RedHat-8"}`` — puts a quote
    between the name and the colon, so the literal marker never matches. Both
    quote styles are optional so ``job_output: 17.9.4a`` still matches too.
    """
    key = _marker_key(marker)
    if key is None:
        return re.compile(re.escape(marker), re.IGNORECASE)
    return re.compile(rf"""['"]?{re.escape(key)}['"]?\s*:""", re.IGNORECASE)


def _enclosing_mapping(console: str, index: int) -> str | None:
    """The ``{...}`` literal containing `index`, if the marker sits inside one.

    Scans forward from the nearest preceding brace tracking depth, ignoring
    braces inside quoted strings so a value like ``"{unclosed"`` cannot throw
    the balance off.
    """
    start = console.rfind("{", 0, index)
    if start == -1 or index - start > _MAX_LITERAL_CHARS:
        return None

    depth = 0
    quote: str | None = None
    i = start
    while i < len(console):
        char = console[i]
        if quote:
            if char == "\\":
                i += 2
                continue
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                # A literal that closes before the marker does not contain it.
                return console[start : i + 1] if i > index else None
        if i - start > _MAX_LITERAL_CHARS:
            return None
        i += 1
    return None


def _value_from_mapping(blob: str, key: str) -> str | None:
    """Pull `key` out of a printed mapping, trying Python then JSON syntax.

    ``literal_eval`` first because jobs print Python dicts (single quotes,
    ``True``/``None``) far more often than strict JSON; both are literal-only,
    so nothing in the console is executed.
    """
    for parse in (ast.literal_eval, json.loads):
        try:
            data = parse(blob)
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            continue
        if not isinstance(data, dict):
            continue
        for name, value in data.items():
            if not isinstance(name, str) or name.strip().lower() != key.lower():
                continue
            if value is None:
                return None
            if isinstance(value, str):
                return value.strip() or None
            # Nested structures go back out as JSON so the caller can re-parse.
            if isinstance(value, (dict, list)):
                return json.dumps(value)
            return str(value)
    return None


def _unquote(value: str) -> str:
    """Strip the quotes off a value the job printed quoted, escapes and all."""
    match = _QUOTED_VALUE.match(value)
    if not match:
        return value
    try:
        unquoted = ast.literal_eval(match.group(0))
    except (ValueError, SyntaxError):
        return value
    return unquoted if isinstance(unquoted, str) else value


def extract_output(console: str, marker: str, multiline: bool = False) -> str | None:
    """Return the job's declared result.

    Jobs print their result one of two ways:

    * as a bare marker line — ``job_output: 17.9.4a`` — where the result is the
      rest of that line. A Jenkins console interleaves plenty of its own
      epilogue (``Build step ... marked build as``, ``Archiving artifacts``,
      ``[Pipeline] // stage``) between the job's output and ``Finished:``, so
      consuming past the newline would capture that noise.
    * as a printed mapping — ``{'job_output': "RedHat-8"}`` — where the result
      is that key's value, unwrapped from the dict and its quotes.

    If the marker appears more than once the last occurrence wins, since a rerun
    inside one build should report its final answer.

    ``multiline`` (registry opt-in) extends capture past the first newline for
    jobs that emit block output such as pretty-printed JSON. It stops at a blank
    line, at a line that looks like Jenkins' own output, or at the next marker.
    It does not apply to the mapping form, which is already self-delimiting.
    """
    if not console or not marker:
        return None

    pattern = _marker_pattern(marker)
    matches = list(pattern.finditer(console))
    if not matches:
        return None

    last = matches[-1]

    key = _marker_key(marker)
    if key is not None:
        blob = _enclosing_mapping(console, last.start())
        if blob is not None:
            value = _value_from_mapping(blob, key)
            if value is not None:
                return value

    tail = console[last.end():]
    first, _, rest = tail.partition("\n")

    if not multiline:
        # A mapping we could not parse (truncated console, say) still leaves a
        # quoted value behind — return that rather than the raw `"x"}` fragment.
        return _unquote(first.strip()) or None

    lines = [first]
    for line in rest.splitlines():
        if not line.strip() or _JENKINS_EPILOGUE.match(line) or pattern.search(line):
            break
        lines.append(line)

    return "\n".join(lines).strip() or None


def tail_lines(text: str, count: int) -> str:
    """Return the last ``count`` lines. ``count <= 0`` means no limit.

    Zero is the "return everything" sentinel rather than "return nothing":
    the callers are context-budget knobs, and the useful way to switch a budget
    off is to set it to zero.
    """
    if count <= 0:
        return text
    lines = text.splitlines()
    if len(lines) <= count:
        return text
    return "\n".join(lines[-count:])


@dataclass
class BuildResult:
    build_number: int
    result: str | None
    building: bool
    url: str
    duration_ms: int | None
    output: str | None
    console_tail: str


class JenkinsClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.base_url,
            auth=settings.auth,
            verify=settings.verify_ssl,
            trust_env=settings.trust_env,
            timeout=httpx.Timeout(30.0),
            follow_redirects=False,
        )
        self._crumb: tuple[str, str] | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- internals ---------------------------------------------------------

    async def _crumb_headers(self) -> dict[str, str]:
        """Fetch and cache a CSRF crumb. Absent crumb issuer is not an error."""
        if self._crumb is None:
            try:
                resp = await self._client.get("/crumbIssuer/api/json")
                if resp.status_code == 200:
                    data = resp.json()
                    self._crumb = (data["crumbRequestField"], data["crumb"])
                else:
                    self._crumb = ("", "")
            except (httpx.HTTPError, ValueError, KeyError):
                self._crumb = ("", "")
        field, value = self._crumb
        return {field: value} if field else {}

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Single choke point for HTTP, so transport failures become typed
        errors instead of escaping as bare httpx exceptions (several of which,
        like ConnectTimeout, carry an empty message)."""
        try:
            return await self._client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise JenkinsConnectionError(
                f"Timed out connecting to Jenkins at {redact(self._settings.base_url)}. "
                f"Check JENKINS_URL and that the host is reachable from this machine "
                f"(VPN?)."
            ) from exc
        except httpx.HTTPError as exc:
            detail = str(exc) or type(exc).__name__
            raise JenkinsConnectionError(
                f"Could not reach Jenkins at {redact(self._settings.base_url)}: {detail}"
            ) from exc

    async def _get_json(self, url: str) -> dict:
        resp = await self._request("GET", url)
        if resp.status_code >= 400:
            raise JenkinsAPIError(
                f"GET {redact(str(resp.request.url))} returned HTTP {resp.status_code}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise JenkinsAPIError(
                f"GET {redact(str(resp.request.url))} did not return JSON"
            ) from exc

    def _job_path(self, suffix: str = "") -> str:
        # Jenkins accepts a plain job name here; folders would need /job/a/job/b.
        return f"/job/{self._settings.registry.job}{suffix}"

    async def _sleep_backoff(self, interval: float) -> float:
        await asyncio.sleep(interval)
        return min(interval * 1.5, float(self._settings.poll_max_interval))

    # -- API ---------------------------------------------------------------

    async def trigger(self, parameters: dict[str, str]) -> str:
        """Start a build. Returns the queue item URL."""
        params: dict[str, str] = {}
        token = self._settings.registry.build_token()
        if token:
            params["token"] = token

        headers = await self._crumb_headers()
        resp = await self._request(
            "POST",
            self._job_path("/buildWithParameters"),
            params=params,
            data=parameters,
            headers=headers,
        )

        if resp.status_code >= 400:
            raise JenkinsAPIError(
                f"Triggering job '{self._settings.registry.job}' failed with "
                f"HTTP {resp.status_code}. Check the job name, credentials and build token."
            )

        location = resp.headers.get("Location")
        if not location:
            raise JenkinsAPIError(
                f"Jenkins accepted the request (HTTP {resp.status_code}) but returned no "
                f"queue Location header; cannot track the build."
            )
        return location.rstrip("/")

    async def wait_for_build_number(self, queue_url: str, deadline: float) -> int:
        """Poll a queue item until Jenkins assigns it a build number."""
        interval = float(self._settings.poll_interval)
        while True:
            data = await self._get_json(f"{queue_url}/api/json")

            executable = data.get("executable")
            if executable and executable.get("number") is not None:
                return int(executable["number"])

            if data.get("cancelled"):
                raise BuildCancelled(
                    f"Queue item {redact(queue_url)} was cancelled before it started."
                )

            if time.monotonic() >= deadline:
                reason = (data.get("why") or "").strip()
                raise JenkinsTimeout(
                    "Timed out waiting for Jenkins to start the build"
                    + (f" (queue status: {reason})" if reason else "")
                )
            interval = await self._sleep_backoff(interval)

    async def get_build_info(self, build_number: int) -> dict:
        return await self._get_json(self._job_path(f"/{build_number}/api/json"))

    async def wait_for_completion(self, build_number: int, deadline: float) -> dict:
        interval = float(self._settings.poll_interval)
        while True:
            data = await self.get_build_info(build_number)
            if not data.get("building", False) and data.get("result") is not None:
                return data
            if time.monotonic() >= deadline:
                raise JenkinsTimeout(
                    f"Timed out waiting for build #{build_number} to finish; "
                    f"it is still running."
                )
            interval = await self._sleep_backoff(interval)

    async def get_console(self, build_number: int) -> str:
        resp = await self._request(
            "GET", self._job_path(f"/{build_number}/consoleText")
        )
        if resp.status_code >= 400:
            raise JenkinsAPIError(
                f"Could not read console for build #{build_number} "
                f"(HTTP {resp.status_code})"
            )
        return resp.text

    async def summarise_build(
        self, build_number: int, build_data: dict, console_tail_lines: int
    ) -> BuildResult:
        console = await self.get_console(build_number)
        registry = self._settings.registry
        marker = registry.output_marker
        return BuildResult(
            build_number=build_number,
            result=build_data.get("result"),
            building=bool(build_data.get("building", False)),
            url=redact(build_data.get("url") or ""),
            duration_ms=build_data.get("duration"),
            output=extract_output(console, marker, registry.output_multiline),
            console_tail=tail_lines(console, console_tail_lines),
        )
