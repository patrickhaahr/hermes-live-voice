"""talk-desktop — backend half: mintea sesiones Realtime para el desktop nativo.

Reusa el lane Codex-OAuth del plugin hermes-talk (talk_* modules) y agrega lo
que el browser tab no tiene: identity por BOT (SOUL.md del profile del VPS)
y gestión de la sesión Codex OAuth (estado, login device-code, logout).

Routes (mounted at /api/plugins/talk-desktop/):
  GET  /status              → auth codex ok? profile list? modelo/voz configuradas?
  POST /session             → {profile?, voice?} → descriptor efímero (to_wire)
  GET  /codex/status        → estado de la sesión Codex (sin secretos) + login en curso
  POST /codex/login/start   → inicia `codex login --device-auth` (URL + código para el usuario)
  POST /codex/login/cancel  → cancela un login pendiente
  POST /codex/logout        → cierra la sesión (respaldo del auth.json; afecta voz + Codex CLI del VPS)
  GET  /codex/usage         → uso/quota del plan ChatGPT (rate_limit del endpoint wham/usage)
  GET  /voice/usage         → uso local acumulado de Live Voice (últimos 7 días)
  POST /voice/usage/report  → {durationMs, audioMs} → acumula la sesión al día (lo llama el renderer al colgar)
  POST /tool                → ejecuta una tool de voz (talk_tools) y devuelve texto para hablar
  POST /codexlive/session   → {profile?, voice?, language?, offer(SDP)} → negocia gpt-live-1-codex (v3) vía
                              codex app-server; cada llamada tiene su propio thread (threadId = id de la llamada)
  POST /codexlive/interrupt → {threadId, turnId} → barge-in, solo sobre esa llamada
  POST /codexlive/stop      → {threadId} → cuelga esa llamada; las demás siguen

El secret efímero va SOLO al renderer del desktop; el OAuth nunca sale del VPS.
"""

from __future__ import annotations

