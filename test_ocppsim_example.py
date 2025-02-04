# Example of how to use the ocppsim_test module in a test script (pytest style)

import asyncio

import pytest

from ocppsim_test import SimConnection


@pytest.mark.asyncio(loop_scope="function")
async def test_case1():
    url = "ws://localhost:1234"

    # Create a connection to the simulator
    conn = SimConnection(url)
    await conn.connect()
    if not conn.ws:
        raise Exception(f"Failed to connect to simulator at {url}")

    # Wait a little to ensure various initializations are done.
    await asyncio.sleep(5)

    # Standard charging scenario
    # plugin cable
    response = await conn.command("plugin")
    assert response == "Cable plugged. Status Preparing"

    # scan the default tag
    response = await conn.command("tag")
    assert response == "Tag Accepted. Parent: , new status: SuspendedEVSE"

    await asyncio.sleep(40)  # Charge for a little, check status
    response = await conn.command("status")
    assert response == "Status: Charging, transaction_id: 1, offer: 17 A, energy (rounded): 0 Wh, delay: False"

    await asyncio.sleep(40)  # Charge for a little, check status
    response = await conn.command("status")
    assert response == "Status: Charging, transaction_id: 1, offer: 16 A, energy (rounded): 200 Wh, delay: False"

    await asyncio.sleep(40)  # Charge for a little, check status
    response = await conn.command("status")
    assert response == "Status: Charging, transaction_id: 1, offer: 16 A, energy (rounded): 300 Wh, delay: False"

    # Finish charging by unplugging the cable
    response = await conn.command("unplug")
    assert response == "Succesfully stopped transaction. id_tag_info: None"

    # Wait a little to close things off
    await asyncio.sleep(5)

    # Disconnect from the simulator
    await conn.disconnect()


def main():
    # Run test case outside of pytest
    asyncio.run(test_case1())


if __name__ == "__main__":
    main()
