# BlueOS Odometer Extension

The BlueOS Odometer Extension tracks your vehicle's usage stats and maintenance history, providing you with valuable data about your vehicle's operational life and history. When understanding failure rates and the cost of system operations vs. time, data is key! This extension aspires to be like the odometer of your car - but perhaps easier to fiddle with! 

## Features

- **Uptime Tracking**: Counts total minutes of vehicle operation
- **Armed/Disarmed Time**: Tracks how long your vehicle has been in armed vs. disarmed states
- **Per-Thruster Run Hours**: Individual run-hour tracking for each thruster
- **Thruster Duty Cycle**: Average PWM value when armed (duty-cycle indicator)
- **Battery Monitoring**: Records battery voltage and detects battery swaps
- **Maintenance Log**: Add and track repair, replacement, and maintenance events
- **Data Export**: Download all collected data as CSV files

## Installation

Once launched in the Extensions Manager, install this extension directly from the Extensions page in your BlueOS web interface.

## Manual Install

To install Odometer manually from the BlueOS Extension Manager, choose **Install from Scratch** from the + icon in the lower right of the Installed Extensions page and use:

```text
Image: vshie/blueos-blueos-odometer
Tag: beta
```

Copy and paste this permissions JSON when BlueOS asks for extension permissions:

```json
{
  "ExposedPorts": {
    "80/tcp": {},
    "8765/tcp": {}
  },
  "HostConfig": {
    "CpuPeriod": 100000,
    "CpuQuota": 100000,
    "Binds": [
      "/usr/blueos/extensions/odometer/data:/app/data",
      "/usr/blueos/extensions/odometer/logs:/app/logs"
    ],
    "ExtraHosts": ["host.docker.internal:host-gateway"],
    "PortBindings": {
      "80/tcp": [
        {
          "HostPort": ""
        }
      ],
      "8765/tcp": [
        {
          "HostPort": ""
        }
      ]
    }
  }
}
```

The other fields don't matter, make them something logical!

## How It Works

### Usage Tracking

The Odometer polls the Mavlink2Rest API once per minute to:

1. Increment the total uptime counter
2. Check armed status and increment the relevant counter (armed or disarmed). If the autopilot stops sending HEARTBEAT for more than 10 seconds, the vehicle is treated as offline (not armed), since Mavlink2Rest keeps serving its last cached message
3. Monitor battery voltage and detect battery swaps (when voltage increases by > 1V)
4. Record all this data to a persistent CSV file

### Connection

The extension finds the vehicle on its own:

- **BlueOS host:** it tries `host.docker.internal`, the container's default gateway (the Docker host), `192.168.2.2` and `blueos.local`, then keeps using whichever answers. The same host is used for the vehicle name and clock.
- **Autopilot system ID:** it is discovered from Mavlink2Rest (the system whose component 1 sends a fresh autopilot HEARTBEAT), so vehicles with `SYSID_THISMAV`/`MAV_SYSID` other than 1 are tracked. If that system goes quiet, it is rediscovered.

Changes are logged once (endpoint in use, autopilot found or lost, vehicle online/offline), and the current state is shown as a chip at the top of the page and returned under `connection` in `/stats`.

A startup is counted when the vehicle has rebooted (detected from the kernel boot ID), not when only the extension restarts. Dive time is only counted for ArduSub vehicles.

### Energy

Each power-on block (or hot battery swap) is a session. Session energy comes from the autopilot's `BATTERY_STATUS.energy_consumed` (falling back to `current_consumed` in mAh x average voltage), measured from the reading when the session started so an extension restart never counts the same energy twice. Lifetime energy is the sum of all closed sessions plus the current one.

### Per-Thruster / Per-Motor Tracking

The extension detects vehicle type from the MAVLink HEARTBEAT message and tracks run hours per thruster (ROVs) or per motor (boats):

- **ArduSub** (ROV): 4–8 thrusters (default 8)
- **BlueBoat/ArduRover** (boat with skid steer): 2 motors

Thrusters are mapped to output channels from the autopilot's `SERVOn_FUNCTION` parameters (Motor1-8 for ArduSub, ThrottleLeft/ThrottleRight for boats), so a 6-thruster BlueROV2 shows 6 thrusters. Until the parameters can be read, ArduSub defaults to 8 thrusters and boats to 2 on channels 1..N.

Output PWM is sampled every 5 seconds. A thruster's run time only accrues while the vehicle is armed and that thruster's output is more than 25 us from neutral (1500 us), so each thruster's hours reflect when it actually ran. The "hard use" session flag only looks at thruster channels, so lights or a gripper don't trigger it. The UI shows position grids to correlate numbers with physical layout (e.g. vertical vs horizontal thrusters for ROVs, port/starboard for boats).

Accessories (added via **Add Device**) accrue run time whenever their output differs from their configured **Off PWM** (default 1100 us, typical for lights; use 1500 for a gripper).

### Maintenance Logging

The maintenance log helps you track all important events related to your vehicle's upkeep. You can access it through the web interface, where you can:

1. **Add New Records**:
   - Select an event type (Repair, Replacement, Maintenance, Inspection, Thruster Sticking, or Note)
   - Add detailed description of the work performed
   - For **Replacement** or **Repair**: optionally select which thruster(s) were replaced/repaired and check "Reset run hours" to zero the run-time for those thrusters
   - For **Thruster Sticking**: select which thruster(s) were found sticking and required minor intervention
   - Records are automatically timestamped with the current time

2. **Edit Records**:
   - Click the pencil icon next to any record
   - Modify the date and time to reflect when the event actually occurred
   - This is useful for logging past maintenance events

3. **Delete Records**:
   - Click the trash icon next to any record
   - Confirm the deletion in two steps to prevent accidental deletions
   - This action cannot be undone

4. **View Records**:
   - Records are displayed in a table format
   - Each record shows the date/time, event type (color-coded), and details
   - Use the pagination controls to view more records
   - Records are sorted by date (newest first)

5. **Export Records**:
   - Download all maintenance records as a CSV file
   - Useful for backup or analysis in spreadsheet software

### Event Types

The maintenance log supports the following event types, each color-coded for easy identification:

- **Repair** (Red): For fixing issues or problems
- **Replacement** (Blue): For replacing components or parts
- **Maintenance** (Green): For documenting routine maintenance tasks
- **Inspection** (Amber): For system checks and inspections
- **Thruster Sticking** (Orange): For logging when a thruster motor was found sticking and required minor intervention
- **Note** (Grey): For general notes or observations

## Requirements

- BlueOS version 1.3.1 or higher
- OR A vehicle with a functioning Mavlink2Rest API (??)

## License

This project is licensed under the MIT License - see the LICENSE file for details.

