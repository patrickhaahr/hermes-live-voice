# Live Voice — technical recipe (ChatGPT/Codex subscription realtime)

How this plugin runs **full-duplex GPT-Live-1 voice on a ChatGPT/Codex subscription**, with no API
key on the voice lane. Everything below was verified end-to-end against a real offer (SDP), real
audio and real events (September 2026, `codex-cli 0.154`; broker flow re-verified October 2026 on `0.160.0`).

## The idea

The open-source [Codex CLI](https://github.com/openai/codex) (≥ 0.154) contains a realtime
conversation mode backed by the user's ChatGPT subscription. A local broker can:

1. spawn `codex app-server` (stdio JSON-RPC),
2. open a **thread** and start a **realtime session** with a WebRTC offer,
3. hand the SDP answer back to the client.

Audio then flows **client ↔ backend directly** (WebRTC); the broker only signals. No credentials
ever leave the host: the app-server uses the local `codex login` (`~/.codex/auth.json`).

> Realtime calls go to `{base}/realtime/calls?intent=quicksilver&architecture=avas`. On boxes whose
> Codex default provider is a custom proxy, force the provider for the app-server process:
> `codex app-server --listen stdio:// --enable realtime_conversation -c model_provider="openai"`.

## Broker flow (server side)

```
codex app-server --listen stdio:// --enable realtime_conversation \
  --disable apps --disable plugins --disable shell_tool --disable unified_exec \
  --disable browser_use --disable computer_use --disable image_generation \
  --disable multi_agent --disable view_image --disable hooks \
  -c model_provider=openai -c 'web_search="disabled"'
initialize {capabilities:{experimentalApi:true}}                 # once per process; userAgent carries the version
# per call:
thread/start  { cwd: <empty private dir>, modelProvider: "openai", model?,
                ephemeral: true, environments: [], sandbox: "read-only", approvalPolicy: "untrusted" }
# check the response echoes ephemeral, environments [], sandbox readOnly without network, approvalPolicy
thread/realtime/start {
  threadId, transport: { type: "webrtc", sdp: <client offer> },
  outputModality: "audio", version: "v3", voice: "cove",
  prompt: <persona>, realtimeStartInstructions: <agent hints>,
  clientManagedHandoffs: true, delegationAckFiller: true
}
# notifications, each carrying threadId: thread/realtime/sdp → { sdp: <answer> } ;
# thread/realtime/started → { realtimeSessionId } ; thread/realtime/error ; thread/realtime/closed
thread/realtime/stop { threadId }                                # hang up this call only
```

Every response is matched to its request by JSON-RPC id and every notification to its call by
`threadId`; notifications for a thread with no live call are dropped. Do not cache a thread between
calls: a reused thread carries the previous call's context, and stopping "the" thread stops whichever
call happens to be on it.

The client renderer does: `RTCPeerConnection` + `createDataChannel("oai-events")` + mic track →
`createOffer()` → POST the SDP to the broker → `setRemoteDescription(answer)`.

## Protocol `v3` (FramelessBidi) — essentials

- **Version map**: `v1` → `gpt-realtime-1.5` (header `openai-alpha: quicksilver=v1`); `v3` →
  **`gpt-live-1-codex`** (header `quicksilver=v2`). AVAS endpoints require `quicksilver=v2` → use `v3`.
- **Voices (v1/v3)**: `cove, juniper, maple, spruce, ember, vale, breeze, arbor, sol` (default `cove`).
- **Events (datachannel)**:
  - `session.started` / `session.updated`, `session.usage.updated` → `{usage:{audio_duration_ms}, usage_limit:{status,reset_seconds}}`
  - `input_transcript.added` / `output_transcript.added` — **deltas**; `turn.created` / `turn.delta` /
    `turn.done` — turn bookkeeping. ⚠️ Turns **rotate** and `turn.done` can arrive with **partial
    transcripts** — never shrink or close a bubble from it; a bubble continues while same-role deltas
    keep arriving within ~3.5 s.
  - Audio from the model arrives as a **normal WebRTC media track** (`ontrack`) — do NOT build a
    PCM/`output_audio.delta` playback path.
- **Client → server messages accepted**: `session.update`, `session.context.append`,
  `response.create` *(only with Responses delegation)*, `delegation.context.append`,
  `delegation.function_call_output.create`, `input_audio.pause|resume`,
  `output_audio.playback.play`, `session.feedback`, `session.close`.
  **`conversation.item.create` does NOT exist in v3** (that's the v1 shape).
- **Text-triggered turn** (no mic needed): send
  `{"type":"session.context.append","content":[{"type":"input_text","text":"..."}]}` — the model
  answers with audio.

## Task delegation (tools from voice)

The voice model emits:

```json
{"type":"delegation.created","item":{"id":"item_…","type":"delegation",
 "content":[{"type":"input_text","text":"<the user's request>"}],
 "handoff_id":"handoff_1","target":"client","user_bidi_turn_id":"turn_…"}}
```

The **executor** is chosen when starting the session:

- `clientManagedHandoffs: true` → **the client runs the task** and answers with
  `{"type":"delegation.context.append","delegation_item_id":"item_…","content":[{"type":"input_text","text":"<result>"}]}`
  — the model then speaks/uses it. (This plugin routes the task to the user's focused Hermes chat,
  so it executes with the user's own models/providers.)
- `clientManagedHandoffs: false` → the **core** routes the delegation into the Codex thread agent
  and streams its output back into the session (observable as `delegation.context.appended` events).
  This broker never uses it: tasks must run in Hermes, not in a Codex thread.
- `delegationAckFiller: true` lets the model fill the wait with natural phrases (“let me check…”).

Notes: the `content` text carries the user's request when it came from real audio; with text-only
setups (`session.context.append` instead of speech) it can be empty — fall back to the transcript.
`delegation.function_call_output.create` expects an `item` object; `delegation.context.append` is the
working result channel.

## Gotchas (learned the hard way)

- **`codex` not found under systemd**: services don't include `~/.npm-global/bin` in `PATH`. Resolve
  the binary with candidate paths and spawn the app-server with an augmented `PATH`; keep
  `OPENAI_API_KEY`/`CODEX_API_KEY` **out** of the child env so the subscription auth is used.
- **Delegated turn 400**: if the machine's Codex default model isn't ChatGPT-valid (e.g. a custom
  provider proxy like `nousportal/…`), the thread agent fails with
  `"<model>" is not supported when using Codex with a ChatGPT account`. Force a valid model at
  `thread/start` (`gpt-5.6-sol`, `gpt-5.6-terra`, … — see the Codex model catalog).
- **Thread lifecycle**: one fresh ephemeral thread per call, never reused, so `thread not found` and
  `already running` cannot come from another call. If the app-server process exits, every call it
  served fails and the next call starts a new process (`initialize` takes about 2 s cold).
- **Voice allowance**: desktop/Codex voice runs on a **separate rolling 5-hour allowance** per plan
  (Plus ≈ 15–30 min, Pro 5x ≈ 1–2.5 h, Pro 20x unlimited). The plan-side counter is **not exposed**
  to clients (checked `wham/usage`, `account/rateLimits/read`, session events — `usage_limit` stays
  `null`), so metering is done locally from `audio_duration_ms`.
- **Phantom tool work (plan drain)**: `clientManagedHandoffs: true` only gates whether the thread's
  output streams back — the core **still routes every delegation into the Codex thread** (re-checked
  on 0.160.0: a `turn/started` with a `<realtime_delegation>` user message follows each
  `delegation.created`), where the agent can run real tool work in the background on the user's plan.
  A `skip`-without-tools instruction reduces this but is only a prompt. The broker enforces it
  instead: restricted thread (no environments, read-only, no network, untrusted approvals that it
  declines), tool features disabled on the app-server, and `turn/interrupt` sent for every backing
  turn as it starts. Verified on 0.160.0: the backing turn ends `interrupted` with **0 items** about
  0.1 s after it starts, and the voice still speaks the client's `delegation.context.append` result.
- **`turn.done` is not guaranteed**: a reply can stream `output_transcript.added` deltas and never
  get its `turn.done` (seen once in four live runs on 0.160.0). Treat a reply as finished after a
  quiet gap, not only on `turn.done`.
- **`/codexlive/interrupt` does not stop speech on 0.160.0**: the realtime turn ids from the data
  channel (`turn_…`) are not Codex turns, so `turn/interrupt` answers `no active turn to interrupt`
  (or names the thread's last backing turn). Spoken barge-in works without it: the service cuts its
  reply when the user talks. The route stays scoped to its own call.
- **Delegation quality**: with no voice-side policy the model delegates *everything* (fragments,
  fillers, "aló") — each becomes a full agent turn, and users interrupt turns while waiting. Put a
  labelled **delegation policy** in the session instructions (delegate only action / fact requests;
  clarify small ambiguities yourself; one short ack, then wait silently; read results back short)
  and prepend a **per-turn note** when submitting to the chat (speech transcript may contain
  mis-hearings → use the latest intent; reply short and plain for TTS; no markdown/lists).
  Client-side: skip filler delegations and serialize runs (one task at a time).
- **Aux-model trap**: a Hermes profile can bind auxiliary models (compression / approval / title /
  mcp) to `openai-codex` — those quietly consume the same weekly plan bucket on every heavy turn.
  Check before blaming the voice lane.
- **ESM plugin syntax**: validate the desktop `plugin.js` with a module-mode syntax check
  (`node --check file.mjs`) **plus** a stub-import harness — `node --check file.js` can false-OK an
  ES module. See `tools/plugin-load-test.sh`.

## Live verification (codex-cli 0.160.0)

`tools/live_call_check.py` runs two WebRTC reference clients (aiortc) against this broker's routes,
in-process, on a real ChatGPT Plus Codex login. Utterances were synthesized with `espeak-ng -v en-us`:
"Please check how much free disk space the server has." (delegate), "Hi there. What is two plus
two?" (chat), "Please tell me a long and detailed story about a lighthouse keeper and his cat."
(story), "Stop. Wait. What colour is the sky?" (bargein), "Are you still there? Say yes." (still).

Recorded run, 2026-10-08, zaza (NixOS, codex-cli 0.160.0 from the system profile):

| check | result |
|---|---|
| two calls started together, separate threads | ready (SDP applied → `session.started`) in 1.91 s and 2.59 s |
| phone delegation answered by the client while the desktop chatted | phone: "…The server has forty-two gigabytes free."; desktop: "Two plus two is four."; the desktop saw no delegation |
| backing turn for the delegation | interrupted after ~0.1 s with 0 items; no execution item, no approval request |
| spoken barge-in on each call | each reply was cut and the new question answered; the other call saw no events meanwhile |
| desktop hangs up, phone continues | phone answered "Yes." |
| new desktop call while the phone is live, then the phone hangs up | new thread, ready in 1.51 s; it answered "Yep, I'm here. Yes." |

Deployed run, 2026-10-08: the same scenario through a Hermes dashboard (v0.21.6 Nix build) serving
the installed plugin at commit `9366f20`, using `--url` and the dashboard's loopback session token.
Every check passed; the three calls were ready in 1.47 s, 1.53 s and 1.84 s, with broker startup
(app-server, thread, realtime start, SDP answer) of 0.93–1.35 s, almost all of it the SDP answer.
Twelve more concurrent starts through that dashboard were ready in 1.5–3.0 s. Two earlier
runs through a dashboard each had one slow call (12.9 s and 17.1 s from request to `session.started`)
before the broker logged its startup breakdown; neither repeated, so their cause is unknown. Each
start now logs `codex live: call … startup …s (…)` so a slow one shows whether the broker or the
service took the time.

This is live evidence for one account and one machine, with synthetic speech over a direct WebRTC
client. It does not cover the Hermes Desktop renderer, a phone, echo on a loudspeaker, other plans,
or Danish speech (espeak-ng's Danish was transcribed as English).

## Reference implementation

- `desktop/plugin.js` — renderer half (WebRTC, transcript, delegation, settings UI).
- `dashboard/plugin_api.py` — broker half (app-server lifecycle, call ownership, sessions, personas, usage).
- `tools/live_call_check.py` — two-client live check (real calls, spends voice allowance).

## License note

This documents interoperating with the open-source Codex CLI and a user's own ChatGPT account.
Not affiliated with or endorsed by OpenAI. Respect the OpenAI terms for your account, never share
accounts, never bypass rate limits.
