# Azzurro Inverter

## Installation

### Git repository

1. Add the repository containing the `azzurro-inverter` directory to Home Assistant:
   **Settings → Apps → App store → ⋮ → Repositories**.
2. Install **Azzurro Inverter** from the app store.
3. Open the app's **Configuration** tab and enter the Azzurro credentials, inverter serial, and polling interval.
4. Save the configuration and start the app.

### Local installation

For local development, copy this directory to:

```text
/addons/local/azzurro-inverter/
```

Then use **Settings → Apps → App store → ⋮ → Reload**, install the app from **Local apps**, configure it, and start it.

### MQTT prerequisite

Install and start **Mosquitto broker**, then configure the MQTT integration from **Settings → Devices & services** with MQTT Discovery enabled. The app uses Supervisor MQTT service discovery automatically. Manual MQTT connection options are only required for an external broker.

If you already installed the app, upgrade it from **Settings → Apps → App store**:

1. Open **Azzurro Inverter**.
2. Click **Update** (or **Rebuild**) when available.
3. Start the app again.

For Git repositories, run **App store → ⋮ → Reload** first.

Configure the required options as follows:

```yaml
username: your-azzurro-email
password: your-azzurro-password
inverter: your-inverter-serial
update_interval: 5
firebase_api_key: ""
discovery_prefix: homeassistant
log_level: info

# Optional manual MQTT fallback settings
mqtt_host: ""
mqtt_port: 1883
mqtt_ssl: false
mqtt_username: ""
mqtt_password: ""
```

The interval is in minutes and must be between 1 and 60.

`discovery_prefix` must match the discovery prefix of your MQTT integration (default `homeassistant`).

`log_level` accepts `debug` or `info`. Use `debug` when troubleshooting: it logs the resolved broker, every discovery topic and payload, the field names present in the Azzurro response, and every published value. Passwords and tokens are never logged.

`firebase_api_key` is optional. Keep it empty to use the bundled default key; set it only if Azzurro changes their Firebase key.

Credentials are stored by Home Assistant in the app options and are passed to the container through `/data/options.json`.

The app requires MQTT. It first asks Supervisor for the MQTT service details. If Supervisor service discovery is unavailable, configure the manual MQTT options.

For manual MQTT fallback, set `mqtt_ssl: true` for TLS brokers (for example port `8883`) or keep it `false` for non-TLS brokers (usually `1883`).

Telemetry is published with retained MQTT Discovery configuration under:

```text
homeassistant/sensor/azzurro_<serial>/<sensor>/config
azzurro/<serial>/<sensor>
azzurro/<serial>/availability
```

A failed poll marks the device unavailable rather than publishing zero values. The next successful poll marks it online again.

## Troubleshooting: no Azzurro entities in Home Assistant

"Azzurro authentication succeeded" and "Fetched Azzurro inverter data" only prove the cloud leg works. Discovery problems are separate. Set `log_level: debug`, restart the app, and read the log top to bottom.

1. **Check the broker line.**
   `MQTT broker (Supervisor service discovery): <host>:<port> tls=... username=...`
   Compare it with the broker configured in **Settings → Devices & services → MQTT → Configure**. If the app publishes to a different broker (for example `core-mosquitto` while the MQTT integration points at an external broker), Home Assistant never sees the discovery messages. In that case set `mqtt_host`, `mqtt_port` and credentials explicitly and stop relying on Supervisor discovery.

2. **Check the discovery line.**
   `Published 6 MQTT discovery configs under homeassistant/sensor/azzurro_<serial>/`
   If this line is missing, the MQTT session failed before publishing; look for `MQTT connection lost or refused` or an authentication error in the MQTT broker log.

3. **Check the value lines.**
   `Fetched Azzurro inverter data: battery_charge=..., solar_production=...`
   If instead you see `Azzurro payload has no usable value for ...`, the entity is created but stays `unknown` because those GraphQL fields were missing or null. With `log_level: debug` the raw payload is logged so the real field names can be compared with `SENSORS` in `app.py`.

4. **Verify the broker actually holds the messages.** From a shell with access to the broker (replace the values with your own connection details):

   ```bash
   BROKER=core-mosquitto
   USER=your-mqtt-user
   PASS=your-mqtt-password
   mosquitto_sub -h "$BROKER" -p 1883 -u "$USER" -P "$PASS" \
     -t 'homeassistant/sensor/azzurro_#' -v --retained-only
   ```

   You should see six config payloads. If you do, the problem is on the Home Assistant side: confirm MQTT Discovery is enabled in the MQTT integration, check **Settings → Devices & services → MQTT**, and search the Home Assistant log for `mqtt` errors while restarting the app.

5. **Check the discovery prefix.** If your MQTT integration uses a non-default discovery prefix, set `discovery_prefix` accordingly. A wrong prefix produces no error anywhere: the app publishes happily and Home Assistant ignores the topics.

6. **Check `Settings → Devices & services → MQTT → devices` for `Azzurro <serial>`.** Entities are disabled-by-default only for a few device classes; battery and power sensors are enabled. Disabled entities are hidden from the entity list, so look at the device page, not the raw entity list.

Stale retained discovery from an older attempt can be cleared (carefully) with:

```bash
mosquitto_pub -h "$BROKER" -p 1883 -u "$USER" -P "$PASS" -r -n \
  -t 'homeassistant/sensor/azzurro_ZW1025/battery_charge/config'
```

Use the topic printed in the debug log, with your own serial and sensor key. The app re-publishes discovery on every reconnect and hourly, so a cleared topic comes back on its own.
