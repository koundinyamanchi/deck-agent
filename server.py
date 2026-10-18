#!/usr/bin/env python3
"""Deck Agent: runs Claude Code on the Steam Deck and serves a phone-friendly control page.

Standard library only, so it runs on SteamOS's stock Python with nothing to install.
Claude Code is driven in headless mode (`claude -p`), which uses the Claude account you
logged in with (Pro/Max) -- no API key.
"""
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import deckctl  # noqa: E402  (mode/status helpers shared with the CLI)

PORT = int(os.environ.get("DECK_AGENT_PORT", "8765"))
STATE_DIR = os.environ.get("DECK_AGENT_STATE", os.path.expanduser("~/.local/state/deck-agent"))
WORK_DIR = os.environ.get("DECK_AGENT_WORKDIR", os.path.expanduser("~"))
APPROVAL_TIMEOUT = 280  # seconds; the hook itself is given 300
# Speech-to-text runs in its own virtualenv (faster-whisper), set up by setup_voice.sh.
STT_PYTHON = os.environ.get("DECK_AGENT_STT_PYTHON",
                            os.path.join(HERE, "stt-venv", "bin", "python"))
MAX_AUDIO_BYTES = 15 * 1024 * 1024
CHATS_DIR = os.path.join(STATE_DIR, "chats")
MAX_CHATS = 50
MAX_EVENTS = 800
# Claude Code model aliases offered in the page. Sonnet is the default: fast, and light on Pro limits.
MODELS = {"sonnet": "Sonnet", "opus": "Opus", "haiku": "Haiku"}
DEFAULT_MODEL = "sonnet"

# Things Claude may do without asking. File edits and mkdir/cp/mv/touch inside the home
# folder and the SD card are covered by acceptEdits mode; everything else is sent to the
# phone for approval through the PermissionRequest hook.
ALLOWED_TOOLS = [
    "Read", "Glob", "Grep", "WebSearch", "WebFetch",
    "Bash(deckctl status*)", "Bash(deckctl mode*)", "Bash(deckctl apps*)", "Bash(deckctl open *)",
    "Bash(du *)", "Bash(df*)", "Bash(lsblk*)", "Bash(free*)", "Bash(uptime*)",
    "Bash(flatpak list*)", "Bash(flatpak search *)", "Bash(flatpak info *)",
    "Bash(sha256sum *)", "Bash(chmod +x *)",
]
DISALLOWED_TOOLS = ["AskUserQuestion"]


# ---------------------------------------------------------------- state

