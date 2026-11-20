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

Configure the required options as follows:

```yaml
username: your-azzurro-email
password: your-azzurro-password
inverter: your-inverter-serial
update_interval: 5
firebase_api_key: ""
```

The interval is in minutes and must be between 1 and 60.

`firebase_api_key` is optional. Keep it empty to use the bundled default key; set it only if Azzurro changes their Firebase key.

Credentials are stored by Home Assistant in the app options and are passed to the container through `/data/options.json`.

The app requires MQTT. It first asks Supervisor for the MQTT service details. If Supervisor service discovery is unavailable, configure the manual MQTT options.

Telemetry is published with retained MQTT Discovery configuration under:

```text
homeassistant/sensor/azzurro_<serial>/<sensor>/config
azzurro/<serial>/<sensor>
azzurro/<serial>/availability
```

A failed poll marks the device unavailable rather than publishing zero values. The next successful poll marks it online again.
