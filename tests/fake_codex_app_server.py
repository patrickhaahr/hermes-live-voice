"""Stand-in for `codex app-server --listen stdio://`, for broker tests.

It behaves like the real server observed on codex-cli 0.160.0: one process,
many threads, JSON-RPC responses by request id, and notifications that carry
the threadId they belong to. It knows nothing about calls or owners; keeping
calls apart is the broker's job. Every message it receives is appended to
$FAKE_CODEX_LOG so tests can see what the "service" was asked to do.

A call's behaviour is chosen by its SDP offer, the way a real offer differs per
client:
  "...slow..."     the SDP answer arrives after $FAKE_CODEX_SLOW_S seconds
  "...fail..."     the session reports thread/realtime/error, then a late SDP
  "...delegate..." the backing thread starts a turn that runs a command and
                   asks for approval, as a delegation does on the real server
Server-wide behaviour comes from $FAKE_CODEX_MODE:
  "old"            reports codex 0.150.0
  "ignores-limits" accepts thread/start but does not apply the restrictions
  "strict-fields"  rejects clientManagedHandoffs as an unknown field
"""
import json
import os
import sys
import threading
import time
import uuid

MODE = os.environ.get("FAKE_CODEX_MODE", "")
LOG = os.environ.get("FAKE_CODEX_LOG")
SLOW_S = float(os.environ.get("FAKE_CODEX_SLOW_S", "0.6"))
_out = threading.Lock()
_threads: dict[str, dict] = {}


def log(entry):
    if LOG:
        with _out, open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")


def emit(obj):
    with _out:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def notify(method, params):
    emit({"jsonrpc": "2.0", "method": method, "params": params})


def later(delay, fn, *args):
    threading.Timer(delay, fn, args).start()


def realtime_session(tid, offer):
    notify("thread/realtime/started", {"threadId": tid, "realtimeSessionId": "rt-" + tid, "version": "v3"})
    if "fail" in offer:
        notify("thread/realtime/error", {"threadId": tid, "message": "upstream refused the session"})
        later(0.2, notify, "thread/realtime/sdp", {"threadId": tid, "sdp": "answer:" + offer})
        return
    later(SLOW_S if "slow" in offer else 0.05, notify, "thread/realtime/sdp",
          {"threadId": tid, "sdp": "answer:" + offer})
    if "delegate" in offer:
        later(0.3, backing_turn, tid)


def backing_turn(tid):
    turn = "turn-" + uuid.uuid4().hex[:8]
    notify("turn/started", {"threadId": tid, "turn": {"id": turn, "status": "inProgress"}})
    notify("item/started", {"threadId": tid, "turnId": turn,
                            "item": {"type": "commandExecution", "id": "cmd-1", "command": "df -h"}})
    emit({"jsonrpc": "2.0", "id": "approval-" + tid, "method": "item/commandExecution/requestApproval",
          "params": {"threadId": tid, "turnId": turn, "itemId": "cmd-1", "command": "df -h"}})


def handle(msg):
    method, params, rid = msg.get("method"), msg.get("params") or {}, msg.get("id")

    def reply(result):
        emit({"jsonrpc": "2.0", "id": rid, "result": result})

    def fail(message):
        emit({"jsonrpc": "2.0", "id": rid, "error": {"code": -32600, "message": message}})

    if method == "initialize":
        version = "0.150.0" if MODE == "old" else "0.160.0"
        reply({"userAgent": f"hermes-talk-desktop/{version} (Linux; x86_64)", "platformOs": "linux"})
    elif method == "thread/start":
        tid = str(uuid.uuid4())
        _threads[tid] = {"realtime": False}
        applied = MODE != "ignores-limits"
        reply({
            "thread": {"id": tid, "ephemeral": bool(params.get("ephemeral")) and applied,
                       "environments": params.get("environments") if applied else [{"environmentId": "local"}]},
            "sandbox": {"type": "readOnly" if applied else "workspaceWrite", "networkAccess": False},
            "approvalPolicy": params.get("approvalPolicy") if applied else "on-request",
        })
        notify("thread/started", {"thread": {"id": tid}})
    elif method == "thread/realtime/start":
        tid = params.get("threadId")
        if tid not in _threads:
            return fail("thread not found")
        if MODE == "strict-fields" and "clientManagedHandoffs" in params:
            return fail("unknown field `clientManagedHandoffs`")
        if _threads[tid]["realtime"]:
            return fail("realtime conversation already running")
        _threads[tid]["realtime"] = True
        reply({})
        later(0.05, realtime_session, tid, params["transport"]["sdp"])
    elif method == "thread/realtime/stop":
        tid = params.get("threadId")
        reply({})
        if _threads.get(tid, {}).get("realtime"):
            _threads[tid]["realtime"] = False
            notify("thread/realtime/closed", {"threadId": tid, "reason": "requested"})
    elif method == "turn/interrupt":
        reply({})
        notify("turn/completed", {"threadId": params.get("threadId"),
                                  "turn": {"id": params.get("turnId"), "status": "interrupted"}})
    elif rid is not None and method:
        fail(f"method not found: {method}")


def main():
    log({"argv": sys.argv[1:], "cwd": os.getcwd()})
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        log(msg)
        handle(msg)


if __name__ == "__main__":
    main()
