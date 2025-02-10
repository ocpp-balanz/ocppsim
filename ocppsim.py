"""
OCCPSIM - OCCP v1.6 Charge Point Simulator with Websocket Control Interface

For supported functions, commands, etc. see README.md
"""

import argparse
import asyncio
import base64
import configparser
import json
import logging
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import websockets
import websockets.asyncio
import websockets.asyncio.server
from ocpp.routing import on

# OCPP v1.6 classes and enums
from ocpp.v16 import ChargePoint as cp
from ocpp.v16 import call, call_result
from ocpp.v16.datatypes import ChargingProfile
from ocpp.v16.enums import (
    Action,
    AuthorizationStatus,
    ChargePointErrorCode,
    ChargePointStatus,
    ChargingProfilePurposeType,
    ChargingProfileStatus,
    ClearChargingProfileStatus,
    ConfigurationStatus,
    MessageTrigger,
    Reason,
    RegistrationStatus,
    RemoteStartStopStatus,
    ResetStatus,
    ResetType,
    TriggerMessageStatus,
)
from websockets.frames import CloseCode

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ocppsim")


# default keys - inspired by ABB TerraConfig
default_keys = {}
default_keys["AuthorizeRemoteTxRequests"] = {"value": "FALSE", "readonly": True}
default_keys["ClockAlignedDataInterval"] = {"value": "0", "readonly": False}
default_keys["ConnectionTimeOut"] = {"value": "120", "readonly": False}
default_keys["GetConfigurationMaxKeys"] = {"value": "20", "readonly": False}
default_keys["HeartbeatInterval"] = {"value": "120", "readonly": False}
default_keys["MeterValuesAlignedData"] = {"value": "Energy.Active.Import.Register", "readonly": False}
default_keys["MeterValuesAlignedDataMaxLength"] = {"value": "4", "readonly": False}
default_keys["MeterValuesAlignedData"] = {"value": "Energy.Active.Import.Register", "readonly": True}
default_keys["MeterValuesSampleInterval"] = {"value": "30", "readonly": False}
default_keys["LocalAuthorizeOffline"] = {"value": "TRUE", "readonly": False}
default_keys["LocalPreAuthorize"] = {"value": "FALSE", "readonly": False}
default_keys["NumberOfConnectors"] = {"value": "1", "readonly": True}
default_keys["SupportedFeatureProfiles"] = {"value": "Core,Reservation,SmartCharging", "readonly": True}
default_keys["AuthorizeRemoteTxRequests"] = {"value": "FALSE", "readonly": False}


def time_str(t: float) -> str:
    """Converts a timestamp to a string (local time)"""
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else "N/A"


def status_in_transaction(status) -> bool:
    """Charger status should be associated with a transaction"""
    return status in [
        ChargePointStatus.charging,
        ChargePointStatus.suspended_evse,
        ChargePointStatus.suspended_ev,
    ]


offset: float = 0


def now() -> str:
    """Now in ISO. With support for offset"""
    global offset
    return datetime.fromtimestamp(time.time() + offset, tz=timezone.utc).isoformat()


