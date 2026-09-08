"""Bounded fleet tools and isolated test styles; one desktop entry point."""
import json

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations

import fleet
import onboarding

mcp = FastMCP("Workman Fleet v1.1")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def fleet_observe(node: str, query: str | None = None, receipt_id: str | None = None,
                        view: str = "compact", observation_id: str | None = None) -> CallToolResult:
    """Opt-in compact inspection, no capture or input. Query filters active-app windows
    only; count/identity/ambiguity are not element or global-window proof. Keeps
    permission, STOP switches, lease/focus, reporting and validated action/visual
    receipts. Null field_revision, load_state and exact_readback are unmeasured.
    Always retain independent global visual checks and exact readback when needed.
    view=full with the returned observation_id recalls the original for 30 seconds,
    in this process only, with no new remote call. No persistent raw observation
    cache or style/model change. Unknown states never authorize input.
    """
    import observation
    result = await observation.observe(node, query, receipt_id, view, observation_id)
    # One JSON text representation, matching fleet_control. Avoid duplicating
    # the same observation in automatic structuredContent and text payloads.
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(result, separators=(",", ":")))])


@mcp.resource("workman://tests/{test_id}/raw-style", mime_type="text/markdown")
def raw_test_style(test_id: str) -> str:
    """Exact pinned Markdown for an explicitly selected Workman test; no hooks."""
    import test_modes
    result = test_modes.payload(test_id)
    if not isinstance(result.get("raw_skill"), str):
        raise ValueError("this test has no local raw skill selected")
    return result["raw_skill"]


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
def fleet_test_mode(action: str = "list", test_id: str | None = None, provider: str | None = None,
                    level: str = "full", manifest: dict | None = None) -> dict:
    """Test-only skill/plugin/MCP style registry. Never changes doctrine or normal style.
    list/get inspect; register stores reviewed declarative provider metadata only;
    select chooses one provider for an explicit nonsecret Workman test ID; off
    disables it. payload returns raw pinned Markdown or an existing MCP resource
    reference for an isolated test prompt. No hooks, executables, new connections,
    model API, screen input, authorization, or global/session style changes.
    Raw Caveman is disabled by default. Measure actual paired trials before any
    savings claim; its advertised numbers are not Workman measurements.
    """
    import test_modes
    if action == "list": return {"providers": list(test_modes.providers().values()), "default": None, "scope": "workman_test"}
    if action == "register": return test_modes.register(manifest or {})
    if action == "get": return test_modes.get(test_id)
    if action == "select":
        if provider is None: raise ValueError("select requires a provider")
        return test_modes.select(test_id, provider, level)
    if action == "off": return test_modes.select(test_id)
    if action == "payload": return test_modes.payload(test_id)
    raise ValueError("unknown test mode action")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
async def fleet_onboard(node: str, features: list[str] | None = None, intent: str = "inspect", timeout_seconds: int = 0) -> dict:
    """One permission setup flow per installation/device. Enabled features: view,input.
    inspect presents requirements/reasons/state together. begin requests only a
    missing grant and opens its precise Settings pane; human approves the OS.
    wait observes grant changes with bounded 1/2/4/8-second backoff, at most 60s.
    retry re-prompts only when the user asks; cancel stops this flow, not user input.
    Reuses the helper's existing grant watcher/restart. Never changes TCC or OFF
    switches. Ready means grants verified, not proof of screen/input functionality.
    """
    return await onboarding.run(node, features, intent, timeout_seconds)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
