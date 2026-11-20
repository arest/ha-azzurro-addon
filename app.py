"""Azzurro inverter telemetry app for Home Assistant."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import time
from dataclasses import dataclass
from typing import Any

import aiohttp
from aiomqtt import Client as MqttClient, Will

_LOGGER = logging.getLogger("azzurro_inverter")

# Firebase Web API key embedded in the Azzurro/Solarman clients. It is the
# `?key=` parameter of the original login URL in azzurro.py / azzurro_login.py.
# This is a public project identifier, not a user secret: it cannot read or
# write account data on its own, and the per-user password is still required.
DEFAULT_FIREBASE_API_KEY = "AIzaSyCTFwAoDwQaUWFhMEHeqUDw-rvu65vuX_0"
IDENTITY_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
REFRESH_URL = "https://securetoken.googleapis.com/v1/token"
GRAPHQL_URL = "https://yyhpek4qzrg25gh56dg4w3pnou.appsync-api.eu-west-1.amazonaws.com/graphql"


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

        query = """query ($serial: String!) {
          inverter: getLastAggregate(serial: $serial, series: DAY, type: \"Solarman\") {
            BP BP1 BP2 P_B1 P_B2 P_DC_TOT P_SELF P_B_AGGR P_EXT P_EXP P_LOAD
            V EG1 EG2 EG3 EG4 EG5 EG6 ET1 ET2 ET3 ET4 ET5 ET6 TS SK __typename
          }
        }"""
        payload = {"operationName": None, "variables": {"serial": self.inverter}, "query": query}
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
            raise RuntimeError(f"Azzurro data request failed with HTTP {response.status}")
        values = body.get("data", {}).get("inverter")
        if not values:
            raise RuntimeError("Azzurro response did not contain inverter data")
        return values[0]


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
                        return data
                _LOGGER.warning("Supervisor MQTT service was unavailable; using manual MQTT options")
        except aiohttp.ClientError as error:
            _LOGGER.warning("Could not query Supervisor MQTT service: %s", error)

    if not manual_host:
        raise RuntimeError("No MQTT service found. Install/configure MQTT or set mqtt_host in the app options.")
    return {
        "host": manual_host,
        "port": int(options.get("mqtt_port", 1883)),
        "username": options.get("mqtt_username") or None,
        "password": options.get("mqtt_password") or None,
        "ssl": bool(options.get("mqtt_ssl", False)),
    }


SENSORS = {
    "battery_charge": {"name": "Battery charge", "unit": "%", "device_class": "battery", "state_class": "measurement", "field": "BP1"},
    "solar_production": {"name": "Solar PV production", "unit": "W", "device_class": "power", "state_class": "measurement", "field": "P_SELF"},
    "grid_absorption": {"name": "Grid absorption", "unit": "W", "device_class": "power", "state_class": "measurement", "field": "P_EXP", "absolute": True},
    "home_consumption": {"name": "Home consumption", "unit": "W", "device_class": "power", "state_class": "measurement", "field": "P_LOAD"},
    "battery_absorption": {"name": "Battery discharging", "unit": "W", "device_class": "power", "state_class": "measurement", "field": "P_B_AGGR", "negative_absolute": True},
    "battery_charging": {"name": "Battery charging", "unit": "W", "device_class": "power", "state_class": "measurement", "field": "P_B_AGGR", "positive_absolute": True},
}


async def publish_discovery(mqtt: MqttClient, inverter: str, prefix: str) -> None:
    device = {"identifiers": [f"azzurro_{inverter}"], "name": f"Azzurro {inverter}", "manufacturer": "Azzurro", "model": "Inverter"}
    availability = f"{prefix}/availability"
    for key, definition in SENSORS.items():
        config = {
            "name": definition["name"],
            "unique_id": f"azzurro_{inverter}_{key}",
            "state_topic": f"{prefix}/{key}",
            "availability_topic": availability,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device,
            "unit_of_measurement": definition["unit"],
            "device_class": definition["device_class"],
            "state_class": definition["state_class"],
        }
        await mqtt.publish(f"homeassistant/sensor/azzurro_{inverter}/{key}/config", json.dumps(config), retain=True)


async def publish_values(mqtt: MqttClient, data: dict[str, Any], prefix: str) -> None:
    for key, definition in SENSORS.items():
        field = definition["field"]
        if field not in data or data[field] is None:
            continue
        value = float(data[field])
        if definition.get("absolute"):
            value = abs(value)
        elif definition.get("negative_absolute"):
            value = abs(value) if value < 0 else 0
        elif definition.get("positive_absolute"):
            value = abs(value) if value >= 0 else 0
        await mqtt.publish(f"{prefix}/{key}", str(round(value, 2)), retain=True)


async def run(options: dict[str, Any]) -> None:
    interval = int(options.get("update_interval", 5)) * 60
    inverter = options["inverter"]
    prefix = f"azzurro/{inverter}"
    firebase_api_key = options.get("firebase_api_key") or DEFAULT_FIREBASE_API_KEY
    azzurro = AzzurroClient(options["username"], options["password"], inverter, firebase_api_key)
    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        broker = await mqtt_config(session, options)
        tls_context = ssl.create_default_context() if broker.get("ssl", False) else None
        async with MqttClient(
            hostname=broker["host"],
            port=int(broker["port"]),
            username=broker.get("username"),
            password=broker.get("password"),
            tls_context=tls_context,
            will=Will(f"{prefix}/availability", payload="offline", retain=True),
        ) as mqtt:
            await publish_discovery(mqtt, inverter, prefix)
            await mqtt.publish(f"{prefix}/availability", "online", retain=True)
            while True:
                try:
                    data = await azzurro.fetch(session)
                    await publish_values(mqtt, data, prefix)
                    await mqtt.publish(f"{prefix}/availability", "online", retain=True)
                    _LOGGER.info("Fetched Azzurro inverter data")
                except (aiohttp.ClientError, KeyError, RuntimeError, TypeError, ValueError) as error:
                    _LOGGER.error("Azzurro update failed: %s", error)
                    await mqtt.publish(f"{prefix}/availability", "offline", retain=True)
                await asyncio.sleep(interval)


def load_options() -> dict[str, Any]:
    with open("/data/options.json", encoding="utf-8") as options_file:
        options = json.load(options_file)
    for required in ("username", "password", "inverter"):
        if not options.get(required):
            raise RuntimeError(f"Missing required app option: {required}")
    return options


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run(load_options()))
