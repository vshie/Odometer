#!/usr/bin/env python3

import os
import csv
import time
import json
import datetime
import logging
import logging.handlers
import threading
import requests
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from litestar import Litestar, get, post, put, MediaType
from litestar.controller import Controller
from litestar.datastructures import State
from litestar.logging import LoggingConfig
from litestar.static_files.config import StaticFilesConfig
from litestar.response import FileResponse

# Set up logging
logging_config = LoggingConfig(
    loggers={
        __name__: dict(
            level='INFO',
            handlers=['queue_listener'],
        )
    },
)

log_dir = Path('/app/logs')
log_dir.mkdir(parents=True, exist_ok=True)
fh = logging.handlers.RotatingFileHandler(log_dir / 'lumber.log', maxBytes=2**16, backupCount=1)

# Constants
DATA_DIR = Path('/app/data')
DATA_DIR.mkdir(parents=True, exist_ok=True)
ODOMETER_CSV = DATA_DIR / 'odometer.csv'
MAINTENANCE_CSV = DATA_DIR / 'maintenance.csv'
STARTUP_MARKER = DATA_DIR / '.startup_marker'

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
PORT = 7042  # Port to run the server on

class OdometerController(Controller):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stop_event = threading.Event()
        self.stats_lock = threading.Lock()
        self.stats = {
            'total_minutes': 0,
            'armed_minutes': 0,
            'disarmed_minutes': 0,
            'battery_swaps': 0,
            'startups': 0,
            'last_voltage': 0.0,
        }
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
        """
        is_new_startup = not STARTUP_MARKER.exists()
        
        if is_new_startup:
            logging.info("Detected new vehicle startup")
            with self.stats_lock:
                self.stats['startups'] += 1
            
            # Create the marker file
            with open(STARTUP_MARKER, 'w') as f:
                f.write(datetime.datetime.now().isoformat())
            
            # Write the updated stats to CSV right away
            self.write_stats_to_csv(startup_detected=True)
    
    def setup_csv_files(self):
        # Set up odometer CSV file
        if not ODOMETER_CSV.exists():
            with open(ODOMETER_CSV, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['timestamp', 'total_minutes', 'armed_minutes', 'disarmed_minutes', 'battery_swaps', 'startups', 'voltage', 'time_status'])
        
        # Set up maintenance CSV file
        if not MAINTENANCE_CSV.exists():
            with open(MAINTENANCE_CSV, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['timestamp', 'event_type', 'details'])
    
    def load_stats(self):
        """Load the latest stats from the CSV file"""
        if ODOMETER_CSV.exists():
            with open(ODOMETER_CSV, 'r', newline='') as f:
                reader = csv.reader(f)
                headers = next(reader)  # Skip header row
                last_row = None
                for row in reader:
                    last_row = row
                
                if last_row:
                    with self.stats_lock:
                        self.stats['total_minutes'] = int(last_row[1])
                        self.stats['armed_minutes'] = int(last_row[2])
                        self.stats['disarmed_minutes'] = int(last_row[3])
                        self.stats['battery_swaps'] = int(last_row[4])
                        
                        # Handle loading "startups" field if it exists in the CSV
                        if len(last_row) > 5 and last_row[5]:
                            self.stats['startups'] = int(last_row[5])
                        
                        # Voltage is now at index 6 if startups field exists
                        if len(last_row) > 6:
                            self.stats['last_voltage'] = float(last_row[6])
                        else:
                            self.stats['last_voltage'] = float(last_row[5])
    
    def update_loop(self):
        """Main update loop that runs every minute"""
        while not self.stop_event.is_set():
            try:
                self.update_stats()
                # Sleep for the update interval
                time.sleep(UPDATE_INTERVAL)
            except Exception as e:
                logging.error(f"Error in update loop: {e}")
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
                logging.warning(f"Time jump detected! Diff: {time_diff_seconds/60:.2f} minutes. Using expected time interval.")
                minutes_to_add = expected_diff_seconds / 60
                time_status = "corrected"
            else:
                # Normal update, use actual time diff
                minutes_to_add = time_diff_seconds / 60
                time_status = "normal"
            
            # Get current voltage and armed status
            current_voltage, is_armed = self.get_vehicle_status()
            
            with self.stats_lock:
                # Update total minutes
                self.stats['total_minutes'] += 1  # Always add 1 minute regardless of time jump
                
                # Update armed/disarmed minutes
                if is_armed:
                    self.stats['armed_minutes'] += 1
                else:
                    self.stats['disarmed_minutes'] += 1
                
                # Check for battery swap
                if current_voltage > (self.stats['last_voltage'] + 1.0) and self.stats['last_voltage'] > 0:
                    self.stats['battery_swaps'] += 1
                
                # Update last voltage
                self.stats['last_voltage'] = current_voltage
                
                # Write to CSV
                self.write_stats_to_csv(time_status)
            
            # Update the last update time
            self.last_update_time = current_time
            
            # Send stats to Mavlink
            self.send_stats_to_mavlink()
        
        except Exception as e:
            logging.error(f"Error updating stats: {e}")
    
    def write_stats_to_csv(self, time_status="normal", startup_detected=False):
        """Write the current stats to the CSV file"""
        with open(ODOMETER_CSV, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.datetime.now().isoformat(),
                self.stats['total_minutes'],
                self.stats['armed_minutes'],
                self.stats['disarmed_minutes'],
                self.stats['battery_swaps'],
                self.stats['startups'],
                self.stats['last_voltage'],
                time_status + (" (startup)" if startup_detected else "")
            ])
    
    def get_vehicle_status(self) -> Tuple[float, bool]:
        """Get the vehicle's current voltage and armed status from Mavlink2Rest"""
        voltage = 0.0
        is_armed = False
        
        # Try each endpoint until we get a successful response
        for endpoint in MAVLINK_ENDPOINTS:
            try:
                # Get battery voltage from SYS_STATUS message
                sys_status_url = f"{endpoint}/SYS_STATUS"
                logging.info(f"Trying to get SYS_STATUS from {sys_status_url}")
                sys_status_response = requests.get(sys_status_url, timeout=2)
                
                if sys_status_response.status_code == 200:
                    # The structure depends on which endpoint we're using
                    sys_status_data = sys_status_response.json()
                    
                    # Try to handle different response formats
                    if 'message' in sys_status_data:
                        sys_status = sys_status_data.get("message", {})
                    else:
                        sys_status = sys_status_data
                        
                    # Extract voltage, which might be in different fields depending on the endpoint
                    if 'voltage_battery' in sys_status:
                        voltage = sys_status.get("voltage_battery", 0) / 1000.0  # Convert from mV to V
                    elif 'voltages' in sys_status and len(sys_status.get('voltages', [])) > 0:
                        voltage = sys_status.get('voltages')[0] / 1000.0
                    
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
                        
                        logging.info(f"Successfully got vehicle status from {endpoint}: voltage={voltage}V, armed={is_armed}")
                        return voltage, is_armed
            
            except requests.exceptions.RequestException as e:
                logging.warning(f"Failed to connect to mavlink endpoint {endpoint}: {e}")
                continue
            except Exception as e:
                logging.warning(f"Error processing mavlink data from {endpoint}: {e}")
                continue
        
        logging.error(f"Could not get vehicle status from any mavlink endpoint")
        return voltage, is_armed
    
    def send_stats_to_mavlink(self):
        """Send odometer stats to Mavlink as named float values"""
        stats_to_send = {
            "ODO_UPTM": self.stats['total_minutes'],
            "ODO_ARMM": self.stats['armed_minutes'],
            "ODO_DARM": self.stats['disarmed_minutes'],
            "ODO_BSWP": self.stats['battery_swaps'],
            "ODO_STRT": self.stats['startups']
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
                    logging.info(f"Successfully sent {name}={value} to Mavlink2Rest via {post_url}")
                    return True
                else:
                    logging.warning(f"Failed to send to {post_url} with status code {response.status_code}")
                    continue  # Try next endpoint
            except Exception as e:
                logging.warning(f"Failed to send {name}={value} to {post_url}: {e}")
                continue  # Try next endpoint
        
        logging.error(f"Could not send {name}={value} to any Mavlink2Rest endpoint")
        return False
    
    @get("/stats", sync_to_thread=False)
    def get_stats(self) -> Dict[str, Any]:
        """Get the current odometer statistics"""
        with self.stats_lock:
            return {
                "status": "success",
                "data": self.stats
            }
    
    @get("/maintenance", sync_to_thread=False)
    def get_maintenance(self) -> Dict[str, Any]:
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
        
        return {
            "status": "success",
            "data": maintenance_records
        }
    
    @post("/maintenance")
    async def add_maintenance(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Add a new maintenance record"""
        event_type = data.get('event_type')
        details = data.get('details')
        
        if not event_type or not details:
            return {"status": "error", "message": "Event type and details are required"}
        
        timestamp = datetime.datetime.now().isoformat()
        
        with open(MAINTENANCE_CSV, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([timestamp, event_type, details])
        
        return {"status": "success", "message": "Maintenance record added"}
    
    @get("/download/odometer", sync_to_thread=False)
    def download_odometer(self) -> Dict[str, Any]:
        """Get the odometer data as CSV for download"""
        if not ODOMETER_CSV.exists():
            return {"status": "error", "message": "Odometer data file does not exist"}
        
        with open(ODOMETER_CSV, 'r') as f:
            csv_data = f.read()
        
        return {"status": "success", "data": csv_data}
    
    @get("/download/maintenance", sync_to_thread=False)
    def download_maintenance(self) -> Dict[str, Any]:
        """Get the maintenance data as CSV for download"""
        if not MAINTENANCE_CSV.exists():
            return {"status": "error", "message": "Maintenance data file does not exist"}
        
        with open(MAINTENANCE_CSV, 'r') as f:
            csv_data = f.read()
        
        return {"status": "success", "data": csv_data}
    
    @get("/", sync_to_thread=False)
    def index(self) -> FileResponse:
        """Serve the main index.html file"""
        return FileResponse(path="static/index.html")
    
    @get("/register_service", sync_to_thread=False)
    def register_service(self) -> FileResponse:
        """Register the extension as a service in BlueOS."""
        response = FileResponse(path="static/register_service")
        # Add header to prevent BlueOS from wrapping the page
        response.headers['X-Frame-Options'] = 'ALLOWALL'
        return response
    
    @get("/{file_path:path}", sync_to_thread=False)
    def static_files(self, file_path: str) -> FileResponse:
        """Serve static files or fall back to index.html for SPA routing"""
        from pathlib import Path
        
        # First check if file exists in static directory
        static_file = Path(f"static/{file_path}")
        if static_file.exists() and static_file.is_file():
            return FileResponse(path=str(static_file))
        
        # If not found, try to serve index.html for SPA routing
        return FileResponse(path="static/index.html")

app = Litestar(
    route_handlers=[OdometerController],
    state=State({'bag_url':'http://host.docker.internal/bag/v1.0'}),
    static_files_config=[
        StaticFilesConfig(
            directories=['static'],  # Path relative to /app where the code runs in container
            path='/',
            html_mode=True,
            name="static"
        )
    ],
    logging_config=logging_config,
)

app.logger.addHandler(fh)

# If run directly, start the app
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
