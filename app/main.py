#!/usr/bin/env python3

import os
import csv
import time
import json
import datetime
import logging
import logging.handlers
import threading
import asyncio
import requests
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from flask import Flask, jsonify, request, send_from_directory, send_file
import websockets
from websockets.exceptions import ConnectionClosed

# Set up logging
log_dir = Path('/app/logs')
log_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.handlers.RotatingFileHandler(log_dir / 'lumber.log', maxBytes=2**16, backupCount=1),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Constants
DATA_DIR = Path('/app/data')
DATA_DIR.mkdir(parents=True, exist_ok=True)
ODOMETER_CSV = DATA_DIR / 'odometer.csv'
MAINTENANCE_CSV = DATA_DIR / 'maintenance.csv'
STARTUP_MARKER = DATA_DIR / '.startup_marker'
CPU_TEMP_PATH = Path('/sys/class/thermal/thermal_zone0/temp')

# Define potential Mavlink endpoints to try
MAVLINK_ENDPOINTS = [
    'http://host.docker.internal/mavlink2rest/mavlink/vehicles/1/components/1/messages',  # Primary endpoint
    'http://host.docker.internal:6040/v1/mavlink',  # Backup endpoint
    'http://192.168.2.2/mavlink2rest/mavlink/vehicles/1/components/1/messages',  # Standard BlueOS IP
    'http://localhost/mavlink2rest/mavlink/vehicles/1/components/1/messages',
    'http://blueos.local/mavlink2rest/mavlink/vehicles/1/components/1/messages'
]

UPDATE_INTERVAL = 60  # Update every 60 seconds (1 minute)
ARMED_FLAG = 128  # MAV_MODE_FLAG_SAFETY_ARMED (0b10000000)
MAX_TIME_JUMP_MINUTES = 5  # Maximum acceptable time jump in minutes
PORT = 80  # Port to run the server on
WEBSOCKET_PORT = 8765  # Port for Cockpit data lake streaming
WEBSOCKET_UPDATE_INTERVAL = 1.0  # Seconds between WebSocket updates

app = Flask(__name__, static_folder='static')

REGISTER_SERVICE = {
    "name": "Odometer",
    "description": "Track vehicle usage statistics, armed time, battery swaps, and maintenance history with beautiful visualizations",
    "icon": "mdi-counter",
    "company": "Blue Robotics",
    "version": "0.1.0",
    "webpage": "https://github.com/vshie/Odometer",
    "api": "https://github.com/bluerobotics/BlueOS-docker"
}

async def websocket_handler(websocket):
    """Stream odometer metrics to Cockpit's data lake via WebSocket."""
    logger.info("WebSocket client connected: %s", websocket.remote_address)
    await websocket.send("odometer-connection-status=connected")

    try:
        while True:
            with odometer_service.stats_lock:
                armed_minutes = odometer_service.stats.get("armed_minutes", 0)
                disarmed_minutes = odometer_service.stats.get("disarmed_minutes", 0)
                total_wh = odometer_service.stats.get("total_wh_consumed", 0.0)

            await websocket.send(f"odometer-armed-minutes={armed_minutes}")
            await websocket.send(f"odometer-disarmed-minutes={disarmed_minutes}")
            await websocket.send(f"odometer-total-wh={total_wh:.3f}")

            await asyncio.sleep(WEBSOCKET_UPDATE_INTERVAL)
    except ConnectionClosed:
        logger.info("WebSocket client disconnected: %s", websocket.remote_address)


async def websocket_main():
    """Run the WebSocket server for Cockpit data lake streaming."""
    async with websockets.serve(websocket_handler, "0.0.0.0", WEBSOCKET_PORT):
        logger.info("WebSocket server started on ws://0.0.0.0:%s", WEBSOCKET_PORT)
        await asyncio.Future()