import asyncio
import atexit
import itertools
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_TALK_VENDOR_ROOT = _PLUGIN_ROOT / "dashboard" / "talk_vendor"
_HERMES_TALK_ROOT = Path.home() / ".hermes" / "plugins" / "hermes-talk"
# Orden de precedencia (el último insertado gana): el plugin hermes-talk completo
# si está instalado; si no, el bundle vendorizado que viaja en el repo.
for _p in (str(_PLUGIN_ROOT), str(_TALK_VENDOR_ROOT), str(_HERMES_TALK_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import talk_auth  # noqa: E402
import talk_capabilities  # noqa: E402
import talk_config  # noqa: E402
import talk_host  # noqa: E402
import talk_identity  # noqa: E402
import talk_tools  # noqa: E402
import talk_wire  # noqa: E402

try:
    from fastapi import APIRouter, HTTPException, Request
except ImportError:  # pragma: no cover
    APIRouter = None
    HTTPException = None
    Request = None

router = APIRouter() if APIRouter else None
_log = logging.getLogger("hermes.plugins.talk-desktop")

_PROFILES_ROOT = Path.home() / ".hermes" / "profiles"
_MINT_LOCK = threading.Lock()


def _list_bots() -> list[str]:
    if not _PROFILES_ROOT.is_dir():
        return []
    return sorted(
        e.name for e in _PROFILES_ROOT.iterdir()
        if e.is_dir() and (e / "SOUL.md").is_file()
    )


def _profile_home(profile: str) -> Path | None:
    # Nombre de perfil simple (sin traversal). Los perfiles pueden ser symlinks
    # (p.ej. mi-bot → ~/.hermes-mi-bot), así que NO se exige que el path
    # resuelto quede bajo profiles/ — solo que el nombre sea seguro y exista.
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", profile or ""):
        return None
    p = _PROFILES_ROOT / profile
    if not p.is_dir():
        return None
    return p.resolve()


def _bot_display_name(profile: str) -> str:
    """Nombre del bot desde el encabezado del SOUL.md; fallback al slug.

    Formatos vistos: "# SOUL.md — MiBot", "# Asistente — CEO de Acme", "# Ops",
    "# MiProyecto DevOps".
    """
    home = _profile_home(profile)
    if home is not None:
        try:
            first = ((home / "SOUL.md").read_text(encoding="utf-8", errors="replace").splitlines() or [""])[0].strip()
        except OSError:
            first = ""
        first = re.sub(r"[^\w\sÁÉÍÓÚÜÑáéíóúüñ—·-]", " ", first.lstrip("#").strip())
        parts = [p.strip() for p in first.split("—") if p.strip()]
        name = ""
        for part in parts:
            if part.lower().replace(" ", "").startswith("soul.md") or part.lower().startswith("soul"):
                continue
            name = part
            break
        if not name and parts:
            name = parts[-1]
        name = name.split("·")[0].strip()
        if name:
            return name[:40]
    return profile.capitalize()


def _bot_identity_sections(profile: str) -> dict[str, str]:
    """Identity del bot: SOUL.md del profile, con la misma sanitización que hermes-talk."""
    home = _profile_home(profile)
    if home is None:
        return {}
    sections: dict[str, str] = {}
    soul = home / "SOUL.md"
    if soul.is_file():
        try:
            body = soul.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            body = ""
        if body:
            try:
                body = talk_host._sanitize_identity_entries(body, "SOUL.md")
            except Exception:  # noqa: BLE001
                body = ""
            if body:
                sections["PERSONA"] = body[: talk_config.identity_char_limit("persona") or 8000]
    return sections


_DIRECTIVE_PATH = _PLUGIN_ROOT / "language_directive.txt"
# The voice speaks English or Danish only, never any other language.
_LANGUAGE_DIRECTIVE_DEFAULT = {
    "en": (
        '\n\nLANGUAGE AND VOICE: Speak English. If the user speaks Danish, reply in natural Danish instead. '
        'Use only English or Danish: if the user speaks another language, or a transcript looks like one, '
        'reply in English. Never mix languages. Your voice should sound like a native speaker of the language '
        'you are using.'
    ),
    "da": (
        '\n\nLANGUAGE AND VOICE: Speak Danish. If the user speaks English, reply in English instead. '
        'Use only Danish or English: if the user speaks another language, or a transcript looks like one, '
        'reply in Danish. Never mix languages. Your voice should sound like a native speaker of the language '
        'you are using.'
    ),
}

_LANGUAGE_DIRECTIVE_BUNDLE = "\n\n".join(value.strip() for value in _LANGUAGE_DIRECTIVE_DEFAULT.values())


def _voice_language(value) -> str:
    """The call's default spoken language: Danish when asked for, otherwise English."""
    return "da" if str(value or "").strip().lower().startswith("da") else "en"


def _language_directive(language: str = "en") -> str:
    """Select shipped defaults by language; preserve custom file content verbatim."""
    try:
        if _DIRECTIVE_PATH.is_file():
            text = _DIRECTIVE_PATH.read_text(encoding="utf-8")
            shipped = {value.strip() for value in _LANGUAGE_DIRECTIVE_DEFAULT.values()}
            shipped.add(_LANGUAGE_DIRECTIVE_BUNDLE)
            if text.strip() and text.strip() not in shipped:
                return "\n\n" + text
    except OSError:
        pass
    return _LANGUAGE_DIRECTIVE_DEFAULT[_voice_language(language)]


def _resolve_voice(requested: str | None) -> str:
    """Voice pedida o la configurada (TALK_VOICE / config)."""
    if requested:
        v = str(requested).strip().lower()
        if v in talk_config.OPENAI_REALTIME_VOICES:
            return v
        raise HTTPException(status_code=400, detail=f"voice '{v}' no disponible")
    try:
        return talk_config.voice() or "marin"
    except Exception:  # noqa: BLE001
        return "marin"


_SEND_TO_CHAT_DESC = (
    "Send a request to the user's currently open chat session so the REAL agent handles it "
    "with its full context, memory and tools. The request appears in that chat as a message "
    "from the user and the agent's reply streams there; you receive the reply text back and "
    "read or summarize it aloud. Use this whenever the user asks for actual work, actions, or "
    "information from their conversations, files or services \u2014 anything beyond a quick "
    "conversational answer. Do NOT use it for small talk or things you can answer instantly."
)


def _send_to_chat_tool() -> dict:
    return {
        "type": "function",
        "name": "send_to_chat",
        "description": _SEND_TO_CHAT_DESC,
        "parameters": {
            "type": "object",
            "properties": {
                "request": {
                    "type": "string",
                    "description": "The full request to hand to the agent, phrased naturally in the user's own language.",
                },
            },
            "required": ["request"],
        },
    }


def _mint_for(profile: str | None, voice: str, allow_chat: bool = True, language: str | None = "en"):
    """Mint con identity del bot (o del host si no se pide profile)."""
    tools = talk_tools.default_talk_tools()
    if allow_chat:
        tools = tools + [_send_to_chat_tool()]
    if profile:
        sections = _bot_identity_sections(profile)
    else:
        sections = talk_host.host().identity_sections()
    instructions = talk_identity.build_instructions(
        sections,
        tools=tools,
        lane="dashboard",
        capabilities=talk_capabilities.instruction_section(),
    )
    instructions = instructions + _language_directive(language)
    if allow_chat:
        instructions += (
            "\n\nVOICE + CHAT: The user has a chat open in the app. Answer quick questions and "
            "small talk directly yourself. When they ask for REAL WORK (doing, creating, reviewing, "
            "searching their information, or taking action), call send_to_chat with the complete "
            "request phrased naturally in their language. The system sends it to their open chat, "
            "where the real agent replies. You will then receive the result; read or summarize it "
            "aloud naturally in no more than 2-3 sentences. If no chat is open, the tool will tell you."
        )
    instructions += _VOICE_POLICY
    if profile:
        name = _bot_display_name(profile)
        if "You are Hermes, speaking live" in instructions:
            instructions = instructions.replace("You are Hermes, speaking live", f"You are {name}, speaking live", 1)
        instructions += (
            f"\n\nIDENTITY: You are {name}. If asked who you are, "
            f"answer as {name} in that role — never say you are Hermes, a model, or a generic assistant."
        )
    auth = talk_auth.resolve_auth()
    descriptor = talk_wire.mint_ephemeral_session(
        auth_token=auth.token,
        model=talk_config.talk_model(),
        voice=voice,
        instructions=instructions,
        tools=tools,
        text_output=False,
    )
    return descriptor, auth


# ── Codex OAuth: sesión de voz (device-code login / logout / status) ─────────

_LOGIN_URL = "https://auth.openai.com/codex/device"
_URL_RE = re.compile(r"https://auth\.openai\.com/codex/device")
# Device code is a hyphenated uppercase token (codex 0.145 prints e.g.
# "IK34-27GA1"). codex wraps it in ANSI SGR colour codes with no surrounding
# whitespace, so the escape's trailing "m" (a word char) sits right before the
# code and defeats a leading \b word boundary — the old pattern silently never
# matched, so the code was never exposed (the URL regex has no \b, which is
# exactly why copying the sign-in link worked). Strip ANSI first, then match.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_CODE_RE = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4,5})\b")
_LOGIN_TIMEOUT_S = 16 * 60

_LOGIN: dict = {
    "status": "idle",  # idle | pending | done | error
    "url": None,
    "code": None,
    "message": None,
    "started_at": None,
    "proc": None,
    "output": "",
}
_LOGIN_LOCK = threading.Lock()


def _codex_auth_path() -> Path:
    configured = os.environ.get("CODEX_HOME", "").strip()
    base = Path(configured) if configured else Path.home() / ".codex"
    return base / "auth.json"


def _codex_binary() -> str | None:
    candidates = (str(Path.home() / ".local" / "bin" / "codex"), shutil.which("codex"), "/usr/local/bin/codex")
    for cand in candidates:
        if cand and os.access(cand, os.X_OK):
            return cand
    return None


def _terminate(proc) -> None:
    try:
        proc.terminate()
        try:
            proc.wait(timeout=4)
        except Exception:  # noqa: BLE001
            proc.kill()
    except Exception:  # noqa: BLE001
        pass


def _login_reader(proc) -> None:
    try:
        for line in iter(proc.stdout.readline, ""):
            _LOGIN["output"] = (_LOGIN["output"] + line)[-4000:]
            clean = _ANSI_RE.sub("", line)
            if not _LOGIN["url"]:
                m = _URL_RE.search(clean)
                if m:
                    _LOGIN["url"] = m.group(0)
            if not _LOGIN["code"]:
                m = _CODE_RE.search(clean)
                if m:
                    _LOGIN["code"] = m.group(1)
    except Exception:  # noqa: BLE001
        pass


def _login_check() -> None:
    """Fold the device-login process state into _LOGIN (call before reporting)."""
    proc = _LOGIN.get("proc")
    if _LOGIN.get("status") != "pending":
        return
    if proc is not None and proc.poll() is None:
        started = _LOGIN.get("started_at") or 0
        if time.time() - started > _LOGIN_TIMEOUT_S:
            _terminate(proc)
            _LOGIN.update(status="error", message="timeout: el código expiró sin aprobarse", proc=None)
        return
    # process finished (or vanished)
    try:
        diag = talk_auth.auth_diagnostic()
        state = diag.get("codex_oauth")
    except Exception:  # noqa: BLE001
        state = None
    if state in {"valid", "expired"}:
        _LOGIN.update(status="done", message="Sesión iniciada", proc=None, url=None, code=None,
                      output="")
    else:
        tail = " / ".join((_LOGIN.get("output") or "").strip().splitlines()[-3:])
        _LOGIN.update(status="error", message=("No se pudo iniciar sesión" + (f": {tail}" if tail else "")),
                      proc=None)


def _start_codex_login() -> dict:
    with _LOGIN_LOCK:
        _login_check()
        proc = _LOGIN.get("proc")
        if proc is not None and proc.poll() is None:
            _terminate(proc)
        binary = _codex_binary()
        if not binary:
            return {"ok": False, "message": "codex CLI no encontrado en el servidor"}
        # Respaldo de la sesión actual: iniciar un login nuevo puede reemplazar/limpiar
        # ~/.codex/auth.json antes de completarse (verificado 2026-09-11).
        auth_path = _codex_auth_path()
        if auth_path.exists():
            try:
                ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
                shutil.copy2(auth_path, auth_path.with_name(f"auth.json.bak-pre-login-{ts}"))
            except OSError:
                pass
        env = os.environ.copy()
        # El home resuelto (CODEX_HOME si está definido) es el mismo que usan
        # backup/estado/logout: no se poppea para que todo lea el mismo auth.json.
        proc = subprocess.Popen(
            [binary, "login", "--device-auth"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
            cwd=str(Path.home()),
        )
        _LOGIN.update(status="pending", url=None, code=None, message=None,
                      started_at=time.time(), proc=proc, output="")
        threading.Thread(target=_login_reader, args=(proc,), daemon=True).start()
    # el reader tarda unos ms en capturar URL/código — esperar un poco
    deadline = time.time() + 8
    while time.time() < deadline and not (_LOGIN.get("url") and _LOGIN.get("code")):
        time.sleep(0.2)
    return {"ok": True, "url": _LOGIN.get("url") or _LOGIN_URL, "code": _LOGIN.get("code"),
            "status": _LOGIN["status"]}


if router is not None:

    @router.get("/status")
    async def status() -> dict:
        try:
            auth = talk_auth.resolve_auth()
            auth_source = auth.source
        except Exception as exc:  # noqa: BLE001
            auth_source = f"unavailable: {exc}"
        return {
            "ok": True,
            "auth": auth_source,
            "model": talk_config.talk_model(),
            "voices": list(talk_config.OPENAI_REALTIME_VOICES),
            "bots": _list_bots(),
        }

    @router.post("/session")
    async def create_session(request: Request) -> dict:
        body = {}
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        profile = str(body.get("profile") or "").strip() or None
        allow_chat = bool(body.get("allowChat", True))
        if profile and _profile_home(profile) is None:
            raise HTTPException(status_code=400, detail=f"perfil '{profile}' no existe")
        voice = _resolve_voice(body.get("voice"))

        def _do():
            # Serializar mints con identity override: el patch de get_hermes_home es
            # global al proceso mientras dura _mint_for; un mint a la vez. El lock
            # vive en este worker, no en el coroutine: un threading.Lock tomado a
            # través de un await bloquea el event loop — al esperar el lock, el
            # callback que lo liberaría no puede correr (deadlock). Así el override
            # también se restaura en el mismo worker que mintea.
            with _MINT_LOCK:
                if profile:
                    orig = talk_config.get_hermes_home
                    talk_config.get_hermes_home = lambda: _profile_home(profile)
                    try:
                        return _mint_for(profile, voice, allow_chat, body.get("language"))
                    finally:
                        talk_config.get_hermes_home = orig
                return _mint_for(None, voice, allow_chat, body.get("language"))

        descriptor, auth = await asyncio.to_thread(_do)
        return {
            "ok": True,
            "profile": profile,
            "botName": _bot_display_name(profile) if profile else "Luna",
            **descriptor.to_wire(),
            "authSource": auth.source,
            "voiceMode": "native",
        }

    @router.get("/codex/status")
    async def codex_status() -> dict:
        _login_check()
        try:
            st = talk_auth.auth_status()
            diag = talk_auth.auth_diagnostic()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "login": {k: _LOGIN.get(k) for k in ("status", "url", "code", "message")}}
        return {
            "ok": True,
            "configured": st.get("configured"),
            "lane": st.get("source"),
            "detail": st.get("detail"),
            "codexOauth": diag.get("codex_oauth"),
            "refreshRequired": diag.get("refresh_required"),
            "preference": diag.get("preference"),
            "login": {k: _LOGIN.get(k) for k in ("status", "url", "code", "message")},
        }

    @router.get("/codex/usage")
    async def codex_usage() -> dict:
        def _do() -> dict:
            try:
                auth = talk_auth.resolve_auth()
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": str(exc)[:160]}
            try:
                import httpx
                account_id = ""
                try:
                    data = json.loads(_codex_auth_path().read_text(encoding="utf-8"))
                    account_id = str((data.get("tokens") or {}).get("account_id") or "")
                except Exception:  # noqa: BLE001
                    pass
                headers = {
                    "Authorization": f"Bearer {auth.token}",
                    "Accept": "application/json",
                    "User-Agent": "codex-cli",
                }
                if account_id:
                    headers["ChatGPT-Account-Id"] = account_id
                payload = None
                last_status = None
                for url in (
                    "https://chatgpt.com/backend-api/wham/usage",
                    "https://api.openai.com/api/codex/usage",
                ):
                    with httpx.Client(timeout=12.0) as client:
                        resp = client.get(url, headers=headers)
                    last_status = resp.status_code
                    if resp.status_code == 200:
                        payload = resp.json() or {}
                        break
                if payload is None:
                    return {"ok": False, "error": f"usage http {last_status}"}
                rl = payload.get("rate_limit") or {}
                windows: dict = {}
                for key, fallback in (("primary_window", "5h"), ("secondary_window", "semanal")):
                    w = rl.get(key) or {}
                    if not (isinstance(w, dict) and w):
                        continue
                    mins = w.get("window_minutes")
                    secs = w.get("limit_window_seconds")
                    if not mins and secs:
                        try:
                            mins = int(secs) // 60
                        except Exception:  # noqa: BLE001
                            mins = None
                    if mins:
                        mins = int(mins)
                        if mins >= 6 * 1440:
                            label = "semanal"
                        else:
                            label = f"{max(1, round(mins / 60))}h"
                    else:
                        label = fallback
                    windows[label] = {
                        "usedPercent": w.get("used_percent"),
                        "windowMinutes": mins,
                        "resetsAt": w.get("reset_at") or w.get("resets_at"),
                    }
                return {
                    "ok": True,
                    "plan": payload.get("plan_type") or payload.get("plan") or None,
                    "windows": windows,
                }
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:160]}

        return await asyncio.to_thread(_do)

    # ── uso de Live Voice (metrado local por sesión) ──────────────────────────
    _VU_FILE = _PLUGIN_ROOT / "data" / "voice-usage.json"
    _VU_LOCK = threading.Lock()

    def _vu_load() -> dict:
        try:
            data = json.loads(_VU_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("sessions"), list):
                return data
        except Exception:  # noqa: BLE001
            pass
        return {"sessions": []}

    def _vu_save(data: dict) -> None:
        try:
            _VU_FILE.parent.mkdir(parents=True, exist_ok=True)
            _VU_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    @router.post("/voice/usage/report")
    async def voice_usage_report(request: Request) -> dict:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        dur = max(0, int((body or {}).get("durationMs") or 0))
        aud = max(0, int((body or {}).get("audioMs") or 0))
        if dur <= 0:
            return {"ok": False, "error": "durationMs requerido"}
        now_ms = int(time.time() * 1000)
        with _VU_LOCK:
            data = _vu_load()
            sess = data.setdefault("sessions", [])
            sess.append({"t": now_ms, "ms": dur, "audio_ms": aud})
            keep = [s for s in sess if isinstance(s, dict) and int(s.get("t") or 0) > now_ms - 8 * 24 * 3600 * 1000]
            data["sessions"] = keep[-2000:]
            _vu_save(data)
        return {"ok": True}

    @router.get("/voice/usage")
    async def voice_usage() -> dict:
        with _VU_LOCK:
            data = _vu_load()
        import datetime as _dt
        now_ms = int(time.time() * 1000)
        midnight = _dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        today_ms = int(midnight.timestamp() * 1000)
        sess = [s for s in (data.get("sessions") or []) if isinstance(s, dict)]

        def agg(since_ms: int) -> dict:
            ms = aud = n = 0
            for s in sess:
                if int(s.get("t") or 0) >= since_ms:
                    ms += int(s.get("ms") or 0)
                    aud += int(s.get("audio_ms") or 0)
                    n += 1
            return {"minutes": ms / 60000.0, "audioMinutes": aud / 60000.0, "sessions": n}

        return {
            "ok": True,
            "rolling5h": agg(now_ms - 5 * 3600 * 1000),
            "rolling24h": agg(now_ms - 24 * 3600 * 1000),
            "week": agg(now_ms - 7 * 24 * 3600 * 1000),
            "today": agg(today_ms),
        }

    @router.post("/tool")
    async def run_tool(request: Request) -> dict:
        body = {}
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        name = str(body.get("name") or "").strip()
        arguments = body.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        if not name:
            raise HTTPException(status_code=400, detail="name is required")
        try:
            output = await asyncio.wait_for(
                asyncio.to_thread(
                    talk_tools.execute_talk_tool, name, arguments,
                    # Tool output is read by the voice model, which answers in English or Danish.
                    **({"language": "en"} if getattr(talk_tools, "SUPPORTS_LANGUAGE", False) else {}),
                ),
                timeout=110,
            )
        except asyncio.TimeoutError:
            output = (
                f"The tool {name} is still running; I will stop waiting here. "
                "Ask me to check the result in a moment."
            )
        except talk_tools.TalkToolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "output": output}

    @router.post("/codex/login/start")
    async def codex_login_start() -> dict:
        return await asyncio.to_thread(_start_codex_login)

    @router.post("/codex/login/cancel")
    async def codex_login_cancel() -> dict:
        with _LOGIN_LOCK:
            proc = _LOGIN.get("proc")
            if proc is not None and proc.poll() is None:
                _terminate(proc)
            _LOGIN.update(status="idle", proc=None, url=None, code=None, message=None, output="")

        def _restore_if_needed() -> None:
            # Un login cancelado puede dejar ~/.codex/auth.json limpio; restaurar el
            # respaldo pre-login más reciente para no perder la sesión previa.
            path = _codex_auth_path()
            if path.exists():
                return
            backups = sorted(path.parent.glob("auth.json.bak-pre-login-*"))
            if backups:
                try:
                    shutil.copy2(backups[-1], path)
                except OSError:
                    pass

        await asyncio.to_thread(_restore_if_needed)
        return {"ok": True}

    @router.post("/codex/logout")
    async def codex_logout() -> dict:
        def _do() -> dict:
            path = _codex_auth_path()
            if not path.exists():
                return {"ok": True, "message": "No había sesión que cerrar"}
            ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            bak = path.with_name(f"auth.json.bak-voice-logout-{ts}")
            try:
                shutil.copy2(path, bak)
                path.unlink()
            except OSError as exc:
                return {"ok": False, "message": f"No se pudo cerrar la sesión: {exc}"}
            return {"ok": True, "message": f"Sesión cerrada (respaldo: {bak.name})"}

        return await asyncio.to_thread(_do)


