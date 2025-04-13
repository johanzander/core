```
This is a modification of the Growatt component that adds the following features:

1. **Support for Controlling Growatt TLX Hybrid Inverters**:
    - Specifically supports models such as MID 25KTL3-XH and APX batteries.
    - Enables control of:
      - Charge/Discharge Stop SOC.
      - Charging/Discharging Power Rate via number entities.
      - Grid Charging as a switch entity.
    - Allows updating time slot schedules with Battery/Grid/Load using service calls.

2. **Additional Sensors**:
    - Adds a number of power and lifetime energy sensors.
    - These sensors are available in the Growatt ShinePhone app and web dashboard.

3. **Uses official token based authentication towards Growatt server**:
    - Implements a new flow in HA where users can choose between legacy login/password authentication and token-based authentication required by `openapi.growatt.com`.
    - To get your token, login to `server.growatt.com` -> Settings -> Account -> API Token.
    Use this token in the HA Growatt setup flow.
    - Enter the token during the integration setup in Home Assistant.

4. **Temporary Dependency on a Local Version of PyPi_GrowattServer**:
    - Utilizes a local version of the library from https://github.com/indykoning/PyPi_GrowattServer.
    - This is necessary as the official Growatt API is not yet supported by the library.
    - Once Pull Request https://github.com/indykoning/PyPi_GrowattServer/pull/96 has been accepted, this local version will be removed.

5. **Installation as a Custom Component**:
    - Clone this repository or download the `growatt_server` folder.
    - Place the `growatt_server` folder in your Home Assistant `custom_components` directory. If the directory does not exist, create it under your Home Assistant configuration folder.
    - Restart Home Assistant to load the custom component.
    - Add the Growatt integration via the Home Assistant UI and follow the setup instructions.
    - Note: you must add a version to the growatt_server/manifest.json file, e.g.
      "version": "1.7.0" as this is required for custom components.


This modification enhances the functionality of the Growatt integration, providing more control and monitoring capabilities for hybrid inverters and batteries.
```