English | [日本語](README.ja.md)

# Claude Code Monitor

A monitoring tool that lists the Claude Code sessions currently running on a
host. For each session it shows the **session name** (click the "UUID" button
next to it to reveal/hide the session ID), **context usage** (% + token
count), and a **status lamp** (working = blinking red, idle = solid green).
(Web UI only — the desktop GUI version does not show the session ID.)

The bundled files are listed below. The monitor itself is either
`claude_monitor.py` **or** `claude_monitor_web.py` — **you only need one of
the two**. `daily_kickoff.py` and the files below it are for the "send Hello
every morning at 4am" feature.

| File | Purpose |
|---|---|
| `claude_monitor.py` | Desktop GUI version (tkinter). Use on a machine with a GUI, or over X11 forwarding |
| `claude_monitor_web.py` | Web browser version. **Recommended for headless environments such as a Proxmox LXC container** |
| `daily_kickoff.py` | The script that sends "Hello" to Claude every morning at 4am JST (details below) |
| `kickoff_state.json` | State for `daily_kickoff.py` (enabled/disabled, last run result). Auto-generated at runtime |
| `kickoff.log` | A simple log for `daily_kickoff.py`. Auto-generated at runtime |
| `systemd/claude-daily-kickoff.service` / `.timer` | systemd units that fire `daily_kickoff.py` every morning at 4am JST |

Both versions run on the Python standard library only (no extra packages
needed — the GUI version may require installing `tkinter` separately
depending on your environment).

---

## Prerequisite: the data this tool reads

The following files are generated automatically by Claude Code and **must
exist on the same host where this tool runs** (run it on the machine/container
where the Claude Code sessions actually run — not on a remote box you only
SSH from).

- `~/.claude/sessions/*.json` — metadata for running sessions (PID, session ID,
  working directory, etc.)
- `~/.claude/projects/*/<session-id>.jsonl` — the conversation transcript
  (used to compute context usage)

If these don't exist or can't be read, the session list shows "No active
sessions."

---

## Setup

1. Extract the files into any folder on the host running Claude Code (e.g.
   inside your Proxmox LXC container):
   ```bash
   mkdir -p ~/claude_monitor
   # extract the files here
   ```
2. Confirm Python 3 is installed:
   ```bash
   python3 --version
   ```

---

## Usage A: Web browser version (recommended for headless environments)

### 1. Start the server on the LXC container
```bash
cd ~/claude_monitor
python3 claude_monitor_web.py --port 8765
```
By default it binds only to `127.0.0.1` (not exposed on the LAN).
Press `Ctrl+C` to stop it.

To access it directly from the LAN (without an SSH tunnel), pass
`--host 0.0.0.0`. If the host is also on a VPN such as Tailscale, it will be
reachable on the same port from that virtual IP too.
```bash
python3 claude_monitor_web.py --host 0.0.0.0 --port 8765
```
⚠️ Exposing it on `0.0.0.0` means **anyone can view it without
authentication** (session names, working directories, and context usage are
visible). If you can't trust every device on your LAN/VPN, keep it on
`127.0.0.1` and use an SSH tunnel instead.

### 2a. Viewing over an SSH tunnel
```bash
ssh -L 8765:localhost:8765 <user>@<LXC IP or hostname>
```
Keep this SSH connection open and browse to `http://localhost:8765`.

### 2b. Direct LAN/Tailscale access
```
http://<LXC's LAN IP or Tailscale IP>:8765
```

The page refreshes every 1.5 seconds. Sessions that are currently working
show a blinking red lamp.

### Installing it as a standalone window (PWA)
It supports PWA features (manifest, icon, service worker), so Chrome/Edge's
"Install" feature can turn it into a standalone window with no tab or
address bar.

⚠️ **Browsers only allow the install feature in a "secure context" (HTTPS, or
`localhost`).** Plain HTTP access such as `http://<LAN IP>:8765` won't show
an install button. To install it reliably, use option 2a above (SSH tunnel →
`localhost:8765`).

### Keeping it running (optional)
Run it inside `screen`/`tmux`, or set it up as a systemd service.

