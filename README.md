# BlueOS Odometer Extension

The BlueOS Odometer Extension tracks your vehicle's usage stats and maintenance history, providing you with valuable data about your vehicle's operational profile.

## Features

- **Uptime Tracking**: Counts total minutes of vehicle operation
- **Armed/Disarmed Time**: Tracks how long your vehicle has been in armed vs. disarmed states
- **Battery Monitoring**: Records battery voltage and detects battery swaps
- **Maintenance Log**: Add and track repair, replacement, and maintenance events
- **Data Export**: Download all collected data as CSV files

## Installation

Install this extension directly from the BlueOS Extensions page in your vehicle's web interface.

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

## Requirements

- BlueOS version 1.1 or higher
- A vehicle with a functioning Mavlink2Rest API

## Development

### Building from Source

1. Clone this repository:
```bash
git clone https://github.com/yourusername/BlueOS-Odometer.git
cd BlueOS-Odometer
```

2. Build the Docker image:
```bash
docker build -t blueos-odometer .
```

3. Run the container:
```bash
docker run -p 8000:8000 blueos-odometer
```

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Acknowledgments

- BlueOS team for their excellent vehicle management platform
- Blue Robotics for fostering the underwater robotics community
