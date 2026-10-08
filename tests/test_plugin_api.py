"""Regression tests for the audit findings in issue #1 (talk-desktop backend).

The AST-extraction technique used here (load one top-level function out of
`dashboard/plugin_api.py` and exec it in a namespace with stubs) is adapted
from the reproduction script in issue #1, thanks @whyyagswhy.

No network, no codex binary, no OpenAI calls: everything is stubbed.
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
PLUGIN_API = REPO / "dashboard" / "plugin_api.py"
VENDOR = REPO / "dashboard" / "talk_vendor"


def load_unit(path: Path, name: str) -> dict:
    """Extract one top-level function `name` from `path` and exec it with stub globals."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = [n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    assert len(nodes) == 1, f"expected exactly one {name} in {path}"
    node = nodes[0]
    node.decorator_list = []
    unit = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")],
                             level=0), node],
        type_ignores=[],
    )
    ast.fix_missing_locations(unit)
    ns: dict = {"asyncio": asyncio, "time": time, "json": json}
    exec(compile(unit, str(path), "exec"), ns)
    return ns


def test_vendored_bundle_imports_without_hermes_talk(tmp_path):
    """#1: on a clean install (no ~/.hermes/plugins/hermes-talk) talk_auth must come from the bundle.

    `-S` keeps site-packages (and any pip-installed talk_* module) out of the
    child's sys.path; httpx is stubbed because the vendored bundle legitimately
    imports it at module level and `-S` removes the environment that provides it.
    """
    child = tmp_path / "child.py"
    child.write_text(textwrap.dedent(f"""
        import importlib.util, json, sys, types
        stub = types.ModuleType("httpx")
        def _blocked(*a, **k):
            raise AssertionError("httpx must not be called during import")
        stub.post = _blocked
        stub.get = _blocked
        sys.modules["httpx"] = stub
        spec = importlib.util.spec_from_file_location("plugin_api", {str(PLUGIN_API)!r})
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        import talk_auth
        print(json.dumps({{"talk_auth": talk_auth.__file__ or "", "vendor": {str(VENDOR)!r}}}))
    """), encoding="utf-8")
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)  # empty home: hermes-talk is NOT installed
    env["PYTHONPATH"] = ""
    res = subprocess.run([sys.executable, "-S", str(child)],
                         capture_output=True, text=True, timeout=60, env=env)
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout.strip().splitlines()[-1])
    assert Path(payload["talk_auth"]).resolve().is_relative_to(Path(payload["vendor"]).resolve()), payload


def test_create_session_concurrent_mints_do_not_deadlock():
    """#3: two concurrent /session mints must complete; the lock lives inside the worker.

    Pre-fix this deadlocks the event loop: a threading.Lock held across
    `await asyncio.to_thread(...)` lets the second coroutine block the loop
    thread, so the first can never be resumed to release it.
    """
    ns = load_unit(PLUGIN_API, "create_session")
    ns.update(
        _MINT_LOCK=threading.Lock(),
        _resolve_voice=lambda _: "marin",
        _profile_home=lambda _p: Path("/tmp"),
        _bot_display_name=lambda p: "Luna",
        HTTPException=type("HTTPException", (Exception,), {}),
    )

    class TalkConfigStub:
        get_hermes_home = staticmethod(lambda: Path.home())

    ns["talk_config"] = TalkConfigStub

    def mint(*args):
        time.sleep(0.05)
        return SimpleNamespace(to_wire=lambda: {}), SimpleNamespace(source="stub")

    ns["_mint_for"] = mint

    class Request:
        async def json(self):
            return {}

    async def run():
        return await asyncio.wait_for(
            asyncio.gather(ns["create_session"](Request()), ns["create_session"](Request())),
            timeout=3,
        )

    # Pre-fix this is a hard deadlock of the event loop itself (the thread
    # blocked on _MINT_LOCK can never be resumed, so not even wait_for fires).
    # Run the loop on a daemon thread with a join guard: a regression fails the
    # test cleanly instead of hanging the whole suite.
    outcome: dict = {}

    def target():
        try:
            outcome["value"] = asyncio.run(run())
        except BaseException as exc:  # noqa: BLE001
            outcome["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive(), "event loop deadlocked on _MINT_LOCK"
    assert "error" not in outcome, outcome.get("error")
    responses = outcome["value"]
    assert all(r["ok"] for r in responses)