# ── Codex Live-1 (gpt-live-1-codex, v3/frameless) vía codex app-server ───────
#
# El app-server de Codex negocia una sesión WebRTC contra el backend de ChatGPT
# usando la suscripción (model_provider=openai). El cliente manda su SDP offer;
# acá se devuelve el answer. El audio va directo cliente<->OpenAI.
#
# Call ownership: one long-lived app-server process serves every call, and each
# call owns a fresh ephemeral thread. Responses are correlated by request id and
# notifications by threadId, so SDP, errors, closes and stops only ever reach
# their own call; a call that is not registered (never started, failed, stopped)
# receives nothing. Nothing is cached between calls, so each call starts with
# fresh voice context.
#
# Execution guard: with clientManagedHandoffs the realtime core still opens a
# Codex turn on the backing thread for every delegation (observed on 0.160.0).
# Tasks must run in the client's Hermes chat only, so the backing thread is
# created without environment access, read-only, network-less and with every
# approval routed to this broker, which declines it; any backing turn is
# interrupted the moment it starts, and any execution item interrupts it again.

# Validated against codex-cli 0.160.0 (see docs/live-voice-recipe.md). Older
# app-servers either lack the thread restrictions below or silently ignore them.
_CODEX_MIN_VERSION = (0, 160, 0)
_LIVE_START_TIMEOUT_S = 20.0
_LIVE_RPC_TIMEOUT_S = 25.0
# Features that give the backing Codex thread tools; the voice lane needs none.
_LIVE_DISABLED_FEATURES = (
    "apps", "plugins", "shell_tool", "unified_exec", "browser_use", "computer_use",
    "image_generation", "multi_agent", "view_image", "hooks",
)
_EXECUTION_ITEMS = frozenset({
    "commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall", "collabAgentToolCall",
    "subAgentActivity", "webSearch", "imageView", "imageGeneration",
})
_SERVER_REQUEST_DECLINES = {
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
    "execCommandApproval": {"decision": "denied"},
    "applyPatchApproval": {"decision": "denied"},
    "mcpServer/elicitation/request": {"action": "decline"},
}


