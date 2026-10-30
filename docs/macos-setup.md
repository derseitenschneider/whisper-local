# macOS setup (menu-bar app from this fork)

How to set up Whisper Local on a new Mac from
[derseitenschneider/whisper-local](https://github.com/derseitenschneider/whisper-local),
as a menu-bar app that starts from Spotlight or Raycast without a terminal.

The app bundle only launches the installed `whisper-local` command, and that command
runs straight from the cloned repo (editable install). A `git pull` takes effect the
next time the app starts; the bundle never needs rebuilding for code changes.

## 1. Prerequisites

- Apple Silicon Mac, macOS 12 or later
- Xcode Command Line Tools (for `clang` and `git`): `xcode-select --install`
- [Homebrew](https://brew.sh), then:

```bash
brew install python@3.12 uv
```

## 2. Clone and install the command

```bash
git clone git@github.com:derseitenschneider/whisper-local.git ~/dev/personal/whisper-local
cd ~/dev/personal/whisper-local
uv tool install --python /opt/homebrew/bin/python3.12 -e '.[whispercpp]'
```

This puts `whisper-local` (and the short alias `wl`) in `~/.local/bin`. If your shell
can't find them, run `uv tool update-shell` and open a new terminal.

The `whispercpp` extra adds the whisper.cpp backend, which runs on the Mac's GPU
(Metal). Leave it out only if you want the default faster-whisper backend.

## 3. Build the app

```bash
packaging/macos/make-app.sh
```

This creates `~/Applications/Whisper Local.app`. Pass a different folder as the first
argument to install it elsewhere.

## 4. First launch and permissions

```bash
open ~/Applications/"Whisper Local.app"
```

macOS asks for permissions on first use. Grant all of them to **Whisper Local**:

- **Microphone**, to record.
- **Accessibility**, for the global hotkey and auto-paste. If no prompt appears, add the
  app under System Settings → Privacy & Security → Accessibility, then quit and relaunch it.
- **Input Monitoring**, if macOS asks for it.

The first launch downloads the Whisper model, so it takes a while before the app is ready.
Logs go to `~/.whisperkey/app.log` and `~/.whisperkey/launcher.log`.

To start it at login, tick **Start on login** in the menu-bar menu.

## 5. Settings

Your settings live in `~/.whisperkey/` (model, hotkeys, hotwords, microphone, profiles,
voice commands). To move them from another Mac, copy at least
`~/.whisperkey/user_settings.yaml` before the first launch. Everything else in that
folder is optional (`commands.yaml`, `profiles.yaml`, `app_rules.yaml`, `transforms.yaml`,
`transcripts.jsonl` for history).

The microphone is stored by name (`audio.input_device`). If that device isn't plugged
in, the app uses the system default input and switches back when it reappears.

## Updating

```bash
cd ~/dev/personal/whisper-local
git pull
```

Then pick **Restart Whisper Local** in the menu-bar menu. Re-run `uv tool install ...` only if
`pyproject.toml` dependencies changed, and `packaging/macos/make-app.sh` only if
something in `packaging/macos/` changed.

## Troubleshooting

- `whisper-local --doctor` checks dependencies, config, audio devices, model cache and
  hotkeys.
- Hotkey does nothing: check the Accessibility permission (step 4). After rebuilding
  the app, macOS can need the permission toggled off and on again.
- More in [troubleshooting.md](troubleshooting.md).

## Uninstall

```bash
uv tool uninstall whisper-local
rm -rf ~/Applications/"Whisper Local.app"
rm -rf ~/.whisperkey   # settings, history, logs
```
