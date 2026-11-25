"""F4. MCP server over stdio: every tools/call goes through the guard."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import anyio
import anyio.to_thread
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ListToolsResult, TextContent, ToolAnnotations
from mcp.types import Tool as McpTool

from .audit import Audit, AuditError
from .common import ROOT, Clock, StartupError, ensure_state_dir, load_env, log_exception, quiet_loggers
from .guard import Guard, Tool
from .hubspot import HubSpot, ReadTools
from .models import ContactArgs, NoArgs, ScoreArgs, SearchArgs, TaskArgs
from .policy import load_policy
from .proposals import ProposalTools
from .research import ResearchTool, make_client
from .slack import SlackTool
from .store import Store

POOL = 4


def build_tools(reads: ReadTools, research: ResearchTool, proposals: ProposalTools, slack: SlackTool) -> dict[str, Tool]:
    rows = [
        Tool("search_new_leads", "List contacts created in the last N days. Call this first.", SearchArgs,
             reads.search_new_leads, True),
        Tool("get_company", "Company details for a lead.", ContactArgs, reads.get_company, True),
        Tool("research_company", "Look up cited public facts about a lead's company. Costs money; call once per lead.",
             ContactArgs, research.research_company, True),
        Tool("update_lead_score", "Propose a score. A person must approve it before HubSpot changes.", ScoreArgs,
             proposals.update_lead_score, False),
        Tool("create_followup_task", "Propose a follow-up task. A person must approve it before HubSpot changes.",
             TaskArgs, proposals.create_followup_task, False),
        Tool("post_slack_summary", "Post today's summary to Slack. Once a day; the server writes it.", NoArgs,
             slack.post_slack_summary, False),
    ]
    return {t.name: t for t in rows}


def make_server(guard: Guard) -> Server:
    active = 0
    workers = anyio.CapacityLimiter(POOL)
    spare = anyio.CapacityLimiter(1)  # refusals are still audited when the pool is full

    async def on_list_tools(ctx: Any, params: Any) -> ListToolsResult:
        return ListToolsResult(tools=[
            McpTool(name=t.name, description=t.description, input_schema=t.model.model_json_schema(),
                    annotations=ToolAnnotations(read_only_hint=t.read_only, destructive_hint=False))
            for t in guard.tools.values()])

    async def on_call_tool(ctx: Any, params: Any) -> CallToolResult:
        nonlocal active
        if active >= POOL:
            payload, is_error = await anyio.to_thread.run_sync(guard.busy, params.name, params.arguments,
                                                               limiter=spare)
        else:
            active += 1
            try:
                # Not abandoned on cancellation: a started call always reaches its result record.
                payload, is_error = await anyio.to_thread.run_sync(guard.call, params.name, params.arguments,
                                                                   abandon_on_cancel=False, limiter=workers)
            finally:
                active -= 1
        # ensure_ascii escapes every control and invisible character
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=True))],
                              is_error=is_error)

    return Server("hubspot-governed", version="0.1.0", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


def build(root: Path = ROOT, *, hs_transport: Any = None, slack_transport: Any = None, create: Any = None,
          clock: Clock | None = None) -> Guard:
    quiet_loggers()
    clock = clock or Clock()
    state = ensure_state_dir(root / "state")
    env = load_env(root / ".env.server",
                   ["HUBSPOT_READ_KEY", "ANTHROPIC_API_KEY", "SLACK_WEBHOOK_URL", "HUBSPOT_PORTAL_ID"],
                   forbidden=["HUBSPOT_WRITE_KEY"])
    policy, policy_hash = load_policy(root / "policy.yaml")
    store = Store(state, policy.tz, clock)
    audit = Audit(state, "server", policy_hash=policy_hash, clock=clock)
    audit.check_tail()
    hs = HubSpot(env["HUBSPOT_READ_KEY"], hs_transport, clock)
    reads = ReadTools(hs, store, clock)
    create = create or make_client(env["ANTHROPIC_API_KEY"]).beta.messages.create
    tools = build_tools(
        reads, ResearchTool(reads, store, policy, state, create, clock),
        ProposalTools(reads, hs, store, policy, clock),
        SlackTool(store, policy, audit, state, env["SLACK_WEBHOOK_URL"], env["HUBSPOT_PORTAL_ID"],
                  slack_transport, clock))
    guard = Guard(policy, audit, store, tools)
    try:
        audit.append("start", result="server started")
    except AuditError:
        raise StartupError("audit log cannot be written") from None
    return guard


async def serve(guard: Guard) -> None:
    server = make_server(guard)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> int:
    try:
        guard = build()
    except StartupError as e:
        print(f"hubspot-mcp: {e}", file=sys.stderr)
        return 1
    except Exception:
        log_exception("startup")
        return 1
    anyio.run(serve, guard)
    return 0


if __name__ == "__main__":
    sys.exit(main())
