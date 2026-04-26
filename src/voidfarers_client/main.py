from __future__ import annotations

import argparse
import asyncio
import uuid

from livekit import rtc

from .backend import request_livekit_token


DEFAULT_BACKEND_URL = "https://backend.voidfarers.space"
DEFAULT_SYSTEM_ADDRESS = "10477373803"
DEFAULT_SYSTEM_NAME = "Sol"


async def connect_test(args: argparse.Namespace) -> None:
    client_id = args.client_id or f"vf-{uuid.uuid4()}"

    print(f"Requesting token from {args.backend_url}...")
    token_data = request_livekit_token(
        backend_url=args.backend_url,
        client_id=client_id,
        display_name=args.display_name,
        system_address=args.system_address,
        system_name=args.system_name,
    )

    print(f"LiveKit URL: {token_data['url']}")
    print(f"Room:        {token_data['room']}")
    print(f"Identity:    {client_id}")

    room = rtc.Room()

    @room.on("connected")
    def on_connected() -> None:
        print("Connected to LiveKit.")

    @room.on("disconnected")
    def on_disconnected(reason=None) -> None:
        print(f"Disconnected from LiveKit. Reason: {reason}")

    @room.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant) -> None:
        print(f"Participant joined: {participant.identity} / {participant.name}")

    @room.on("participant_disconnected")
    def on_participant_disconnected(participant: rtc.RemoteParticipant) -> None:
        print(f"Participant left: {participant.identity} / {participant.name}")

    await room.connect(token_data["url"], token_data["token"])

    print("Connected participants already in room:")

    if not room.remote_participants:
        print("  None")
    else:
        for participant in room.remote_participants.values():
            print(f"  {participant.identity} / {participant.name}")

    print("Connection test is running. Press Ctrl+C to quit.")

    try:
        while True:
            await asyncio.sleep(1)
    finally:
        await room.disconnect()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Voidfarers LiveKit connection test client")

    parser.add_argument("--backend-url", default=DEFAULT_BACKEND_URL)
    parser.add_argument("--client-id", default=None)
    parser.add_argument("--display-name", default="CMDR Test")
    parser.add_argument("--system-address", default=DEFAULT_SYSTEM_ADDRESS)
    parser.add_argument("--system-name", default=DEFAULT_SYSTEM_NAME)

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    try:
        asyncio.run(connect_test(args))
    except KeyboardInterrupt:
        print("Exiting.")


if __name__ == "__main__":
    main()
