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

Once launched in the Extensions Manager, install this extension directly from the Extensions page in your BlueOS web interface. Prior to that, install with vshie/blueos-odometer as docker image and main as the branch. Use the contents of the docker-file permissions section with \ removed for the settings. 

## How It Works

### Usage Tracking

The Odometer polls the Mavlink2Rest API once per minute to:

1. Increment the total uptime counter
2. Check armed status and increment the relevant counter (armed or disarmed)
3. Monitor battery voltage and detect battery swaps (when voltage increases by > 1V)
4. Record all this data to a persistent CSV file

### Per-Thruster / Per-Motor Tracking

The extension detects vehicle type from the MAVLink HEARTBEAT message and tracks run hours per thruster (ROVs) or per motor (boats):

- **ArduSub** (ROV): 4–8 thrusters (default 8)
- **BlueBoat/ArduRover** (boat with skid steer): 2 motors

When armed, each thruster/motor's run minutes are incremented. PWM values are sampled every 5 seconds for duty-cycle averaging. The UI shows position grids to correlate numbers with physical layout (e.g. vertical vs horizontal thrusters for ROVs, port/starboard for boats).

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

