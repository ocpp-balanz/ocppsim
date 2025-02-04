# ocpp-simulator

OCPP v1.6 Charge Point (CP) Simulator used to test OCPP Central Systems (CS).

## Build

Clone the repository:

```text
git clone https://github.com/ocpp-balanz/ocpp-simulator.git
cd ocpp-simulator
```

Install dependencies:

```text
make install
```

## Start

Run `python ocppsim.py -h` to retrieve the following help message:

```text
usage: ocppsim.py [-h] [--version] [--config CONFIG] [--port PORT] [--id ID]

OCCPSIM - OCCP v1.6 Charge Point Simulator with Websocket Control Interface

options:
  -h, --help       show this help message and exit
  --version        show program's version number and exit
  --config CONFIG  Configuration file (INI format). Default ocpp.ini
  --port PORT      Command Interface Port. Default in config file.
  --id ID          Charger Id. Default in config file.
```

The configuration file is intended to configure a _type_ of charger, with the actual charger id supplied at startup. Review the file in detail to understand how to configure occpsim.

Start simulating a charger.

    python ocppsim.py --id TACW6243111G2672 --port 1234

Next, connect to the port using e.g. [websocat](https://github.com/vi/websocat) to interact with the simulator through the simplistic command interface.

    websocat ws://localhost:1234

Alternatively, use the included test driver `ocppsim_test.py` in interactive mode as follows:

   python ocppsim_test.py --url ws://localhost:321

`occpsim_test.py` may also be imported as a module and used in test scripts, possibly connecting to multiple simulators.

## Supported commands

The command interface is very simply. Enter a command and retrieve a one line response. The simulator will then execute the appropriate logic.

Commands           | Description
------------------ | ----------------------------------------------------------------
`wait [sec]`       | Wait some seconds. Default 5 sec
`status`           | Get the internal status (does not send/receive anything)
`full`             | Emulate that EV is full/does not want to charge more
`fullafter [wh]`   | Emulate that EV is full after having received at least [wh]
`delay`            | Set delayed charing. EV will not start charging
`nodelay`          | Charging no longer delayed
`plugin`           | Cable is plugged into EV
`unplug`           | Unplug cable/disconnect EV
`tag [id_tag]`     | RFID tag is scanned. Default tag in config file
`clock [offset]`   | Adjust timestamps send by offset seconds, e.g. clock -3600
`suspend`          | EV suspends charging (for some reason)
`resume`           | EV restarts charging (for some reason)
`max [Amps]`       | Set maximum charging usage in Amps. Reset without argument.
`reset`            | Reset the simulator (clear all state)
`exit`             | Exit the command session
`shutdown`         | Shutdown the simulator (process will stop)


## Automating Tests

The simulator can be used to automate tests. For example, you could write a script that sends commands and checks the responses. See a `pytest` based example in the `test_ocppsim_example.py` file.

## Supported CP to CS messages

BootNotification
StatusNotification
MeterValues
Authorize
StartTransaction
StopTransaction
TriggerMessage (some messages)

## Supported CS to Charger (CP) messages

SetChargingProfile
ClearChargingProfile
Reset
ChangeConfiguration (only actively uses AuthorizationKey during http authentication if set)
RemoteStartTransaction
RemoteStopTransaction

## Limitations

- Charging profile evaluation assuming timing "forever"
- Currently only chargers with a single connector/outlet is supported.
- Many messages not supported
