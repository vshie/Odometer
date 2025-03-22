# BlueOS Odometer Extension

The BlueOS Odometer Extension tracks your vehicle's usage stats and maintenance history, providing you with valuable data about your vehicle's operational life and history. When understanding failure rates and theh cost of system operations vs. time, data is key! This extension aspires to be like the odometer of your car - but perhaps easier to fiddle with! 

## Features

- **Uptime Tracking**: Counts total minutes of vehicle operation
- **Armed/Disarmed Time**: Tracks how long your vehicle has been in armed vs. disarmed states
- **Battery Monitoring**: Records battery voltage and detects battery swaps
- **Maintenance Log**: Add and track repair, replacement, and maintenance events
- **Data Export**: Download all collected data as CSV files

## Installation

Once launched in the Extensions Manager, install this extension directly from the Extensions page in your BlueOS web interface. Prior to that, instal with vshie/blueos-blueos-odometer as docker image and main as the branch. Use the contents of the docker-file permissions section with \ removed for  the settings. 

## How It Works

### Usage Tracking

The Odometer polls the Mavlink2Rest API once per minute to:

1. Increment the total uptime counter
2. Check armed status and increment the relevant counter (armed or disarmed)
3. Monitor battery voltage and detect battery swaps (when voltage increases by > 1V)
4. Record all this data to a persistent CSV file

### Maintenance Logging

You can add maintenance records through the web interface, which will be stored in a separate CSV file. Each record includes:

- Timestamp
- Event type (Repair, Replacement, Maintenance, Inspection, Note)
- Detailed description

You can edit the time after creating a log entry to be when the event occured, instead of when it was recorded. 

## Requirements

- BlueOS version 1.3.1 or higher
- OR A vehicle with a functioning Mavlink2Rest API (??)

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Acknowledgments

- BlueOS team for their excellent vehicle management platform
- Blue Robotics for fostering the underwater robotics community
