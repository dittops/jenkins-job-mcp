#!/usr/bin/env python3
"""Sample MCP client for jenkins-mcp: list the tools, then execute one.

Works against either transport.

    # stdio — this script launches the server itself (no server needed first)
    .venv/bin/python examples/client_example.py

    # http — server must already be running:
    #   MCP_TRANSPORT=streamable-http .venv/bin/python -m jenkins_mcp.server
    .venv/bin/python examples/client_example.py --url http://127.0.0.1:8000/mcp

By default this runs the read-only `list_jenkins_actions` tool. Pass --trigger
to actually start a Jenkins build, which will run a real job against a real
device:

    .venv/bin/python examples/client_example.py --trigger \
        --action fetchos --param ipaddress=10.0.0.1 --param hostname=sw1 \
        --param customer_name=acme --param username=admin --param device_type=cisco_ios

Note the SDK's snake_case attributes (`server_info`, `input_schema`) — MCP
Python SDK v2 renamed these from the camelCase used in v1 examples.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def connect(url: str | None):
    """Yield a live ClientSession over HTTP if --url was given, else stdio."""
    if url:
        async with streamable_http_client(url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
        return

    # stdio: we spawn the server ourselves and own its pipes. Run it from the
    # project root so a relative JENKINS_ACTIONS_CONFIG in .env resolves.
    params = StdioServerParameters(
        command=str(PROJECT_ROOT / ".venv" / "bin" / "python"),
        args=["-m", "jenkins_mcp.server"],
        env={**os.environ, "MCP_TRANSPORT": "stdio"},
        cwd=str(PROJECT_ROOT),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def parse_result(result) -> dict | str:
    """Tool results arrive as text content; ours are always JSON."""
    if not result.content:
        return {}
    text = getattr(result.content[0], "text", "")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


async def run(args: argparse.Namespace) -> int:
    async with connect(args.url) as session:
        # ---- 1. list the tools the server exposes ----------------------
        print("=" * 60)
        print("TOOLS")
        print("=" * 60)
        tools = await session.list_tools()
        for tool in tools.tools:
            params = ", ".join(tool.input_schema.get("properties", {}))
            summary = (tool.description or "").strip().splitlines()[0]
            print(f"\n  {tool.name}({params})")
            print(f"      {summary}")

        # ---- 2. execute a tool -----------------------------------------
        print("\n" + "=" * 60)
        print("CALL: list_jenkins_actions")
        print("=" * 60)
        actions = parse_result(await session.call_tool("list_jenkins_actions", {}))
        if not actions.get("ok"):
            print(f"  FAILED: {actions.get('error')}")
            return 1

        print(f"  jenkins job: {actions['jenkins_job']}")
        for action in actions["actions"]:
            required = [p["name"] for p in action["parameters"] if p["required"]]
            print(f"\n  - {action['action']}: {action['description']}")
            print(f"      required: {', '.join(required)}")

        if not args.trigger:
            print(
                "\nRe-run with --trigger to actually start a build "
                "(this runs a real Jenkins job)."
            )
            return 0

        # ---- 3. trigger a real build -----------------------------------
        parameters = dict(p.split("=", 1) for p in args.param)
        print("\n" + "=" * 60)
        print(f"CALL: run_jenkins_action(action={args.action!r})")
        print("=" * 60)
        print(f"  parameters: {parameters}")
        print(f"  wait={not args.no_wait} timeout={args.timeout}s")

        result = parse_result(
            await session.call_tool(
                "run_jenkins_action",
                {
                    "action": args.action,
                    "parameters": parameters,
                    "wait": not args.no_wait,
                    "timeout_seconds": args.timeout,
                },
            )
        )
        print("\n" + json.dumps(result, indent=2))

        if not result.get("ok"):
            print(f"\n  FAILED ({result.get('error_type')}): {result.get('error')}")
            return 1

        # With wait=false we only have a handle; fetch the result separately.
        if args.no_wait:
            handle = (
                {"build_number": result["build_number"]}
                if result.get("build_number") is not None
                else {"queue_url": result["queue_url"]}
            )
            print(f"\n  polling for completion: {handle}")
            result = parse_result(
                await session.call_tool(
                    "get_jenkins_build",
                    {**handle, "wait": True, "timeout_seconds": args.timeout},
                )
            )
            print("\n" + json.dumps(result, indent=2))

        print(f"\n  status : {result.get('status')}")
        print(f"  output : {result.get('output')!r}")
        return 0 if result.get("ok") else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--url",
        help="Connect over HTTP to a running server, e.g. "
        "http://127.0.0.1:8000/mcp. Omitted: spawn the server over stdio.",
    )
    parser.add_argument(
        "--trigger",
        action="store_true",
        help="Actually start a Jenkins build (runs a real job).",
    )
    parser.add_argument("--action", default="fetchos", help="Action name to run.")
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Build parameter; repeat for each one.",
    )
    parser.add_argument("--timeout", type=int, default=300, help="Seconds.")
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Return as soon as the build starts, then poll get_jenkins_build.",
    )
    args = parser.parse_args()

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
