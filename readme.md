# Voidfarers Voice Client

Voidfarers is an experimental proximity/system voice client for **Elite Dangerous**.

The client reads your local Elite Dangerous journal files, detects your current commander/session state, and connects you to a LiveKit voice room based on where you are and how you are playing.

Current routing behavior:

* **Open:** connects to a room for your current system.
* **Private Group:** connects to a room for your current system and group.
* **Solo:** does not connect to voice.
* **Not in game:** waits for an active Elite Dangerous session.

Voidfarers is designed to let commanders communicate with others who are plausibly in the same in-game context, without requiring Discord servers, manual room selection, or static channels.

---

## Beta Warning

Voidfarers is currently beta software.

Expect bugs, connection issues, audio device problems, UI rough edges, and possible behavior changes between builds. The project is still under active development and should not be treated as a finished or fully stable voice platform.

During beta testing:

* Do not rely on Voidfarers for critical communication.
* Report unexpected behavior when possible.
* Be aware that logs may be useful for troubleshooting.
* Features, room behavior, account-linking behavior, and backend requirements may change.

---

## Privacy

Voidfarers is designed to collect as little personal information as possible.

The client does **not** require an email address, password, or separate Voidfarers account. Commander verification is handled through Frontier authentication so the service can confirm that a connected client belongs to a real Elite Dangerous commander.

The client may send the following information to the Voidfarers backend when requesting voice access:

* Client install ID
* Display name / verified commander name
* Frontier commander identifier after account linking
* Current Elite Dangerous system address
* Current system name
* Current game mode, such as Open or Group
* Private group name, when applicable
* Client platform/version metadata used for troubleshooting

Voidfarers does **not** intentionally collect chat transcripts, voice recordings, email addresses, passwords, or unnecessary personal information.

Voice traffic is handled through LiveKit infrastructure. Audio is transmitted for real-time voice communication and is not intended to be recorded by Voidfarers.

The backend uses verification and session data to:

* Confirm commander identity
* Prevent obvious abuse
* Route clients to the correct voice room
* Support moderation if needed
* Troubleshoot service problems

Because Voidfarers is beta software, avoid sharing anything over voice that you would not want other participants in the same room to hear.

---

## Account Linking

Voidfarers requires a linked Frontier commander account before joining voice rooms.

On first launch, the client will walk you through the linking process:

1. Open the account link page.
2. Log in with Frontier.
3. Copy the pairing code.
4. Paste the pairing code into the client.
5. The client stores a local session token.

The session token is saved locally in your client configuration file. Treat it like a login token. If you believe it was exposed, clear your linked account in the client and link again.

---

## How Voice Rooms Work

Voidfarers uses Elite Dangerous journal events to determine where to connect.

Room examples:

```text
system-10477373803-open
system-10477373803-Mobius_PvE
```

The system address is used because it is stable and unique. The system name is shown in the client for readability.

Voidfarers does not connect in Solo mode.

---

## Requirements

* Windows
* Elite Dangerous
* A Frontier account with an Elite Dangerous commander
* Microphone and audio output device
* Internet access
* Access to the Voidfarers backend service

For development:

* Python 3.11 or newer recommended
* PowerShell
* Virtual environment recommended

---

## Running From Source

From the project root, the folder containing:

```text
run_voidfarers.py
src
assets
requirements.txt
```

Create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install dependencies:

```powershell
pip install -r requirements.txt
```

Run the client:

```powershell
$env:PYTHONPATH="src"
python run_voidfarers.py
```

The `PYTHONPATH` setting only applies to the current PowerShell window.

---

## Building the Windows EXE

From the project root:

```powershell
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
Remove-Item -Force Voidfarers.spec -ErrorAction SilentlyContinue

pyinstaller `
  --name Voidfarers `
  --windowed `
  --onefile `
  --icon assets\voidfarers.ico `
  --add-data "assets\voidfarers.ico;assets" `
  --add-data "assets\voidfarers_icon.png;assets" `
  --paths src `
  --collect-all PySide6 `
  --collect-all sounddevice `
  --collect-all livekit `
  --collect-all livekit-api `
  --hidden-import livekit.rtc.resources `
  --hidden-import livekit.rtc.resources.liblivekit_ffi `
  --hidden-import numpy `
  --hidden-import pynput `
  run_voidfarers.py
```

The built executable will be located in:

```text
dist\Voidfarers.exe
```

---

## Configuration

Voidfarers stores local client configuration in the user profile.

The configuration may include:

* Client install ID
* Backend URL
* Display name
* Audio device choices
* Push-to-talk key
* Local session token
* Journal folder path
* Startup/tray preferences

Deleting the config file resets the client to first-run behavior and requires account linking again.

---

## Journal Detection

Voidfarers reads local Elite Dangerous journal files to detect:

* Commander name
* Current system
* System address
* Game mode
* Private group name
* Login/logout state
* System changes

By default, the client looks in the standard Elite Dangerous journal folder:

```text
%USERPROFILE%\Saved Games\Frontier Developments\Elite Dangerous
```

The journal folder can be changed in Settings if needed.

---

## Push-to-Talk

Voidfarers currently uses push-to-talk.

The default key is:

```text
F12
```

You can change the push-to-talk key in the client.

---

## Audio Devices

The client allows selecting input and output devices.

If audio does not work:

1. Open the client.
2. Refresh audio devices.
3. Select the correct microphone.
4. Select the correct output device.
5. Save settings.
6. Restart the voice connection.

---

## Troubleshooting

### The client says my account is not linked

Open **Link Account**, log in with Frontier, copy the pairing code, and paste it into the client.

### The client will not connect in Solo mode

This is expected. Voidfarers does not connect to voice rooms while Elite Dangerous is in Solo mode.

### The client is waiting for Elite Dangerous

The client has not detected an active journal state yet. Start Elite Dangerous or load into the game.

### The wrong commander name appears

If your account is verified, the verified commander name should be used. If it is not, clear the linked account and link again.

### Audio devices are missing

Use **Refresh devices** in the client. If the device still does not appear, restart the client.

### I pasted or exposed my session token

Clear the linked account in the client and link again.

---

## Development Status

Voidfarers is currently focused on:

* Stable account linking
* Journal-based room routing
* Reliable audio handling
* Simple Windows packaging
* Minimal data collection
* Beta testing with real Elite Dangerous sessions

Possible future work:

* Improved moderation tools
* Better diagnostics
* More polished UI
* Better participant display
* Optional issue reporting
* More robust installer/update flow

---

## Security Notes

The client is not trusted by the backend.

The backend decides whether a client is verified, whether it may join a room, and which LiveKit room/token it receives.

Do not place backend secrets, LiveKit API secrets, Frontier client secrets, or admin keys in this client repository.

---

## Service Dependency

The Voidfarers Voice Client is open source, but it is designed to connect to the official Voidfarers backend service.

The backend service, LiveKit infrastructure, Frontier OAuth configuration, verification system, and moderation tools are operated separately and are not included in this client repository.

Forks or modified versions of this client may require their own compatible backend service to function.

---

## License

Copyright © 2026 Jay Clark

Licensed under the Apache License, Version 2.0. You may obtain a copy of the license in the `LICENSE` file included with this project.

Unless required by applicable law or agreed to in writing, software distributed under the Apache License, Version 2.0 is distributed on an “AS IS” basis, without warranties or conditions of any kind.
