#!/usr/bin/env python3
"""Sample MCP client for itsm-mcp: list the tools, then execute one.

Works against either transport.

    # stdio — this script launches the server itself (no server needed first)
    .venv/bin/python examples/itsm_client_example.py

    # http — server must already be running:
    #   MCP_TRANSPORT=streamable-http MCP_PORT=8001 .venv/bin/itsm-mcp
    .venv/bin/python examples/itsm_client_example.py --url http://127.0.0.1:8001/mcp

By default this runs the read-only `list_itsm_groups` tool. Pass --update to
actually write to a ticket — that change is immediate and cannot be undone
from here:

    .venv/bin/python examples/itsm_client_example.py --update \
        --ticket 2977634 --group "ICCM Tools" --note "Reassigning for triage"

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
    # project root so a relative ITSM_CONFIG in .env resolves.
    params = StdioServerParameters(
        command=str(PROJECT_ROOT / ".venv" / "bin" / "python"),
        args=["-m", "itsm_mcp.server"],
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
        print("CALL: list_itsm_groups")
        print("=" * 60)
        policy = parse_result(await session.call_tool("list_itsm_groups", {}))
        if not policy.get("ok"):
            print(f"  FAILED: {policy.get('error')}")
            return 1

        print(f"  itsm: {policy['itsm_url']}")
        if policy["groups_restricted"]:
            for group in policy["groups"]:
                print(f"\n  - {group['name']}: {group['description']}")
        else:
            print("\n  (no group allowlist configured — any group name is accepted)")

        notes = policy["note_policy"]
        print(f"\n  note defaults : {notes['defaults']}")
        print(f"  note allowed  : {notes['allowed']}")
        print(f"  note max_len  : {notes['max_length']}")

        if not args.update:
            print(
                "\nRe-run with --update to actually modify a ticket "
                "(this writes to the live ITSM system)."
            )
            return 0

        # ---- 3. write to a real ticket ---------------------------------
        if args.ticket is None:
            print("\n  --update needs --ticket <id>")
            return 2
        if args.group is None and args.note is None:
            print("\n  --update needs --group and/or --note")
            return 2

        payload: dict = {"request_id": args.ticket}
        for key, value in (
            ("group", args.group),
            ("note", args.note),
            ("show_to_requester", args.show_to_requester),
            ("mark_first_response", args.mark_first_response),
            ("add_to_linked_requests", args.add_to_linked_requests),
        ):
            if value is not None:
                payload[key] = value

        print("\n" + "=" * 60)
        print(f"CALL: update_itsm_ticket(request_id={args.ticket})")
        print("=" * 60)
        print(f"  {json.dumps(payload)}")

        result = parse_result(await session.call_tool("update_itsm_ticket", payload))
        print("\n" + json.dumps(result, indent=2))

        if not result.get("ok"):
            print(f"\n  FAILED ({result.get('error_type')}): {result.get('error')}")
            return 1

        applied = result["applied"]
        print(f"\n  group applied : {applied['group']}")
        print(f"  note added    : {applied['note_added']}")
        print(f"  ticket now    : {result['ticket']}")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--url",
        help="Connect over HTTP to a running server, e.g. "
        "http://127.0.0.1:8001/mcp. Omitted: spawn the server over stdio.",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Actually modify the ticket (writes to the live ITSM system).",
    )
    parser.add_argument("--ticket", type=int, help="Request id to update.")
    parser.add_argument("--group", help="Group to assign, e.g. 'ICCM Tools'.")
    parser.add_argument("--note", help="Note text to append.")
    # BooleanOptionalAction gives each flag a --no- counterpart and leaves the
    # default at None, so an unpassed flag is omitted from the call and the
    # server's configured default applies. Both directions matter: the stock
    # policy publishes to the requester, so --no-show-to-requester is how you
    # post an internal note.
    parser.add_argument(
        "--show-to-requester",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Publish the note to the requester (default: server policy).",
    )
    parser.add_argument(
        "--mark-first-response",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Mark the note as the first response (default: server policy).",
    )
    parser.add_argument(
        "--add-to-linked-requests",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Copy the note onto linked tickets (default: server policy).",
    )
    args = parser.parse_args()

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
