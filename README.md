# Azzurro Inverter Home Assistant app

This app logs in to the Azzurro/Solarman cloud service, fetches inverter telemetry, and publishes MQTT Discovery entities to Home Assistant.

## Installation

### Prerequisites

- Home Assistant OS or another installation that supports Apps (formerly add-ons).
- An MQTT broker connected to Home Assistant. The easiest option is the official **Mosquitto broker** app.
- MQTT Discovery enabled in the MQTT integration.
- Your Azzurro/Solarman account email, password, and inverter serial number.

### Install from a Git repository

1. Publish or fork this app repository so that `azzurro-inverter/config.yaml` is available in the repository.
2. In Home Assistant, open **Settings → Apps → App store**.
3. Open the three-dot menu in the upper-right corner and select **Repositories**.
4. Add the Git repository URL and confirm.
5. Find **Azzurro Inverter** in the app store and select **Install**.
6. Wait for the image to build, then open the **Configuration** tab.
7. Enter the configuration described below, click **Save**, and start the app.

### Install locally for development

1. Copy the `azzurro-inverter` directory into Home Assistant's local Apps directory:

   ```text
   /addons/local/azzurro-inverter/
   ```

2. In Home Assistant, open **Settings → Apps → App store**.
3. Open the three-dot menu and choose **Reload**.
4. Install **Azzurro Inverter** from the **Local apps** section.
5. Configure it and start it as described below.

### Configure the app

Set these required options:

```yaml
username: your-azzurro-email
password: your-azzurro-password
inverter: ZH1025006KE241210086
update_interval: 5
```

`update_interval` is expressed in minutes and accepts values from 1 to 60.

The app obtains MQTT connection details automatically from Supervisor service discovery. The `mqtt_host`, `mqtt_port`, `mqtt_username`, and `mqtt_password` options are only needed when using a broker that is not managed by Supervisor.

### Configure MQTT

If Mosquitto broker is not installed yet:

1. Open **Settings → Apps → App store**.
2. Install **Mosquitto broker**.
3. Start the broker app.
4. Open **Settings → Devices & services**.
5. Add or configure the discovered **MQTT** integration.
6. Ensure MQTT Discovery is enabled.

After the Azzurro app starts successfully, the sensors will be created automatically through MQTT Discovery. They can be found under **Settings → Devices & services → MQTT**, attached to an `Azzurro <serial>` device.

### Verify the installation

Open the Azzurro app's **Log** tab. A working installation should show messages similar to:

```text
Azzurro authentication succeeded
Fetched Azzurro inverter data
```

If the app fails to connect, check the log for authentication or MQTT errors. Do not paste passwords, refresh tokens, or ID tokens into support requests.

## Entities

The app creates one `Azzurro <serial>` device and these sensors:

- Battery charge (%)
- Solar PV production (W)
- Grid absorption (W; absolute value of `P_EXP`)
- Home consumption (W)
- Battery discharging (W)
- Battery charging (W)

The existing scripts use `P_EXP` as grid absorption and infer battery charge/discharge from the sign of `P_B_AGGR`; this app preserves that behavior. Verify the sign convention against the inverter during a known import/export and charge/discharge period before using these values for energy accounting.

## Authentication

The app uses the existing Firebase Identity Toolkit login flow and refreshes the returned ID token before expiry. Tokens and passwords are never logged or persisted. A restart logs in again with the configured password.
