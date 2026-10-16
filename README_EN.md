# OBS Mic Light

[中文](README.md)

A tiny always-on-top dot that shows whether your **OBS microphone is live or muted** — so you never record 30 minutes of silence again.

![states](assets/states.png)

- 🟢 **Green** = mic live　🔴 **Red** = muted　⚪ **Gray** = not connected to OBS
- Updates instantly no matter how you mute: hotkey, the OBS mixer, Stream Deck…
- **Windows & macOS**, no Python needed for the download version
- No config editing: right-click the dot to pick the mic, enter the password, change size
- Drag it anywhere; position is remembered
- Free & open source (MIT). Talks only to your local OBS, no telemetry

## Quick start

1. **OBS → Tools → WebSocket Server Settings** → check **Enable WebSocket server** → **Show Connect Info** → copy the **Server Password** → OK. (OBS 28+)
2. Download from **[Releases](https://github.com/wangchuanqiw/obs-mic-light/releases/latest)**:
   - Windows: `OBS-Mic-Light-Windows.zip` → unzip → run `OBS Mic Light.exe`
     (SmartScreen warning: *More info → Run anyway* — the app isn't code-signed)
   - Mac (Apple Silicon / Intel): unzip → drag to Applications → open.
     If macOS blocks it: *System Settings → Privacy & Security → Open Anyway*.
3. Paste the password when asked. The dot turns green/red — done.

## Usage

| Action | Result |
|---|---|
| Drag the dot | Move it (remembered) |
| Right-click the dot / click the tray (menu bar) icon | Menu: choose mic, size, password, reset position, logs, quit |
| Hover | Shows status and which source is monitored |

**Dot stays gray?** OBS isn't running or the WebSocket server is off.
**Color doesn't change when you mute?** You're muting a different source — right-click → choose the right mic.
**Not visible in a fullscreen game?** Switch the game to borderless windowed.

## Run from source

```bash
git clone https://github.com/wangchuanqiw/obs-mic-light.git
cd obs-mic-light
python3 -m pip install -r requirements.txt
python3 run.pyw
```

Build: `pip install pyinstaller pillow && pyinstaller packaging/obs_mic_light.spec`.
Pushing a `v*` tag builds Windows/macOS packages via GitHub Actions and publishes a release.

## License

[MIT](LICENSE)
