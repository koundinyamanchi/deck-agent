#!/usr/bin/env python3
"""Stand-in for `claude -p --output-format stream-json` so the service and page can be tested
without a Claude login. Prompts containing "delete" exercise the real PermissionRequest hook.

  DECK_AGENT_CLAUDE="python|dev/fake_claude.py" python server.py
"""
import json
import subprocess
import sys
import time
import uuid

args = sys.argv[1:]
opt = lambda name: args[args.index(name) + 1] if name in args else None
prompt = sys.stdin.read()
session = opt("--resume") or str(uuid.uuid4())


def emit(obj):
    print(json.dumps(obj), flush=True)
    time.sleep(0.4)


def assistant(*blocks):
    emit({"type": "assistant", "message": {"content": list(blocks)}, "session_id": session})


def tool(tid, name, inp, output, ok=True):
    assistant({"type": "tool_use", "id": tid, "name": name, "input": inp})
    emit({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid, "content": output, "is_error": not ok}]}})


emit({"type": "system", "subtype": "init", "session_id": session,
      "model": f"claude-{opt('--model') or 'opus'}-5-5"})
assistant({"type": "text", "text": f"On it: **{prompt.strip()[:60]}**"})
tool("t1", "Bash", {"command": "deckctl status"}, "Mode: gaming\nBattery: 81% (Discharging)")

if "delete" in prompt.lower():
    with open(opt("--settings"), encoding="utf-8") as f:
        hook_cmd = json.load(f)["hooks"]["PermissionRequest"][0]["hooks"][0]["command"]
    request = {"hook_event_name": "PermissionRequest", "tool_name": "Bash",
               "tool_input": {"command": "rm -rf ~/Downloads/old-isos"}}
    out = subprocess.run(hook_cmd, input=json.dumps(request), capture_output=True, text=True, shell=True)
    decision = json.loads(out.stdout)["hookSpecificOutput"]["decision"]
    if decision["behavior"] == "allow":
        tool("t2", "Bash", {"command": "rm -rf ~/Downloads/old-isos"}, "")
        assistant({"type": "text", "text": "Deleted `~/Downloads/old-isos`."})
    else:
        assistant({"type": "text", "text": f"Okay, I left it alone. ({decision.get('message', '')})"})
else:
    assistant({"type": "text", "text": "Done. You're in Gaming Mode with 81% battery.\n\n```\ndeckctl status\n```"})

emit({"type": "result", "subtype": "success", "is_error": False, "duration_ms": 2100,
      "result": "done", "session_id": session})