async def on_connect(websocket):
    """Command handling via websocket"""
    global charger
    global offset

    # Command Loop.
    while True:
        message = await websocket.recv()
        try:
            msg = message.split()
            command: str = msg[0]
            # --- quit
            if command == "quit":
                result = "bye"
            # --- max [A]
            elif command == "max":
                charger.max_usage = float(msg[1]) if len(msg) > 1 else None
                result = f"Ok. max set to {charger.max_usage}"
            # --- wait [sec]
            elif command == "wait":
                sec = int(msg[1]) if len(msg) > 1 else 5
                await asyncio.sleep(sec)
                result = "Done waiting"
            # --- clock [offset]
            elif command == "clock":
                offset = float(msg[1]) if len(msg) > 1 else 0
                result = f"Offset is now {offset}"
            # --- status
            elif command == "status":
                result = (
                    f"Status: {charger.status}, transaction_id: "
                    f"{charger.transaction_id}, offer: {charger.offer:.1f} A, energy: "
                    f"{round(charger.energy)} Wh, delay: "
                    f"{charger._delay}, max_usage: {charger.max_usage}"
                )
            elif command == "jsonstatus":
                fields = [
                    "status",
                    "offer",
                    "usage",
                    "max_usage",
                    "last_id_tag",
                    "transaction_id",
                    "energy",
                    "fullafter",
                    "_delay",
                ]
                ch = {}
                for f in fields:
                    ch[f] = getattr(charger, f)
                result = json.dumps(ch)
            # --- full/suspend
            elif command == "full" or command == "suspend":
                if charger.transaction_id is None:
                    result = f"Cannot full/suspend in a non current state: {charger.status}"
                else:
                    await charger.send_status_notification(status=ChargePointStatus.suspended_ev)
                    charger._delay = True
                    result = f"Suspended charging. Status: {charger.status}"
            elif command == "fullafter":
                charger.fullafter = int(msg[1]) if len(msg) > 1 else None
                result = f"ok, full after {charger.fullafter}"
            # --- resume
            elif command == "resume":
                if charger.status == ChargePointStatus.suspended_ev:
                    await charger.send_status_notification(status=ChargePointStatus.charging)
                charger._delay = False
                result = f"Resumed charging. Status: {charger.status}"
            # --- delay
            elif command == "delay":
                charger._delay = True
                result = f"Ok charger delay {charger._delay}"
            # -- nodelay
            elif command == "nodelay":
                charger._delay = False
                await charger.update_state_and_send()
                await asyncio.sleep(1)
                result = f"Ok. Status now {charger.status}"
            # --- plugin
            elif command == "plugin":
                if charger.status == ChargePointStatus.available:
                    await charger.send_status_notification(status=ChargePointStatus.preparing)
                    result = f"Cable plugged. Status {charger.status}"
                else:
                    result = "Already plugged"
            # --- unplug
            elif command == "unplug":
                if not status_in_transaction(charger.status):
                    if charger.status == ChargePointStatus.available:
                        result = "Ok, nothing to do"
                    else:
                        await charger.send_status_notification(status=ChargePointStatus.available)
                        result = "Ok, status change to available"
                else:
                    # Then stop transaction
                    result = await charger.stop_transaction()
                # Always reset energy and delay
                charger.energy = 0.0
                charger._delay = False
            # --- tag [id_tag]
            elif command == "tag":
                id_tag: str = msg[1] if len(msg) > 1 else config.get("command", "tag")
                if not status_in_transaction(charger.status):
                    result = await charger.authorize(id_tag=id_tag)
                else:
                    await charger.stop_transaction(reason=Reason.local)
                    result = "Ok, stopping transaction. Reason local"
            # --- reset
            elif command == "reset":
                result = await charger.reset()
            # --- reset
            elif command == "shutdown":
                charger.shutdown = True
                await charger._connection.close()
                result = "Shutting down"
            else:
                result = f"Error: Unknown command {command}"
        except Exception as e:
            result = f"Error: {e}"
        await websocket.send(result)
        if command == "quit":
            await websocket.close(CloseCode.GOING_AWAY)
            quit()


def conf_filename(charger_id: str) -> str:
    """Construct configuration filename"""
    return config.get("charger", "conf_dir") + "/" + charger_id + ".conf"


def read_config_file(charger_id: str) -> dict[str]:
    """Read config file and return as dict. Create if not there."""
    conf_file = conf_filename(charger_id)
    config = {}
    try:
        for entry in open(conf_file, "r").read().split("\n"):
            if "=" not in entry:
                continue
            key, value = entry.split("=")
            config[key] = value
            logger.debug(f"Read CP config setting {key}={value}")
    except Exception as e:
        logger.debug(e)
        logger.warning(f"No configuration file for {charger_id} found. Creating {conf_file}")
        file = open(conf_file, "w")
        file.close()
    return config


