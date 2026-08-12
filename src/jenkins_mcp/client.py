"""Thin async wrapper over the Jenkins remote API.

Knows nothing about MCP. Mirrors the flow the original script.py performed:
trigger -> resolve queue item to a build number -> wait for completion ->
read the console. Unlike the script, every wait is bounded by a deadline and
every response is status-checked.
"""

from __future__ import annotations

import asyncio
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


def extract_output(console: str, marker: str, multiline: bool = False) -> str | None:
    """Return the job's declared result.

    Jobs print ``job_output: <result>``. The result is the rest of that line —
    a Jenkins console interleaves plenty of its own epilogue (``Build step ...
    marked build as``, ``Archiving artifacts``, ``[Pipeline] // stage``) between
    the job's output and ``Finished:``, so consuming past the newline would
    capture that noise.

    If the marker appears more than once the last occurrence wins, since a rerun
    inside one build should report its final answer.

    ``multiline`` (registry opt-in) extends capture past the first newline for
    jobs that emit block output such as pretty-printed JSON. It stops at a blank
    line, at a line that looks like Jenkins' own output, or at the next marker.
    """
    if not console or not marker:
        return None

    pattern = re.compile(re.escape(marker), re.IGNORECASE)
    matches = list(pattern.finditer(console))
    if not matches:
        return None

    tail = console[matches[-1].end():]
    first, _, rest = tail.partition("\n")

    if not multiline:
        return first.strip() or None

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