Example (systemd service, for direct LAN/Tailscale exposure):
```ini
# /etc/systemd/system/claude-monitor.service
[Unit]
Description=Claude Code Monitor (Web)
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /root/claude_monitor/claude_monitor_web.py --host 0.0.0.0 --port 8765
Restart=on-failure
User=root

[Install]
WantedBy=multi-user.target
```
```bash
systemctl daemon-reload
systemctl enable --now claude-monitor
```
In production (the claudecode LXC container) it is already running
persistently as `claude-monitor.service` with the config above.

---

## Usage A': address-switchable PWA shell (`docs/index.html`)

A lightweight front end, separate from the server itself, for situations
where **the server address changes** — a LAN IP, a Tailscale IP, or
`localhost` via an SSH tunnel. Publishing it on GitHub Pages gives you a
single, stable URL: enter the server's address once and it's saved on that
device, then it reconnects automatically on every later launch (no address
or other secret is ever baked into the server itself).

Published URL: `https://<your GitHub username>.github.io/claude-code-monitor/`

### How to use it
1. Open the URL above in a browser (on first use you can also "Install" it in
   Chrome/Edge to place it on your home screen/desktop — GitHub Pages serves
   over HTTPS, so PWA installation works correctly here).
2. On first launch you'll see a form asking for the address of the server
   running `claude_monitor_web.py` (e.g. `192.168.1.10:8765` or
   `localhost:8765`). Press "Connect" and, once it verifies the connection,
   it's saved on that device (browser).
3. On every later launch this device automatically connects to the saved
   address. To change it, use "Address: ... (change)" in the top right.

### Server-side prerequisite (CORS)
Since `docs/index.html` fetches the server's `/api/sessions` etc. from a
different origin (GitHub Pages), `claude_monitor_web.py` sends
`Access-Control-Allow-Origin: *` (the only data exposed is session names,
working directories, and context usage — nothing that requires
authentication — so no origin restriction is applied). An older server
version won't send the CORS header and the connection will fail, so make
sure `claude_monitor_web.py` is up to date.

### Setting up GitHub Pages (one-time)
In the repository's Settings → Pages, set Source to "Deploy from a branch"
and Branch to `main` / `/docs`. It can take a few minutes to go live.

---

## Usage B: desktop GUI version

For environments where a GUI is directly available (the container has a
desktop environment, or you use SSH X11 forwarding).

### If you have a local GUI environment
```bash
python3 claude_monitor.py
```
If `tkinter` isn't installed you'll get
`ModuleNotFoundError: No module named 'tkinter'`. On Debian/Ubuntu:
```bash
apt install python3-tk
```

### Forwarding the GUI over SSH (X11 forwarding)
- Remote (LXC) side: `sshd_config` needs `X11Forwarding yes`, and the `xauth`
  package must be installed
- Local side: you need an X server (Windows: VcXsrv / X410; Mac: XQuartz;
  Linux usually has one built in)

```bash
ssh -X <user>@<LXC IP or hostname>
python3 claude_monitor.py
```

---

## Changing settings (optional)

### Context limit (important — always check for your environment)

Context usage is calculated as `tokens used ÷ CONTEXT_TOKEN_LIMIT`. This
limit is **200,000** or **1,000,000 (extended context)** depending on your
plan/model, so leaving it at the default can make it disagree with the
percentage Claude Code's `/context` command shows.

Running `/context` prints something like `Tokens: 96.6k / 1m (10%)` — check
the denominator on the right (`1m` = 1,000,000 in this example) and match it
using one of the following:

- Set it via an environment variable (shared by both files; add it to
  `.bashrc` etc. to make it permanent):
  ```bash
  export CLAUDE_CONTEXT_TOKEN_LIMIT=1000000
  python3 claude_monitor_web.py --port 8765
  ```
- The web version also accepts a command-line flag:
  ```bash
  python3 claude_monitor_web.py --port 8765 --context-limit 1000000
  ```
- If nothing is set, the default is `1,000,000`. If your plan uses 200,000
  tokens, set `200000` explicitly.

### Other constants

Both files have a block of constants near the top. Edit them as needed.

- `WORKING_THRESHOLD_SEC` (default 6s): if the transcript was updated within
  this many seconds, the session is considered "working" (red)
