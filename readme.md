Voidfarers Voice Client - Beta 1.01 MVP edition

Download the [Latest Release](https://github.com/MostlyAwol/Void-Farers/releases/latest)

Requirements:
- Windows 10/11
- Microphone and headphones recommended
- Elite Dangerous journal mode requires Elite Dangerous journal files.

First Run:
1. Open Voidfarers.exe.
2. Enter a display name.
3. Choose input and output audio devices.
4. Choose Push-to-Talk key. Default is F12.
5. Use Static Test Room for initial testing, or Journal Mode for Elite Dangerous.
6. Click Connect.

Notes:
- Use headphones to avoid echo.
- If audio devices do not work, refresh devices and select them manually.
- The app runs in the system tray when closed if "Minimize/close to tray" is enabled.
- Right-click the tray icon and choose Quit to fully exit.
- Windows SmartScreen may warn because this beta is unsigned. Choose More info > Run anyway if you trust the build.
- This is a minimum viable product to just test the services and see if it does what I want it to do. More features will come if there is interest.

Beta Feedback Wanted:
- Did the app launch?
- Did your microphone and output device appear?
- Could you connect?
- Could you hear another tester?
- Did journal mode switch rooms correctly?
- Any crashes, delays, or audio glitches?

## Backend Overview

Void Farers uses LiveKit as the backend voice service. [LiveKit](https://github.com/livekit/livekit) provides the WebRTC infrastructure used to connect commanders together in real time. 

The backend uses LiveKit rooms as the voice channels. A room represents a shared voice space, such as a specific star system and private group. Clients connected to the same room can hear each other, while clients in different rooms are kept separate. Rooms are based on the commander’s current Elite Dangerous context. When a client detects that the player is in a system, it requests access to the matching voice room. Commanders in the same system can then be placed into the same LiveKit room so they can speak with each other.

Commander Client
  -> requests voice access for current system/group
  -> receives LiveKit connection details
  -> joins LiveKit room
  -> publishes microphone audio
  -> receives audio from other commanders in the same room

LiveKit handles the WebRTC side of the system, including audio transport, publishing local microphone audio, subscribing to remote audio tracks, and managing connection state. The Void Farers backend setup is mainly responsible for deciding which LiveKit room a client should join and providing the client with the access needed to connect.

In simple terms:

Client
  -> Backend API
      -> creates LiveKit access token
  <- token + LiveKit URL
Client
  -> LiveKit server
      -> joins room and sends/receives audio

Voice audio is handled by LiveKit and WebRTC. Void Farers uses that backend to group commanders by in-game location so voice chat follows where players are in the galaxy.