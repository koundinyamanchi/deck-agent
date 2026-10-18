# Deck Agent

Talk to Claude and it does things on your Steam Deck: opens games and apps, moves files, writes
and runs scripts, checks storage and battery, and troubleshoots. Use it by voice on the Deck
itself (as an app in Gaming Mode or Desktop Mode) or from your phone's browser.

Under the hood it runs [Claude Code](https://code.claude.com) in headless mode on the Deck, behind
a small local web page. Speech-to-text runs offline on the Deck with Whisper. Nothing needs `sudo`.

> Not affiliated with or endorsed by Anthropic or Valve. "Claude" is a trademark of Anthropic.

## Features

- **Voice:** hold the mic button (or a key you map, F8) and talk. Whisper transcribes it on the Deck.
- **Works in both modes:** add it to Steam as a non-Steam game for Gaming Mode; it's in the app
  menu in Desktop Mode. A background service keeps it ready either way.
- **Phone control:** scan a QR code once, then use it from your phone on the same Wi-Fi.
- **Approvals:** reading files, editing files in your home folder or SD card, and the built-in
  `deckctl` helper run straight away. Anything else (deleting, installing, running scripts) shows
  an **Allow / Deny** card first.
- **History:** your last 50 chats are saved on the Deck. Reopen one to keep going where you left off.
- **Model picker:** Sonnet (default), Opus or Haiku. Each reply shows which model answered.

## Before you install: what you're giving it

Deck Agent lets an AI run commands as your Deck user. It can't use `sudo`, and risky actions wait
for your approval, but it can read and change files in your home folder and on your SD card. The
page is served over plain HTTP on your local network on port 8765. Other devices need the
pairing link (which contains a secret code), so keep that link private and use it only on a
network you trust.

## Requirements

- A Steam Deck (SteamOS 3.x), in Desktop Mode for the install.
- A Chromium-based browser from Discover (Google Chrome or Chromium) for the app and the sign-in.
- One way to reach Claude, chosen during install:
  - **Anthropic API key** (pay as you go, from [console.anthropic.com](https://console.anthropic.com)).
    This is the standard option for tools like this one.
  - **Claude subscription** (Pro or Max) through your own Claude Code login. Usage counts toward
    your plan's limits and is subject to Anthropic's terms for your plan.
- About 300 MB of free space if you add voice.

## Install

In Desktop Mode, open **Konsole** and run:

```bash
git clone <this repository's URL> deck-agent
cd deck-agent
./install.sh
```

(Or use **Code → Download ZIP** on GitHub, unzip it, and run `./install.sh` in that folder.)

The installer:
1. Asks how to connect to Claude: API key or subscription login.
2. Installs Claude Code if needed, then saves your API key (in `~/.config/deck-agent/env`, readable
   only by you) or walks you through the sign-in.
3. Installs Deck Agent as a background service and adds it to the app menu.
4. Offers to set up offline voice (Whisper).

It ends with the address for your phone. To use it in Gaming Mode, open Steam in Desktop Mode
and choose **Add a Game → Add a Non-Steam Game → Deck Agent**.

Non-interactive options: `./install.sh --auth api-key|subscription --voice|--no-voice` (with an
API key, set `ANTHROPIC_API_KEY` in the environment first to skip the prompt).

## Using it

- **On the Deck:** open **Deck Agent** from your Steam library or the app menu. Hold the mic and
  talk, or tap it to start and tap again to stop. In Steam's controller settings for Deck Agent you
  can map a back button to **F8** for hold-to-talk.
- **On your phone:** open the pairing link (or scan the QR code shown at `http://localhost:8765` on
  the Deck) and use your keyboard's dictation. Browsers only allow the page's own mic button on the
  Deck itself.
- Try: "open Firefox", "what's using space on my SD card?", "move the ISOs in Downloads to the SD
  card", "write a script that backs up my saves".
- **Stop** ends the current task. **New chat** starts fresh; older chats stay in **History**.
- In Gaming Mode, use the **Steam button** to switch between Deck Agent and a game. Steam games and
  non-Steam shortcuts launch reliably from Gaming Mode; plain desktop apps may not.

## Updating and switching

```bash
cd deck-agent && git pull && ./install.sh
```

Run `./install.sh --auth api-key` or `./install.sh --auth subscription` at any time to switch.

## Uninstall

```bash
./uninstall.sh
```

This removes the service, files, voice setup and saved API key, and keeps your chats.
`./uninstall.sh --all` removes the chats and pairing code too. Claude Code itself stays installed.

## Troubleshooting

| Problem | Fix |
| - | - |
| Sign-in opens the Discover store | Install Google Chrome or Chromium from Discover, then rerun `./install.sh`. |
| No mic button on the Deck | Voice isn't installed: run `~/.local/share/deck-agent/setup_voice.sh`. |
| Phone can't connect | Same Wi-Fi? Open the full pairing link from the installer (or the QR code at `http://localhost:8765` on the Deck). |
| Anything else | `journalctl --user -u deck-agent -f` shows the service log. |

## How it's built

| File | What it does |
| - | - |
| `server.py` | Background service: runs `claude -p`, keeps chats, transcribes voice, serves the page on port 8765 (Python standard library only) |
| `web/index.html` | The page: chat, mic, approvals, history, model picker |
| `permission_hook.py` | Claude Code `PermissionRequest` hook that asks the page for Allow / Deny |
| `stt_worker.py` | Whisper worker (faster-whisper, `base.en`), kept loaded between requests |
| `deckctl.py` | Lists and launches Steam games, non-Steam games and apps; switches modes; shows status |
| `deck-agent-app.sh` | Opens the page as a full-screen app (browser app mode) |
| `system_prompt.md` | Tells Claude it's on a Steam Deck and how to behave |
| `install.sh`, `setup_voice.sh`, `uninstall.sh` | Install, voice setup, removal |
| `dev/` | Stand-ins for Claude and Whisper, for testing on any computer |

To try the service without a Deck or a Claude account:

```bash
DECK_AGENT_CLAUDE="python3|dev/fake_claude.py" DECK_AGENT_STATE=/tmp/deck-agent python3 server.py
```

Then open http://localhost:8765. Messages containing "delete" exercise the Allow / Deny flow.

Note: faster-whisper's own audio loader passes an option that PyAV 15+ removed, so
`stt_worker.py` decodes audio with PyAV itself and hands Whisper the samples.

## License

MIT. See [LICENSE](LICENSE).
