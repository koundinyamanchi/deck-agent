#!/usr/bin/env bash
# Deck Agent installer. Run it on the Steam Deck in Desktop Mode (Konsole), from the folder
# you cloned or unzipped:
#
#   ./install.sh                         asks how to connect to Claude and whether to add voice
#   ./install.sh --auth api-key          use an Anthropic API key (or set ANTHROPIC_API_KEY first)
#   ./install.sh --auth subscription     use your own Claude Code login (Pro/Max)
#   ./install.sh --voice | --no-voice    add or skip offline speech-to-text
#
# Everything installs for the current user; nothing needs sudo. Run it again to update,
# or to switch between an API key and a subscription login.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.local/share/deck-agent"
CONF_DIR="$HOME/.config/deck-agent"
ENV_FILE="$CONF_DIR/env"
UNIT_DIR="$HOME/.config/systemd/user"
CLAUDE="$HOME/.local/bin/claude"
AUTH=""
VOICE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --auth) AUTH="${2:-}"; shift 2 ;;
    --voice) VOICE=yes; shift ;;
    --no-voice) VOICE=no; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)"; exit 1 ;;
  esac
done

say() { echo "==> $*"; }

# A browser for Claude's sign-in page. SteamOS opens web links in the Discover store by default.
find_browser() {
  local app dir
  for app in com.google.Chrome org.chromium.Chromium com.brave.Browser com.microsoft.Edge org.mozilla.firefox; do
    for dir in /var/lib/flatpak/exports/bin "$HOME/.local/share/flatpak/exports/bin"; do
      if [ -x "$dir/$app" ]; then echo "$dir/$app"; return; fi
    done
  done
}

# True if Claude Code answers a tiny request. With "login", any API key in the environment
# is ignored, so this checks the subscription login only.
claude_works() {
  local cmd=("$CLAUDE")
  [ "${1:-}" = login ] && cmd=(env -u ANTHROPIC_API_KEY "$CLAUDE")
  "${cmd[@]}" -p "Reply with just OK" --output-format json 2>/dev/null \
    | python3 -c 'import sys, json; sys.exit(1 if json.load(sys.stdin).get("is_error") else 0)' 2>/dev/null
}

# ---------------------------------------------------------------- 1. how to reach Claude

if [ -z "$AUTH" ]; then
  echo
  echo "How should Deck Agent connect to Claude?"
  echo "  1) Anthropic API key   Pay as you go, from console.anthropic.com. The standard"
  echo "                         option for tools like this one."
  echo "  2) Claude subscription Your own Claude Code login (Pro or Max). Usage counts toward"
  echo "                         your plan's limits and is subject to Anthropic's terms for it."
  read -rp "Choose 1 or 2: " choice
  case "$choice" in
    1) AUTH=api-key ;;
    2) AUTH=subscription ;;
    *) echo "Please run it again and choose 1 or 2."; exit 1 ;;
  esac
fi
case "$AUTH" in api-key|subscription) ;; *) echo "--auth must be api-key or subscription"; exit 1 ;; esac

# ---------------------------------------------------------------- 2. Claude Code

if [ -x "$CLAUDE" ]; then
  say "Claude Code is already installed"
else
  say "Installing Claude Code"
  curl -fsSL https://claude.ai/install.sh | bash
fi

# ---------------------------------------------------------------- 3. credentials

if [ "$AUTH" = api-key ]; then
  KEY="${ANTHROPIC_API_KEY:-}"
  if [ -z "$KEY" ]; then
    read -rsp "Paste your Anthropic API key (hidden as you type): " KEY
    echo
  fi
  case "$KEY" in sk-ant-*) ;; *) echo "!! That doesn't look like an Anthropic API key (they start with sk-ant-)."; exit 1 ;; esac
  say "Checking the API key"
  if ! ANTHROPIC_API_KEY="$KEY" claude_works; then
    echo "!! Claude didn't accept that key. Check it at console.anthropic.com and try again."
    exit 1
  fi
  mkdir -p "$CONF_DIR"
  chmod 700 "$CONF_DIR"
  (umask 077 && printf 'ANTHROPIC_API_KEY=%s\n' "$KEY" > "$ENV_FILE")
  unset KEY
  say "API key saved to $ENV_FILE (readable only by you)"
