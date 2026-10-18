#!/usr/bin/env bash
# Stops and removes Deck Agent, including the voice setup and any saved API key.
# Claude Code itself and its login are left alone.
#   ./uninstall.sh            keeps your saved chats and pairing code
#   ./uninstall.sh --all      removes those too
set -uo pipefail
systemctl --user disable --now deck-agent.service 2>/dev/null
rm -f "$HOME/.config/systemd/user/deck-agent.service" "$HOME/.local/bin/deckctl" \
      "$HOME/.local/share/applications/deck-agent.desktop"
systemctl --user daemon-reload
rm -rf "$HOME/.local/share/deck-agent" "$HOME/.config/deck-agent"
if [ "${1:-}" = "--all" ]; then
  rm -rf "$HOME/.local/state/deck-agent"
  for dir in "$HOME"/.var/app/*/deck-agent-profile; do [ -d "$dir" ] && rm -rf "$dir"; done
  echo "Removed Deck Agent, its chats, pairing code and browser profile."
else
  echo "Removed Deck Agent. Saved chats and the pairing code remain in ~/.local/state/deck-agent"
  echo "(run with --all to remove them too)."
fi
echo "Remove 'Deck Agent' from your Steam library by hand if you added it."
