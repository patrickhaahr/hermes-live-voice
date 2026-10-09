"""Codex Live broker: call ownership at the HTTP boundary.

The routes run in a real FastAPI app against `fake_codex_app_server.py`, a
stand-in for `codex app-server` that emits per-thread events like the real one
and logs what it was asked to do. The fake has no notion of calls or owners, so
every isolation property below is the broker's own. These are test doubles: they
do not prove anything about the subscription service itself (see the live
verification notes in docs/live-voice-recipe.md).
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

REPO = Path(__file__).resolve().parents[1]
FAKE = Path(__file__).with_name("fake_codex_app_server.py")


class Plugin:
    def __init__(self, module, log: Path):
        self.module = module
        self.log = log
        self.app = FastAPI()
        self.app.include_router(module.router)

    def received(self, method: str | None = None) -> list[dict]:
        if not self.log.exists():
            return []
        entries = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        return [e for e in entries if method is None or e.get("method") == method]

    def params(self, method: str) -> list[dict]:
        return [e.get("params") or {} for e in self.received(method)]

    def stopped_threads(self) -> list[str]:
        return [p["threadId"] for p in self.params("thread/realtime/stop")]

    def wait_for(self, predicate, timeout: float = 3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(0.02)
        raise AssertionError("condition not reached; codex received: " + json.dumps(self.received())[:2000])

    def run(self, *requests):
        """Send (path, body) requests concurrently; return the responses in order."""
        async def go():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://plugin") as client:
                return await asyncio.gather(*(client.post(path, json=body) for path, body in requests))
        return asyncio.run(go())

    def call(self, offer: str, language: str = "en") -> httpx.Response:
        return self.run(("/codexlive/session", {"offer": offer, "language": language}))[0]


@pytest.fixture
def plugin_factory(tmp_path, monkeypatch):
    loaded = []

    def make(mode: str = "", settings: dict | None = None, slow_s: float = 0.6) -> Plugin:
        root = tmp_path / f"plugin-{len(loaded)}"
        shutil.copytree(REPO / "dashboard", root / "dashboard", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(REPO / "language_directive.txt", root / "language_directive.txt")
        if settings is not None:
            (root / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        log = root / "codex.log"
        codex = root / "codex"
        codex.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n', encoding="utf-8")
        codex.chmod(0o755)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("TALK_CODEX_BINARY", str(codex))
        monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
        monkeypatch.setenv("FAKE_CODEX_MODE", mode)
        monkeypatch.setenv("FAKE_CODEX_SLOW_S", str(slow_s))
        spec = importlib.util.spec_from_file_location(f"plugin_api_{uuid.uuid4().hex}", root / "dashboard" / "plugin_api.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        loaded.append(module)
        return Plugin(module, log)

    yield make
    for module in loaded:
        module._LIVE.close()


def test_overlapping_calls_are_independent(plugin_factory):
    """A second call neither waits for, replaces, nor inherits the first."""
    plugin = plugin_factory()
    started = time.monotonic()
    finished = {}

    async def timed(client, name, body):
        response = await client.post("/codexlive/session", json=body)
        finished[name] = time.monotonic() - started
        return response

    async def go():
        transport = httpx.ASGITransport(app=plugin.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://plugin") as client:
            return await asyncio.gather(
                timed(client, "desktop", {"offer": "offer-desktop slow", "language": "da"}),
                timed(client, "phone", {"offer": "offer-phone", "language": "en"}),
            )

    desktop, phone = asyncio.run(go())
    assert desktop.status_code == phone.status_code == 200, (desktop.text, phone.text)
    desktop, phone = desktop.json(), phone.json()
    assert desktop["answer"] == "answer:offer-desktop slow"
    assert phone["answer"] == "answer:offer-phone"
    assert finished["phone"] < finished["desktop"], "the phone call waited for the desktop call"

    # Each call has its own fresh, restricted thread and its own language.
    assert desktop["threadId"] != phone["threadId"]
    starts = {p["threadId"]: p for p in plugin.params("thread/realtime/start")}
    assert "LANGUAGE AND VOICE: Speak English." in starts[phone["threadId"]]["prompt"]
    assert "LANGUAGE AND VOICE: Speak Danish." in starts[desktop["threadId"]]["prompt"]
    assert all(p["realtimeStartInstructions"].startswith("You are connected") for p in starts.values())
    assert all(p["clientManagedHandoffs"] is True for p in starts.values())

    # Interrupt and stop reach only the call they name.
    interrupted, stopped = plugin.run(
        ("/codexlive/interrupt", {"threadId": desktop["threadId"], "turnId": "turn-9"}),
        ("/codexlive/stop", {"threadId": phone["threadId"]}),
    )
    assert interrupted.json() == {"ok": True}
    assert stopped.json() == {"ok": True, "stopped": True}
    assert plugin.params("turn/interrupt") == [{"threadId": desktop["threadId"], "turnId": "turn-9"}]
    assert plugin.stopped_threads() == [phone["threadId"]]

    # A stop without an owner, or for a call that already ended, touches nothing.
    unowned = plugin.run(("/codexlive/stop", {}), ("/codexlive/stop", {"threadId": phone["threadId"]}),
                         ("/codexlive/interrupt", {"threadId": phone["threadId"], "turnId": "turn-1"}))
    assert [r.json().get("stopped") for r in unowned[:2]] == [None, False]
    assert unowned[2].json()["ok"] is False
    assert plugin.stopped_threads() == [phone["threadId"]]

    # The phone calls again: a new thread, while the desktop call is still live.
    again = plugin.call("offer-phone-2").json()
    assert again["threadId"] not in (desktop["threadId"], phone["threadId"])
    assert plugin.run(("/codexlive/stop", {"threadId": desktop["threadId"]}))[0].json()["stopped"] is True
    assert plugin.stopped_threads() == [phone["threadId"], desktop["threadId"]]


def test_failed_startup_and_its_late_answer_stay_with_their_call(plugin_factory):
    plugin = plugin_factory()
    healthy = plugin.call("offer-desktop").json()

    failed = plugin.call("offer-phone fail")
    assert failed.status_code == 502
    assert failed.json()["detail"].startswith("LIVE_START_FAILED")
    failed_thread = plugin.params("thread/realtime/start")[-1]["threadId"]
    assert plugin.stopped_threads() == [failed_thread]

    # The failed call's late SDP arrives while the next call is starting.
    retry = plugin.call("offer-phone-retry slow")
    assert retry.status_code == 200
    assert retry.json()["answer"] == "answer:offer-phone-retry slow"

    assert plugin.run(("/codexlive/interrupt", {"threadId": healthy["threadId"], "turnId": "t"}))[0].json() == {"ok": True}
    assert healthy["threadId"] not in plugin.stopped_threads()


def test_startup_timeout_closes_the_attempt(plugin_factory):
    plugin = plugin_factory(slow_s=0.5)
    plugin.module._LIVE_START_TIMEOUT_S = 0.25
    healthy = plugin.call("offer-desktop").json()

    late = plugin.call("offer-phone slow")
    assert late.status_code == 502
    assert late.json()["detail"].startswith("LIVE_TIMEOUT")
    late_thread = plugin.params("thread/realtime/start")[-1]["threadId"]
    assert plugin.stopped_threads() == [late_thread]
    assert plugin.run(("/codexlive/stop", {"threadId": late_thread}))[0].json()["stopped"] is False

    time.sleep(0.5)  # the answer shows up after the attempt was abandoned
    assert plugin.run(("/codexlive/interrupt", {"threadId": healthy["threadId"], "turnId": "t"}))[0].json() == {"ok": True}
    assert healthy["threadId"] not in plugin.stopped_threads()


def test_backing_thread_never_executes_delegated_work(plugin_factory):
    """The realtime core opens a backing turn per delegation; the broker stops it and declines its tools."""
    plugin = plugin_factory()
    other = plugin.call("offer-desktop").json()
    phone = plugin.call("offer-phone delegate").json()

    plugin.wait_for(lambda: any(e.get("id") == "approval-" + phone["threadId"] for e in plugin.received()))
    approval = next(e for e in plugin.received() if e.get("id") == "approval-" + phone["threadId"])
    assert approval["result"] == {"decision": "decline"}
    interrupts = plugin.params("turn/interrupt")
    assert interrupts and {p["threadId"] for p in interrupts} == {phone["threadId"]}
    assert other["threadId"] not in plugin.stopped_threads()

    thread = plugin.params("thread/start")[-1]
    assert thread["environments"] == [] and thread["ephemeral"] is True
    assert thread["sandbox"] == "read-only" and thread["approvalPolicy"] == "untrusted"
    assert Path(thread["cwd"]).is_dir() and not any(Path(thread["cwd"]).iterdir())


@pytest.mark.parametrize("mode, settings, code", [
    ("old", None, "LIVE_UNSUPPORTED"),
    ("ignores-limits", None, "LIVE_UNSAFE"),
    ("strict-fields", None, "LIVE_UNSUPPORTED"),
    ("", {"delegation": "server"}, "LIVE_UNSUPPORTED"),
])
def test_unsafe_or_unsupported_backends_fail_without_switching_lanes(plugin_factory, mode, settings, code):
    plugin = plugin_factory(mode=mode, settings=settings)
    response = plugin.call("offer-phone")
    assert response.status_code == 502
    assert response.json()["detail"].startswith(code)
    starts = plugin.params("thread/realtime/start")
    assert all(p.get("clientManagedHandoffs") is True for p in starts), "retried on another execution lane"
    assert len(starts) <= 1
    if mode in ("old", "ignores-limits") or settings:
        assert starts == []


def test_answer_for_a_departed_client_is_not_left_running(plugin_factory):
    plugin = plugin_factory()
    body = json.dumps({"offer": "offer-phone", "language": "en"}).encode()
    messages = [{"type": "http.request", "body": body, "more_body": False}, {"type": "http.disconnect"}]
    sent = []

    async def receive():
        return messages.pop(0) if len(messages) > 1 else messages[0]

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "http", "path": "/codexlive/session", "raw_path": b"/codexlive/session",
             "query_string": b"", "root_path": "", "headers": [(b"content-type", b"application/json")],
             "client": ("127.0.0.1", 1), "server": ("plugin", 80)}
    asyncio.run(plugin.app(scope, receive, send))
    started = plugin.params("thread/realtime/start")
    assert len(started) == 1
    assert plugin.stopped_threads() == [started[0]["threadId"]]
    assert sent[0]["status"] != 200


def test_the_public_api_starts_isolated_calls_outside_the_dashboard(plugin_factory):
    """Hermes Gadget drives the broker from the gateway process, without the HTTP routes."""
    from concurrent.futures import ThreadPoolExecutor

    plugin = plugin_factory()
    api = plugin.module
    with ThreadPoolExecutor(2) as pool:
        slow = pool.submit(api.start_call, profile=None, offer="offer-desktop slow", language="da")
        phone = pool.submit(api.start_call, profile=None, offer="offer-phone", language="en").result()
        desktop = slow.result()
    assert phone["answer"] == "answer:offer-phone" and desktop["threadId"] != phone["threadId"]
    starts = {p["threadId"]: p for p in plugin.params("thread/realtime/start")}
    assert "LANGUAGE AND VOICE: Speak English." in starts[phone["threadId"]]["prompt"]
    assert starts[phone["threadId"]]["clientManagedHandoffs"] is True

    assert api.stop_call(phone["threadId"]) is True
    assert api.stop_call(phone["threadId"]) is False
    assert plugin.stopped_threads() == [phone["threadId"]]

    with pytest.raises(api.LiveCallError) as refused:
        api.start_call(profile="nobody", offer="offer-phone-2")
    assert refused.value.code == "LIVE_BAD_PROFILE"
    with pytest.raises(api.LiveCallError) as failed:
        api.start_call(profile=None, offer="offer-phone fail")
    assert failed.value.code == "LIVE_START_FAILED"
    assert len(plugin.params("thread/realtime/start")) == 3, "a refused profile started nothing"
    assert desktop["threadId"] not in plugin.stopped_threads()
