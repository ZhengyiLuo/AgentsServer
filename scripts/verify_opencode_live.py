#!/usr/bin/env python3
"""Opt-in real OpenCode smoke test through AgentsServer's HTTP application.

Uses temporary server/config/provider state and scratch workspaces. Requires a
locally installed OpenCode CLI and network access for the selected free model.
No production server is contacted or restarted. Run with the server's Python:
  python scripts/verify_opencode_live.py --binary /path/to/opencode
Temporary diagnostics are retained at the printed path; no credentials are
copied from the operator's OpenCode state.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time


async def verify(server, root: Path, model: str) -> None:
    import httpx

    await server.CROSS_CHAT.initialize()
    server.AGENT_TOKEN = "isolated-opencode-smoke"
    results = []
    sessions = []
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app),
        base_url="http://isolated-test",
        headers={"x-agentsdock-token": server.AGENT_TOKEN},
        timeout=180,
    )

    async def api(method, path, **kwargs):
        response = await client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    def events(sid):
        path = server.events_path(sid)
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    async def create(name, **options):
        workspace = root / name
        workspace.mkdir()
        payload = await api("POST", "/api/sessions", json={
            "backend": "opencode", "cwd": str(workspace), "model": model,
            **options,
        })
        sid = payload["session"]["id"]
        sessions.append(sid)
        return sid, workspace

    async def begin(sid, prompt, *, file_ids=None):
        payload = {"prompt": prompt}
        if file_ids:
            payload["file_ids"] = list(file_ids)
        await api("POST", f"/api/sessions/{sid}/turns", json=payload)

    async def finish(sid, previous, *, success=True):
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            terminals = [e for e in events(sid) if e["type"] == "turn_finished"]
            if len(terminals) > previous and sid not in server.BUSY_SESSIONS:
                terminal = terminals[-1]
                if success:
                    errors = [e.get("message") for e in events(sid)
                              if e["type"] == "error" and e.get("run_id") == terminal.get("run_id")]
                    assert not terminal.get("is_error"), errors
                    assert terminal.get("exit_code") == 0, terminal
                return terminal
            await asyncio.sleep(0.1)
        raise AssertionError("live turn exceeded 150-second smoke deadline")

    async def turn(sid, prompt, *, success=True, file_ids=None):
        previous = sum(e["type"] == "turn_finished" for e in events(sid))
        await begin(sid, prompt, file_ids=file_ids)
        return await finish(sid, previous, success=success)

    def record(name, **details):
        result = {"check": name, "passed": True, **details}
        results.append(result)
        print(json.dumps(result), flush=True)

    try:
        health = await api("GET", "/api/health")
        assert health["capabilities"]["opencode_backend"]["available"]
        diagnostic = await asyncio.to_thread(server.probe_runtime, "opencode")
        assert diagnostic["status"] == "ready", diagnostic.get("message")
        catalog = await asyncio.to_thread(server.discover_opencode_catalog)
        assert model in [item["value"] for item in catalog["models"]], catalog
        record("runtime_and_catalog", server_version=health["server_version"], model=model)

        missing_workspace = root / "missing-workspace"
        invalid = await client.post("/api/sessions", json={
            "backend": "opencode", "cwd": str(missing_workspace), "model": model,
        })
        assert invalid.status_code == 400, invalid.text
        workspace_file = root / "workspace-file"
        workspace_file.write_text("not a directory")
        invalid = await client.post("/api/sessions", json={
            "backend": "opencode", "cwd": str(workspace_file), "model": model,
        })
        assert invalid.status_code == 400, invalid.text
        record("invalid_workspace", missing_rejected=True, file_rejected=True)

        (first, workspace), (second, workspace2) = await asyncio.gather(
            create("workspace-a"), create("workspace-b"),
        )
        async def proof(sid, path, marker):
            terminal = await turn(sid,
                f"Isolated integration test. Remember marker {marker}. "
                f"Use a tool to create ONLY proof.txt in your current workspace, containing exactly {marker}. "
                "Do not contact AgentsDock helpers. Reply with the marker when finished.")
            assert (path / "proof.txt").read_text().strip() == marker
            types = {e["type"] for e in events(sid)}
            assert {"tool_started", "tool_finished", "assistant_text"} <= types, types
            assert terminal.get("input_tokens", 0) > 0
            return (await api("GET", f"/api/sessions/{sid}"))["session"]["opencode_session_id"]

        provider_a, provider_b = await asyncio.gather(
            proof(first, workspace, "LIME-48271"),
            proof(second, workspace2, "PLUM-93608"),
        )
        assert provider_a and provider_b and provider_a != provider_b
        record("two_concurrent_sessions", distinct_provider_sessions=True, files_isolated=True)

        terminal = await turn(first, "Without tools, repeat the marker I asked you to remember in my previous message. Nothing else.")
        assert "LIME-48271" in terminal["result_text"], terminal["result_text"]
        session = (await api("GET", f"/api/sessions/{first}"))["session"]
        assert session["opencode_session_id"] == provider_a
        record("resume", remembered_marker=True, provider_session_unchanged=True)

        moved = root / "workspace-moved"
        moved.mkdir()
        await api("PATCH", f"/api/sessions/{first}", json={"cwd": str(moved)})
        await turn(first, "Reply exactly MOVED-OK. Do not use tools.")
        session = (await api("GET", f"/api/sessions/{first}"))["session"]
        assert session["opencode_session_id"] != provider_a
        assert any(e["type"] == "provider_session_reset" for e in events(first))
        record("workspace_change", new_provider_session=True, visible_reset=True)

        dead, _ = await create("workspace-dead")
        server.STORE.sessions[dead]["opencode_session_id"] = "ses_missing_readiness_test"
        server.STORE.sessions[dead]["opencode_session_cwd"] = server.STORE.sessions[dead]["cwd"]
        terminal = await turn(dead, "Reply OK. No tools.", success=False)
        assert terminal["is_error"]
        assert not server.STORE.sessions[dead].get("opencode_session_id")
        record("missing_resume", explicit_error=True, dead_pointer_cleared=True)

        permission_sid, permission_workspace = await create(
            "workspace-permissions", opencode_permission_mode="plan",
        )
        await turn(permission_sid,
            "Isolated permission test. Try to create forbidden.txt containing NO. "
            "If write tools are unavailable, reply BLOCKED and stop immediately. "
            "Do not delegate, retry, or work around unavailable tools.")
        assert not (permission_workspace / "forbidden.txt").exists()
        record("plan_permission", requested_file_not_created=True)

        uploaded = await api(
            "POST", f"/api/sessions/{permission_sid}/files",
            files={"file": ("outside-note.txt", b"APRICOT-73159", "text/plain")},
        )
        file_record = uploaded["file"]
        terminal = await turn(
            permission_sid,
            "Read the attached text file and reply with its exact contents. Do not modify it.",
            file_ids=[file_record["id"]],
        )
        assert "APRICOT-73159" in terminal["result_text"], terminal["result_text"]
        assert Path(file_record["path"]).read_text() == "APRICOT-73159"
        record("external_attachment", readable_in_plan=True, source_unchanged=True)

        permission_before = (
            await api("GET", f"/api/sessions/{permission_sid}")
        )["session"]["opencode_session_id"]
        assert permission_before
        (permission_workspace / "opencode.json").write_text(json.dumps({
            "permission": {"bash": "deny", "write": "deny", "edit": "deny"},
        }))
        await api("PATCH", f"/api/sessions/{permission_sid}", json={"opencode_permission_mode": "full_access"})
        permission_reset = (
            await api("GET", f"/api/sessions/{permission_sid}")
        )["session"]
        assert not permission_reset.get("opencode_session_id"), permission_reset
        assert any(
            e["type"] == "provider_session_reset"
            and e.get("previous_provider_session_id") == permission_before
            for e in events(permission_sid)
        )
        await turn(permission_sid,
            "The operator enabled full access for this isolated test. Use a tool "
            "to create allowed.txt containing exactly ALLOWED. No other changes or helpers.")
        assert (permission_workspace / "allowed.txt").read_text().strip() == "ALLOWED"
        permission_after = (
            await api("GET", f"/api/sessions/{permission_sid}")
        )["session"]["opencode_session_id"]
        assert permission_after and permission_after != permission_before
        record(
            "full_access_permission", explicit_override_writes_file=True,
            provider_session_rotated=True, visible_reset=True,
        )

        invalid, _ = await create("workspace-invalid-model", model="opencode/readiness-model-does-not-exist")
        terminal = await turn(invalid, "Reply OK.", success=False)
        assert terminal["is_error"]
        assert any(e["type"] == "error" for e in events(invalid))
        assert invalid not in server.BUSY_SESSIONS
        record("invalid_model", visible_error=True, turn_slot_released=True)

        stopping, stop_workspace = await create("workspace-stop")
        await turn(stopping, "Reply exactly READY-TO-STOP. Do not use tools.")
        stop_provider = (
            await api("GET", f"/api/sessions/{stopping}")
        )["session"]["opencode_session_id"]
        assert stop_provider
        await begin(stopping,
            "Isolated cancellation test. Use bash to execute exactly: "
            "printf started > started.txt; sleep 90; printf finished > finished.txt . "
            "Do not use any other tools, helpers, or background processes.")
        deadline = time.monotonic() + 90
        while not (stop_workspace / "started.txt").exists():
            assert time.monotonic() < deadline, "provider never started cancellation test tool"
            assert sum(e["type"] == "turn_finished" for e in events(stopping)) == 1, "provider skipped cancellation test tool"
            await asyncio.sleep(0.1)

        wrapper = await api("POST", "/api/sessions", json={
            "backend": "opencode", "cwd": str(stop_workspace), "model": model,
            "provider_session_id": stop_provider, "import_history": False,
        })
        wrapper_sid = wrapper["session"]["id"]
        sessions.append(wrapper_sid)
        conflict = await client.post(
            f"/api/sessions/{wrapper_sid}/turns", json={"prompt": "Reply CONFLICT."},
        )
        assert conflict.status_code == 409, conflict.text
        assert wrapper_sid not in server.BUSY_SESSIONS
        record("shared_provider_serialization", concurrent_wrapper_rejected=True)

        started = time.monotonic()
        active_proc = server.ACTIVE[stopping]["proc"]
        await api("POST", f"/api/sessions/{stopping}/stop")
        terminal = await finish(stopping, 1, success=False)
        assert terminal.get("stopped"), terminal
        assert stopping not in server.ACTIVE and stopping not in server.BUSY_SESSIONS
        assert active_proc.returncode is not None
        assert not (stop_workspace / "finished.txt").exists()
        stopped_session = (await api("GET", f"/api/sessions/{stopping}"))["session"]
        assert not stopped_session.get("opencode_session_id"), stopped_session
        assert not stopped_session.get("session_id"), stopped_session
        assert any(
            e["type"] == "provider_session_reset"
            and e.get("previous_provider_session_id") == stop_provider
            for e in events(stopping)
        )
        record(
            "stop_live_tool", elapsed_seconds=round(time.monotonic() - started, 2),
            runner_exited=True, provider_session_quarantined=True,
            visible_reset=True,
        )

        await turn(stopping, "Reply exactly FRESH-AFTER-STOP. Do not use tools.")
        fresh_provider = (
            await api("GET", f"/api/sessions/{stopping}")
        )["session"]["opencode_session_id"]
        assert fresh_provider and fresh_provider != stop_provider
        record("post_stop_fresh_session", provider_session_rotated=True)
        print(json.dumps({"passed": len(results), "root": str(root)}), flush=True)
    finally:
        for sid in sessions:
            if sid in server.BUSY_SESSIONS or sid in server.ACTIVE:
                await asyncio.wait_for(server.stop_turn(sid), timeout=30)
        await client.aclose()
        (root / "results.json").write_text(json.dumps(results, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--model", default="opencode/big-pickle")
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="opencode-live-readiness-"))
    for name, suffix in {
        "AGENTSDOCK_STATE_DIR": "state", "AGENTS_SERVER_STATE_DIR": "state",
        "AGENTS_SERVER_CONFIG_DIR": "config", "XDG_CONFIG_HOME": "xdg-config",
        "XDG_DATA_HOME": "xdg-data", "XDG_CACHE_HOME": "xdg-cache",
        "XDG_STATE_HOME": "xdg-state",
    }.items():
        path = root / suffix
        path.mkdir(exist_ok=True)
        os.environ[name] = str(path)
    os.environ["OPENCODE_BIN"] = str(Path(args.binary).resolve())
    os.environ.pop("OPENCODE_CONFIG_CONTENT", None)
    os.environ.pop("OPENCODE_CONFIG", None)
    os.environ.pop("OPENCODE_CONFIG_DIR", None)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    print(f"Isolated diagnostics: {root}", flush=True)
    import agent_server
    asyncio.run(verify(agent_server, root, args.model))


if __name__ == "__main__":
    main()