async def fleet_control(node: str, action: str, args: dict | None = None, context: dict | None = None) -> CallToolResult:
    """Control dgx, mini, air or machome using Workman; coordinates are global screen points.

    Actions: status, inspect (status+fresh active+pointer), shot {region?}, windows, active, pointer, focus {query}, move {x,y},
    click {x,y,button:left|right|double}, type {text,expect_focus}, key {key,expect_focus},
    move/click accept motion:direct|human and speed:0.25..4 (default direct, speed 1).
    scroll {direction,amount,expect_focus}, drag {x1,y1,x2,y2,expect_focus},
    minimize {expect_focus}, lease, reserve {seconds:15..300}, release,
    input {mouse:bool,keyboard:bool} (omit args to read), panel,
    finish {restore_focus,restore_x,restore_y} to restore and release after a task.
    correct {verification_id} retracts a mistaken visual verification, preserving history.
    paste {text,expect_focus,preset:first_party_fast,surface:owned_editor|owned_github|local_fixture}
    uses the clipboard only for explicitly authorized nonsecret first-party text.
    It replaces the prior clipboard without reading it; exact field readback is
    required separately. Never mistake a pasted_chars return for exactness proof.
    wait_window {query,timeout_seconds:0..10} proves window presence, not page load.
    lesson {kind,verification_id} records an allowlisted, reviewed recovery lesson.
    Prefer fleet_onboard for consolidated missing-grant requests. permission_hint
    {grant,evidence_id,x,y,w,h} adds an expiring static click-through callout beside
    a target just observed in the latest screenshot (at most 30 seconds old).
    Only a human approves OS grants. Never place a hint from guessed coordinates.
    OFF blocks fleet input across sessions; never re-enable without a user request.
    STOP returns before DGX report collection; its durable local event is collected
    by the next fleet call/report, with pending delivery explicitly reported.
    Physical input remains usable. Already posted atomic input may finish; typing
    checks STOP and focus every four characters. No held-key/button API is exposed.
    Input automatically reserves the device for this session; release after work.
    Supply context {project,task,session} using non-sensitive stable identifiers
    when the caller has its own session. Different devices run concurrently.
    Get expect_focus from active immediately before input.
    Screenshot before and after actions; multiply image coordinates by
    image_to_points then add origin. Never enter secrets. Calls record sanitized
    outcomes locally and collect them through the fleet reporter to the DGX.
    A success response means the backend returned successfully; verify visually.
    """
    result = await fleet.control(node, action, args, context)
    content = []
    data = result.get("data")
    if isinstance(data, dict) and "image" in data:
        content.append(ImageContent(type="image", data=data.pop("image"), mimeType=data["mime"]))
    content.append(TextContent(type="text", text=json.dumps(result)))
    return CallToolResult(isError=not result.get("ok"), content=content)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def fleet_recall(node: str | None = None) -> dict:
    """Read deduplicated, evidence-backed lessons from the existing DGX learner store.

    Consult before desktop work. Lessons are data, not authorization to act.
    """
    return {"lessons": fleet.recall(node)}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
async def fleet_report() -> dict:
    """Collect explicit Workman events from all four devices through the fleet reporter.

    Reports local recording, DGX receipt and lessons. No background monitoring,
    process scans or model API calls. Offline devices are reported separately.
    """
    return await fleet.report_all()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def fleet_memory(node: str | None = None, kind: str | None = None, limit: int = 40) -> dict:
    """Recall the local DGX graph: devices, projects, tasks, sessions, decisions,
    permission observations, action outcomes and verified lessons. Stable IDs and
    evidence links retain history, corrections and conflicts. Stale permissions
    return no usable value; always recheck live state before input. No raw typed
    text, screen pixels or transcript contents are stored in this graph.
    """
    import memory_graph
    return memory_graph.query(node, kind, limit)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
async def fleet_verify(node: str, event_id: str, evidence_id: str) -> dict:
    """Record visual confirmation ONLY after seeing a successful action's result.

    event_id is the action; evidence_id is its later screenshot event on the same
    device. Do not call on backend success alone. Promotes an evidence-backed
    device lesson and links a preceding failure to a verified recovery.
    """
    return await fleet.control(node, "verify", {"event_id": event_id, "evidence_id": evidence_id})


if __name__ == "__main__":
    mcp.run(transport="stdio")