else
  rm -f "$ENV_FILE"  # an old API key would take priority over the login
  if claude_works login; then
    say "Claude Code is already logged in"
  else
    echo
    echo "Claude Code opens next. Choose 'Claude account with subscription' and sign in"
    echo "in the browser. Once you see the chat prompt (or a folder trust question), exit"
    echo "with /exit or by choosing 'No, exit'."
    read -rp "Press Enter to start... "
    BROWSER="$(find_browser)" env -u ANTHROPIC_API_KEY "$CLAUDE" || true
    if ! claude_works login; then
      echo "!! Still not logged in. Run ./install.sh again to retry."
      exit 1
    fi
  fi
fi

# ---------------------------------------------------------------- 4. Deck Agent files

say "Copying files to $DEST"
mkdir -p "$DEST/bin" "$DEST/web" "$HOME/.local/bin" "$UNIT_DIR"
cp "$SRC/server.py" "$SRC/permission_hook.py" "$SRC/deckctl.py" "$SRC/stt_worker.py" \
   "$SRC/system_prompt.md" "$SRC/deck-agent-app.sh" "$SRC/setup_voice.sh" "$DEST/"
cp "$SRC/web/index.html" "$SRC/web/icon.svg" "$DEST/web/"
chmod +x "$DEST/server.py" "$DEST/permission_hook.py" "$DEST/deckctl.py" "$DEST/stt_worker.py" \
         "$DEST/deck-agent-app.sh" "$DEST/setup_voice.sh"
ln -sf "$DEST/deckctl.py" "$DEST/bin/deckctl"
ln -sf "$DEST/deckctl.py" "$HOME/.local/bin/deckctl"

say "Adding Deck Agent to the app menu (and to Steam's 'Add a Non-Steam Game' list)"
mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/deck-agent.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Deck Agent
Comment=Talk to Claude on your Steam Deck
Exec=$DEST/deck-agent-app.sh
Icon=$DEST/web/icon.svg
Categories=Utility;
EOF

# ---------------------------------------------------------------- 5. background service

say "Setting up the background service"
cat > "$UNIT_DIR/deck-agent.service" <<EOF
[Unit]
Description=Deck Agent (Claude on your Steam Deck)
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 $DEST/server.py
Environment=PATH=$DEST/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-$ENV_FILE
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable deck-agent.service >/dev/null
# Keep the service alive while SteamOS switches between Gaming and Desktop Mode.
loginctl enable-linger "$USER" 2>/dev/null || true

# ---------------------------------------------------------------- 6. voice (optional)

if [ -x "$DEST/stt-venv/bin/python" ]; then
  say "Voice is already set up"
else
  if [ -z "$VOICE" ]; then
    echo
    echo "Add offline voice input? It downloads about 300 MB once (Whisper speech-to-text)"
    read -rp "and runs entirely on the Deck. [Y/n] " answer
    case "$answer" in [nN]*) VOICE=no ;; *) VOICE=yes ;; esac
  fi
  if [ "$VOICE" = yes ]; then
    bash "$DEST/setup_voice.sh"
  fi
fi

systemctl --user restart deck-agent.service
sleep 2
if systemctl --user is-active --quiet deck-agent.service; then
  echo
  echo "Deck Agent is running (connected with: $AUTH)."
  echo "  On the Deck:   http://localhost:8765, or open 'Deck Agent' from the app menu"
  echo "  In Gaming Mode: in Steam, Add a Game > Add a Non-Steam Game > Deck Agent"
  echo "  On your phone (same Wi-Fi):"
  echo "    $(/usr/bin/python3 "$DEST/server.py" --pair-url)"
  echo "  Keep that phone link private: it contains the pairing code."
  echo
  echo "Logs: journalctl --user -u deck-agent -f"
else
  echo "!! The service didn't start. Logs:"
  journalctl --user -u deck-agent -n 30 --no-pager
  exit 1
fi