- `POLL_INTERVAL_MS` (default 1500ms): how often the list is refreshed
- `BLINK_INTERVAL_MS` (GUI version only, default 500ms): how fast the red
  lamp blinks

If you change the web version's client-side poll interval
(`POLL_INTERVAL_MS`), restart the server — it's baked into the generated
HTML.

---

## The "Hello every morning at 4am" feature (daily_kickoff.py)

A persistent scheduler that runs `claude -p -n daily-4am-hello "Hello"` every
morning at 4am JST, starting one new session. Because it uses `-p`
(print/non-interactive mode), the process exits as soon as it gets a
response — unlike `--bg`, it never lingers as a background agent that piles
up in the session list or `claude agents`. The conversation itself is saved
to history just like any normal session.

The web monitor's top panel shows its status and lets you toggle it on/off.
`claude_monitor_web.py` imports `daily_kickoff.py` from the same folder to
read/write that state (`/api/kickoff`, `/api/kickoff/toggle`).

### Making it persistent (systemd timer)

```bash
cp systemd/claude-daily-kickoff.service systemd/claude-daily-kickoff.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now claude-daily-kickoff.timer
```

`claude-daily-kickoff.service`'s `ExecStart` points at the production
location (`/root/claude_monitor/daily_kickoff.py`), so copy it there the same
way as `claude_monitor_web.py` (see Deployment below).

The timer fires on `OnCalendar=*-*-* 04:00:00 Asia/Tokyo` — 4am JST (19:00
UTC the previous day, depending on the season). It's already `enable`d, so it
stays active across LXC restarts. With `Persistent=true`, if the container
was stopped when 4am passed, it runs once as soon as it comes back up.

Check the next scheduled run:
```bash
systemctl list-timers claude-daily-kickoff.timer
```

### Enabling/disabling it

Normally you'd use the toggle in the web panel, but it can also be controlled
from the CLI:

```bash
python3 daily_kickoff.py --set-enabled false   # disable (next run is skipped)
python3 daily_kickoff.py --set-enabled true    # enable
python3 daily_kickoff.py --status              # show current state
```

Disabling it leaves the systemd timer running — `daily_kickoff.py` itself
just records it as "skipped" and never launches `claude`.

### Running it manually (to test it)

```bash
python3 daily_kickoff.py
```

Note that this actually runs `claude -p`, which consumes real Claude
API/session usage.

---

## Deployment (pushing changes to production)

After editing `claude_monitor.py` / `claude_monitor_web.py` /
`daily_kickoff.py` / `kickoff_state.json`, copy them to the production
location `/root/claude_monitor/` and restart the relevant service.

```bash
cp claude_monitor_web.py claude_monitor.py daily_kickoff.py /root/claude_monitor/
systemctl restart claude-monitor.service
```

If you edit `systemd/*.service` / `*.timer`, copy them to
`/etc/systemd/system/` and run `systemctl daemon-reload`.

---

## Troubleshooting

| Symptom | What to check |
|---|---|
| No sessions show up at all | Check that `~/.claude/sessions/` and `~/.claude/projects/` exist on this host — files on a different host/container are never visible |
| `ModuleNotFoundError: No module named 'tkinter'` | Only affects the GUI version. Install it with `apt install python3-tk`, or use the web version instead |
| Can't open it in a browser | Check that the SSH tunnel (`ssh -L 8765:localhost:8765 ...`) is still connected, and that the server is actually running (`python3 claude_monitor_web.py`) |
| The lamp stays green forever | Check whether Claude is actually processing in that session. There can be a delay of up to `WORKING_THRESHOLD_SEC` seconds right after it starts working |
| Context % doesn't match `/context` | Match the "Context limit" setting above to `/context`'s denominator (200k or 1m). If it's still off, a sub-agent (Task) may be running — wait a moment and check again |
| No install button appears in the browser | Installation only works in a secure context (HTTPS or `localhost`) — this is a browser restriction. It won't appear over plain HTTP to a LAN/Tailscale IP. Open it via an SSH tunnel at `localhost:8765` instead |
| After setting `--host 0.0.0.0`, session info became visible to anyone | This is expected (there's no authentication). On a network with untrusted devices, go back to `127.0.0.1` + an SSH tunnel |
