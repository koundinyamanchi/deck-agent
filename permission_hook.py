#!/usr/bin/env python3
"""Claude Code PermissionRequest hook: asks the phone page, waits for Allow/Deny."""
import json
import os
import sys
import urllib.request

payload = json.load(sys.stdin)
request = urllib.request.Request(
    os.environ.get("DECK_AGENT_URL", "http://127.0.0.1:8765") + "/internal/permission",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json",
             "X-Hook-Secret": os.environ.get("DECK_AGENT_HOOK_SECRET", "")})
try:
    with urllib.request.urlopen(request, timeout=295) as response:
        decision = json.load(response)
except Exception as e:  # never leave Claude hanging: an unreachable service means "no"
    decision = {"behavior": "deny", "message": f"Couldn't reach the approval service: {e}"}

print(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}))
