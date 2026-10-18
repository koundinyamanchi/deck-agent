#!/usr/bin/env python3
"""deckctl: Steam Deck helpers for the Deck Agent (and for you).

  deckctl status              mode, battery and free space
  deckctl mode                gaming | desktop | unknown
  deckctl apps [search]       list Steam games, non-Steam shortcuts and desktop apps
  deckctl open <name|id>      launch one of them
  deckctl switch desktop|gaming
"""
import glob
import os
import re
import shlex
import shutil
import subprocess
import sys

HOME = os.path.expanduser("~")
STEAM_ROOTS = [os.path.join(HOME, ".local/share/Steam"), os.path.join(HOME, ".steam/steam")]
DESKTOP_DIRS = [
    os.path.join(HOME, ".local/share/applications"),
    os.path.join(HOME, ".local/share/flatpak/exports/share/applications"),
    "/var/lib/flatpak/exports/share/applications",
    "/usr/share/applications",
]
SKIP_STEAM_NAMES = ("Proton", "Steam Linux Runtime", "Steamworks Common", "SteamVR")
SESSION_KEYS = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS",
                "XDG_RUNTIME_DIR", "XDG_CURRENT_DESKTOP", "XDG_SESSION_TYPE")


# ---------------------------------------------------------------- session

def _processes():
    """Yield (pid, name) for this user's processes."""
    uid = os.getuid() if hasattr(os, "getuid") else None
    for pid in os.listdir("/proc") if os.path.isdir("/proc") else []:
        if not pid.isdigit():
            continue
        try:
            if uid is not None and os.stat(f"/proc/{pid}").st_uid != uid:
                continue
            with open(f"/proc/{pid}/comm") as f:
                yield int(pid), f.read().strip()
        except OSError:
            continue


def current_mode():
    names = {name for _, name in _processes()}
    if "plasmashell" in names:
        return "desktop"
    if any(n.startswith("gamescope") for n in names):
        return "gaming"
    return "unknown"


def session_env():
    """Borrow display/D-Bus variables from the running desktop or Steam, so apps we start
    from a background service show up on screen."""
    env = dict(os.environ)
    wanted = {"desktop": ("plasmashell",), "gaming": ("steam", "gamescope-sessi")}.get(current_mode(), ())
    for pid, name in _processes():
        if name not in wanted:
            continue
        try:
            with open(f"/proc/{pid}/environ", "rb") as f:
                pairs = [p.split(b"=", 1) for p in f.read().split(b"\0") if b"=" in p]
        except OSError:
            continue
        found = {k.decode(): v.decode(errors="replace") for k, v in pairs if k.decode() in SESSION_KEYS}
        if "DISPLAY" in found or "WAYLAND_DISPLAY" in found:
            env.update(found)
            break
    return env