def start_websocket_server():
    """Start the WebSocket server in its own event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(websocket_main())

class OdometerService:
    def __init__(self):
        self.stop_event = threading.Event()
        self.stats_lock = threading.Lock()
        self.stats = {
            'total_minutes': 0,
            'armed_minutes': 0,
            'disarmed_minutes': 0,
            'battery_swaps': 0,
            'startups': 0,
            'last_voltage': 0.0,
            'cpu_temp': 0.0,
            'previous_batteries_wh': 0.0,  # Accumulated watt-hours from all previous (swapped) batteries
            'current_battery_wh': 0.0,  # Current battery watt-hours (from MAVLink current_consumed)
            'total_wh_consumed': 0.0,  # Lifetime total = previous_batteries_wh + current_battery_wh
            'voltage_sum': 0.0,  # Sum of voltages for averaging
            'voltage_count': 0,  # Number of voltage readings
            'last_current_consumed': 0.0,  # Last current consumed for battery swap detection
            'current_mission': {
                'start_time': None,
                'start_voltage': 0.0,
                'start_cpu_temp': 0.0,
                'end_voltage': 0.0,
                'end_cpu_temp': 0.0,
                'total_ah': 0.0,
                'start_uptime': 0,
                'end_uptime': 0
            }
        }
        self.missions = []  # List to store completed missions
        self.last_update_time = time.time()
        self.minutes_since_update = 0
        self.setup_csv_files()
        self.load_stats()
        self.detect_startup()
        
        # Start the update thread
        self.update_thread = threading.Thread(target=self.update_loop)
        self.update_thread.daemon = True
        self.update_thread.start()
    
    def detect_startup(self):
        """
        Detect if this is a new startup and increment the counter if it is.
        Uses a marker file to detect if this is a new startup.
        
        Note: Energy tracking is handled automatically:
        - previous_batteries_wh is loaded from CSV
        - current_battery_wh is calculated from MAVLink current_consumed (which persists)
        - total_wh_consumed = previous_batteries_wh + current_battery_wh
        """
        is_new_startup = not STARTUP_MARKER.exists()
        
        if is_new_startup:
            logger.info("Detected new vehicle startup")
            with self.stats_lock:
                self.stats['startups'] += 1
                # Reset voltage averaging for fresh start
                self.stats['voltage_sum'] = 0.0
                self.stats['voltage_count'] = 0
            
            # Create the marker file
            with open(STARTUP_MARKER, 'w') as f:
                f.write(datetime.datetime.now().isoformat())
            
            # Write the updated stats to CSV right away
            self.write_stats_to_csv(startup_detected=True)
    
    def upgrade_csv_format(self):
        """Upgrade old CSV format to new format if needed"""
        try:
            with open(ODOMETER_CSV, 'r', newline='') as f:
                reader = csv.reader(f)
                headers = next(reader)  # Get header row
                
                # Check if this is an old format file (has mah_consumed column)
                if 'mah_consumed' in headers:
                    logger.info("Detected old format CSV file, upgrading to new format")
                    
                    # Read all existing data
                    rows = []
                    for row in reader:
                        rows.append(row)
                    
                    # Write back with new format
                    with open(ODOMETER_CSV, 'w', newline='') as f:
                        writer = csv.writer(f)
                        # Write new header row
                        writer.writerow(['timestamp', 'total_minutes', 'armed_minutes', 'disarmed_minutes', 
                                       'battery_swaps', 'startups', 'voltage', 'cpu_temp', 'wh_consumed', 'time_status'])
                        
                        # Write existing data, removing mah_consumed column
                        for row in rows:
                            # Keep all columns except mah_consumed
                            new_row = row[:8]  # Keep up to cpu_temp
                            if len(row) > 9:  # If wh_consumed exists
                                new_row.append(row[9])  # Add wh_consumed
                            else:
                                new_row.append('0.0')  # Default to 0 if missing
                            if len(row) > 10:  # If time_status exists
                                new_row.append(row[10])  # Add time_status
                            else:
                                new_row.append('normal')  # Default to normal
                            writer.writerow(new_row)
                    
                    logger.info("Successfully upgraded CSV file to new format")
        except Exception as e:
            logger.error(f"Error upgrading CSV format: {e}")
    
    def setup_csv_files(self):
        # Set up odometer CSV file
        if not ODOMETER_CSV.exists():
            with open(ODOMETER_CSV, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['timestamp', 'total_minutes', 'armed_minutes', 'disarmed_minutes', 
                               'battery_swaps', 'startups', 'voltage', 'cpu_temp', 'wh_consumed', 
                               'current_ah', 'time_status'])
        else:
            # Check if this is an old format file and upgrade it if needed
            self.upgrade_csv_format()
        
        # Set up maintenance CSV file
        if not MAINTENANCE_CSV.exists():
            with open(MAINTENANCE_CSV, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['timestamp', 'event_type', 'details'])
    
    def cleanup_csv(self):
        """Clean up the CSV file by removing bad rows and ensuring proper format"""
        try:
            if not ODOMETER_CSV.exists():
                return

            # Read all rows
            rows = []
            with open(ODOMETER_CSV, 'r', newline='') as f:
                reader = csv.reader(f)
                headers = next(reader)  # Get header row
                rows.append(headers)  # Keep header row
                
                for row in reader:
                    # Skip empty rows or rows with all empty values
                    if not row or all(cell.strip() == '' for cell in row):
                        continue
                    
                    # Skip rows that don't have minimum required columns (at least 9)
                    if len(row) < 9:
                        continue
                    
                    # Skip rows with invalid timestamp
                    try:
                        datetime.datetime.fromisoformat(row[0])
                    except (ValueError, TypeError):
                        continue
                    
                    # Skip rows with invalid numeric values
                    try:
                        if row[1].strip(): int(row[1])  # total_minutes
                        if row[2].strip(): int(row[2])  # armed_minutes
                        if row[3].strip(): int(row[3])  # disarmed_minutes
                        if row[4].strip(): int(row[4])  # battery_swaps
                        if row[5].strip(): int(row[5])  # startups
                        if row[6].strip(): float(row[6])  # voltage
                        if row[7].strip(): float(row[7])  # cpu_temp
                        if row[8].strip(): float(row[8])  # wh_consumed
                        if len(row) > 9 and row[9].strip(): float(row[9])  # current_ah
                    except (ValueError, TypeError):
                        continue
                    
                    rows.append(row)
            
            # Write back cleaned data
            with open(ODOMETER_CSV, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerows(rows)
            
            logger.info("Successfully cleaned up CSV file")
            
        except Exception as e:
            logger.error(f"Error cleaning up CSV file: {e}")

    def load_stats(self):
        """Load the latest stats from the CSV file"""
        if ODOMETER_CSV.exists():
            # First clean up the CSV file
            self.cleanup_csv()
            
            with open(ODOMETER_CSV, 'r', newline='') as f:
                reader = csv.reader(f)
                headers = next(reader)  # Skip header row
                last_row = None
                for row in reader:
                    # Skip empty rows or rows with all empty values
                    if not row or all(cell.strip() == '' for cell in row):
                        continue
                    last_row = row
                
                if last_row:
                    with self.stats_lock:
                        # Basic stats that have always existed
                        self.stats['total_minutes'] = int(last_row[1]) if last_row[1].strip() else 0
                        self.stats['armed_minutes'] = int(last_row[2]) if last_row[2].strip() else 0
                        self.stats['disarmed_minutes'] = int(last_row[3]) if last_row[3].strip() else 0
                        self.stats['battery_swaps'] = int(last_row[4]) if last_row[4].strip() else 0
                        
                        # Handle loading "startups" field if it exists in the CSV
                        if len(last_row) > 5 and last_row[5].strip():
                            self.stats['startups'] = int(last_row[5])
                        else:
                            self.stats['startups'] = 0
                        
                        # Voltage is at index 6 if startups field exists
                        if len(last_row) > 6:
                            self.stats['last_voltage'] = float(last_row[6]) if last_row[6].strip() else 0.0
                        else:
                            self.stats['last_voltage'] = float(last_row[5]) if last_row[5].strip() else 0.0
                        
                        # CPU temperature might be at index 7 if the field exists
                        if len(last_row) > 7 and last_row[7].strip():
                            try:
                                self.stats['cpu_temp'] = float(last_row[7])
                            except (ValueError, TypeError):
                                self.stats['cpu_temp'] = 0.0
                        else:
                            self.stats['cpu_temp'] = 0.0
                        
                        # Load accumulated watt-hours from previous batteries (index 8)
                        # Note: CSV stores previous_batteries_wh, and total_wh_consumed is calculated
                        # as previous_batteries_wh + current_battery_wh on each update
                        if len(last_row) > 8 and last_row[8].strip():
                            try:
                                self.stats['previous_batteries_wh'] = float(last_row[8])
                                # Initialize total to previous batteries until we get MAVLink data
                                self.stats['total_wh_consumed'] = self.stats['previous_batteries_wh']
                            except (ValueError, TypeError):
                                self.stats['previous_batteries_wh'] = 0.0
                                self.stats['total_wh_consumed'] = 0.0
                        else:
                            self.stats['previous_batteries_wh'] = 0.0
                            self.stats['total_wh_consumed'] = 0.0
    
    def update_loop(self):
        """Main update loop that runs every minute"""
        while not self.stop_event.is_set():
            try:
                self.update_stats()
                # Sleep for the update interval
                time.sleep(UPDATE_INTERVAL)
            except Exception as e:
                logger.error(f"Error in update loop: {e}")
                time.sleep(10)  # Sleep for a shorter time if there was an error
    
    def update_stats(self):
        """Update the statistics and write to CSV"""
        try:
            # Check for time jumps (system time corrections)
            current_time = time.time()
            time_diff_seconds = current_time - self.last_update_time
            expected_diff_seconds = UPDATE_INTERVAL
            
            # Calculate minutes to add based on actual time passed vs expected
            if abs(time_diff_seconds - expected_diff_seconds) > (MAX_TIME_JUMP_MINUTES * 60):
                # A significant time jump detected, use expected time diff instead
                logger.warning(f"Time jump detected! Diff: {time_diff_seconds/60:.2f} minutes. Using expected time interval.")
                minutes_to_add = expected_diff_seconds / 60
                time_status = "corrected"
            else:
                # Normal update, use actual time diff
                minutes_to_add = time_diff_seconds / 60
                time_status = "normal"
            
            # Get current voltage, armed status, and current consumed
            current_voltage, is_armed, current_consumed = self.get_vehicle_status()
            
            # Get current CPU temperature
            current_cpu_temp = self.get_cpu_temperature()
            
            with self.stats_lock:
                # Update total minutes
                self.stats['total_minutes'] += 1  # Always add 1 minute regardless of time jump
                
                # Update armed/disarmed minutes
                if is_armed:
                    self.stats['armed_minutes'] += 1
                else:
                    self.stats['disarmed_minutes'] += 1
                
                # Update voltage tracking for averaging
                if current_voltage > 0:
                    self.stats['voltage_sum'] += current_voltage
                    self.stats['voltage_count'] += 1
                    
                    # Calculate average voltage for this battery
                    avg_voltage = self.stats['voltage_sum'] / self.stats['voltage_count']
                    
                    # Calculate watt-hours for this update
                    # current_consumed is in mAh, convert to Ah and multiply by voltage to get Wh
                    wh_consumed = (abs(current_consumed) / 1000.0) * avg_voltage
                    
                    # Check for battery swap (current_consumed reset and voltage increase)
                    # Battery swap is detected when:
                    # 1. The consumed current drops (battery was replaced with a fresh one)
                    # 2. Voltage increases by more than 1V (fresh battery has higher voltage)
                    # 3. We have a valid previous voltage reading
                    if (current_consumed < self.stats['last_current_consumed'] and 
                        current_voltage > (self.stats['last_voltage'] + 1.0) and 
                        self.stats['last_voltage'] > 0):
                        
                        # Add current battery's watt-hours to accumulated total before resetting
                        if self.stats['current_battery_wh'] > 0:
                            self.stats['previous_batteries_wh'] += self.stats['current_battery_wh']
                            logger.info(f"Battery swap: Adding {self.stats['current_battery_wh']:.2f}Wh to previous batteries total: {self.stats['previous_batteries_wh']:.2f}Wh")
                        
                        # Save the completed mission if we have one
                        if self.stats['current_mission']['start_time'] is not None:
                            mission = {
                                'start_time': self.stats['current_mission']['start_time'],
                                'end_time': self.get_local_time(),
                                'start_voltage': self.stats['current_mission']['start_voltage'],
                                'end_voltage': self.stats['last_voltage'],
                                'start_cpu_temp': self.stats['current_mission']['start_cpu_temp'],
                                'end_cpu_temp': self.stats['cpu_temp'],
                                'total_ah': self.stats['current_mission']['total_ah'],
                                'start_uptime': self.stats['current_mission'].get('start_uptime', self.stats['total_minutes']),
                                'end_uptime': self.stats['total_minutes']
                            }
                            self.missions.append(mission)
                            logger.info(f"Mission completed: {mission}")
                        
                        # Start new mission
                        self.stats['current_mission'] = {
                            'start_time': self.get_local_time(),
                            'start_voltage': current_voltage,
                            'start_cpu_temp': current_cpu_temp if current_cpu_temp > 0 else 0.0,
                            'end_voltage': current_voltage,
                            'end_cpu_temp': current_cpu_temp if current_cpu_temp > 0 else 0.0,
                            'total_ah': 0.0,
                            'start_uptime': self.stats['total_minutes'],
                            'end_uptime': self.stats['total_minutes']
                        }
                        
                        # Battery swap detected
                        self.stats['battery_swaps'] += 1
                        logger.info(f"Battery swap detected! Starting new mission.")
                        
                        # Reset voltage tracking for the new battery
                        self.stats['voltage_sum'] = current_voltage
                        self.stats['voltage_count'] = 1
                        avg_voltage = current_voltage
                        
                        # Recalculate wh_consumed for the new battery (should be near zero)
                        wh_consumed = (abs(current_consumed) / 1000.0) * avg_voltage
                    
                    # Update current battery watt-hours (energy consumed from current battery)
                    self.stats['current_battery_wh'] = wh_consumed
                    
                    # Update lifetime total (previous batteries + current battery)
                    self.stats['total_wh_consumed'] = self.stats['previous_batteries_wh'] + self.stats['current_battery_wh']
                    
                    # Update current mission stats
                    if self.stats['current_mission']['start_time'] is None:
                        self.stats['current_mission'] = {
                            'start_time': self.get_local_time(),
                            'start_voltage': current_voltage,
                            'start_cpu_temp': current_cpu_temp if current_cpu_temp > 0 else 0.0,
                            'end_voltage': current_voltage,
                            'end_cpu_temp': current_cpu_temp if current_cpu_temp > 0 else 0.0,
                            'total_ah': 0.0,
                            'start_uptime': self.stats['total_minutes'],
                            'end_uptime': self.stats['total_minutes']
                        }
                    
                    # Update mission end values
                    self.stats['current_mission']['end_voltage'] = current_voltage
                    if current_cpu_temp > 0:
                        self.stats['current_mission']['end_cpu_temp'] = current_cpu_temp
                    self.stats['current_mission']['total_ah'] = abs(current_consumed) / 1000.0  # Convert mAh to Ah
                    self.stats['current_mission']['end_uptime'] = self.stats['total_minutes']
                    
                    # Log energy consumption
                    logger.info(f"Energy - Current battery: {self.stats['current_battery_wh']:.2f}Wh, Previous batteries: {self.stats['previous_batteries_wh']:.2f}Wh, Lifetime total: {self.stats['total_wh_consumed']:.2f}Wh")
                    
                    # Update last values
                    self.stats['last_current_consumed'] = current_consumed
                    self.stats['last_voltage'] = current_voltage
                
                # Update CPU temperature if valid
                if current_cpu_temp > 0:
                    self.stats['cpu_temp'] = current_cpu_temp
                
                # Write to CSV
                self.write_stats_to_csv(time_status)
            
            # Update the last update time
            self.last_update_time = current_time
            
            # Send stats to Mavlink
            self.send_stats_to_mavlink()
        
        except Exception as e:
            logger.error(f"Error updating stats: {e}")
    
    def get_local_time(self) -> datetime.datetime:
        """Get the local time from the system-information endpoint"""
        try:
            response = requests.get('http://host.docker.internal/system-information/system/unix_time_seconds', timeout=2)
            if response.status_code == 200:
                unix_time = float(response.text)
                return datetime.datetime.fromtimestamp(unix_time)
        except Exception as e:
            logger.warning(f"Failed to get local time from system-information endpoint: {e}")
        
        # Fallback to system time if endpoint is not available
        return datetime.datetime.now()

    def write_stats_to_csv(self, time_status="normal", startup_detected=False):
        """Write the current stats to the CSV file.
        
        Note: This method should be called while holding self.stats_lock or with
        a copy of the stats values to ensure thread safety.
        """
        # Only write valid CPU temperature values to CSV
        cpu_temp_value = str(self.stats['cpu_temp']) if self.stats['cpu_temp'] > 0 else ''
        
        # Get local time from system-information endpoint
        local_time = self.get_local_time()
        
        # Create row with all fields, converting all values to strings
        # Note: wh_consumed column stores previous_batteries_wh (the base for lifetime total)
        # total_wh_consumed = previous_batteries_wh + current_battery_wh (calculated live)
        row = [
            local_time.isoformat(),
            str(self.stats['total_minutes']),
            str(self.stats['armed_minutes']),
            str(self.stats['disarmed_minutes']),
            str(self.stats['battery_swaps']),
            str(self.stats['startups']),
            str(self.stats['last_voltage']),
            cpu_temp_value,
            str(self.stats['previous_batteries_wh']),
            str(self.stats['current_mission']['total_ah']),  # Add current mission's amp-hours
            time_status + (" (startup)" if startup_detected else "")
        ]
        
        with open(ODOMETER_CSV, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(row)
    
    def get_vehicle_status(self) -> Tuple[float, bool, float]:
        """Get the vehicle's current voltage, armed status, and current consumed from Mavlink2Rest"""
        voltage = 0.0
        is_armed = False
        current_consumed = 0.0
        
        # Try each endpoint until we get a successful response
        for endpoint in MAVLINK_ENDPOINTS:
            try:
                # Get battery status from BATTERY_STATUS message
                battery_status_url = f"{endpoint}/BATTERY_STATUS"
                logger.info(f"Trying to get BATTERY_STATUS from {battery_status_url}")
                battery_status_response = requests.get(battery_status_url, timeout=2)
                
                if battery_status_response.status_code == 200:
                    # The structure depends on which endpoint we're using
                    battery_status_data = battery_status_response.json()
                    
                    # Try to handle different response formats
                    if 'message' in battery_status_data:
                        battery_status = battery_status_data.get("message", {})
                    else:
                        battery_status = battery_status_data
                        
                    # Extract voltage and current consumed
                    if 'voltages' in battery_status and len(battery_status.get('voltages', [])) > 0:
                        voltage = battery_status.get('voltages')[0] / 1000.0  # Convert from mV to V
                    
                    if 'current_consumed' in battery_status:
                        # Handle negative values - they represent actual consumption
                        current_consumed = abs(float(battery_status.get('current_consumed', 0)))
                        logger.info(f"Raw current_consumed: {battery_status.get('current_consumed')}, Processed: {current_consumed}")
                    
                    # Get armed status from HEARTBEAT message
                    heartbeat_url = f"{endpoint}/HEARTBEAT"
                    heartbeat_response = requests.get(heartbeat_url, timeout=2)
                    
                    if heartbeat_response.status_code == 200:
                        # Parse out the nested structure according to documentation
                        heartbeat_data = heartbeat_response.json()
                        
                        # Try to handle different response formats
                        if 'message' in heartbeat_data:
                            heartbeat = heartbeat_data.get("message", {})
                        else:
                            heartbeat = heartbeat_data
                        
                        # Handle the nested structure - base_mode is an object with a 'bits' field
                        base_mode_obj = heartbeat.get("base_mode", {})
                        if isinstance(base_mode_obj, dict):
                            base_mode = base_mode_obj.get("bits", 0)
                        else:
                            base_mode = base_mode_obj  # Fallback for older API versions
                            
                        is_armed = bool(base_mode & ARMED_FLAG)  # Check if the ARMED flag is set
                        
                        logger.info(f"Successfully got vehicle status from {endpoint}: voltage={voltage}V, armed={is_armed}, current_consumed={current_consumed}mAh")
                        return voltage, is_armed, current_consumed
            
            except requests.exceptions.RequestException as e:
                logger.warning(f"Failed to connect to mavlink endpoint {endpoint}: {e}")
                continue
            except Exception as e:
                logger.warning(f"Error processing mavlink data from {endpoint}: {e}")
                continue
        
        logger.error(f"Could not get vehicle status from any mavlink endpoint")
        return voltage, is_armed, current_consumed
    
    def send_stats_to_mavlink(self):
        """Send odometer stats to Mavlink as named float values"""
        stats_to_send = {
            "ODO_UPTM": self.stats['total_minutes'],
            "ODO_WH": self.stats['total_wh_consumed']  # Add watt-hours consumed
        }
        
        for name, value in stats_to_send.items():
            self.send_to_mavlink(name, float(value))
    
    def send_to_mavlink(self, name, value):
        """Send a named value float to Mavlink2Rest."""
        # Create name array of exactly 10 characters (as required by MAVLink)
        name_array = []
        for i in range(10):
            if i < len(name):
                name_array.append(name[i])
            else:
                name_array.append('\u0000')
        
        payload = {
            "header": {
                "system_id": 255,
                "component_id": 0,
                "sequence": 0
            },
            "message": {
                "type": "NAMED_VALUE_FLOAT",
                "time_boot_ms": 0,
                "value": value,
                "name": name_array
            }
        }
        
        # Try each POST endpoint - for the new endpoint structure, we need different URLs
        post_endpoints = [
            'http://host.docker.internal/mavlink2rest/mavlink', # Primary endpoint
            'http://host.docker.internal:6040/v1/mavlink',      # Backup endpoint
            'http://192.168.2.2/mavlink2rest/mavlink',          # Standard BlueOS IP
            'http://localhost/mavlink2rest/mavlink',
            'http://blueos.local/mavlink2rest/mavlink'
        ]
        
        for post_url in post_endpoints:
            try:
                response = requests.post(post_url, json=payload, timeout=2.0)
                if response.status_code == 200:
                    logger.info(f"Successfully sent {name}={value} to Mavlink2Rest via {post_url}")
                    return True
                else:
                    logger.warning(f"Failed to send to {post_url} with status code {response.status_code}")
                    continue  # Try next endpoint
            except Exception as e:
                logger.warning(f"Failed to send {name}={value} to {post_url}: {e}")
                continue  # Try next endpoint
        
        logger.error(f"Could not send {name}={value} to any Mavlink2Rest endpoint")
        return False

    def get_cpu_temperature(self) -> float:
        """Get the current CPU temperature in Celsius"""
        try:
            if CPU_TEMP_PATH.exists():
                with open(CPU_TEMP_PATH, 'r') as f:
                    temp = float(f.read().strip()) / 1000.0  # Convert millidegrees to degrees
                    # Validate the temperature - don't return zero or unreasonable values
                    if temp <= 0 or temp > 125:  # Most CPUs can't exceed 125°C without damage
                        logger.warning(f"Invalid CPU temperature reading: {temp}°C")
                        return -1.0  # Return negative value to indicate invalid reading
                    return round(temp, 1)
            
            # Fallback for non-Raspberry Pi systems or if temp file doesn't exist
            logger.warning("CPU temperature file not found")
            return -1.0
        except Exception as e:
            logger.warning(f"Failed to read CPU temperature: {e}")
            return -1.0

