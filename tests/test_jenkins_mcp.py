"""Tests for config validation, output extraction, and the full build flow."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
import pytest

from jenkins_mcp.client import JenkinsClient, extract_output, redact, tail_lines
from jenkins_mcp.config import JobRegistry, Settings, load_registry
from jenkins_mcp.errors import (
    BuildCancelled,
    ConfigError,
    JenkinsAPIError,
    JenkinsConnectionError,
    JenkinsTimeout,
    ParameterError,
    UnknownActionError,
)

REGISTRY = {
    "job": "AUTOMATION_Trigger_Workflow",
    "output_marker": "job_output:",
    "parameters": [
        {"name": "customer_name"},
        {"name": "ipaddress"},
        {"name": "device_type", "required": False, "default": "cisco_ios"},
    ],
    "actions": {
        "fetchos": {"description": "Fetch OS version"},
        "fetchcpu": "Fetch CPU utilisation",
        "fetchtop": {"description": "Top processes", "action_value": "fetch_top_v2"},
    },
}


def write_registry(tmp_path: Path, data=None) -> JobRegistry:
    path = tmp_path / "actions.json"
    path.write_text(json.dumps(data if data is not None else REGISTRY))
    return load_registry(path)


def make_settings(registry: JobRegistry) -> Settings:
    return Settings(
        base_url="http://jenkins.test",
        user="u",
        api_token="t",
        registry=registry,
        poll_interval=0,
        poll_max_interval=0,
        console_tail_lines=50,
    )


# --- extraction -----------------------------------------------------------


def test_extract_simple():
    assert extract_output("noise\njob_output: 17.9.4a\nFinished: SUCCESS", "job_output:") == "17.9.4a"


def test_extract_last_marker_wins():
    console = "job_output: first\nmore\njob_output: second\n"
    assert extract_output(console, "job_output:") == "second"


REALISTIC_CONSOLE = """Started by user jenkins-user
+ python fetch.py --action fetchos
job_output: 17.9.4a
Build step 'Execute shell' marked build as SUCCESS
Archiving artifacts
[Pipeline] // stage
Notifying upstream projects
Finished: SUCCESS"""


def test_extract_is_single_line_by_default():
    """Jenkins epilogue between the marker and 'Finished:' must not be captured."""
    assert extract_output(REALISTIC_CONSOLE, "job_output:") == "17.9.4a"


def test_extract_multiline_opt_in_stops_at_jenkins_epilogue():
    assert (
        extract_output(REALISTIC_CONSOLE, "job_output:", multiline=True) == "17.9.4a"
    )


def test_extract_multiline_captures_block_output():
    console = 'job_output: {\n  "cpu": 42\n}\nBuild step marked build as SUCCESS'
    assert extract_output(console, "job_output:", multiline=True) == '{\n  "cpu": 42\n}'
    # Default mode takes only the marker's line.
    assert extract_output(console, "job_output:") == "{"


def test_extract_multiline_stops_at_blank_line():
    console = "job_output: a\nb\n\nunrelated trailing noise"
    assert extract_output(console, "job_output:", multiline=True) == "a\nb"


def test_extract_missing_marker_returns_none():
    assert extract_output("nothing here\nFinished: SUCCESS", "job_output:") is None


def test_extract_empty_result_is_none():
    assert extract_output("job_output:   \nFinished: SUCCESS", "job_output:") is None


def test_extract_case_insensitive():
    assert extract_output("JOB_OUTPUT: ok", "job_output:") == "ok"


def test_extract_json_payload():
    console = 'job_output: {"cpu": 42, "load": "0.7"}\nFinished: SUCCESS'
    assert json.loads(extract_output(console, "job_output:")) == {"cpu": 42, "load": "0.7"}


def test_tail_lines():
    assert tail_lines("a\nb\nc\nd", 2) == "c\nd"
    assert tail_lines("a\nb", 10) == "a\nb"


def test_redact_strips_credentials_and_query():
    assert redact("http://user:pw@host:8080/job/x/1/?token=secret") == "http://host:8080/job/x/1/"


# --- registry / parameters ------------------------------------------------


def test_registry_parses_action_shorthand_and_override(tmp_path):
    reg = write_registry(tmp_path)
    assert reg.actions["fetchcpu"].description == "Fetch CPU utilisation"
    assert reg.actions["fetchcpu"].value == "fetchcpu"
    assert reg.actions["fetchtop"].value == "fetch_top_v2"


def test_unknown_action_rejected(tmp_path):
    reg = write_registry(tmp_path)
    with pytest.raises(UnknownActionError):
        reg.action("rm-rf")


def test_validate_injects_action_name_and_default(tmp_path):
    reg = write_registry(tmp_path)
    params = reg.validate_parameters(
        reg.action("fetchtop"), {"customer_name": "acme", "ipaddress": "10.0.0.1"}
    )
    assert params["action_name"] == "fetch_top_v2"
    assert params["device_type"] == "cisco_ios"


def test_validate_rejects_unknown_parameter(tmp_path):
    reg = write_registry(tmp_path)
    with pytest.raises(ParameterError, match="Unknown parameter"):
        reg.validate_parameters(
            reg.action("fetchos"),
            {"customer_name": "a", "ipaddress": "1", "evil": "x"},
        )


def test_validate_rejects_caller_supplied_action_name(tmp_path):
    reg = write_registry(tmp_path)
    with pytest.raises(ParameterError, match="set by the server"):
        reg.validate_parameters(
            reg.action("fetchos"),
            {"customer_name": "a", "ipaddress": "1", "action_name": "spoof"},
        )


def test_validate_reports_missing(tmp_path):
    reg = write_registry(tmp_path)
    with pytest.raises(ParameterError, match="Missing required"):
        reg.validate_parameters(reg.action("fetchos"), {"customer_name": "a"})


def test_registry_requires_actions(tmp_path):
    with pytest.raises(ConfigError, match="actions"):
        write_registry(tmp_path, {"job": "J"})


def test_missing_registry_file_errors(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_registry(tmp_path / "nope.json")


# --- client flow against a mock Jenkins -----------------------------------


class MockJenkins:
    """Minimal Jenkins: queue item resolves after N polls, build after M."""

    def __init__(self, queue_delay=1, build_delay=1, console="job_output: 17.9.4a\nFinished: SUCCESS"):
        self.queue_delay = queue_delay
        self.build_delay = build_delay
        self.console = console
        self.queue_polls = 0
        self.build_polls = 0
        self.triggered_with: dict[str, str] = {}
        self.trigger_params: dict[str, str] = {}
        self.cancelled = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/crumbIssuer/api/json"):
            return httpx.Response(404)
        if path.endswith("/buildWithParameters"):
            self.triggered_with = dict(httpx.QueryParams(request.content.decode()))
            self.trigger_params = dict(request.url.params)
            return httpx.Response(201, headers={"Location": "http://jenkins.test/queue/item/7/"})
        if "/queue/item/" in path:
            self.queue_polls += 1
            if self.cancelled:
                return httpx.Response(200, json={"cancelled": True})
            if self.queue_polls >= self.queue_delay:
                return httpx.Response(200, json={"executable": {"number": 412}})
            return httpx.Response(200, json={"why": "Waiting for executor"})
        if path.endswith("/api/json"):
            self.build_polls += 1
            building = self.build_polls < self.build_delay
            return httpx.Response(200, json={
                "building": building,
                "result": None if building else "SUCCESS",
                "number": 412,
                "duration": 0 if building else 38210,
                "url": "http://jenkins.test/job/J/412/",
            })
        if path.endswith("/consoleText"):
            return httpx.Response(200, text=self.console)
        return httpx.Response(404)


def make_client(settings: Settings, mock: MockJenkins) -> JenkinsClient:
    api = JenkinsClient(settings)
    api._client = httpx.AsyncClient(
        base_url=settings.base_url,
        transport=httpx.MockTransport(mock.handler),
        follow_redirects=False,
    )
    return api


@pytest.mark.asyncio
async def test_full_flow_returns_extracted_output(tmp_path, monkeypatch):
    monkeypatch.setenv("JENKINS_BUILD_TOKEN", "your-build-token")
    reg = write_registry(tmp_path)
    cfg = make_settings(reg)
    mock = MockJenkins(queue_delay=2, build_delay=2)
    api = make_client(cfg, mock)

    params = reg.validate_parameters(
        reg.action("fetchos"), {"customer_name": "acme", "ipaddress": "10.0.0.1"}
    )
    queue_url = await api.trigger(params)
    build_no = await api.wait_for_build_number(queue_url, time.monotonic() + 5)
    data = await api.wait_for_completion(build_no, time.monotonic() + 5)
    summary = await api.summarise_build(build_no, data, 50)

    assert mock.triggered_with["action_name"] == "fetchos"
    assert mock.trigger_params["token"] == "your-build-token"
    assert build_no == 412
    assert summary.result == "SUCCESS"
    assert summary.output == "17.9.4a"
    assert summary.url == "http://jenkins.test/job/J/412/"
    await api.aclose()


@pytest.mark.asyncio
async def test_trigger_without_location_header_errors(tmp_path):
    cfg = make_settings(write_registry(tmp_path))

    def handler(request):
        if request.url.path.endswith("/crumbIssuer/api/json"):
            return httpx.Response(404)
        return httpx.Response(201)  # no Location

    api = JenkinsClient(cfg)
    api._client = httpx.AsyncClient(base_url=cfg.base_url, transport=httpx.MockTransport(handler))
    with pytest.raises(JenkinsAPIError, match="no queue Location"):
        await api.trigger({"a": "b"})
    await api.aclose()


@pytest.mark.asyncio
async def test_trigger_http_error_surfaces(tmp_path):
    cfg = make_settings(write_registry(tmp_path))

    def handler(request):
        if request.url.path.endswith("/crumbIssuer/api/json"):
            return httpx.Response(404)
        return httpx.Response(403)

    api = JenkinsClient(cfg)
    api._client = httpx.AsyncClient(base_url=cfg.base_url, transport=httpx.MockTransport(handler))
    with pytest.raises(JenkinsAPIError, match="HTTP 403"):
        await api.trigger({"a": "b"})
    await api.aclose()


@pytest.mark.asyncio
async def test_cancelled_queue_item_raises(tmp_path):
    cfg = make_settings(write_registry(tmp_path))
    mock = MockJenkins()
    mock.cancelled = True
    api = make_client(cfg, mock)
    with pytest.raises(BuildCancelled):
        await api.wait_for_build_number("http://jenkins.test/queue/item/7", time.monotonic() + 5)
    await api.aclose()


@pytest.mark.asyncio
async def test_queue_wait_times_out(tmp_path):
    """The unbounded while-True of the original script must not recur."""
    cfg = make_settings(write_registry(tmp_path))
    mock = MockJenkins(queue_delay=10_000)
    api = make_client(cfg, mock)
    with pytest.raises(JenkinsTimeout, match="Waiting for executor"):
        await api.wait_for_build_number("http://jenkins.test/queue/item/7", time.monotonic() - 1)
    await api.aclose()


@pytest.mark.asyncio
async def test_build_wait_times_out(tmp_path):
    cfg = make_settings(write_registry(tmp_path))
    mock = MockJenkins(build_delay=10_000)
    api = make_client(cfg, mock)
    with pytest.raises(JenkinsTimeout, match="still running"):
        await api.wait_for_completion(412, time.monotonic() - 1)
    await api.aclose()


@pytest.mark.asyncio
async def test_wait_false_returns_queue_url_instead_of_blocking(tmp_path, monkeypatch):
    """wait=false must not sit on the full build deadline, and must not orphan
    a triggered build when the queue is slow."""
    from jenkins_mcp import server

    reg = write_registry(tmp_path)
    cfg = make_settings(reg)
    cfg.timeout_seconds = 600
    mock = MockJenkins(queue_delay=10_000)  # never assigns a build number
    api = make_client(cfg, mock)

    monkeypatch.setattr(server, "_settings", cfg)
    monkeypatch.setattr(server, "_client", api)
    monkeypatch.setattr(server, "QUEUE_ONLY_WAIT_SECONDS", 0)

    started = time.monotonic()
    res = await server.run_jenkins_action(
        action="fetchos",
        parameters={"customer_name": "acme", "ipaddress": "10.0.0.1"},
        wait=False,
    )
    elapsed = time.monotonic() - started

    assert res["status"] == "QUEUED"
    assert res["queue_url"]  # handle preserved, build not orphaned
    assert elapsed < 5, "wait=false blocked on the full build deadline"
    await api.aclose()


@pytest.mark.asyncio
async def test_get_build_resolves_a_queue_url(tmp_path, monkeypatch):
    from jenkins_mcp import server

    cfg = make_settings(write_registry(tmp_path))
    mock = MockJenkins(queue_delay=1, build_delay=1)
    api = make_client(cfg, mock)
    monkeypatch.setattr(server, "_settings", cfg)
    monkeypatch.setattr(server, "_client", api)

    res = await server.get_jenkins_build(
        queue_url="http://jenkins.test/queue/item/7", wait=True
    )
    assert res["build_number"] == 412
    assert res["output"] == "17.9.4a"
    await api.aclose()


@pytest.mark.asyncio
async def test_get_build_requires_a_handle(tmp_path, monkeypatch):
    from jenkins_mcp import server

    cfg = make_settings(write_registry(tmp_path))
    monkeypatch.setattr(server, "_settings", cfg)
    monkeypatch.setattr(server, "_client", make_client(cfg, MockJenkins()))
    res = await server.get_jenkins_build()
    assert res["ok"] is False and "build_number or queue_url" in res["error"]


def test_dotenv_loads_but_real_env_wins(tmp_path, monkeypatch):
    from jenkins_mcp.config import load_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text(
        '# comment\nJENKINS_URL="http://from-file:8080"\nJENKINS_USER=file_user\n\n'
    )
    monkeypatch.delenv("JENKINS_URL", raising=False)
    monkeypatch.setenv("JENKINS_USER", "env_user")

    load_dotenv(env_file)
    assert os.environ["JENKINS_URL"] == "http://from-file:8080"  # quotes stripped
    assert os.environ["JENKINS_USER"] == "env_user"  # real env not clobbered


@pytest.mark.asyncio
async def test_connect_timeout_becomes_typed_error(tmp_path):
    """httpx.ConnectTimeout stringifies to '' — it must not reach the model blank."""
    cfg = make_settings(write_registry(tmp_path))

    def handler(request):
        raise httpx.ConnectTimeout("")

    api = JenkinsClient(cfg)
    api._client = httpx.AsyncClient(
        base_url=cfg.base_url, transport=httpx.MockTransport(handler)
    )
    with pytest.raises(JenkinsConnectionError, match="Timed out connecting"):
        await api.trigger({"a": "b"})
    await api.aclose()


@pytest.mark.asyncio
async def test_unreachable_host_returns_structured_error(tmp_path, monkeypatch):
    """The tool must return {ok: false, ...}, never raise out to the MCP layer."""
    from jenkins_mcp import server

    cfg = make_settings(write_registry(tmp_path))

    def handler(request):
        raise httpx.ConnectError("nope")

    api = JenkinsClient(cfg)
    api._client = httpx.AsyncClient(
        base_url=cfg.base_url, transport=httpx.MockTransport(handler)
    )
    monkeypatch.setattr(server, "_settings", cfg)
    monkeypatch.setattr(server, "_client", api)

    res = await server.run_jenkins_action(
        action="fetchos",
        parameters={"customer_name": "a", "ipaddress": "1"},
        wait=False,
    )
    assert res["ok"] is False
    assert res["error_type"] == "JenkinsConnectionError"
    assert res["error"].strip()  # never blank
    await api.aclose()


@pytest.mark.asyncio
async def test_unexpected_exception_is_never_blank(tmp_path, monkeypatch):
    from jenkins_mcp import server

    cfg = make_settings(write_registry(tmp_path))
    monkeypatch.setattr(server, "_settings", cfg)

    class Boom(Exception):
        pass

    def explode():
        raise Boom("")  # empty message, like ConnectTimeout

    monkeypatch.setattr(server, "client", explode)
    res = await server.run_jenkins_action(
        action="fetchos", parameters={"customer_name": "a", "ipaddress": "1"}
    )
    assert res["ok"] is False and "Boom" in res["error"]


@pytest.mark.asyncio
async def test_failed_build_reports_no_output(tmp_path):
    cfg = make_settings(write_registry(tmp_path))
    mock = MockJenkins(console="ERROR: device unreachable\nFinished: FAILURE")
    api = make_client(cfg, mock)
    data = await api.wait_for_completion(412, time.monotonic() + 5)
    summary = await api.summarise_build(412, data, 50)
    assert summary.output is None
    assert "device unreachable" in summary.console_tail
    await api.aclose()
