#!/usr/bin/env python3
"""Live check: two reference clients make overlapping Codex Live calls.

This places REAL calls on the ChatGPT subscription the local `codex login`
belongs to and spends its voice allowance (a run takes about two minutes of two
calls). It mounts this checkout's broker routes in-process, without the Hermes
dashboard, and drives them with two WebRTC clients that speak recorded WAV
utterances and read the realtime data channel.

Requirements: `pip install aiortc numpy fastapi httpx` and a directory with
16-bit mono WAV files named delegate.wav, chat.wav, story.wav, bargein.wav and
still.wav (see docs/live-voice-recipe.md for the phrases used).

    python tools/live_call_check.py /path/to/wavs

The script prints one JSON report and exits non-zero if a check failed.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
import time
import wave
from fractions import Fraction
from pathlib import Path

import httpx
import numpy as np
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from av import AudioFrame
from fastapi import FastAPI

REPO = Path(__file__).resolve().parents[1]
RATE = 48000
FRAME = 960


class Speaker(MediaStreamTrack):
    """Microphone stand-in: silence, or the queued utterance in real time."""

    kind = "audio"

    def __init__(self):
        super().__init__()
        self.pts = 0
        self.t0 = None
        self.queue = np.zeros(0, dtype=np.int16)

    def say(self, path: Path) -> None:
        with wave.open(str(path)) as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
            rate = w.getframerate()
        n = int(len(pcm) * RATE / rate)
        self.queue = np.concatenate([self.queue, np.interp(np.linspace(0, len(pcm) - 1, n), np.arange(len(pcm)), pcm).astype(np.int16)])

    async def recv(self):
        if self.t0 is None:
            self.t0 = time.monotonic()
        wait = self.t0 + self.pts / RATE - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        chunk, self.queue = self.queue[:FRAME], self.queue[FRAME:]
        if len(chunk) < FRAME:
            chunk = np.concatenate([chunk, np.zeros(FRAME - len(chunk), dtype=np.int16)])
        frame = AudioFrame.from_ndarray(chunk.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate, frame.pts, frame.time_base = RATE, self.pts, Fraction(1, RATE)
        self.pts += FRAME
        return frame


class Client:
    def __init__(self, name: str, http: httpx.AsyncClient, t0: float):
        self.name, self.http, self.t0 = name, http, t0
        self.events: list[tuple[float, dict]] = []
        self.thread_id = None
        self.pc = None
        self.mic = None
        self.delegation_reply = None

    def log(self, *parts):
        print(f"{time.monotonic() - self.t0:7.2f} [{self.name}]", *parts, file=sys.stderr, flush=True)

    async def start(self, language: str = "en") -> float:
        self.events = []
        self.pc = RTCPeerConnection()
        self.mic = Speaker()
        self.pc.addTrack(self.mic)
        dc = self.pc.createDataChannel("oai-events")
        self.dc = dc

        @dc.on("message")
        def on_message(data):
            try:
                event = json.loads(data)
            except ValueError:
                return
            self.events.append((time.monotonic(), event))
            kind = event.get("type")
            if kind == "delegation.created":
                self.log("delegation:", json.dumps(event["item"].get("content"))[:160])
                if self.delegation_reply:
                    dc.send(json.dumps({"type": "delegation.context.append", "delegation_item_id": event["item"]["id"],
                                        "content": [{"type": "input_text", "text": self.delegation_reply}]}))
            elif kind == "turn.done":
                turn = event.get("turn") or {}
                self.log(f"{turn.get('role')} turn done:", (turn.get("transcript") or "")[:120])

        @self.pc.on("track")
        def on_track(track):
            async def drain():
                while True:
                    try:
                        await track.recv()
                    except Exception:  # noqa: BLE001
                        return
            asyncio.ensure_future(drain())

        await self.pc.setLocalDescription(await self.pc.createOffer())
        began = time.monotonic()
        response = await self.http.post("/codexlive/session", json={
            "offer": self.pc.localDescription.sdp, "language": language, "voice": "cove"}, timeout=60)
        if response.status_code != 200:
            raise RuntimeError(f"{self.name}: session {response.status_code} {response.text[:300]}")
        body = response.json()
        self.thread_id = body["threadId"]
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=body["answer"], type="answer"))
        await self.wait(lambda e: e.get("type") == "session.started", 20, since=began)
        ready = time.monotonic() - began
        self.log(f"ready in {ready:.2f}s thread={self.thread_id} codex={body.get('codexVersion')}")
        return ready

    async def wait(self, predicate, timeout: float, since: float = 0.0) -> dict:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for at, event in list(self.events):
                if at >= since and predicate(event):
                    return event
            await asyncio.sleep(0.05)
        seen = [e.get("type") for at, e in self.events if at >= since]
        raise TimeoutError(f"{self.name}: event not seen within {timeout}s; rtc={self.pc.connectionState} "
                           f"ice={self.pc.iceConnectionState} events since={seen[-12:]}")

    def seen(self, predicate, since: float = 0.0) -> bool:
        return any(at >= since and predicate(e) for at, e in self.events)

    async def say_and_hear(self, wav: Path, timeout: float = 30, quiet: float = 3.0) -> str:
        """Speak, then return the reply: its turn.done transcript, or the streamed
        output once it has been quiet for `quiet` seconds (turn.done can be late)."""
        since = time.monotonic()
        self.mic.say(wav)
        await self.wait(lambda e: e.get("type") == "output_transcript.added", timeout, since=since)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            recent = [(at, e) for at, e in self.events if at >= since]
            done = [e for _, e in recent if e.get("type") == "turn.done" and assistant_turn(e)]
            if done:
                return done[0]["turn"].get("transcript") or ""
            output = [(at, e) for at, e in recent if e.get("type") == "output_transcript.added"]
            if time.monotonic() - output[-1][0] > quiet:
                self.log("reply streamed without turn.done")
                return "".join((e.get("item") or {}).get("text") or "" for _, e in output)
            await asyncio.sleep(0.1)
        raise TimeoutError(f"{self.name}: reply did not finish within {timeout}s")

    async def stop(self) -> dict:
        response = await self.http.post("/codexlive/stop", json={"threadId": self.thread_id})
        await self.pc.close()
        return response.json()


def assistant_turn(event):
    return (event.get("turn") or {}).get("role") == "assistant"


async def barge_in(client: Client, wavs: Path, other: Client) -> dict:
    since = time.monotonic()
    client.mic.say(wavs / "story.wav")
    created = await client.wait(lambda e: e.get("type") == "turn.created" and assistant_turn(e), 30, since=since)
    turn_id = created["turn"]["id"]
    await asyncio.sleep(2.5)
    route = (await client.http.post("/codexlive/interrupt", json={"threadId": client.thread_id, "turnId": turn_id})).json()
    cut_at = time.monotonic()
    client.mic.say(wavs / "bargein.wav")
    ended = await client.wait(lambda e: e.get("type") == "turn.done" and (e.get("turn") or {}).get("id") == turn_id, 30, since=since)
    answer = await client.wait(lambda e: e.get("type") == "turn.done" and assistant_turn(e)
                               and (e.get("turn") or {}).get("id") != turn_id, 30, since=cut_at)
    return {
        "interruptRoute": route,
        "interruptedTurnTranscript": ended["turn"].get("transcript"),
        "answerAfterBargeIn": answer["turn"].get("transcript"),
        "otherCallSawEvents": other.seen(lambda e: e.get("type") in ("turn.created", "delegation.created"), since=since),
    }


async def main(wavs: Path) -> int:
    records: list[logging.LogRecord] = []

    class Keep(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger("hermes.plugins.talk-desktop")
    logger.setLevel(logging.INFO)
    logger.addHandler(Keep())
    echo = logging.StreamHandler(sys.stderr)
    echo.setFormatter(logging.Formatter("%(relativeCreated)9.0fms broker %(message)s"))
    logger.addHandler(echo)

    spec = importlib.util.spec_from_file_location("talk_desktop_plugin_api", REPO / "dashboard" / "plugin_api.py")
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    app = FastAPI()
    app.include_router(plugin.router)
    report: dict = {"checks": {}}
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://broker", timeout=60) as http:
            desktop, phone = Client("desktop", http, t0), Client("phone", http, t0)
            phone.delegation_reply = "Task result (answer the user with this): the server has forty-two gigabytes free."

            ready = await asyncio.gather(desktop.start(), phone.start())
            report["readySeconds"] = {"desktop": round(ready[0], 2), "phone": round(ready[1], 2)}
            report["threads"] = {"desktop": desktop.thread_id, "phone": phone.thread_id}
            report["checks"]["distinct threads"] = desktop.thread_id != phone.thread_id

            since = time.monotonic()
            phone_reply, desktop_reply = await asyncio.gather(
                phone.say_and_hear(wavs / "delegate.wav"), desktop.say_and_hear(wavs / "chat.wav"))
            await asyncio.sleep(3)
            report["overlap"] = {"phoneReply": phone_reply, "desktopReply": desktop_reply}
            report["checks"]["phone delegated"] = phone.seen(lambda e: e.get("type") == "delegation.created", since)
            report["checks"]["phone spoke the client result"] = "42" in phone_reply or "forty" in phone_reply.lower()
            report["checks"]["desktop did not receive the phone delegation"] = not desktop.seen(
                lambda e: e.get("type") == "delegation.created", since)

            report["bargeInDesktop"] = await barge_in(desktop, wavs, phone)
            report["bargeInPhone"] = await barge_in(phone, wavs, desktop)
            for key in ("bargeInDesktop", "bargeInPhone"):
                report["checks"][f"{key}: other call quiet"] = not report[key]["otherCallSawEvents"]

            report["stopDesktop"] = await desktop.stop()
            report["phoneAfterDesktopHangUp"] = await phone.say_and_hear(wavs / "still.wav")
            desktop2 = Client("desktop-2", http, t0)
            report["readySeconds"]["desktop-2"] = round(await desktop2.start(), 2)
            report["checks"]["new call got a new thread"] = desktop2.thread_id not in (desktop.thread_id, phone.thread_id)
            report["stopPhone"] = await phone.stop()
            report["desktopAfterPhoneHangUp"] = await desktop2.say_and_hear(wavs / "still.wav")
            report["stopDesktop2"] = await desktop2.stop()
            report["checks"]["each hang-up stopped only its call"] = (
                report["stopDesktop"] == report["stopPhone"] == report["stopDesktop2"] == {"ok": True, "stopped": True}
                and bool(report["phoneAfterDesktopHangUp"]) and bool(report["desktopAfterPhoneHangUp"]))
            await asyncio.sleep(2)
    finally:
        plugin._LIVE.close()

    messages = [r.getMessage() for r in records]
    report["backingTurnsInterrupted"] = sum("interrupting backing turn" in m for m in messages)
    report["backingExecutionItems"] = [m for m in messages if "backing thread started" in m]
    report["declinedBackingRequests"] = [m for m in messages if "declined backing-thread request" in m]
    report["checks"]["delegation backing turn was interrupted"] = report["backingTurnsInterrupted"] >= 1
    report["checks"]["no backing execution item"] = not report["backingExecutionItems"]
    report["backingTurnOutcomes"] = [m for m in messages if "backing turn" in m and "ended" in m]
    report["checks"]["every backing turn ended interrupted and empty"] = (
        len(report["backingTurnOutcomes"]) == report["backingTurnsInterrupted"]
        and all(" ended interrupted with 0 items" in m for m in report["backingTurnOutcomes"])
        and not any("could not interrupt" in m for m in messages))
    report["ok"] = all(report["checks"].values())
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(asyncio.run(main(Path(sys.argv[1]))))
