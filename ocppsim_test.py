# ocppsim test driver module
# Can be imported and used in test script or run directly to interact with the simulator

import argparse
import asyncio

import aioconsole
import websockets
import websockets.asyncio
import websockets.asyncio.server


class SimConnection:
    """Simulator connection class."""

    def __init__(self, url: str):
        self.url = url
        self.ws = None

    async def connect(self) -> None:
        self.ws = await websockets.connect(self.url)
        await asyncio.sleep(1)
        print(f"Connected to {self.url}")

    async def disconnect(self) -> None:
        if self.ws is not None:
            await self.ws.close()
            print("Disconnected from simulator")

    async def command(self, command: str) -> str:
        if self.ws is not None:
            await self.ws.send(command)
            response = await self.ws.recv()
            return response
        else:
            return "Not connected"


async def main():
    parser = argparse.ArgumentParser(description="OCCPSIM - test driver - main")
    parser.add_argument(
        "--url",
        default="ws://localhost:1234",
        type=str,
        help="Url of the simulator, e.g. ws://localhost:321/TACW6243111G2672",
    )

    args = parser.parse_args()
    if not args.url:
        print("No url provided. Exiting.")
        exit(1)

    conn = SimConnection(url=args.url)
    await conn.connect()
    if not conn.ws:
        print("Failed to connect to simulator. Exiting.")
        exit(1)

    while True:
        command = await aioconsole.ainput("Enter command: ")
        if command.lower() == "exit":
            break
        try:
            response = await conn.command(command)
            print(response)
        except websockets.exceptions.ConnectionClosedError:
            await conn.connect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Shutting down")
        exit(0)
