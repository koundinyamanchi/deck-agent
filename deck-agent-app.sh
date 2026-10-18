#!/usr/bin/env bash
# Opens Deck Agent as a full-screen app. Add this to Steam (as "Deck Agent") to use it in
# Gaming Mode; in Desktop Mode it's also in the app menu.
# Needs a Chromium-based browser from Flatpak (Chrome, Chromium, Brave or Edge), for its app
# mode. It runs with its own profile, where the mic is pre-allowed for Deck Agent's page only.

APP=""
for id in com.google.Chrome org.chromium.Chromium com.brave.Browser com.microsoft.Edge; do
  if flatpak info "$id" >/dev/null 2>&1; then APP="$id"; break; fi
done
if [ -z "$APP" ]; then
  msg="Deck Agent needs Google Chrome or Chromium. Install one from Discover, then try again."
  notify-send "Deck Agent" "$msg" 2>/dev/null || echo "$msg" >&2
  exit 1
fi

PROFILE="$HOME/.var/app/$APP/deck-agent-profile"

if [ ! -e "$PROFILE/SingletonLock" ]; then  # the browser rewrites Preferences while it runs
  python3 - "$PROFILE/Default/Preferences" <<'EOF'
import json, os, sys
path = sys.argv[1]
os.makedirs(os.path.dirname(path), exist_ok=True)
try:
    with open(path, encoding="utf-8") as f:
        prefs = json.load(f)
except (OSError, ValueError):
    prefs = {}
exceptions = prefs.setdefault("profile", {}).setdefault("content_settings", {}).setdefault("exceptions", {})
exceptions.setdefault("media_stream_mic", {})["http://localhost:8765,*"] = {"setting": 1}
with open(path, "w", encoding="utf-8") as f:
    json.dump(prefs, f)
EOF
fi

exec flatpak run "$APP" \
  --app="http://localhost:8765/?app=1" \
  --user-data-dir="$PROFILE" \
  --no-first-run --no-default-browser-check \
  --start-fullscreen --class=DeckAgent \
  "$@"