def spawn(argv):
    subprocess.Popen(argv, env=session_env(), start_new_session=True, cwd=HOME,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------- Steam data

def parse_binary_vdf(buf):
    pos = 0

    def cstr():
        nonlocal pos
        end = buf.index(b"\0", pos)
        s = buf[pos:end].decode("utf-8", "replace")
        pos = end + 1
        return s

    def table():
        nonlocal pos
        out = {}
        while pos < len(buf):
            kind = buf[pos]
            pos += 1
            if kind == 0x08:
                return out
            key = cstr()
            if kind == 0x00:
                out[key] = table()
            elif kind == 0x01:
                out[key] = cstr()
            elif kind == 0x02:
                out[key] = int.from_bytes(buf[pos:pos + 4], "little")
                pos += 4
            elif kind == 0x07:
                out[key] = int.from_bytes(buf[pos:pos + 8], "little")
                pos += 8
            else:
                raise ValueError(f"unknown VDF type {kind:#x}")
        return out

    return table()


def steam_root():
    return next((r for r in STEAM_ROOTS if os.path.isdir(os.path.join(r, "steamapps"))), None)


def steam_games():
    root = steam_root()
    if not root:
        return []
    libraries = {root}
    try:
        with open(os.path.join(root, "steamapps/libraryfolders.vdf"), encoding="utf-8") as f:
            libraries.update(re.findall(r'"path"\s+"([^"]+)"', f.read()))
    except OSError:
        pass
    games = {}
    for lib in libraries:
        for manifest in glob.glob(os.path.join(lib, "steamapps", "appmanifest_*.acf")):
            try:
                with open(manifest, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            appid = re.search(r'"appid"\s+"(\d+)"', text)
            name = re.search(r'"name"\s+"([^"]+)"', text)
            if appid and name and not name.group(1).startswith(SKIP_STEAM_NAMES):
                games[appid.group(1)] = {"id": f"steam:{appid.group(1)}", "name": name.group(1),
                                         "kind": "Steam game"}
    return list(games.values())


def steam_shortcuts():
    root = steam_root()
    out = []
    for path in glob.glob(os.path.join(root or "/nonexistent", "userdata", "*", "config", "shortcuts.vdf")):
        try:
            with open(path, "rb") as f:
                data = parse_binary_vdf(f.read())
        except (OSError, ValueError):
            continue
        for entry in data.get("shortcuts", {}).values():
            fields = {k.lower(): v for k, v in entry.items()}
            appid, name = fields.get("appid"), fields.get("appname")
            if appid is None or not name:
                continue
            game_id = ((appid & 0xFFFFFFFF) << 32) | 0x02000000
            out.append({"id": f"shortcut:{game_id}", "name": name, "kind": "Non-Steam game"})
    return out


# ---------------------------------------------------------------- desktop apps

def desktop_apps():
    apps = {}
    for folder in DESKTOP_DIRS:
        for path in glob.glob(os.path.join(folder, "*.desktop")):
            app_id = os.path.basename(path)[:-len(".desktop")]
            if app_id in apps:
                continue  # earlier folders (the user's own) win
            entry = {}
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    in_main = False
                    for line in f:
                        line = line.strip()
                        if line.startswith("["):
                            in_main = line == "[Desktop Entry]"
                        elif in_main and "=" in line:
                            key, value = line.split("=", 1)
                            entry.setdefault(key.strip(), value.strip())
            except OSError:
                continue
            if (entry.get("Type") != "Application" or entry.get("NoDisplay") == "true"
                    or entry.get("Hidden") == "true" or not entry.get("Exec")):
                continue
            apps[app_id] = {"id": f"app:{app_id}", "name": entry.get("Name", app_id),
                            "kind": "App", "exec": entry["Exec"]}
    return list(apps.values())


def exec_argv(exec_line):
    exec_line = re.sub(r"%[fFuUdDnNickvm]", "", exec_line).replace("%%", "%")
    # Flatpak exports wrap file arguments in @@ / @@u markers; with no files they're just noise.
    return [arg for arg in shlex.split(exec_line) if arg not in ("@@", "@@u")]


# ---------------------------------------------------------------- commands

def catalog():
    return steam_games() + steam_shortcuts() + desktop_apps()


def find(query, items):
    q = query.strip().lower()
    for test in (lambda i: i["id"].lower() == q,
                 lambda i: i["name"].lower() == q,
                 lambda i: i["name"].lower().startswith(q),
                 lambda i: q in i["name"].lower() or q in i["id"].lower()):
        hits = [i for i in items if test(i)]
        if hits:
            return hits
    return []


def cmd_apps(args):
    items = catalog()
    if args:
        items = find(" ".join(args), items)
    for item in sorted(items, key=lambda i: (i["kind"], i["name"].lower())):
        print(f"{item['kind']:<15} {item['name']}  [{item['id']}]")
    if not items:
        print("Nothing found.")


def cmd_open(args):
    if not args:
        sys.exit("usage: deckctl open <name or id>")
    hits = find(" ".join(args), catalog())
    if not hits:
        sys.exit(f"Nothing called '{' '.join(args)}'. Try: deckctl apps <search>")
    if len(hits) > 1:
        print("More than one match; open one by its id:")
        for item in hits[:15]:
            print(f"  {item['name']}  [{item['id']}]")
        sys.exit(2)
    item, mode = hits[0], current_mode()
    if item["id"].startswith(("steam:", "shortcut:")):
        steam = shutil.which("steam") or "/usr/bin/steam"
        spawn([steam, f"steam://rungameid/{item['id'].split(':', 1)[1]}"])
    else:
        spawn(exec_argv(item["exec"]))
        if mode == "gaming":
            print("Note: in Gaming Mode, desktop apps may not appear on screen. "
                  "Add the app to Steam (Add a Non-Steam Game) to launch it reliably.")
    print(f"Launching {item['name']} ({mode} mode).")


def cmd_switch(args):
    target = (args or [""])[0].lower()
    if target in ("desktop", "plasma"):
        subprocess.run(["steamos-session-select", "plasma"], env=session_env(), check=False)
    elif target in ("gaming", "game", "gamescope"):
        returner = next((a for a in desktop_apps() if "return to gaming" in a["name"].lower()), None)
        if returner:
            spawn(exec_argv(returner["exec"]))
        else:
            subprocess.run(["steamos-session-select", "gamescope"], env=session_env(), check=False)
    else:
        sys.exit("usage: deckctl switch desktop|gaming")
    print(f"Switching to {target} mode.")


def cmd_status(_args):
    print(f"Mode: {current_mode()}")
    for bat in glob.glob("/sys/class/power_supply/BAT*"):
        try:
            with open(os.path.join(bat, "capacity")) as f:
                level = f.read().strip()
            with open(os.path.join(bat, "status")) as f:
                state = f.read().strip()
            print(f"Battery: {level}% ({state})")
        except OSError:
            pass
    for mount in [HOME] + sorted(glob.glob("/run/media/*/*") + glob.glob("/run/media/*")):
        if os.path.ismount(mount) or mount == HOME:
            usage = shutil.disk_usage(mount)
            print(f"Free on {mount}: {usage.free / 1e9:.1f} GB of {usage.total / 1e9:.1f} GB")


COMMANDS = {"apps": cmd_apps, "open": cmd_open, "switch": cmd_switch,
            "status": cmd_status, "mode": lambda _a: print(current_mode())}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(__doc__)
    COMMANDS[sys.argv[1]](sys.argv[2:])