def _load_json(name, default):
    try:
        with open(os.path.join(STATE_DIR, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save_json(name, data):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, name)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(path + ".tmp", path)


def load_config():
    cfg = _load_json("config.json", {})
    if not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(16)
        _save_json("config.json", cfg)
        try:
            os.chmod(os.path.join(STATE_DIR, "config.json"), 0o600)
        except OSError:
            pass
    return cfg


def find_claude():
    override = os.environ.get("DECK_AGENT_CLAUDE")
    if override:
        return override.split("|")  # e.g. "python|dev/fake_claude.py" for local testing
    found = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    return [found]


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent; this just picks the LAN interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def pair_url(cfg):
    return f"http://{lan_ip()}:{PORT}/?t={cfg['token']}"


# ---------------------------------------------------------------- event log

class EventLog:
    """Append-only list of chat events that the page long-polls."""

    def __init__(self):
        self.cond = threading.Condition()
        self.events = []
        self.next_id = 1
        self.log_id = uuid.uuid4().hex[:8]
        self.dirty = False  # set on every change; the chat store saves it shortly after

    def add(self, type_, **data):
        with self.cond:
            ev = {"id": self.next_id, "type": type_, "ts": time.time(), **data}
            self.next_id += 1
            self.events.append(ev)
            del self.events[:-MAX_EVENTS]
            self.dirty = True
            self.cond.notify_all()
            return ev

    def reset(self, events=()):
        """Switch to another chat's events (or an empty chat); open pages reload."""
        with self.cond:
            self.events = list(events)
            self.next_id = max((e["id"] for e in self.events), default=0) + 1
            self.log_id = uuid.uuid4().hex[:8]
            self.dirty = False
            self.cond.notify_all()

    def snapshot(self):
        with self.cond:
            self.dirty = False
            return list(self.events)

    def wait(self, after, log_id, timeout):
        deadline = time.time() + timeout
        with self.cond:
            while True:
                if log_id != self.log_id:
                    return self.log_id, list(self.events)
                new = [e for e in self.events if e["id"] > after]
                remaining = deadline - time.time()
                if new or remaining <= 0:
                    return self.log_id, new
                self.cond.wait(remaining)


# ---------------------------------------------------------------- Claude runner

def summarize_tool(name, inp):
    inp = inp or {}
    for key in ("command", "file_path", "pattern", "url", "query", "path"):
        if inp.get(key):
            return str(inp[key])
    return json.dumps(inp)[:300]


def tool_result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


class ChatStore:
    """Saved conversations: one JSON file each in CHATS_DIR, newest MAX_CHATS kept."""

    def __init__(self):
        os.makedirs(CHATS_DIR, exist_ok=True)

    @staticmethod
    def valid(chat_id):
        return bool(re.fullmatch(r"[0-9a-f]{12}", chat_id or ""))

    def _path(self, chat_id):
        return os.path.join(CHATS_DIR, f"{chat_id}.json")

    def load(self, chat_id):
        if not self.valid(chat_id):
            return None
        try:
            with open(self._path(chat_id), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def save(self, chat):
        path = self._path(chat["id"])
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(chat, f)
        os.replace(path + ".tmp", path)

    def new(self, session_id=None):
        now = time.time()
        chat = {"id": uuid.uuid4().hex[:12], "title": "", "created": now, "updated": now,
                "session_id": session_id, "model": None, "events": []}
        self.save(chat)
        self.prune()
        return chat

    def delete(self, chat_id):
        if self.valid(chat_id):
            try:
                os.remove(self._path(chat_id))
            except OSError:
                pass

    def list(self):
        chats = []
        for name in os.listdir(CHATS_DIR):
            if name.endswith(".json"):
                chat = self.load(name[:-5])
                if chat:
                    chats.append({k: chat.get(k) for k in ("id", "title", "created", "updated", "model")}
                                 | {"messages": sum(e["type"] == "user" for e in chat.get("events", []))})
        return sorted(chats, key=lambda c: c["updated"] or 0, reverse=True)

    def prune(self):
        for chat in self.list()[MAX_CHATS:]:
            self.delete(chat["id"])


class Agent:
    def __init__(self, log):
        self.log = log
        self.proc = None
        self.stopped = False
        self.lock = threading.Lock()
        self.save_lock = threading.RLock()  # guards switching chats vs. the background save
        self.store = ChatStore()
        self.chat = self.store.load(_load_json("current.json", {}).get("chat_id"))
        if not self.chat:
            # First run with saved chats: carry over the conversation from before.
            self.chat = self.store.new(_load_json("session.json", {}).get("session_id"))
            self._remember_current()
        self.log.reset(self.chat.get("events", []))
        self.model = _load_json("settings.json", {}).get("model", DEFAULT_MODEL)
        self.run_model = None
        self.approvals = {}
        threading.Thread(target=self._autosave, daemon=True).start()
        self.hook_secret = secrets.token_urlsafe(16)
        self.settings_path = self._write_settings()
        with open(os.path.join(HERE, "system_prompt.md"), encoding="utf-8") as f:
            self.system_prompt = f.read()

    def _write_settings(self):
        hook = f'"{sys.executable}" "{os.path.join(HERE, "permission_hook.py")}"'
        settings = {"hooks": {"PermissionRequest": [
            {"matcher": "*", "hooks": [{"type": "command", "command": hook, "timeout": 300}]}
        ]}}
        _save_json("claude-settings.json", settings)
        return os.path.join(STATE_DIR, "claude-settings.json")

    @property
    def busy(self):
        return self.proc is not None

    @property
    def session_id(self):
        return self.chat.get("session_id")

    def _set_session(self, sid):
        if sid and sid != self.session_id:
            self.chat["session_id"] = sid
            self.flush(force=True)

    # -- saved chats

    def _remember_current(self):
        _save_json("current.json", {"chat_id": self.chat["id"]})

    def flush(self, force=False):
        """Write the current chat to disk if anything changed."""
        with self.save_lock:
            if not (force or self.log.dirty):
                return
            events = self.log.snapshot()
            self.chat["events"] = events
            if not self.chat.get("title"):
                first = next((e["text"] for e in events if e["type"] == "user"), "")
                self.chat["title"] = first[:80]
            if events:
                self.chat["updated"] = events[-1]["ts"]
            self.store.save(self.chat)

    def _autosave(self):
        while True:
            time.sleep(2)
            try:
                self.flush()
            except OSError:
                pass

    def open_chat(self, chat_id):
        with self.lock:
            if self.busy:
                return "busy"
            chat = self.store.load(chat_id)
            if not chat:
                return "missing"
            with self.save_lock:
                self.flush()
                self.chat = chat
                self._remember_current()
                self.log.reset(chat.get("events", []))
            return "ok"

    def delete_chat(self, chat_id):
        if not self.store.valid(chat_id):
            return False
        with self.lock, self.save_lock:
            if chat_id == self.chat["id"]:
                if self.busy:
                    return False
                self.chat = self.store.new()
                self._remember_current()
                self.log.reset()
            self.store.delete(chat_id)
            return True

    def set_model(self, model):
        if model not in MODELS:
            return False
        self.model = model
        _save_json("settings.json", {"model": model})
        return True

    def send(self, text):
        with self.lock:
            if self.busy:
                return False
            self.log.add("user", text=text)
            self.stopped = False
            self.run_model = None
            self.chat["model"] = self.model
            cmd = find_claude() + [
                "-p", "--output-format", "stream-json", "--verbose", "--model", self.model,
                "--permission-mode", "acceptEdits",
                "--settings", self.settings_path,
                "--append-system-prompt", self.system_prompt,
                "--allowedTools", ",".join(ALLOWED_TOOLS),
                "--disallowedTools", ",".join(DISALLOWED_TOOLS),
            ]
            if os.path.isdir("/run/media"):
                cmd += ["--add-dir", "/run/media"]  # SD card and USB drives
            if self.session_id:
                cmd += ["--resume", self.session_id]
            env = dict(os.environ,
                       DECK_AGENT_URL=f"http://127.0.0.1:{PORT}",
                       DECK_AGENT_HOOK_SECRET=self.hook_secret,
                       PATH=os.path.join(HERE, "bin") + os.pathsep + os.environ.get("PATH", ""))
            try:
                self.proc = subprocess.Popen(
                    cmd, cwd=WORK_DIR, env=env, text=True, encoding="utf-8", errors="replace",
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    start_new_session=(os.name == "posix"))
            except OSError as e:
                self.log.add("error", text=f"Couldn't start Claude Code ({e}). Is it installed?")
                return True
            self.log.add("state", busy=True)
        threading.Thread(target=self._pump, args=(self.proc, text), daemon=True).start()
        return True

    def _pump(self, proc, text):
        proc.stdin.write(text)
        proc.stdin.close()
        stderr_tail = []
        threading.Thread(target=lambda: stderr_tail.extend(proc.stderr.readlines()[-20:]),
                         daemon=True).start()
        got_result = False
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            got_result |= self._handle(msg)
        proc.wait()
        if self.stopped:
            self.log.add("status", text="Stopped.")
        elif not got_result and proc.returncode != 0:
            tail = "".join(stderr_tail).strip()[-800:]
            self.log.add("error", text=f"Claude Code exited with code {proc.returncode}. {tail}")
        with self.lock:
            self.proc = None
            self._cancel_approvals("The run ended.")
            self.log.add("state", busy=False)
            self.flush(force=True)

    def _handle(self, msg):
        kind = msg.get("type")
        if kind == "system" and msg.get("subtype") == "init":
            self.run_model = msg.get("model")
            self._set_session(msg.get("session_id"))
        elif kind == "system" and msg.get("subtype") == "api_retry":
            self.log.add("status", text=f"Retrying ({msg.get('error', 'error')})...")
        elif kind == "assistant":
            for block in msg.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text", "").strip():
                    self.log.add("assistant", text=block["text"])
                elif block.get("type") == "tool_use":
                    self.log.add("tool", tool_id=block.get("id"), name=block.get("name"),
                                 summary=summarize_tool(block.get("name"), block.get("input")))
        elif kind == "user":
            content = msg.get("message", {}).get("content", [])
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_result":
                    out = tool_result_text(block.get("content")).strip()
                    self.log.add("tool_result", tool_id=block.get("tool_use_id"),
                                 ok=not block.get("is_error"), text=out[-1500:])
        elif kind == "result":
            self._set_session(msg.get("session_id"))
            is_error = bool(msg.get("is_error")) or msg.get("subtype") != "success"
            if is_error and msg.get("result"):
                self.log.add("error", text=str(msg["result"]))
            model = self.run_model or next(iter(msg.get("modelUsage") or {}), None)
            self.log.add("done", ok=not is_error, duration_ms=msg.get("duration_ms"), model=model)
            return True
        return False

    def stop(self):
        with self.lock:
            proc = self.proc
            self.stopped = proc is not None
            self._cancel_approvals("Stopped by the user.")
        if not proc:
            return
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGINT)  # SIGINT lets Claude end the turn cleanly
            else:
                proc.terminate()
        except OSError:
            pass

        def _force():
            time.sleep(8)
            if proc.poll() is None:
                proc.kill()
        threading.Thread(target=_force, daemon=True).start()

    def new_chat(self):
        with self.lock:
            if self.busy:
                return False
            with self.save_lock:
                self.flush()
                if self.chat.get("events") or self.session_id:  # reuse an untouched empty chat
                    self.chat = self.store.new()
                    self._remember_current()
                self.log.reset()
            return True

    # -- approvals (called from the PermissionRequest hook via /internal/permission)

    def request_permission(self, payload):
        name = payload.get("tool_name", "?")
        approval = {"done": threading.Event(), "decision": None}
        aid = uuid.uuid4().hex[:10]
        with self.lock:
            if not self.busy:
                return {"behavior": "deny", "message": "No active run."}
            self.approvals[aid] = approval
        self.log.add("approval", approval_id=aid, name=name,
                     summary=summarize_tool(name, payload.get("tool_input")))
        if not approval["done"].wait(APPROVAL_TIMEOUT):
            approval["decision"] = {"behavior": "deny",
                                    "message": "The user didn't answer in time. Ask them again."}
            self.log.add("approval_resolved", approval_id=aid, allowed=False, reason="timeout")
        with self.lock:
            self.approvals.pop(aid, None)
        return approval["decision"]

    def resolve(self, aid, allow):
        with self.lock:
            approval = self.approvals.get(aid)
            if not approval or approval["done"].is_set():
                return False
            approval["decision"] = {"behavior": "allow"} if allow else {
                "behavior": "deny", "message": "The user said no. Don't retry it; ask what they want instead."}
            approval["done"].set()
        self.log.add("approval_resolved", approval_id=aid, allowed=allow)
        return True

    def _cancel_approvals(self, why):
        for aid, approval in list(self.approvals.items()):
            if not approval["done"].is_set():
                approval["decision"] = {"behavior": "deny", "message": why, "interrupt": True}
                approval["done"].set()
                self.log.add("approval_resolved", approval_id=aid, allowed=False, reason="cancelled")


# ---------------------------------------------------------------- speech-to-text

class Transcriber:
    """Keeps one Whisper worker process alive so the model loads only once."""

    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()

    @property
    def available(self):
        return shutil.which(STT_PYTHON) is not None

    def _worker(self):
        if self.proc is None or self.proc.poll() is not None:
            self.proc = subprocess.Popen(
                [STT_PYTHON, os.path.join(HERE, "stt_worker.py")], text=True, encoding="utf-8",
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            ready = json.loads(self.proc.stdout.readline() or "{}")
            if not ready.get("ready"):
                self.proc = None
                raise RuntimeError(ready.get("error", "the speech model didn't start"))
        return self.proc

    def transcribe(self, audio, suffix):
        if not self.available:
            return {"error": "Voice isn't set up on this Deck yet (run setup_voice.sh)."}
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(audio)
        try:
            with self.lock:
                worker = self._worker()
                worker.stdin.write(json.dumps({"path": f.name}) + "\n")
                worker.stdin.flush()
                line = worker.stdout.readline()
            return json.loads(line) if line else {"error": "the speech worker stopped"}
        except (OSError, ValueError, RuntimeError) as e:
            self.proc = None
            return {"error": f"Transcription failed: {e}"}
        finally:
            os.unlink(f.name)


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "DeckAgent/1"
    cfg = None
    agent = None
    log = None
    stt = None

    def log_message(self, fmt, *args):  # keep the journal quiet
        pass

    def _is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _authorized(self):
        if self._is_local():
            return True
        token = self.headers.get("X-Deck-Token") or parse_qs(urlparse(self.path).query).get("t", [""])[0]
        return secrets.compare_digest(token, self.cfg["token"])

    def _json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):
        url = urlparse(self.path)
        static = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/index.html": ("index.html", "text/html; charset=utf-8"),
                  "/icon.svg": ("icon.svg", "image/svg+xml")}
        if url.path in static:
            name, ctype = static[url.path]
            with open(os.path.join(HERE, "web", name), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self._authorized():
            return self._json(401, {"error": "bad token"})
        if url.path == "/api/info":
            info = {"busy": self.agent.busy, "mode": deckctl.current_mode(),
                    "has_session": bool(self.agent.session_id), "local": self._is_local(),
                    "voice": self.stt.available, "model": self.agent.model, "models": MODELS,
                    "auth": "api_key" if os.environ.get("ANTHROPIC_API_KEY") else "subscription",
                    "chat_id": self.agent.chat["id"]}
            if self._is_local():
                info["pair_url"] = pair_url(self.cfg)
            return self._json(200, info)
        if url.path == "/api/events":
            q = parse_qs(url.query)
            after = int(q.get("after", ["0"])[0])
            log_id, events = self.log.wait(after, q.get("log", [""])[0], min(25, float(q.get("wait", ["25"])[0])))
            return self._json(200, {"log": log_id, "events": events, "busy": self.agent.busy})
        if url.path == "/api/chats":
            self.agent.flush()
            return self._json(200, {"chats": self.agent.store.list(), "current": self.agent.chat["id"]})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/internal/permission":
            if not (self._is_local() and secrets.compare_digest(
                    self.headers.get("X-Hook-Secret", ""), self.agent.hook_secret)):
                return self._json(403, {"error": "forbidden"})
            return self._json(200, self.agent.request_permission(self._body()))
        if not self._authorized():
            return self._json(401, {"error": "bad token"})
        if url.path == "/api/transcribe":
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_AUDIO_BYTES:
                return self._json(413, {"error": "Recording is empty or too long."})
            audio = self.rfile.read(length)
            suffix = ".ogg" if "ogg" in (self.headers.get("Content-Type") or "") else ".webm"
            result = self.stt.transcribe(audio, suffix)
            return self._json(200 if "text" in result else 500, result)
        data = self._body()
        if url.path == "/api/send":
            text = (data.get("text") or "").strip()
            if not text:
                return self._json(400, {"error": "empty"})
            if not self.agent.send(text):
                return self._json(409, {"error": "Claude is still working. Stop it or wait."})
            return self._json(200, {"ok": True})
        if url.path == "/api/approve":
            ok = self.agent.resolve(data.get("approval_id", ""), bool(data.get("allow")))
            return self._json(200 if ok else 410, {"ok": ok})
        if url.path == "/api/stop":
            self.agent.stop()
            return self._json(200, {"ok": True})
        if url.path == "/api/new":
            ok = self.agent.new_chat()
            return self._json(200 if ok else 409, {"ok": ok})
        if url.path == "/api/chats/open":
            result = self.agent.open_chat(data.get("id", ""))
            codes = {"ok": 200, "busy": 409, "missing": 404}
            return self._json(codes[result], {"ok": result == "ok", "error": result})
        if url.path == "/api/chats/delete":
            ok = self.agent.delete_chat(data.get("id", ""))
            return self._json(200 if ok else 409, {"ok": ok})
        if url.path == "/api/model":
            ok = self.agent.set_model(data.get("model", ""))
            return self._json(200 if ok else 400, {"ok": ok, "model": self.agent.model})
        self._json(404, {"error": "not found"})


def main():
    cfg = load_config()
    if "--pair-url" in sys.argv:
        print(pair_url(cfg))
        return
    log = EventLog()
    Handler.cfg, Handler.log, Handler.agent, Handler.stt = cfg, log, Agent(log), Transcriber()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    httpd.daemon_threads = True
    print(f"Deck Agent listening on port {PORT}", flush=True)
    print(f"Phone link: {pair_url(cfg)}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