class LiveCallError(RuntimeError):
    """A call could not start; `code` tells the client why without parsing prose."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def _codex_binary() -> str | None:
    """Resuelve el binario de codex aunque el PATH del servicio no traiga ~/.npm-global/bin."""
    configured = os.environ.get("TALK_CODEX_BINARY", "").strip()
    if configured:
        return configured
    try:
        found = shutil.which("codex")
    except Exception:  # noqa: BLE001
        found = None
    if found:
        return found
    for cand in (Path.home() / ".npm-global" / "bin" / "codex",
                 Path("/usr/local/bin/codex"),
                 Path.home() / ".local" / "bin" / "codex",
                 Path("/usr/bin/codex")):
        if cand.exists():
            return str(cand)
    try:
        out = subprocess.run(["bash", "-lc", "command -v codex"], capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().splitlines()[0]
    except Exception:  # noqa: BLE001
        pass
    return None


def _codex_version(user_agent: str) -> tuple[int, int, int] | None:
    m = re.search(r"/(\d+)\.(\d+)\.(\d+)", user_agent or "")
    return tuple(int(part) for part in m.groups()) if m else None


class _AppServer:
    """One `codex app-server` process speaking JSON-RPC over stdio."""

    def __init__(self, on_notification, on_exit):
        self._on_notification = on_notification
        self._on_exit = on_exit
        self._proc = None
        self._ids = itertools.count(1)
        self._pending: dict[int, dict] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self.version: tuple[int, int, int] | None = None
        # The backing threads' working directory: empty and private.
        self.cwd = tempfile.mkdtemp(prefix="talk-desktop-live-")

    def start(self) -> None:
        binary = _codex_binary()
        if not binary:
            raise LiveCallError("LIVE_UNAVAILABLE", "codex not found (the service PATH may lack ~/.npm-global/bin; set TALK_CODEX_BINARY)")
        env = os.environ.copy()
        env["PATH"] = (str(Path.home() / ".npm-global" / "bin") + ":" + env.get("PATH", "")).strip(":")
        # clave: la lane de voz usa la SUSCRIPCIÓN (auth.json), no una API key de entorno
        env.pop("OPENAI_API_KEY", None)
        env.pop("CODEX_API_KEY", None)
        argv = [binary, "app-server", "--listen", "stdio://", "--enable", "realtime_conversation"]
        for feature in _LIVE_DISABLED_FEATURES:
            argv += ["--disable", feature]
        argv += ["-c", "model_provider=openai", "-c", 'web_search="disabled"',
                 "-c", "suppress_unstable_features_warning=true"]
        self._proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, env=env, cwd=self.cwd,
        )
        threading.Thread(target=self._read, args=(self._proc,), daemon=True).start()
        result = self.request("initialize", {"clientInfo": {"name": "hermes-talk-desktop", "version": "1.0"},
                                             "capabilities": {"experimentalApi": True}})
        self.version = _codex_version(str((result or {}).get("userAgent") or ""))

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def close(self) -> None:
        if self._proc is not None:
            _terminate(self._proc)
        shutil.rmtree(self.cwd, ignore_errors=True)

    def _write(self, obj: dict) -> None:
        if not self.alive():
            raise RuntimeError("codex app-server is not running")
        with self._write_lock:
            self._proc.stdin.write(json.dumps(obj) + "\n")
            self._proc.stdin.flush()

    def request(self, method: str, params: dict, timeout: float = _LIVE_RPC_TIMEOUT_S):
        rid = next(self._ids)
        slot = {"done": threading.Event(), "msg": None}
        with self._pending_lock:
            self._pending[rid] = slot
        try:
            self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            if not slot["done"].wait(max(0.0, timeout)):
                raise TimeoutError(f"codex app-server: no response to {method}")
        finally:
            with self._pending_lock:
                self._pending.pop(rid, None)
        msg = slot["msg"]
        if msg is None:
            raise RuntimeError("codex app-server exited")
        if "error" in msg:
            err = msg["error"]
            raise RuntimeError(str(err.get("message") if isinstance(err, dict) else err)[:400])
        return msg.get("result")

    def send(self, method: str, params: dict) -> None:
        """Fire a request whose response nobody waits for (safe from the reader thread)."""
        try:
            self._write({"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params})
        except Exception:  # noqa: BLE001
            pass

    def _read(self, proc) -> None:
        try:
            for line in proc.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if "method" in msg and "id" in msg:
                    self._answer_server_request(msg)
                elif "id" in msg:
                    with self._pending_lock:
                        slot = self._pending.get(msg["id"])
                    if slot is not None:
                        slot["msg"] = msg
                        slot["done"].set()
                elif "method" in msg:
                    try:
                        self._on_notification(self, msg)
                    except Exception:  # noqa: BLE001
                        _log.exception("codex live: notification handler failed")
        except Exception:  # noqa: BLE001
            pass
        with self._pending_lock:
            for slot in self._pending.values():
                slot["done"].set()
        self._on_exit(self)

    def _answer_server_request(self, msg: dict) -> None:
        # The broker is the only client of this app-server, so it is the
        # approver: nothing the backing thread asks for is ever granted.
        method = msg.get("method")
        _log.warning("codex live: declined backing-thread request %s", method)
        decline = _SERVER_REQUEST_DECLINES.get(method)
        try:
            if decline is not None:
                self._write({"jsonrpc": "2.0", "id": msg["id"], "result": decline})
            else:
                self._write({"jsonrpc": "2.0", "id": msg["id"],
                             "error": {"code": -32601, "message": "not available to the voice lane"}})
        except Exception:  # noqa: BLE001
            pass


class _LiveCall:
    def __init__(self, thread_id: str, server: _AppServer):
        self.thread_id = thread_id
        self.server = server
        self.answer: str | None = None
        self.session_id: str | None = None
        self.error: str | None = None
        self.closed: str | None = None
        self.settled = threading.Event()


class _LiveBroker:
    """Call-scoped broker: every call owns its thread; nothing is shared between calls."""

    def __init__(self):
        self._server: _AppServer | None = None
        self._spawn_lock = threading.Lock()
        self._calls: dict[str, _LiveCall] = {}
        # Ended calls stay guarded (their backing turns are still interrupted)
        # but receive nothing else.
        self._retired: dict[str, _AppServer] = {}
        self._lock = threading.Lock()

    def _ensure_server(self) -> _AppServer:
        with self._spawn_lock:
            server = self._server
            if server is not None and server.alive():
                return server
            server = _AppServer(self._route, self._server_exited)
            try:
                server.start()
            except LiveCallError:
                server.close()
                raise
            except Exception as exc:  # noqa: BLE001
                server.close()
                raise LiveCallError("LIVE_UNAVAILABLE", f"codex app-server did not start: {exc}") from exc
            if server.version is None or server.version < _CODEX_MIN_VERSION:
                found = ".".join(map(str, server.version)) if server.version else "unknown"
                server.close()
                raise LiveCallError(
                    "LIVE_UNSUPPORTED",
                    f"codex {found} cannot enforce the voice thread restrictions; "
                    + ".".join(map(str, _CODEX_MIN_VERSION)) + " or newer is required",
                )
            self._server = server
            return server

    def _route(self, server: _AppServer, msg: dict) -> None:
        method = msg.get("method") or ""
        params = msg.get("params") or {}
        tid = params.get("threadId")
        if not tid:
            return
        with self._lock:
            call = self._calls.get(tid)
            guarded = call is not None or self._retired.get(tid) is server
        if guarded and method in ("turn/started", "item/started", "turn/completed"):
            turn = params.get("turn") or {}
            if method == "turn/completed":
                level = logging.INFO if turn.get("status") == "interrupted" and not turn.get("items") else logging.WARNING
                _log.log(level, "codex live: backing turn %s ended %s with %d items",
                         turn.get("id"), turn.get("status"), len(turn.get("items") or []))
                return
            item_type = (params.get("item") or {}).get("type")
            if method == "turn/started" or item_type in _EXECUTION_ITEMS:
                if method == "item/started":
                    _log.warning("codex live: backing thread started %s; interrupting", item_type)
                turn_id = turn.get("id") if method == "turn/started" else params.get("turnId")
                if turn_id:
                    # Off the reader thread: the response arrives through it.
                    threading.Thread(target=self._interrupt_backing, args=(server, tid, turn_id), daemon=True).start()
            return
        if call is None or call.server is not server:
            return
        if method == "thread/realtime/sdp":
            call.answer = params.get("sdp") or call.answer
            call.settled.set()
        elif method == "thread/realtime/started":
            call.session_id = params.get("realtimeSessionId") or call.session_id
        elif method == "thread/realtime/error":
            call.error = str(params.get("message") or "error")[:300]
            call.settled.set()
        elif method == "thread/realtime/closed":
            call.closed = str(params.get("reason") or "closed")
            _log.info("codex live: call %s closed by the service (%s)", tid, call.closed)
            self._forget(call)
            call.settled.set()

    @staticmethod
    def _interrupt_backing(server: _AppServer, thread_id: str, turn_id: str) -> None:
        _log.info("codex live: interrupting backing turn %s", turn_id)
        try:
            server.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=8)
        except Exception as exc:  # noqa: BLE001
            _log.warning("codex live: could not interrupt backing turn %s: %s", turn_id, exc)

    def _server_exited(self, server: _AppServer) -> None:
        with self._lock:
            dead = [c for c in self._calls.values() if c.server is server]
            for call in dead:
                self._calls.pop(call.thread_id, None)
            for tid in [t for t, s in self._retired.items() if s is server]:
                self._retired.pop(tid, None)
        for call in dead:
            call.error = call.error or "codex app-server exited"
            call.settled.set()

    def _forget(self, call: _LiveCall) -> bool:
        with self._lock:
            if self._calls.get(call.thread_id) is not call:
                return False
            del self._calls[call.thread_id]
            self._retired[call.thread_id] = call.server
            while len(self._retired) > 256:
                self._retired.pop(next(iter(self._retired)))
            return True

    def _end(self, call: _LiveCall) -> bool:
        if not self._forget(call):
            return False
        try:
            call.server.request("thread/realtime/stop", {"threadId": call.thread_id}, timeout=10)
        except Exception:  # noqa: BLE001
            pass
        return True

    def start_call(self, *, persona: str, start_instructions: str, voice: str, offer: str,
                   model: str | None) -> dict:
        deadline = time.monotonic() + _LIVE_START_TIMEOUT_S

        def remaining() -> float:
            return max(0.1, deadline - time.monotonic())

        began = time.monotonic()
        marks: dict[str, float] = {}
        server = self._ensure_server()
        marks["server"] = time.monotonic()
        body = {
            "cwd": server.cwd, "modelProvider": "openai", "ephemeral": True,
            "environments": [], "sandbox": "read-only", "approvalPolicy": "untrusted",
        }
        if model:
            body["model"] = model
        try:
            started = server.request("thread/start", body, timeout=remaining()) or {}
            marks["thread"] = time.monotonic()
        except TimeoutError as exc:
            raise LiveCallError("LIVE_TIMEOUT", str(exc)) from exc
        except RuntimeError as exc:
            raise LiveCallError("LIVE_START_FAILED", str(exc)) from exc
        thread = started.get("thread") or {}
        tid = thread.get("id")
        if not tid:
            raise LiveCallError("LIVE_START_FAILED", "codex did not create a thread")
        call = _LiveCall(tid, server)
        with self._lock:
            self._calls[tid] = call
        try:
            # Verify the restrictions took effect rather than trusting the request.
            sandbox = started.get("sandbox") or {}
            if not (thread.get("ephemeral") is True and thread.get("environments") == []
                    and sandbox.get("type") == "readOnly" and not sandbox.get("networkAccess")
                    and started.get("approvalPolicy") == "untrusted"):
                raise LiveCallError("LIVE_UNSAFE", "the app-server did not apply the voice thread restrictions")
            params = {
                "threadId": tid,
                "transport": {"type": "webrtc", "sdp": offer},
                "outputModality": "audio",
                "version": "v3",
                "voice": voice or "cove",
                "prompt": persona,
                "realtimeStartInstructions": start_instructions,
                # Delegated tasks run in the client's Hermes chat; the backing
                # thread is never an executor (see the execution guard above).
                "clientManagedHandoffs": True,
                "delegationAckFiller": True,
            }
            try:
                server.request("thread/realtime/start", params, timeout=remaining())
                marks["realtime/start"] = time.monotonic()
            except TimeoutError as exc:
                raise LiveCallError("LIVE_TIMEOUT", str(exc)) from exc
            except RuntimeError as exc:
                low = str(exc).lower()
                if "usage limit" in low or "hit your usage" in low:
                    raise LiveCallError(
                        "LIVE_SIN_QUOTA",
                        "the ChatGPT plan reached its limit (Live Voice shares that allowance). "
                        "Switch the voice engine to gpt-realtime-2.1 in the settings, or wait for the reset.",
                    ) from exc
                if "unknown field" in low or "unknown variant" in low:
                    raise LiveCallError("LIVE_UNSUPPORTED", f"the app-server rejected the voice session: {exc}") from exc
                raise LiveCallError("LIVE_START_FAILED", str(exc)) from exc
            if not call.settled.wait(remaining()):
                raise LiveCallError("LIVE_TIMEOUT", "codex live: no SDP answer")
            marks["sdp"] = time.monotonic()
            if call.error:
                raise LiveCallError("LIVE_START_FAILED", "codex live: " + call.error)
            if not call.answer:
                raise LiveCallError("LIVE_START_FAILED", f"codex live: session closed ({call.closed or 'no SDP answer'})")
        except BaseException:
            self._end(call)
            raise
        finally:
            steps, last = [], began
            for step, at in marks.items():
                steps.append(f"{step} {at - last:.2f}s")
                last = at
            _log.info("codex live: call %s startup %.2fs (%s)", tid, time.monotonic() - began, ", ".join(steps))
        return {
            "answer": call.answer,
            "realtimeSessionId": call.session_id,
            "threadId": tid,
            "version": "v3",
            "engine": "codex",
            "handoff": "client",
            "codexVersion": ".".join(map(str, server.version)),
        }

    def stop_call(self, thread_id: str) -> bool:
        with self._lock:
            call = self._calls.get(thread_id)
        if call is None or not self._end(call):
            return False
        _log.info("codex live: call %s stopped", thread_id)
        return True

    def interrupt(self, thread_id: str, turn_id: str) -> bool:
        with self._lock:
            call = self._calls.get(thread_id)
        if call is None:
            return False
        call.server.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=8)
        return True

    def close(self) -> None:
        with self._spawn_lock:
            server, self._server = self._server, None
        if server is not None:
            server.close()


_LIVE = _LiveBroker()
atexit.register(_LIVE.close)


def _codexlive_persona(profile: str | None, language: str | None = "en") -> str:
    if profile:
        sections = _bot_identity_sections(profile)
        name = _bot_display_name(profile)
    else:
        sections = talk_host.host().identity_sections()
        name = "Luna"
    base = talk_identity.build_instructions(sections, tools=[], lane="dashboard")
    if profile and "You are Hermes, speaking live" in base:
        base = base.replace("You are Hermes, speaking live", f"You are {name}, speaking live", 1)
    base += _language_directive(language)
    base += (
        f"\n\nVOICE: You are speaking with the user in a live voice session. Keep replies brief, natural, and to the point."
        f" IDENTITY: You are {name}; if asked who you are, answer as {name} in that role — never as a generic assistant."
    )
    base += _VOICE_POLICY
    return base


# El CLIENTE ejecuta lo que la voz pide (chat de Hermes del usuario). El core igual
# rutea cada delegación al thread de respaldo; el broker interrumpe ese turno al
# empezar (ver el execution guard). Esta instrucción es solo una capa extra.
_AGENT_INSTR_SKIP = (
    'You are connected to a live voice session, but the CLIENT (the app) executes the requests from that '
    'session. If you receive a <realtime_delegation> message, do NOT execute anything, do NOT use tools, '
    'and do NOT read files: reply with only the word: skip'
)

# Política de delegación para el MODELO DE VOZ (el que decide qué pasa al backend).
# Sin esto delega cualquier cosa (saludos, fragmentos, "hello?") y satura el chat con turnos largos.
_VOICE_POLICY = (
    "\n\nVOICE DELEGATION POLICY (live call):\n"
    "- Delegate to the chat/backend ONLY when the user asks you to DO something (check, review, find, run, make, fix, send, remember) or when answering needs real facts/actions from the backend.\n"
    "- Do NOT delegate greetings, small talk, acknowledgements, filler, thinking out loud, fragments, or repeats of something already answered (e.g. 'ok', 'yeah', 'right', 'hello?', 'can you hear me?', 'ja', 'okay', 'nå', 'hallo?', 'kan du høre mig?'). Answer those yourself, briefly, or stay quiet.\n"
    "- If the request is unclear, ask ONE short clarifying question yourself instead of delegating.\n"
    "- While a task is running: say ONE brief line in the user's language ('sure, I will check' / 'ja, jeg tjekker det') and WAIT silently; do not guess results, do not delegate again in the meantime; if the user speaks, tell them you are still on it.\n"
    "- When the result arrives, read the key facts back in one or two short sentences."
)


def _talk_settings() -> dict:
    try:
        p = _PLUGIN_ROOT / "settings.json"
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:  # noqa: BLE001
        pass
    return {}


def _agent_model() -> str | None:
    """Modelo del thread de respaldo. En cajas con un modelo default no-ChatGPT
    (p.ej. un proxy) hay que forzar un modelo oficial; si no, thread/start falla."""
    v = os.environ.get("TALK_CODEX_AGENT_MODEL") or str(_talk_settings().get("codexAgentModel") or "")
    v = v.strip()
    return v or None


def _codexlive_start(profile: str | None, voice: str, offer: str, language: str | None = "en") -> dict:
    if str(_talk_settings().get("delegation") or "client").strip().lower() == "server":
        raise LiveCallError(
            "LIVE_UNSUPPORTED",
            'settings.json asks for {"delegation": "server"}, but this broker never runs tasks on the Codex '
            "thread: voice tasks run in the Hermes chat. Remove that setting.",
        )
    return _LIVE.start_call(
        persona=_codexlive_persona(profile, language),
        start_instructions=_AGENT_INSTR_SKIP,
        voice=voice,
        offer=offer,
        model=_agent_model(),
    )



# ── Public API for other Hermes plugins ──────────────────────────────────────
#
# Hermes Gadget lets a paired phone make Live calls. Its server half runs in the
# gateway process, which does not serve these dashboard routes and holds no
# dashboard credential, so it loads this module and drives a broker of its own
# (with its own app-server) in that process. Every guarantee above applies:
# each call owns a fresh restricted thread, and stop reaches only its own call.

MIN_CODEX_VERSION = _CODEX_MIN_VERSION


def start_call(*, profile: str | None, offer: str, voice: str = "cove", language: str = "en") -> dict:
    """Start one isolated call and return its SDP answer and threadId (blocking).

    Raises LiveCallError (its `code` says why) and never falls back to another
    voice lane. `profile` must be a Hermes profile name, or None for the default.
    """
    if profile and _profile_home(profile) is None:
        raise LiveCallError("LIVE_BAD_PROFILE", f"profile '{profile}' does not exist")
    if not offer:
        raise LiveCallError("LIVE_BAD_OFFER", "the SDP offer is missing")
    return _codexlive_start(profile, voice, offer, language)


def stop_call(thread_id: str) -> bool:
    """Hang up the call that owns `thread_id`; False if no such call is live."""
    return _LIVE.stop_call(thread_id)

if router is not None:
    @router.post("/codexlive/session")
    async def codexlive_session(request: Request) -> dict:
        body = {}
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        profile = str(body.get("profile") or "").strip() or None
        if profile and _profile_home(profile) is None:
            raise HTTPException(status_code=400, detail=f"profile '{profile}' does not exist")
        voice = str(body.get("voice") or "cove")
        offer = str(body.get("offer") or "")
        if not offer:
            raise HTTPException(status_code=400, detail="the SDP offer is missing")
        try:
            result = await asyncio.to_thread(_codexlive_start, profile, voice, offer, body.get("language"))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=str(exc)[:400]) from exc
        if await request.is_disconnected():
            # Nobody will apply this answer: do not leave an orphan realtime session.
            await asyncio.to_thread(_LIVE.stop_call, result["threadId"])
            raise HTTPException(status_code=499, detail="the client closed the connection")
        return {
            "ok": True,
            "engine": "codex",
            "profile": profile,
            "botName": _bot_display_name(profile) if profile else "Luna",
            **result,
        }

    @router.post("/codexlive/interrupt")
    async def codexlive_interrupt(request: Request) -> dict:
        """Barge-in: corta el turno actual de la voz (turn/interrupt del app-server)."""
        body = {}
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        turn_id = str(body.get("turnId") or "").strip()
        thread_id = str(body.get("threadId") or "").strip()
        if not turn_id or not thread_id:
            return {"ok": False, "error": "turnId and threadId are required"}
        try:
            if not await asyncio.to_thread(_LIVE.interrupt, thread_id, turn_id):
                return {"ok": False, "error": "no such active call"}
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200]}

    @router.post("/codexlive/stop")
    async def codexlive_stop_route(request: Request) -> dict:
        body = {}
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        tid = str(body.get("threadId") or "").strip()
        if not tid:
            return {"ok": False, "error": "threadId is required"}
        return {"ok": True, "stopped": await asyncio.to_thread(_LIVE.stop_call, tid)}
