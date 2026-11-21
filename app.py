"""Azzurro inverter telemetry app for Home Assistant."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import aiohttp
from aiomqtt import Client as MqttClient, MqttError, Will

_LOGGER = logging.getLogger("azzurro_inverter")

APP_VERSION = "0.3.0"
LOG_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO}
DEFAULT_DISCOVERY_PREFIX = "homeassistant"
# Retained discovery configs survive a broker/HA restart, but they are lost if
# the broker forgets retained messages. Refresh them at a low cadence, plus on
# every reconnect, instead of on every telemetry poll.
DISCOVERY_REFRESH_SECONDS = 3600

# Firebase Web API key embedded in the Azzurro/Solarman clients. It is the
# `?key=` parameter of the original login URL in azzurro.py / azzurro_login.py.
# This is a public project identifier, not a user secret: it cannot read or
# write account data on its own, and the per-user password is still required.
DEFAULT_FIREBASE_API_KEY = "AIzaSyCTFwAoDwQaUWFhMEHeqUDw-rvu65vuX_0"
IDENTITY_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
REFRESH_URL = "https://securetoken.googleapis.com/v1/token"
GRAPHQL_URL = "https://yyhpek4qzrg25gh56dg4w3pnou.appsync-api.eu-west-1.amazonaws.com/graphql"

QUERY = """query ($serial: String!) {
  aggregate: getLastAggregate(serial: $serial, series: DAY, type: "Solarman") {
    ALERTS BP1 BP1_MAX BP2 P_SELF P_LOAD P_EXT P_EXP P_B_AGGR P_B1 P_B2 P_DC_TOT
    EG1 EG2 EG3 EG4 EG5 EG6 ET1 ET2 ET3 ET4 ET5 ET6 ARM DSPM DSPS TS
  }
  realtime: getInverterData(serial: $serial) {
    BP1 BP2 P_B_AGGR P_DC_TOT P_LOAD MP1 MP2 MC1 MC2 MT1 MT2 TI TB TS
  }
}"""


@dataclass
class Token:
    id_token: str
    refresh_token: str
    expires_at: float


class AzzurroClient:
    def __init__(self, username: str, password: str, inverter: str, firebase_api_key: str) -> None:
        self.username = username
        self.password = password
        self.inverter = inverter
        self.firebase_api_key = firebase_api_key
        self.token: Token | None = None
        self.firmware: str | None = None

    async def login(self, session: aiohttp.ClientSession) -> None:
        payload = {
            "returnSecureToken": True,
            "email": self.username,
            "password": self.password,
        }
        async with session.post(IDENTITY_URL, params={"key": self.firebase_api_key}, json=payload) as response:
            body = await response.json(content_type=None)
            if response.status != 200:
                raise RuntimeError(f"Azzurro login failed with HTTP {response.status}: {body.get('error', body)}")
            self._set_token(body)
            _LOGGER.info("Azzurro authentication succeeded")

    async def refresh(self, session: aiohttp.ClientSession) -> None:
        if not self.token:
            await self.login(session)
            return
        payload = {"grant_type": "refresh_token", "refresh_token": self.token.refresh_token}
        async with session.post(REFRESH_URL, params={"key": self.firebase_api_key}, data=payload) as response:
            body = await response.json(content_type=None)
            if response.status != 200:
                _LOGGER.warning("Azzurro token refresh failed; performing a new login")
                await self.login(session)
                return
            self.token = Token(
                id_token=body["id_token"],
                refresh_token=body.get("refresh_token", self.token.refresh_token),
                expires_at=time.time() + int(body.get("expires_in", 3600)),
            )
            _LOGGER.info("Azzurro authentication token refreshed")

    def _set_token(self, body: dict[str, Any]) -> None:
        self.token = Token(
            id_token=body["idToken"],
            refresh_token=body["refreshToken"],
            expires_at=time.time() + int(body.get("expiresIn", 3600)),
        )

    async def fetch(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        if not self.token or self.token.expires_at - time.time() < 120:
            await self.refresh(session)
        token = self.token
        if token is None:
            raise RuntimeError("Azzurro login produced no token")

        payload = {"operationName": None, "variables": {"serial": self.inverter}, "query": QUERY}
        headers = {"Authorization": token.id_token}
        async with session.post(GRAPHQL_URL, headers=headers, json=payload) as response:
            if response.status == 401:
                await self.refresh(session)
                refreshed = self.token
                if refreshed is None:
                    raise RuntimeError("Azzurro token refresh produced no token")
                headers["Authorization"] = refreshed.id_token
                async with session.post(GRAPHQL_URL, headers=headers, json=payload) as retry:
                    return await self._parse_response(retry)
            return await self._parse_response(response)

    async def _parse_response(self, response: aiohttp.ClientResponse) -> dict[str, Any]:
        body = await response.json(content_type=None)
        if response.status != 200 or body.get("errors"):
            detail = body.get("errors") or body
            raise RuntimeError(f"Azzurro data request failed with HTTP {response.status}: {detail}")
        data = body.get("data") or {}
        values: dict[str, Any] = {}
        # getLastAggregate returns a single-element list; getInverterData a plain object.
        aggregate = data.get("aggregate")
        if isinstance(aggregate, list):
            for entry in aggregate:
                if isinstance(entry, dict):
                    values.update(entry)
                    break
        realtime = data.get("realtime")
        if isinstance(realtime, dict):
            # Prefer realtime values when present, but do not overwrite valid
            # aggregate values with nulls.
            for key, value in realtime.items():
                if value is not None:
                    values[key] = value
        if not values:
            raise RuntimeError("Azzurro response did not contain inverter data")
        firmware = values.get("ARM")
        if firmware:
            self.firmware = str(firmware)
        return values


def log_broker(broker: dict[str, Any], source: str) -> None:
    _LOGGER.info(
        "MQTT broker (%s): %s:%s tls=%s username=%s",
        source,
        broker.get("host"),
        broker.get("port"),
        bool(broker.get("ssl", False)),
        "set" if broker.get("username") else "none",
    )


async def mqtt_config(session: aiohttp.ClientSession, options: dict[str, Any]) -> dict[str, Any]:
    """Get MQTT details from Supervisor, with explicit manual configuration fallback."""
    manual_host = options.get("mqtt_host")
    token = os.environ.get("SUPERVISOR_TOKEN")
    if token:
        try:
            async with session.get(
                "http://supervisor/services/mqtt",
                headers={"Authorization": f"Bearer {token}"},
            ) as response:
                if response.status == 200:
                    service = await response.json()
                    data = service.get("data", service)
                    if data.get("host") and data.get("port"):
                        log_broker(data, "Supervisor service discovery")
                        return data
                _LOGGER.warning("Supervisor MQTT service was unavailable; using manual MQTT options")
        except aiohttp.ClientError as error:
            _LOGGER.warning("Could not query Supervisor MQTT service: %s", error)

    if not manual_host:
        raise RuntimeError("No MQTT service found. Install/configure MQTT or set mqtt_host in the app options.")
    broker = {
        "host": manual_host,
        "port": int(options.get("mqtt_port", 1883)),
        "username": options.get("mqtt_username") or None,
        "password": options.get("mqtt_password") or None,
        "ssl": bool(options.get("mqtt_ssl", False)),
    }
    log_broker(broker, "manual app options")
    return broker


# ---------------------------------------------------------------------------
# Field reference (reverse-engineered from the Azzurro/Solarman cloud API)
#
#   MP1/MP2  PV string power                                  W
#   MT1/MT2  PV string voltage                                V
#   MC1/MC2  PV string current                                A
#   TI       Inverter temperature                             °C
#   TB       Battery temperature                              °C
#   BP1      Battery state of charge                          %
#   BP1_MAX  Highest state of charge reached today             %
#   P_SELF   PV production power                              W
#   P_LOAD   Home load power                                  W
#   P_EXP    Grid power; negative = import, positive = export  W
#   P_EXT    Unused by the API (always 0)                     W
#   P_B1/2   Battery power per battery input                  W
#   P_B_AGGR Battery power; positive = charging                W
#   P_DC_TOT Total DC power                                   W
#   EG1..6   Energy generated today, per channel              kWh
#   ET1..6   Lifetime energy generated, per channel           kWh
#   ALERTS   Comma-separated active alert codes               -
#   ARM/DSPM/DSPS  Firmware revisions                         -
#   TS       Timestamp of the sample                          epoch
# ---------------------------------------------------------------------------

NUMERIC = "number"
TEXT = "text"
TIMESTAMP = "timestamp"


def _absolute(value: float) -> float:
    return abs(value)


def _positive(value: float) -> float:
    """Keep only the positive part of a signed value."""
    return value if value > 0 else 0.0


def _grid_import(value: float) -> float:
    """Azzurro reports grid power as negative while importing."""
    return -value if value < 0 else 0.0


def sensor(
    name: str,
    *,
    field: str | None = None,
    sum_of: tuple[str, ...] | None = None,
    unit: str | None = None,
    device_class: str | None = None,
    state_class: str | None = None,
    transform: Callable[[float], float] | None = None,
    value_type: str = NUMERIC,
    entity_category: str | None = None,
    icon: str | None = None,
    precision: int | None = None,
    optional: bool = False,
) -> dict[str, Any]:
    return {
        "name": name,
        "field": field,
        "sum_of": sum_of,
        "unit": unit,
        "device_class": device_class,
        "state_class": state_class,
        "transform": transform,
        "value_type": value_type,
        "entity_category": entity_category,
        "icon": icon,
        "precision": precision,
        "optional": optional,
    }


ENERGY_CHANNELS = ("EG1", "EG2", "EG3", "EG4", "EG5", "EG6")
LIFETIME_CHANNELS = ("ET1", "ET2", "ET3", "ET4", "ET5", "ET6")

SENSORS: dict[str, dict[str, Any]] = {
    # --- Power flow -------------------------------------------------------
    "solar_production": sensor(
        "Solar PV production",
        field="P_SELF",
        unit="W",
        device_class="power",
        state_class="measurement",
        icon="mdi:solar-power",
        precision=0,
    ),
    "home_consumption": sensor(
        "Home consumption",
        field="P_LOAD",
        unit="W",
        device_class="power",
        state_class="measurement",
        icon="mdi:home-lightning-bolt",
        precision=0,
    ),
    # Legacy sensor kept for backward compatibility with existing dashboards.
    "grid_absorption": sensor(
        "Grid absorption",
        field="P_EXP",
        unit="W",
        device_class="power",
        state_class="measurement",
        transform=_absolute,
        icon="mdi:transmission-tower",
        precision=0,
    ),
    "grid_import": sensor(
        "Grid import",
        field="P_EXP",
        unit="W",
        device_class="power",
        state_class="measurement",
        transform=_grid_import,
        icon="mdi:transmission-tower-import",
        precision=0,
    ),
    "grid_export": sensor(
        "Grid export",
        field="P_EXP",
        unit="W",
        device_class="power",
        state_class="measurement",
        transform=_positive,
        icon="mdi:transmission-tower-export",
        precision=0,
    ),
    "dc_power": sensor(
        "DC power",
        field="P_DC_TOT",
        unit="W",
        device_class="power",
        state_class="measurement",
        icon="mdi:flash",
        precision=0,
        optional=True,
    ),
    # --- Battery ----------------------------------------------------------
    "battery_charge": sensor(
        "Battery charge",
        field="BP1",
        unit="%",
        device_class="battery",
        state_class="measurement",
        icon="mdi:battery",
        precision=0,
    ),
    "battery_charge_max_today": sensor(
        "Battery charge today maximum",
        field="BP1_MAX",
        unit="%",
        device_class="battery",
        state_class="measurement",
        icon="mdi:battery-plus-variant",
        precision=0,
        optional=True,
    ),
    "battery_charging": sensor(
        "Battery charging",
        field="P_B_AGGR",
        unit="W",
        device_class="power",
        state_class="measurement",
        transform=_positive,
        icon="mdi:battery-charging",
        precision=0,
        optional=True,
    ),
    "battery_absorption": sensor(
        "Battery discharging",
        field="P_B_AGGR",
        unit="W",
        device_class="power",
        state_class="measurement",
        transform=_grid_import,
        icon="mdi:battery-minus",
        precision=0,
        optional=True,
    ),
    "battery_power_1": sensor(
        "Battery power input 1",
        field="P_B1",
        unit="W",
        device_class="power",
        state_class="measurement",
        icon="mdi:battery-sync",
        precision=0,
        optional=True,
    ),
    "battery_power_2": sensor(
        "Battery power input 2",
        field="P_B2",
        unit="W",
        device_class="power",
        state_class="measurement",
        icon="mdi:battery-sync",
        precision=0,
        optional=True,
    ),
    "battery_temperature": sensor(
        "Battery temperature",
        field="TB",
        unit="°C",
        device_class="temperature",
        state_class="measurement",
        icon="mdi:thermometer",
        precision=1,
        optional=True,
    ),
    # --- PV strings -------------------------------------------------------
    "pv1_power": sensor(
        "PV string 1 power",
        field="MP1",
        unit="W",
        device_class="power",
        state_class="measurement",
        icon="mdi:solar-panel",
        precision=0,
        optional=True,
    ),
    "pv1_voltage": sensor(
        "PV string 1 voltage",
        field="MT1",
        unit="V",
        device_class="voltage",
        state_class="measurement",
        icon="mdi:solar-panel",
        precision=1,
        optional=True,
    ),
    "pv1_current": sensor(
        "PV string 1 current",
        field="MC1",
        unit="A",
        device_class="current",
        state_class="measurement",
        icon="mdi:solar-panel",
        precision=2,
        optional=True,
    ),
    "pv2_power": sensor(
        "PV string 2 power",
        field="MP2",
        unit="W",
        device_class="power",
        state_class="measurement",
        icon="mdi:solar-panel",
        precision=0,
        optional=True,
    ),
    "pv2_voltage": sensor(
        "PV string 2 voltage",
        field="MT2",
        unit="V",
        device_class="voltage",
        state_class="measurement",
        icon="mdi:solar-panel",
        precision=1,
        optional=True,
    ),
    "pv2_current": sensor(
        "PV string 2 current",
        field="MC2",
        unit="A",
        device_class="current",
        state_class="measurement",
        icon="mdi:solar-panel",
        precision=2,
        optional=True,
    ),
    # --- Energy -----------------------------------------------------------
    "energy_today": sensor(
        "PV energy today",
        sum_of=ENERGY_CHANNELS,
        unit="kWh",
        device_class="energy",
        state_class="total_increasing",
        icon="mdi:solar-power-variant",
        precision=2,
    ),
    "energy_total": sensor(
        "PV energy total",
        sum_of=LIFETIME_CHANNELS,
        unit="kWh",
        device_class="energy",
        state_class="total_increasing",
        icon="mdi:solar-power-variant-outline",
        precision=2,
    ),
    # --- Inverter ---------------------------------------------------------
    "inverter_temperature": sensor(
        "Inverter temperature",
        field="TI",
        unit="°C",
        device_class="temperature",
        state_class="measurement",
        icon="mdi:thermometer",
        precision=1,
        optional=True,
    ),
    # --- Diagnostics ------------------------------------------------------
    "alerts": sensor(
        "Active alerts",
        field="ALERTS",
        value_type=TEXT,
        entity_category="diagnostic",
        icon="mdi:alert-outline",
        optional=True,
    ),
    "firmware": sensor(
        "Firmware",
        field="ARM",
        value_type=TEXT,
        entity_category="diagnostic",
        icon="mdi:chip",
        optional=True,
    ),
    "last_sample": sensor(
        "Last inverter sample",
        field="TS",
        value_type=TIMESTAMP,
        entity_category="diagnostic",
        icon="mdi:clock-outline",
        optional=True,
    ),
}


def _raw_value(definition: dict[str, Any], data: dict[str, Any]) -> Any:
    sum_of = definition.get("sum_of")
    if sum_of:
        numbers = []
        for field in sum_of:
            value = data.get(field)
            if value is None:
                continue
            numbers.append(float(value))
        if not numbers:
            return None
        return sum(numbers)
    return data.get(definition["field"])


def _format_payload(definition: dict[str, Any], raw: Any) -> str | int | float | None:
    value_type = definition["value_type"]
    if value_type == TIMESTAMP:
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            return None
    if value_type == TEXT:
        text = str(raw).strip()
        return text or None
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    transform = definition.get("transform")
    if transform:
        number = transform(number)
    number = round(number, 2)
    if number == 0:
        number = 0.0
    return number


async def publish_discovery(
    mqtt: MqttClient,
    inverter: str,
    prefix: str,
    discovery_prefix: str,
    firmware: str | None = None,
    quiet: bool = False,
) -> None:
    device = {
        "identifiers": [f"azzurro_{inverter}"],
        "name": f"Azzurro {inverter}",
        "manufacturer": "Azzurro",
        "model": "Inverter",
    }
    if firmware:
        device["sw_version"] = firmware
    availability = f"{prefix}/availability"
    for key, definition in SENSORS.items():
        config: dict[str, Any] = {
            "name": definition["name"],
            "unique_id": f"azzurro_{inverter}_{key}",
            "state_topic": f"{prefix}/{key}",
            "availability_topic": availability,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device,
        }
        if definition.get("unit"):
            config["unit_of_measurement"] = definition["unit"]
        if definition.get("device_class"):
            config["device_class"] = definition["device_class"]
        if definition.get("state_class"):
            config["state_class"] = definition["state_class"]
        if definition.get("entity_category"):
            config["entity_category"] = definition["entity_category"]
        if definition.get("icon"):
            config["icon"] = definition["icon"]
        if definition.get("precision") is not None:
            config["suggested_display_precision"] = definition["precision"]
        topic = f"{discovery_prefix}/sensor/azzurro_{inverter}/{key}/config"
        await mqtt.publish(topic, json.dumps(config), qos=1, retain=True)
        _LOGGER.debug("Published discovery config %s: %s", topic, json.dumps(config))
    if quiet:
        _LOGGER.debug("Re-published %d MQTT discovery configs under %s/", len(SENSORS), discovery_prefix)
    else:
        _LOGGER.info(
            "Published %d MQTT discovery configs under %s/sensor/azzurro_%s/ (device \"Azzurro %s\")",
            len(SENSORS),
            discovery_prefix,
            inverter,
            inverter,
        )


async def publish_values(mqtt: MqttClient, data: dict[str, Any], prefix: str) -> dict[str, Any]:
    published: dict[str, Any] = {}
    missing: set[str] = set()
    for key, definition in SENSORS.items():
        raw = _raw_value(definition, data)
        payload = None if raw is None else _format_payload(definition, raw)
        if payload is None:
            if not definition.get("optional"):
                missing.add(definition.get("field") or "+".join(definition.get("sum_of") or ()))
            continue
        await mqtt.publish(f"{prefix}/{key}", str(payload), qos=1, retain=True)
        _LOGGER.debug("Published %s/%s = %s (unit=%s)", prefix, key, payload, definition.get("unit"))
        published[key] = payload
    if missing:
        _LOGGER.warning(
            "Azzurro payload has no usable value for %s; %d of %d sensors were not updated",
            ", ".join(sorted(missing)),
            len(missing),
            len(SENSORS),
        )
    return published


async def mqtt_worker(
    session: aiohttp.ClientSession,
    broker: dict[str, Any],
    azzurro: AzzurroClient,
    inverter: str,
    prefix: str,
    discovery_prefix: str,
    interval: int,
) -> None:
    tls_context = ssl.create_default_context() if broker.get("ssl", False) else None
    async with MqttClient(
        hostname=broker["host"],
        port=int(broker["port"]),
        username=broker.get("username") or None,
        password=broker.get("password") or None,
        tls_context=tls_context,
        will=Will(f"{prefix}/availability", payload="offline", qos=1, retain=True),
    ) as mqtt:
        _LOGGER.info("Connected to MQTT broker %s:%s", broker["host"], broker["port"])
        last_discovery = 0.0
        while True:
            if time.monotonic() - last_discovery >= DISCOVERY_REFRESH_SECONDS:
                await publish_discovery(
                    mqtt,
                    inverter,
                    prefix,
                    discovery_prefix,
                    firmware=azzurro.firmware,
                    quiet=last_discovery > 0,
                )
                last_discovery = time.monotonic()
            await mqtt.publish(f"{prefix}/availability", "online", qos=1, retain=True)
            try:
                data = await azzurro.fetch(session)
            except (aiohttp.ClientError, KeyError, RuntimeError, TypeError, ValueError) as error:
                _LOGGER.error("Azzurro update failed: %s", error)
                await mqtt.publish(f"{prefix}/availability", "offline", qos=1, retain=True)
            else:
                _LOGGER.debug(
                    "Azzurro payload fields: %s",
                    ", ".join(
                        f"{name}={'null' if value is None else type(value).__name__}"
                        for name, value in sorted(data.items())
                    ),
                )
                published = await publish_values(mqtt, data, prefix)
                if published:
                    _LOGGER.info(
                        "Fetched Azzurro inverter data: %s",
                        ", ".join(f"{key}={value}" for key, value in published.items()),
                    )
                else:
                    _LOGGER.error(
                        "Fetched Azzurro inverter data but none of the expected fields (%s) were present",
                        ", ".join(sorted({definition["field"] for definition in SENSORS.values() if definition.get("field")})),
                    )
            await asyncio.sleep(interval)


async def run(options: dict[str, Any]) -> None:
    interval = int(options.get("update_interval", 5)) * 60
    inverter = str(options["inverter"])
    prefix = f"azzurro/{inverter}"
    discovery_prefix = str(options.get("discovery_prefix") or DEFAULT_DISCOVERY_PREFIX).strip("/")
    firebase_api_key = options.get("firebase_api_key") or DEFAULT_FIREBASE_API_KEY
    azzurro = AzzurroClient(options["username"], options["password"], inverter, firebase_api_key)
    timeout = aiohttp.ClientTimeout(total=30)

    _LOGGER.info(
        "Azzurro Inverter %s starting: inverter=%s poll=%d min discovery_prefix=%s state_prefix=%s sensors=%d",
        APP_VERSION,
        inverter,
        interval // 60,
        discovery_prefix,
        prefix,
        len(SENSORS),
    )

    async with aiohttp.ClientSession(timeout=timeout) as session:
        broker = await mqtt_config(session, options)
        while True:
            try:
                await mqtt_worker(session, broker, azzurro, inverter, prefix, discovery_prefix, interval)
            except MqttError as error:
                _LOGGER.error("MQTT connection lost or refused (%s); reconnecting in 30 seconds", error)
                await asyncio.sleep(30)


def load_options() -> dict[str, Any]:
    with open("/data/options.json", encoding="utf-8") as options_file:
        options = json.load(options_file)
    for required in ("username", "password", "inverter"):
        if not options.get(required):
            raise RuntimeError(f"Missing required app option: {required}")
    return options


if __name__ == "__main__":
    options = load_options()
    level = LOG_LEVELS.get(str(options.get("log_level", "info")).lower(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s")
    if level == logging.DEBUG:
        logging.getLogger("aiomqtt").setLevel(logging.DEBUG)
    asyncio.run(run(options))