# Initialize the service
odometer_service = OdometerService()

# Start WebSocket server in background thread for Cockpit data lake
websocket_thread = threading.Thread(target=start_websocket_server, daemon=True)
websocket_thread.start()

@app.route('/stats')
def get_stats():
    """Get the current odometer statistics"""
    with odometer_service.stats_lock:
        return jsonify({
            "status": "success",
            "data": odometer_service.stats
        })

@app.route('/maintenance')
def get_maintenance():
    """Get the maintenance log"""
    maintenance_records = []
    
    if MAINTENANCE_CSV.exists():
        with open(MAINTENANCE_CSV, 'r', newline='') as f:
            reader = csv.reader(f)
            headers = next(reader)  # Skip header row
            for row in reader:
                if len(row) >= 3:
                    maintenance_records.append({
                        "timestamp": row[0],
                        "event_type": row[1],
                        "details": row[2]
                    })
    
    return jsonify({
        "status": "success",
        "data": maintenance_records
    })

@app.route('/maintenance', methods=['POST'])
def add_maintenance():
    """Add a new maintenance record"""
    data = request.json
    event_type = data.get('event_type', '').strip()
    details = data.get('details', '').strip()
    
    if not event_type or not details:
        return jsonify({"status": "error", "message": "Event type and details are required"}), 400
    
    # Sanitize inputs to prevent CSV injection
    # Remove leading characters that could be interpreted as formulas
    if details and details[0] in ('=', '+', '-', '@', '\t', '\r'):
        details = "'" + details
    
    # Get local time from system-information endpoint (use global odometer_service)
    timestamp = odometer_service.get_local_time().isoformat()
    
    with open(MAINTENANCE_CSV, 'a', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([timestamp, event_type, details])
    
    return jsonify({"status": "success", "message": "Maintenance record added"})

@app.route('/maintenance/update', methods=['POST'])
def update_maintenance():
    """Update an existing maintenance record timestamp"""
    data = request.json
    original_timestamp = data.get('original_timestamp')
    new_timestamp = data.get('new_timestamp')
    
    if not original_timestamp or not new_timestamp:
        return jsonify({"status": "error", "message": "Original and new timestamps are required"}), 400
    
    try:
        # Read all records
        records = []
        with open(MAINTENANCE_CSV, 'r', newline='') as f:
            reader = csv.reader(f)
            headers = next(reader)  # Skip header row
            for row in reader:
                records.append(row)
        
        # Find and update the matching record
        updated = False
        for record in records:
            if record[0] == original_timestamp:
                record[0] = new_timestamp
                updated = True
                break
        
        if not updated:
            return jsonify({"status": "error", "message": "Record not found"}), 404
        
        # Write all records back to the file
        with open(MAINTENANCE_CSV, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(headers)  # Write header row
            writer.writerows(records)  # Write all records
        
        return jsonify({"status": "success", "message": "Maintenance record updated"})
    
    except Exception as e:
        logger.error(f"Error updating maintenance record: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/maintenance/delete', methods=['POST'])
def delete_maintenance():
    """Delete an existing maintenance record"""
    data = request.json
    timestamp = data.get('timestamp')
    
    if not timestamp:
        return jsonify({"status": "error", "message": "Timestamp is required"}), 400
    
    try:
        # Read all records
        records = []
        with open(MAINTENANCE_CSV, 'r', newline='') as f:
            reader = csv.reader(f)
            headers = next(reader)  # Skip header row
            for row in reader:
                records.append(row)
        
        # Filter out the record to delete
        original_count = len(records)
        records = [record for record in records if record[0] != timestamp]
        
        # Check if we actually found and removed a record
        if len(records) == original_count:
            return jsonify({"status": "error", "message": "Record not found"}), 404
        
        # Write all remaining records back to the file
        with open(MAINTENANCE_CSV, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(headers)  # Write header row
            writer.writerows(records)  # Write all records
        
        return jsonify({"status": "success", "message": "Maintenance record deleted"})
    
    except Exception as e:
        logger.error(f"Error deleting maintenance record: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/download/odometer')
def download_odometer():
    """Get the odometer data as CSV for download"""
    if not ODOMETER_CSV.exists():
        return jsonify({"status": "error", "message": "Odometer data file does not exist"}), 404
    
    return send_file(
        ODOMETER_CSV,
        mimetype='text/csv',
        as_attachment=True,
        download_name='odometer_data.csv'
    )

@app.route('/download/maintenance')
def download_maintenance():
    """Get the maintenance data as CSV for download"""
    if not MAINTENANCE_CSV.exists():
        return jsonify({"status": "error", "message": "Maintenance data file does not exist"}), 404
    
    return send_file(
        MAINTENANCE_CSV,
        mimetype='text/csv',
        as_attachment=True,
        download_name='maintenance_data.csv'
    )

@app.route('/register_service')
def register_service():
    """Register the extension as a service in BlueOS."""
    response = jsonify(REGISTER_SERVICE)
    response.headers['X-Frame-Options'] = 'ALLOWALL'
    return response

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def catch_all(path):
    """Serve static files or fall back to index.html for SPA routing"""
    # First check if the requested path exists in the static folder
    static_path = os.path.join(app.static_folder, path)
    if os.path.exists(static_path) and os.path.isfile(static_path):
        return send_from_directory(app.static_folder, path)
    
    # Otherwise, serve index.html for SPA routing
    return send_from_directory(app.static_folder, 'index.html')

@app.route('/clear_history', methods=['POST'])
def clear_history():
    """Clear temperature and voltage history while preserving uptime data"""
    try:
        # Check if the odometer file exists
        if not ODOMETER_CSV.exists():
            return jsonify({"status": "error", "message": "Odometer data file does not exist"}), 404
        
        # Read all existing data
        rows = []
        with open(ODOMETER_CSV, 'r', newline='') as f:
            reader = csv.reader(f)
            headers = next(reader)  # Get the header row
            rows.append(headers)  # Keep the header row
            
            # Process each data row
            for row in reader:
                if len(row) >= 8:  # Ensure the row has enough columns
                    # Zero out voltage and CPU temp values but keep all timing data
                    row[6] = "0.0"  # voltage
                    if len(row) > 7:
                        row[7] = ""  # cpu_temp
                    rows.append(row)
        
        # Write back the modified data
        with open(ODOMETER_CSV, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerows(rows)
        
        # Also update the current stats
        with odometer_service.stats_lock:
            odometer_service.stats['last_voltage'] = 0.0
        
        return jsonify({"status": "success", "message": "Temperature and voltage history cleared successfully"})
    
    except Exception as e:
        logger.error(f"Error clearing history: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/missions')
def get_missions():
    """Get the list of completed missions"""
    with odometer_service.stats_lock:
        return jsonify({
            "status": "success",
            "data": {
                "current_mission": odometer_service.stats['current_mission'],
                "completed_missions": odometer_service.missions
            }
        })

# If run directly, start the app
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