@dataclass
class SimChargingProfile:
    """Simplified representation of charging profile.

    Will handle the different purposes and stack_levels
    but not schedule. Also taking things a bit light on connector_id handling as
    simulator currently supports only 1. Schedule is assumed to be "always".
    Will be indexed by tuple of (connector_id, charing_profile_id)
    """

    charging_profile_purpose: ChargingProfilePurposeType
    stack_level: int
    limit: float


class ChargePoint(cp):
    """The ChargePoint responsible for all things OCPP"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Change unique_id generation. Use integers
        self._unique_id_generator = lambda: str(random.randint(1000000, 9999999))

        # Init local variables.
        self.cp_configuration = read_config_file(self.id)  # key indexed dictionary of config values.
        self.status: ChargePointStatus = None  # Start with None to get notif status Available at startup.
        self.offer: float = None
        self.usage: float = 0
        self.max_usage: float = None
        self.last_id_tag: str = None
        self.transaction_id: int = None
        self.energy: float = 0  # Wh used in transaction.
        self.last_energy_update: float = time.time()  # To be reset by start transaction
        self.shutdown: bool = False
        self.tasks_started: bool = False
        self.fullafter: int = None

        # Charging profiles are entries indexed by connector_id, charging_profile_id
        self.charging_profiles = {}
        self.charging_profiles[(0, 0)] = SimChargingProfile(
            charging_profile_purpose=ChargingProfilePurposeType.charge_point_max_profile,
            stack_level=0,
            limit=config.getint("charger", "max_offer"),
        )

        if config.getboolean("charger", "default_profiles"):
            self.charging_profiles[(0, 1)] = SimChargingProfile(
                charging_profile_purpose=ChargingProfilePurposeType.tx_default_profile,
                stack_level=0,
                limit=6,
            )
            self.charging_profiles[(0, 2)] = SimChargingProfile(
                charging_profile_purpose=ChargingProfilePurposeType.tx_default_profile,
                stack_level=1,
                limit=0,
            )
            logger.info("TxDefaultProfiles set")

        self.recalc_offer()

        # Fields used by command/simulator
        self._delay = False  # delayed charging

    def update_energy(self) -> None:
        """Update energy usage"""
        if self.status == ChargePointStatus.charging:
            self.usage = self.offer if self.max_usage is None else min(self.offer, self.max_usage)
            energy_delta = self.offer * 230 * 3 * (time.time() - self.last_energy_update) / 3600.0
            self.energy += energy_delta
            logger.debug(f"Adding {energy_delta} Wh to energy for a new total of {self.energy}")
            self.last_energy_update = time.time()

    def recalc_offer(self) -> float:
        """Recalculate offer based on charging profiles.

        Sets and returns offer"""
        self.update_energy()

        # Is there an active transaction? If so, check any TxProfile entries
        if self.transaction_id:
            purposes_to_check = [
                ChargingProfilePurposeType.tx_profile,
                ChargingProfilePurposeType.tx_default_profile,
                ChargingProfilePurposeType.charge_point_max_profile,
            ]
        else:
            purposes_to_check = [
                ChargingProfilePurposeType.tx_default_profile,
                ChargingProfilePurposeType.charge_point_max_profile,
            ]

        # Check purposes in order ot priority.
        for purpose in purposes_to_check:
            profiles = [p for p in self.charging_profiles.values() if p.charging_profile_purpose == purpose]
            profiles.sort(key=lambda p: p.stack_level, reverse=True)
            if profiles:
                self.offer = profiles[0].limit
                logger.debug(f"Offer set to {self.offer} by profile {profiles[0]}")
                break
        return self.offer

    def set_config_value(self, key: str, value: str) -> None:
        """Set configuration value, rewrite full file."""
        self.cp_configuration[key] = value
        conf_file = conf_filename(charger_id=self.id)
        file = open(conf_file, "w")
        for key, value in self.cp_configuration.items():
            file.write(f"{key}={value}\n")
        file.close()

    async def heartbeat_task(self, interval: int) -> None:
        """Task to send heartbeat every interval seconds"""
        logger.info(f"Starting heartbeat loop. Will send every {interval} seconds")
        while True:
            await asyncio.sleep(interval)
            request = call.Heartbeat()
            response: call_result.Heartbeat = await self.call(request)
            logger.debug(f"Heart beat success. Current time: {response.current_time}")

    async def send_meter_values(self, context: str = "Sample.Periodic") -> None:
        """Send meter values"""
        self.update_energy()

        def s2d(value: float) -> str:
            return str(round(value, 2))

        def s0d(value: float) -> str:
            return str(round(value, 0))

        # Uhh - let's go for it.
        sampled_values = []
        voltage = 228.9
        factor = 0.992 if self.status == ChargePointStatus.charging else 0
        usage = self.offer * factor
        if self.max_usage:
            usage = min(usage, self.max_usage * factor)

        # Voltage
        for phase in ["L1-N", "L2-N", "L3-N"]:
            sampled_values.append(
                {
                    "value": s2d(voltage),
                    "context": context,
                    "format": "Raw",
                    "measurand": "Voltage",
                    "phase": phase,
                    "unit": "V",
                }
            )

        # Import (offer minus a bit)
        for phase in ["L1", "L2", "L3"]:
            sampled_values.append(
                {
                    "value": s2d(usage),
                    "context": context,
                    "format": "Raw",
                    "measurand": "Current.Import",
                    "phase": phase,
                    "unit": "A",
                }
            )
            sampled_values.append(
                {
                    "value": s0d(usage * voltage),
                    "context": context,
                    "format": "Raw",
                    "measurand": "Power.Active.Import",
                    "phase": phase,
                    "unit": "W",
                }
            )
        sampled_values.append(
            {
                "value": s2d(usage * 3),
                "context": context,
                "format": "Raw",
                "measurand": "Current.Import",
                "unit": "A",
            }
        )
        sampled_values.append(
            {
                "value": s0d(usage * 3 * voltage),
                "context": context,
                "format": "Raw",
                "measurand": "Power.Active.Import",
                "unit": "W",
            }
        )

        # Wh so far
        sampled_values.append(
            {
                "value": s0d(self.energy),
                "context": context,
                "format": "Raw",
                "measurand": "Energy.Active.Import.Register",
                "unit": "Wh",
            }
        )

        # Offered
        sampled_values.append(
            {
                "value": s0d(self.offer),
                "context": context,
                "format": "Raw",
                "measurand": "Current.Offered",
                "unit": "A",
            }
        )

        meter_values = []
        meter_values.append({"timestamp": now(), "sampled_value": sampled_values})

        request = call.MeterValues(connector_id=1, meter_value=meter_values, transaction_id=self.transaction_id)
        response: call_result.MeterValues = await self.call(request)
        logger.debug(f"Metervalues reported {response}")

    async def metervalues_task(self, interval: int) -> None:
        """Metervalues task to send metervalues every interval seconds

        (only when in transition)"""
        logger.info(f"metervalues loop. Sending every {interval} seconds when in transition")
        while True:
            await asyncio.sleep(interval)
            if self.transaction_id is None:
                continue  # skip
            await self.send_meter_values()

            # Also check fullafter
            if (
                self.fullafter
                and self.energy
                and self.energy >= self.fullafter
                and self.status == ChargePointStatus.charging
            ):
                # Full..
                await self.send_status_notification(status=ChargePointStatus.suspended_ev)
                self._delay = True

    async def send_boot_notification(self) -> None:
        """Send boot notification"""
        boot_info = {}
        for element in config["charger"]:
            info_prefix = "info_"
            if element.startswith(info_prefix):
                element_id = element[len(info_prefix) :]
                boot_info[element_id] = config.get("charger", element)

        request = call.BootNotification(**boot_info)
        response: call_result.BootNotification = await self.call(request)
        if response.status == RegistrationStatus.accepted:
            logger.info("Connected to central system.")

            # Start heartbeat task - if not already running
            if not charger.tasks_started:
                asyncio.create_task(self.heartbeat_task(response.interval))
                # Start metervalues task - will run always but not always send stuff
                asyncio.create_task(self.metervalues_task(config.getint("charger", "metervalues_interval")))
                charger.tasks_started = True
        else:
            logger.error(f"Error connecting to server: {response}")

    async def send_status_notification(
        self, status: ChargePointStatus, connector_id=1, force_update: bool = False
    ) -> None:
        """Send status notification"""
        if self.status == status and not force_update:
            return  # Nothing to change
        self.status = status
        self.update_energy()
        request = call.StatusNotification(
            connector_id=connector_id,
            error_code=ChargePointErrorCode.no_error,
            status=self.status,
            vendor_error_code="0x0000",
        )
        await self.call(request)
        if self.status == ChargePointStatus.charging:
            self.last_energy_update = time.time()

    async def start_charging(self) -> None:
        """Meta function to start charging"""
        # If coming form SuspendedEVSE, go via SuspendedEV
        if self.status == ChargePointStatus.suspended_evse:
            await self.send_status_notification(status=ChargePointStatus.suspended_ev)
        await self.send_status_notification(status=ChargePointStatus.charging)
        await asyncio.sleep(1)

        charger.energy = 0.0  # Just to be sure. Should not really be necessary
        charger.last_energy_update = time.time()

        if self.transaction_id is None:
            await self.start_transaction()

        # If in _delay scenario, go pretty much immediately to SuspendedEV
        if self._delay:
            await asyncio.sleep(2)
            await self.send_status_notification(status=ChargePointStatus.suspended_ev)

    async def update_state_and_send(self) -> None:
        """Update the state (depending on charging allocation, etc.)

        Send a status notification if updated"""
        if self.status == ChargePointStatus.preparing:
            if self.offer >= config.getint("charger", "min_to_start"):
                await self.start_charging()
            else:
                # Start transaction, even if charging cannot start
                if self.transaction_id is None:
                    await self.start_transaction()

                # Goto SuspendedEVSE via SuspendedEV
                await self.send_status_notification(status=ChargePointStatus.suspended_ev)

                await self.send_status_notification(status=ChargePointStatus.suspended_evse)
        elif self.status == ChargePointStatus.suspended_evse:
            if self.offer >= config.getint("charger", "min_to_start") and not charger._delay:
                await self.start_charging()
            elif self.offer >= config.getint("charger", "min_to_start") and charger._delay:
                # Goto suspended_ev:
                await self.send_status_notification(status=ChargePointStatus.suspended_ev)
            else:
                # Not ready
                pass
        elif self.status == ChargePointStatus.charging and self.offer < config.getint("charger", "min_to_start"):
            # Goto SuspendedEVSE via SuspendedEV
            await self.send_status_notification(status=ChargePointStatus.suspended_ev)
            await self.send_status_notification(status=ChargePointStatus.suspended_evse)
        elif self.status == ChargePointStatus.suspended_ev and self.offer < config.getint("charger", "min_to_start"):
            # Goto SuspendedEVSE
            await self.send_status_notification(status=ChargePointStatus.suspended_evse)

    async def authorize(self, id_tag: str) -> str:
        """Authorize"""
        request = call.Authorize(id_tag=id_tag)
        response: call_result.Authorize = await self.call(request)
        status = response.id_tag_info["status"]  # For some reason does not work with . for status. occp bug?
        parent_id_tag = (
            response.id_tag_info["parent_id_tag"] if "parent_id_tag" in response.id_tag_info else ""
        )  # as above
        if status == AuthorizationStatus.accepted:
            self.last_id_tag = id_tag
            if charger.status == ChargePointStatus.available:
                await charger.send_status_notification(status=ChargePointStatus.preparing)
            await self.update_state_and_send()
            result = f"Tag Accepted. Parent: {parent_id_tag}, new status: {self.status}"
        else:
            result = f"Tag not accepted. Error: {status}"
        return result

    async def start_transaction(self) -> str:
        """Start Transaction"""
        request = call.StartTransaction(connector_id=1, id_tag=self.last_id_tag, meter_start=0, timestamp=now())
        response: call_result.StartTransaction = await self.call(request)
        self.transaction_id = response.transaction_id
        self.energy = 0
        self.recalc_offer()
        self.update_energy()

        if charger.status == ChargePointStatus.available:
            await charger.send_status_notification(status=ChargePointStatus.preparing)
        await self.update_state_and_send()

        await asyncio.sleep(1)
        await self.send_meter_values(context="Transaction.Begin")
        return f"Transaction id {self.transaction_id} started."

    async def stop_transaction(self, reason: Reason = Reason.ev_disconnected) -> str:
        """Stop transaction"""

        # First go to suspended EV
        if charger.status != ChargePointStatus.suspended_ev:
            await charger.send_status_notification(status=ChargePointStatus.suspended_ev)
        # Then finishing
        await charger.send_status_notification(status=ChargePointStatus.finishing)
        # Then available
        await charger.send_status_notification(status=ChargePointStatus.available)

        self.update_energy()

        # Delete any TxProfiles and update offer
        all_profiles = list(self.charging_profiles)
        for profile in all_profiles:
            profile_connector_id, profile_id = profile
            if self.charging_profiles[profile].charging_profile_purpose == ChargingProfilePurposeType.tx_profile:
                logger.debug(f"Deleting TxProfile profile: {profile}: {self.charging_profiles[profile]}")
                del self.charging_profiles[profile]
        self.recalc_offer()

        await self.send_meter_values(context="Transaction.End")
        await asyncio.sleep(1)
        request = call.StopTransaction(
            meter_stop=round(self.energy),
            timestamp=now(),
            transaction_id=self.transaction_id,
            id_tag=self.last_id_tag,
            reason=reason,
        )
        response: call_result.StopTransaction = await self.call(request)

        self.transaction_id = None
        self.last_id_tag = None
        self.energy = 0
        self.update_energy()

        return f"Succesfully stopped transaction. id_tag_info: {response.id_tag_info}"

    async def reset(self) -> str:
        """Reset charger"""
        await asyncio.sleep(5)

        if self.transaction_id is not None:
            logger.debug(f"Stopping transaction {self.transaction_id}")
            await self.stop_transaction()
        logger.debug("Resetting charger by closing connection")
        await asyncio.sleep(5)

        await charger._connection.close()
        logger.info("Charger reset successfully - automatic reconnect")
        return "Charger reset successfully - automatic reconnect"

    async def send_startup_notifications(self) -> None:
        """Composite startup notifications"""
        await asyncio.sleep(2)
        await self.send_boot_notification()
        await asyncio.sleep(1)
        await self.send_status_notification(connector_id=0, status=ChargePointStatus.available, force_update=True)
        await asyncio.sleep(0.2)
        await self.send_status_notification(status=ChargePointStatus.available, force_update=True)

    async def trigger_message(self, requested_message: MessageTrigger, connector_id: int) -> None:
        """Send message back. Not all support.

        Options are:
        boot_notification = "BootNotification"
        firmware_status_notification = "FirmwareStatusNotification"
        heartbeat = "Heartbeat"
        meter_values = "MeterValues"
        status_notification = "StatusNotification"
        diagnostics_status_notification = "DiagnosticsStatusNotification"
        """
        # A small delay to ensure that the triggerMessage is answered first.
        await asyncio.sleep(0.5)

        if requested_message == MessageTrigger.boot_notification:
            await self.send_boot_notification()
        elif requested_message == MessageTrigger.meter_values:
            await self.send_meter_values()
        elif requested_message == MessageTrigger.status_notification:
            await self.send_status_notification(connector_id=connector_id, status=self.status, force_update=True)
        else:
            logger.warning(f"Ignoring request to send message {requested_message}")

    # -------------------------------
    # Call handling functions (from OCPP Server)

    @on(Action.reset)
    def on_reset(self, type: ResetType):
        logger.info(f"Asking to reset, type {type}")
        asyncio.create_task(self.reset())
        return call_result.Reset(status=ResetStatus.accepted)

    @on(Action.trigger_message)
    def on_trigger_message(self, requested_message: MessageTrigger, connector_id: int = None):
        logger.info(f"Received trigger_message {requested_message} for connector {connector_id}")
        asyncio.create_task(self.trigger_message(requested_message=requested_message, connector_id=connector_id))
        return call_result.TriggerMessage(status=TriggerMessageStatus.accepted)

    @on(Action.change_configuration)
    def on_change_configuration(self, key: str, value: str):
        logger.info(f"Received change_configuration {key} = {value}")

        if key not in default_keys.keys() and key != "AuthorizationKey":
            return call_result.ChangeConfiguration(status=ConfigurationStatus.not_supported)
        elif key != "AuthorizationKey" and default_keys[key]["readonly"]:
            return call_result.ChangeConfiguration(status=ConfigurationStatus.rejected)
        else:
            self.set_config_value(key=key, value=value)
            return call_result.ChangeConfiguration(status=ConfigurationStatus.accepted)

    @on(Action.get_configuration)
    def on_get_configuration(self, key: list[str]):
        config = read_config_file(charger_id=self.id)

        # Prepare results
        configuration_key = []
        unknown_key = None
        if not key:  # None or empty. This means return all configuration keys
            key = list(config.keys()) + list(default_keys.keys())

        for k in key:
            # Note, check match against config first in case key is actually set.
            if k in config:
                if k == "AuthorizationKey":
                    configuration_key.append({"key": k, "readonly": False})
                else:
                    configuration_key.append({"key": k, "value": config[k], "readonly": False})
            elif k in default_keys:
                configuration_key.append(
                    {"key": k, "value": default_keys[k]["value"], "readonly": default_keys[k]["readonly"]}
                )
            else:
                if not unknown_key:
                    unknown_key = []
                unknown_key.append(k)
        kwargs = {}
        kwargs["configuration_key"] = configuration_key
        if unknown_key:
            kwargs["unknown_key"] = unknown_key
        return call_result.GetConfiguration(**kwargs)

    @on(Action.clear_charging_profile)
    def on_clear_charging_profile(
        self,
        id: int = None,
        connector_id: int = None,
        charging_profile_purpose: ChargingProfilePurposeType = None,
        stack_level: int = None,
    ):
        logger.debug(
            f"clear_charging_profile: id={id}, connector_id={connector_id}, "
            f"charging_profile_purpose={charging_profile_purpose}, "
            f"stack_level={stack_level}"
        )
        all_profiles = list(self.charging_profiles)
        for profile in all_profiles:
            profile_connector_id, profile_id = profile
            if (
                (id is None or id == profile_id)
                and (
                    charging_profile_purpose is None
                    or charging_profile_purpose == self.charging_profiles[profile].charging_profile_purpose
                )
                and (stack_level is None or stack_level == self.charging_profiles[profile].stack_level)
            ):
                logger.debug(f"Deleting profile: {profile}: {self.charging_profiles[profile]}")
                del self.charging_profiles[profile]
        self.recalc_offer()
        asyncio.create_task(self.update_state_and_send())
        return call_result.ClearChargingProfile(status=ClearChargingProfileStatus.accepted)

    @on(Action.set_charging_profile)
    def on_set_charging_profile(self, connector_id: int, cs_charging_profiles: ChargingProfile):
        logger.debug(
            f"set_charging_profile: connector_id={connector_id}, " f"cs_charging_profiles={cs_charging_profiles}"
        )

        # Quick check
        if (
            cs_charging_profiles["charging_profile_purpose"] == ChargingProfilePurposeType.tx_profile
            and charger.transaction_id is None
        ):
            logger.warning("SetChargingProfile TxProfile outside of transaction")
            return call_result.SetChargingProfile(status=ChargingProfileStatus.rejected)

        # Let's dig out the limit. This may be a bit error-prone for now. TODO: Improve
        # Also possibe ocpp library bug as . notation does not seem to work for ChargingProfile
        limit = float(cs_charging_profiles["charging_schedule"]["charging_schedule_period"][0]["limit"])
        self.charging_profiles[(connector_id, cs_charging_profiles["charging_profile_id"])] = SimChargingProfile(
            charging_profile_purpose=cs_charging_profiles["charging_profile_purpose"],
            stack_level=cs_charging_profiles["stack_level"],
            limit=limit,
        )
        self.recalc_offer()
        asyncio.create_task(self.update_state_and_send())
        return call_result.SetChargingProfile(status=ChargingProfileStatus.accepted)

    @on(Action.remote_start_transaction)
    def on_remote_start_transaction(self, id_tag: str, connector_id: int):
        if charger.transaction_id is None:
            logger.info("Starting transaction")
            charger.last_id_tag = id_tag
            asyncio.create_task(self.start_transaction())
        return call_result.RemoteStartTransaction(status=RemoteStartStopStatus.accepted)

    @on(Action.remote_stop_transaction)
    def on_remote_stop_transaction(self, transaction_id: int):
        if charger.transaction_id is not None:
            logger.info("Stopping transaction")
            asyncio.create_task(self.stop_transaction())
        return call_result.RemoteStopTransaction(status=RemoteStartStopStatus.accepted)


# global
charger: ChargePoint = None
config = configparser.ConfigParser()


async def main():
    global charger
    cmd_server = None

    while True:
        parser = argparse.ArgumentParser(
            description="OCCPSIM - OCCP v1.6 Charge Point Simulator with Websocket Control Interface"
        )
        parser.add_argument(
            "--config",
            type=str,
            default="ocppsim.ini",
            help="Configuration file (INI format). Default ocpp.ini",
        )
        parser.add_argument("--port", type=str, help="Command Interface Port. Default in config file.")
        parser.add_argument("--id", type=str, help="Charger Id. Default in config file.")
        args = parser.parse_args()

        # Read config. config object is then available (via config import) to all.
        config.read(args.config)

        # Adjust log levels
        for logger_name in config["logging"]:
            logger.warning(f'Setting log level for {logger_name} to {config.get("logging", logger_name)}')
            logging.getLogger(logger_name).setLevel(level=config.get("logging", logger_name))

        # Get host config
        host = config.get("host", "addr")
        port = args.port if args.port else config.get("host", "port")
        charger_id = args.id if args.id else config.get("charger", "charger_id")

        # Connect to OCPP Server and start
        headers = {}
        if config.getboolean("server", "http_auth"):
            cp_config = read_config_file(charger_id=charger_id)
            auth_key = cp_config.get("AuthorizationKey", "")
            auth_string = charger_id + ":" + auth_key
            headers["Authorization"] = "Basic " + base64.b64encode(bytes(auth_string, "utf-8")).decode()

        # BALANZ_SERVER_URL environment variable - if set - takes precedence
        env_url = os.getenv("BALANZ_SERVER_URL", None)
        if env_url:
            url = env_url + charger_id
        else:
            url = config.get("server", "url") + charger_id
        logging.info(f"Connecting to {url}")
        try:
            ws = await websockets.connect(url, subprotocols=["ocpp1.6"], additional_headers=headers)
        except websockets.exceptions.ConnectionClosed as e:
            logger.error(f"Failed to connect to server at {url}. Error: {e}")
            return
        except Exception as e:
            logger.error(f"Failed to connect to server at {url}. Error: {e}")
            return
        charger = ChargePoint(charger_id, ws)
        cp_task = asyncio.create_task(charger.start())

        # Start command server
        if cmd_server is None:
            logger.info(f"Starting command server for {charger_id} on port {port}")
            cmd_server = await websockets.serve(on_connect, host, port)

        # Send startup notification
        await asyncio.sleep(1)
        await charger.send_startup_notifications()

        # Wait for tasks to complete
        done, pending = await asyncio.wait([cp_task], return_when=asyncio.FIRST_COMPLETED)
        logger.debug(f"Task(s) completed for {charger_id}: {done}, {pending}")

        for task in done:
            e = task.exception()
            if e:
                logger.warning(f"(Not serious) Task {task} raised exception {e}")

        # Cancel any remaining tasks
        for task in pending:
            task.cancel()

        await asyncio.sleep(5)
        if charger.shutdown:
            logger.info(f"Shutdown requested for {charger_id}")
            break
        else:
            logger.info(f"Connection for {charger_id} closed. Restart in 5 sec.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.warning("Shutting down server")
        exit(0)
