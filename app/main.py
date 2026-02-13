#!/usr/bin/env python3

import os
import csv
import time
import json
import datetime
from datetime import timezone
import logging
import logging.handlers
import threading
import asyncio
import requests
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from flask import Flask, jsonify, request, send_from_directory, send_file

from pdf_report import generate_report
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
MISSIONS_CSV = DATA_DIR / 'missions.csv'
CURRENT_SESSION_FILE = DATA_DIR / 'current_session.json'
STARTUP_MARKER = DATA_DIR / '.startup_marker'
THRUSTERS_FILE = DATA_DIR / 'thrusters.json'
ACCESSORIES_FILE = DATA_DIR / 'accessories.json'
VEHICLE_FILE = DATA_DIR / 'vehicle.json'
CPU_TEMP_PATH = Path('/sys/class/thermal/thermal_zone0/temp')

# MAV_TYPE enum values for vehicle detection
MAV_TYPE_SUBMARINE = 12      # ArduSub: 4-8 thrusters
MAV_TYPE_GROUND_ROVER = 10   # ArduRover: 2 thrusters (BlueBoat)
MAV_TYPE_SURFACE_BOAT = 16   # Surface boat: 2 thrusters

# Define potential Mavlink endpoints to try
MAVLINK_ENDPOINTS = [
    'http://host.docker.internal/mavlink2rest/mavlink/vehicles/1/components/1/messages',  # Primary endpoint
    'http://host.docker.internal:6040/v1/mavlink',  # Backup endpoint
    'http://192.168.2.2/mavlink2rest/mavlink/vehicles/1/components/1/messages',  # Standard BlueOS IP
    'http://localhost/mavlink2rest/mavlink/vehicles/1/components/1/messages',
    'http://blueos.local/mavlink2rest/mavlink/vehicles/1/components/1/messages'
]

UPDATE_INTERVAL = 60  # Update every 60 seconds (1 minute)
PWM_SAMPLE_INTERVAL = 5  # Sample PWM values every 5 seconds when armed
ARMED_FLAG = 128  # MAV_MODE_FLAG_SAFETY_ARMED (0b10000000)
MAX_TIME_JUMP_MINUTES = 5  # Maximum acceptable time jump in minutes
PORT = 80  # Port to run the server on
WEBSOCKET_PORT = 8765  # Port for Cockpit data lake streaming
WEBSOCKET_UPDATE_INTERVAL = 1.0  # Seconds between WebSocket updates
DIVE_DEPTH_THRESHOLD = 1.0  # Depth threshold in meters to count as diving
BATTERY_SWAP_VOLTAGE_THRESHOLD = 1.0  # Voltage increase (V) to detect battery swap on reconnect

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
                dive_minutes = odometer_service.stats.get("dive_minutes", 0)
                total_wh = odometer_service.stats.get("total_wh_consumed", 0.0)
                current_depth = odometer_service.stats.get("last_depth", 0.0)

            await websocket.send(f"odometer-armed-minutes={armed_minutes}")
            await websocket.send(f"odometer-disarmed-minutes={disarmed_minutes}")
            await websocket.send(f"odometer-dive-minutes={dive_minutes}")
            await websocket.send(f"odometer-total-wh={total_wh:.3f}")
            await websocket.send(f"odometer-depth={current_depth:.2f}")

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
            'dive_minutes': 0,  # Minutes spent at depth > DIVE_DEPTH_THRESHOLD
            'battery_swaps': 0,
            'startups': 0,
            'last_voltage': 0.0,
            'last_depth': 0.0,  # Current depth in meters (positive = underwater)
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
                'end_uptime': 0,
                'voltage_min': 0.0,
                'max_pwm_deviation': 0.0
            },
            'pending_battery_swap_check': False  # Set on startup when previous session had voltage drop
        }
        self.missions = []  # List to store completed missions
        self.thruster_stats = {
            'thruster_count': 0,
            'mav_type': -1,
            'thrusters': []  # [{'run_minutes': 0, 'avg_pwm_sum': 0, 'avg_pwm_count': 0}, ...]
        }
        self.thruster_lock = threading.Lock()
        self.accessories = {}  # {id: {'name': str, 'channel': int, 'run_minutes': int, 'avg_pwm_sum': float, 'avg_pwm_count': int}}
        self.accessory_lock = threading.Lock()
        self._next_accessory_id = 1
        self.last_update_time = time.time()
        self.minutes_since_update = 0
        self.setup_csv_files()
        self.load_stats()
        self.load_missions()
        self.load_thruster_stats()
        self.load_accessories()
        self.close_previous_session_on_startup()
        self.detect_startup()
        
        # Start the update thread
        self.update_thread = threading.Thread(target=self.update_loop)
        self.update_thread.daemon = True
        self.update_thread.start()
        
        # Start PWM sampling thread (runs every 5 seconds when armed)
        self.pwm_sample_thread = threading.Thread(target=self.pwm_sample_loop)
        self.pwm_sample_thread.daemon = True
        self.pwm_sample_thread.start()
    
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
                
                needs_upgrade = False
                
                # Check if this is an old format file (has mah_consumed column or missing dive_minutes)
                if 'mah_consumed' in headers or 'dive_minutes' not in headers:
                    needs_upgrade = True
                
                if needs_upgrade:
                    logger.info("Detected old format CSV file, upgrading to new format with dive tracking")
                    
                    # Read all existing data
                    rows = []
                    for row in reader:
                        rows.append(row)
                    
                    # Write back with new format
                    with open(ODOMETER_CSV, 'w', newline='') as f:
                        writer = csv.writer(f)
                        # Write new header row with dive_minutes and depth
                        writer.writerow(['timestamp', 'total_minutes', 'armed_minutes', 'disarmed_minutes', 
                                       'dive_minutes', 'battery_swaps', 'startups', 'voltage', 'depth',
                                       'cpu_temp', 'wh_consumed', 'current_ah', 'time_status'])
                        
                        # Write existing data, adding dive_minutes and depth columns
                        for row in rows:
                            if len(row) < 4:
                                continue  # Skip malformed rows
                            
                            # Build new row with dive_minutes inserted after disarmed_minutes
                            new_row = [
                                row[0] if len(row) > 0 else '',  # timestamp
                                row[1] if len(row) > 1 else '0',  # total_minutes
                                row[2] if len(row) > 2 else '0',  # armed_minutes
                                row[3] if len(row) > 3 else '0',  # disarmed_minutes
                                '0',  # dive_minutes (new, default to 0)
                                row[4] if len(row) > 4 else '0',  # battery_swaps
                                row[5] if len(row) > 5 else '0',  # startups
                                row[6] if len(row) > 6 else '0.0',  # voltage
                                '0.0',  # depth (new, default to 0)
                                row[7] if len(row) > 7 else '',  # cpu_temp
                                row[8] if len(row) > 8 else '0.0',  # wh_consumed
                                row[9] if len(row) > 9 else '0.0',  # current_ah
                                row[10] if len(row) > 10 else 'normal'  # time_status
                            ]
                            writer.writerow(new_row)
                    
                    logger.info("Successfully upgraded CSV file to new format with dive tracking")
        except Exception as e:
            logger.error(f"Error upgrading CSV format: {e}")
    
    def upgrade_maintenance_csv_format(self):
        """Upgrade maintenance CSV to add thruster_ids, reset_run_hours, device_id, device_name, device_channel if missing"""
        try:
            if not MAINTENANCE_CSV.exists():
                return
            with open(MAINTENANCE_CSV, 'r', newline='') as f:
                reader = csv.reader(f)
                headers = next(reader, [])
                if 'thruster_ids' in headers and 'reset_run_hours' in headers and 'device_id' in headers:
                    return
                rows = list(reader)
            new_headers = ['timestamp', 'event_type', 'details', 'thruster_ids', 'reset_run_hours',
                          'device_id', 'device_name', 'device_channel', 'reset_accessory']
            with open(MAINTENANCE_CSV, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(new_headers)
                for row in rows:
                    if len(row) >= 3:
                        thruster_ids = row[3] if len(row) > 3 else ''
                        reset_run_hours = row[4] if len(row) > 4 else ''
                        new_row = [row[0], row[1], row[2], thruster_ids, reset_run_hours, '', '', '', '']
                        writer.writerow(new_row)
            logger.info("Upgraded maintenance CSV to new format with thruster and accessory support")
        except Exception as e:
            logger.error(f"Error upgrading maintenance CSV format: {e}")
    
    def setup_csv_files(self):
        # Set up odometer CSV file
        if not ODOMETER_CSV.exists():
            with open(ODOMETER_CSV, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['timestamp', 'total_minutes', 'armed_minutes', 'disarmed_minutes', 
                               'dive_minutes', 'battery_swaps', 'startups', 'voltage', 'depth',
                               'cpu_temp', 'wh_consumed', 'current_ah', 'time_status'])
        else:
            # Check if this is an old format file and upgrade it if needed
            self.upgrade_csv_format()
        
        # Set up maintenance CSV file
        if not MAINTENANCE_CSV.exists():
            with open(MAINTENANCE_CSV, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['timestamp', 'event_type', 'details', 'thruster_ids', 'reset_run_hours',
                               'device_id', 'device_name', 'device_channel', 'reset_accessory'])
        else:
            self.upgrade_maintenance_csv_format()
        
        # Set up missions CSV file for persistent usage history
        if not MISSIONS_CSV.exists():
            with open(MISSIONS_CSV, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['start_time', 'end_time', 'start_voltage', 'end_voltage',
                               'start_cpu_temp', 'end_cpu_temp', 'total_ah', 'start_uptime', 'end_uptime',
                               'voltage_min', 'max_pwm_deviation', 'hard_use'])
    
    def load_missions(self):
        """Load completed missions from persistent storage"""
        if MISSIONS_CSV.exists():
            try:
                with open(MISSIONS_CSV, 'r', newline='') as f:
                    reader = csv.reader(f)
                    headers = next(reader, None)
                    if headers:
                        for row in reader:
                            if len(row) >= 9:
                                m = {
                                    'start_time': row[0],
                                    'end_time': row[1],
                                    'start_voltage': float(row[2]) if row[2].strip() else 0.0,
                                    'end_voltage': float(row[3]) if row[3].strip() else 0.0,
                                    'start_cpu_temp': float(row[4]) if row[4].strip() else 0.0,
                                    'end_cpu_temp': float(row[5]) if row[5].strip() else 0.0,
                                    'total_ah': float(row[6]) if row[6].strip() else 0.0,
                                    'start_uptime': int(row[7]) if row[7].strip() else 0,
                                    'end_uptime': int(row[8]) if row[8].strip() else 0
                                }
                                if len(row) >= 12:
                                    m['voltage_min'] = float(row[9]) if row[9].strip() else 0.0
                                    m['max_pwm_deviation'] = float(row[10]) if row[10].strip() else 0.0
                                    m['hard_use'] = (row[11].strip().lower() in ('true', '1', 'yes')) if len(row) > 11 else False
                                self.missions.append(m)
                logger.info(f"Loaded {len(self.missions)} missions from {MISSIONS_CSV}")
            except Exception as e:
                logger.error(f"Error loading missions: {e}")
    
    def load_thruster_stats(self):
        """Load thruster stats from JSON file"""
        if THRUSTERS_FILE.exists():
            try:
                with open(THRUSTERS_FILE, 'r') as f:
                    data = json.load(f)
                with self.thruster_lock:
                    self.thruster_stats['thruster_count'] = data.get('thruster_count', 0)
                    self.thruster_stats['mav_type'] = data.get('mav_type', -1)
                    self.thruster_stats['thrusters'] = data.get('thrusters', [])
                logger.info(f"Loaded thruster stats: {self.thruster_stats['thruster_count']} thrusters")
            except Exception as e:
                logger.error(f"Error loading thruster stats: {e}")
    
    def save_thruster_stats(self):
        """Save thruster stats to JSON file"""
        try:
            with self.thruster_lock:
                data = {
                    'thruster_count': self.thruster_stats['thruster_count'],
                    'mav_type': self.thruster_stats['mav_type'],
                    'thrusters': self.thruster_stats['thrusters']
                }
            with open(THRUSTERS_FILE, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving thruster stats: {e}")
    
    def load_vehicle(self) -> dict:
        """Load vehicle name from JSON file"""
        if VEHICLE_FILE.exists():
            try:
                with open(VEHICLE_FILE, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Error loading vehicle: {e}")
        return {'name': ''}
    
    def save_vehicle(self, data: dict) -> None:
        """Save vehicle name to JSON file"""
        try:
            with open(VEHICLE_FILE, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving vehicle: {e}")
    
    def load_accessories(self) -> None:
        """Load accessories from JSON file"""
        if ACCESSORIES_FILE.exists():
            try:
                with open(ACCESSORIES_FILE, 'r') as f:
                    data = json.load(f)
                raw = data.get('accessories', {})
                with self.accessory_lock:
                    self.accessories = {}
                    for k, v in raw.items():
                        kid = str(k)
                        self.accessories[kid] = {
                            'name': v.get('name', ''),
                            'channel': int(v.get('channel', 1)),
                            'run_minutes': int(v.get('run_minutes', 0)),
                            'avg_pwm_sum': float(v.get('avg_pwm_sum', 0)),
                            'avg_pwm_count': int(v.get('avg_pwm_count', 0))
                        }
                    self._next_accessory_id = max(
                        (int(k) for k in self.accessories if str(k).isdigit()), default=0
                    ) + 1
                logger.info(f"Loaded {len(self.accessories)} accessories")
            except Exception as e:
                logger.error(f"Error loading accessories: {e}")
    
    def save_accessories(self) -> None:
        """Save accessories to JSON file"""
        try:
            with self.accessory_lock:
                data = {'accessories': self.accessories}
            with open(ACCESSORIES_FILE, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving accessories: {e}")
    
    def get_thruster_count_from_mav_type(self, mav_type: int) -> int:
        """Map MAV_TYPE to thruster count. ArduSub: 4-8 (default 8), Boat/Rover: 2"""
        if mav_type == MAV_TYPE_SUBMARINE:
            return 8  # ArduSub: default to 8, covers most frames
        if mav_type in (MAV_TYPE_GROUND_ROVER, MAV_TYPE_SURFACE_BOAT):
            return 2  # BlueBoat/ArduRover
        return 0
    
    def get_layout_config(self, mav_type: int, thruster_count: int) -> Dict[str, Any]:
        """
        Return layout config for position grids. ROVs: vertical + horizontal tables.
        Boats: 2x1 (port, starboard). Layout is list of grids; each grid is list of rows.
        Convention: odd thrusters (1,3,5,7) on starboard (right), even (2,4,6,8) on port (left).
        """
        is_rov = mav_type == MAV_TYPE_SUBMARINE
        is_boat = mav_type in (MAV_TYPE_GROUND_ROVER, MAV_TYPE_SURFACE_BOAT)
        
        if is_boat and thruster_count == 2:
            return {
                'unit': 'motor',
                'unit_plural': 'motors',
                'grids': [
                    {'label': 'Port / Starboard', 'rows': [[2, 1]]}  # Motor 1=starboard, Motor 2=port
                ]
            }
        if is_rov:
            if thruster_count == 4:
                return {
                    'unit': 'thruster',
                    'unit_plural': 'thrusters',
                    'grids': [
                        {'label': 'Horizontal', 'rows': [[2, 1], [4, 3]]}  # Port|Starboard per row
                    ]
                }
            if thruster_count == 5:
                return {
                    'unit': 'thruster',
                    'unit_plural': 'thrusters',
                    'grids': [
                        {'label': 'Horizontal', 'rows': [[2, 1], [4, 3]]},
                        {'label': 'Vertical', 'rows': [[5]]}
                    ]
                }
            if thruster_count == 6:
                return {
                    'unit': 'thruster',
                    'unit_plural': 'thrusters',
                    'grids': [
                        {'label': 'Horizontal', 'rows': [[2, 1], [4, 3]]},
                        {'label': 'Vertical', 'rows': [[6, 5]]}  # Port|Starboard
                    ]
                }
            if thruster_count == 8:
                return {
                    'unit': 'thruster',
                    'unit_plural': 'thrusters',
                    'grids': [
                        {'label': 'Vertical', 'rows': [[2, 1], [4, 3]]},
                        {'label': 'Horizontal', 'rows': [[6, 5], [8, 7]]}
                    ]
                }
            # Default for 3 or other ROV counts
            return {
                'unit': 'thruster',
                'unit_plural': 'thrusters',
                'grids': [
                    {'label': 'Thrusters', 'rows': [list(range(1, thruster_count + 1))]}
                ]
            }
        return {
            'unit': 'thruster',
            'unit_plural': 'thrusters',
            'grids': [{'label': 'Channels', 'rows': [list(range(1, thruster_count + 1))]}]
        }
    
    def add_accessory(self, name: str, channel: int) -> str:
        """Add a new accessory. Returns the new accessory ID."""
        channel = max(1, min(16, int(channel)))
        with self.accessory_lock:
            aid = str(self._next_accessory_id)
            self._next_accessory_id += 1
            self.accessories[aid] = {
                'name': str(name).strip() or f'Device {aid}',
                'channel': channel,
                'run_minutes': 0,
                'avg_pwm_sum': 0.0,
                'avg_pwm_count': 0
            }
            self.save_accessories()
        return aid
    
    def rename_accessory(self, accessory_id: str, new_name: str) -> bool:
        """Rename an accessory. Returns True if found."""
        with self.accessory_lock:
            if accessory_id in self.accessories:
                self.accessories[accessory_id]['name'] = str(new_name).strip() or self.accessories[accessory_id]['name']
                self.save_accessories()
                return True
        return False
    
    def reset_accessory_run_hours(self, accessory_id: str) -> None:
        """Reset run minutes and PWM average for an accessory"""
        with self.accessory_lock:
            if accessory_id in self.accessories:
                self.accessories[accessory_id]['run_minutes'] = 0
                self.accessories[accessory_id]['avg_pwm_sum'] = 0.0
                self.accessories[accessory_id]['avg_pwm_count'] = 0
                self.save_accessories()
    
    def reset_thruster_run_hours(self, thruster_ids: List[int]) -> None:
        """Reset run minutes and PWM averages for specified thrusters (1-indexed)"""
        with self.thruster_lock:
            for tid in thruster_ids:
                idx = tid - 1  # Convert to 0-indexed
                if 0 <= idx < len(self.thruster_stats['thrusters']):
                    self.thruster_stats['thrusters'][idx]['run_minutes'] = 0
                    self.thruster_stats['thrusters'][idx]['avg_pwm_sum'] = 0
                    self.thruster_stats['thrusters'][idx]['avg_pwm_count'] = 0
            self.save_thruster_stats()
    
    def _is_hard_use(self, mission: dict) -> bool:
        """Hard use: rapid discharge (voltage drop) or high duty (|avg_pwm - 1500| > 300)"""
        start_v = mission.get('start_voltage', 0) or 0
        end_v = mission.get('end_voltage', 0) or 0
        voltage_drop = start_v - end_v if (start_v > 0 and end_v > 0) else 0
        rapid_discharge = voltage_drop > 2.0  # >2V drop in session
        max_dev = mission.get('max_pwm_deviation', 0) or 0
        high_duty = max_dev > 300
        return rapid_discharge or high_duty
    
    def delete_mission(self, start_time: str) -> bool:
        """Delete a mission by start_time. Returns True if found and deleted."""
        with self.stats_lock:
            original = len(self.missions)
            self.missions = [m for m in self.missions if m.get('start_time') != start_time]
            if len(self.missions) == original:
                return False
            self._rewrite_missions_csv()
        return True

    def clear_missions(self) -> None:
        """Clear all completed missions. Does not affect current_mission."""
        with self.stats_lock:
            self.missions = []
            self._rewrite_missions_csv()
        logger.info("Cleared all completed missions")

    def _rewrite_missions_csv(self) -> None:
        """Rewrite MISSIONS_CSV with current missions list."""
        headers = ['start_time', 'end_time', 'start_voltage', 'end_voltage',
                   'start_cpu_temp', 'end_cpu_temp', 'total_ah', 'start_uptime', 'end_uptime',
                   'voltage_min', 'max_pwm_deviation', 'hard_use']
        with open(MISSIONS_CSV, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for m in self.missions:
                writer.writerow([
                    m.get('start_time', ''),
                    m.get('end_time', ''),
                    str(m.get('start_voltage', 0)),
                    str(m.get('end_voltage', 0)),
                    str(m.get('start_cpu_temp', 0)),
                    str(m.get('end_cpu_temp', 0)),
                    str(m.get('total_ah', 0)),
                    str(m.get('start_uptime', 0)),
                    str(m.get('end_uptime', 0)),
                    str(m.get('voltage_min', 0)),
                    str(m.get('max_pwm_deviation', 0)),
                    'true' if m.get('hard_use') else 'false'
                ])

    def save_mission(self, mission: dict):
        """Append a single mission to persistent storage"""
        try:
            hard_use = self._is_hard_use(mission)
            voltage_min = mission.get('voltage_min', 0) or 0
            max_pwm = mission.get('max_pwm_deviation', 0) or 0
            with open(MISSIONS_CSV, 'a', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    mission.get('start_time', ''),
                    mission.get('end_time', ''),
                    str(mission.get('start_voltage', 0)),
                    str(mission.get('end_voltage', 0)),
                    str(mission.get('start_cpu_temp', 0)),
                    str(mission.get('end_cpu_temp', 0)),
                    str(mission.get('total_ah', 0)),
                    str(mission.get('start_uptime', 0)),
                    str(mission.get('end_uptime', 0)),
                    str(voltage_min),
                    str(max_pwm),
                    'true' if hard_use else 'false'
                ])
        except Exception as e:
            logger.error(f"Error saving mission: {e}")
    
    def persist_current_session(self):
        """Save current mission/session to disk so it survives power-off"""
        try:
            mission = self.stats['current_mission']
            if mission.get('start_time') is not None:
                with open(CURRENT_SESSION_FILE, 'w') as f:
                    json.dump(mission, f, default=str)
        except Exception as e:
            logger.error(f"Error persisting current session: {e}")
    
    def close_previous_session_on_startup(self):
        """
        On startup, close out the session that was running when the system was powered off.
        This records each power-on block as an entry in usage history.
        Also sets pending_battery_swap_check if voltage decreased during that session
        (will increment battery_swaps when we see high voltage on reconnect).
        """
        if not CURRENT_SESSION_FILE.exists():
            return
        
        try:
            with open(CURRENT_SESSION_FILE, 'r') as f:
                prev_session = json.load(f)
            
            start_time = prev_session.get('start_time')
            if not start_time:
                return
            
            # Get end state from last row of odometer CSV
            end_time = None
            end_voltage = 0.0
            end_cpu_temp = 0.0
            end_uptime = 0
            
            if ODOMETER_CSV.exists():
                with open(ODOMETER_CSV, 'r', newline='') as f:
                    reader = csv.reader(f)
                    headers = next(reader, [])
                    has_dive_minutes = 'dive_minutes' in headers
                    last_row = None
                    for row in reader:
                        if row and not all(cell.strip() == '' for cell in row):
                            last_row = row
                    
                    if last_row:
                        end_time = last_row[0] if last_row else None
                        if has_dive_minutes and len(last_row) >= 10:
                            end_voltage = float(last_row[7]) if last_row[7].strip() else 0.0
                            end_cpu_temp = float(last_row[9]) if last_row[9].strip() else 0.0
                            end_uptime = int(last_row[1]) if last_row[1].strip() else 0  # total_minutes
                        elif len(last_row) >= 9:
                            end_voltage = float(last_row[6]) if last_row[6].strip() else 0.0
                            end_cpu_temp = float(last_row[7]) if last_row[7].strip() else 0.0
                            end_uptime = int(last_row[1]) if last_row[1].strip() else 0
            
            if not end_time:
                end_time = self.get_local_time().isoformat()
            
            # Build completed mission
            mission = {
                'start_time': start_time,
                'end_time': end_time,
                'start_voltage': float(prev_session.get('start_voltage', 0)),
                'end_voltage': end_voltage,
                'start_cpu_temp': float(prev_session.get('start_cpu_temp', 0)),
                'end_cpu_temp': end_cpu_temp,
                'total_ah': float(prev_session.get('total_ah', 0)),
                'start_uptime': prev_session.get('start_uptime', 0),
                'end_uptime': end_uptime,
                'voltage_min': float(prev_session.get('voltage_min', 0) or 0),
                'max_pwm_deviation': float(prev_session.get('max_pwm_deviation', 0) or 0)
            }
            
            self.missions.append(mission)
            self.save_mission(mission)
            logger.info(f"Recorded previous power-on session to usage history: {mission}")
            
            # Check if voltage decreased during that session - if so, battery swap likely on reconnect
            start_voltage = mission['start_voltage']
            if start_voltage > 0 and end_voltage > 0 and end_voltage < start_voltage:
                with self.stats_lock:
                    self.stats['pending_battery_swap_check'] = True
                    self.stats['last_voltage'] = end_voltage  # For comparison when we get first reading
                logger.info(f"Voltage decreased during previous session ({start_voltage}V -> {end_voltage}V); will check for battery swap on first voltage reading")
            
            # Remove the session file so we don't process it again
            CURRENT_SESSION_FILE.unlink(missing_ok=True)
            
        except Exception as e:
            logger.error(f"Error closing previous session on startup: {e}")
    
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
                
                # Determine format based on headers
                has_dive_minutes = 'dive_minutes' in headers
                min_columns = 11 if has_dive_minutes else 9
                
                for row in reader:
                    # Skip empty rows or rows with all empty values
                    if not row or all(cell.strip() == '' for cell in row):
                        continue
                    
                    # Skip rows that don't have minimum required columns
                    if len(row) < min_columns:
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
                        
                        if has_dive_minutes:
                            # New format with dive_minutes
                            if row[4].strip(): int(row[4])  # dive_minutes
                            if row[5].strip(): int(row[5])  # battery_swaps
                            if row[6].strip(): int(row[6])  # startups
                            if row[7].strip(): float(row[7])  # voltage
                            if row[8].strip(): float(row[8])  # depth
                            if row[9].strip(): float(row[9])  # cpu_temp
                            if row[10].strip(): float(row[10])  # wh_consumed
                            if len(row) > 11 and row[11].strip(): float(row[11])  # current_ah
                        else:
                            # Old format without dive_minutes
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
                headers = next(reader)  # Get header row to determine format
                last_row = None
                for row in reader:
                    # Skip empty rows or rows with all empty values
                    if not row or all(cell.strip() == '' for cell in row):
                        continue
                    last_row = row
                
                if last_row:
                    with self.stats_lock:
                        # Determine if this is new format (with dive_minutes) or old format
                        has_dive_minutes = 'dive_minutes' in headers
                        
                        if has_dive_minutes:
                            # New format: timestamp, total_minutes, armed_minutes, disarmed_minutes,
                            # dive_minutes, battery_swaps, startups, voltage, depth, cpu_temp, wh_consumed, current_ah, time_status
                            self.stats['total_minutes'] = int(last_row[1]) if len(last_row) > 1 and last_row[1].strip() else 0
                            self.stats['armed_minutes'] = int(last_row[2]) if len(last_row) > 2 and last_row[2].strip() else 0
                            self.stats['disarmed_minutes'] = int(last_row[3]) if len(last_row) > 3 and last_row[3].strip() else 0
                            self.stats['dive_minutes'] = int(last_row[4]) if len(last_row) > 4 and last_row[4].strip() else 0
                            self.stats['battery_swaps'] = int(last_row[5]) if len(last_row) > 5 and last_row[5].strip() else 0
                            self.stats['startups'] = int(last_row[6]) if len(last_row) > 6 and last_row[6].strip() else 0
                            self.stats['last_voltage'] = float(last_row[7]) if len(last_row) > 7 and last_row[7].strip() else 0.0
                            self.stats['last_depth'] = float(last_row[8]) if len(last_row) > 8 and last_row[8].strip() else 0.0
                            self.stats['cpu_temp'] = float(last_row[9]) if len(last_row) > 9 and last_row[9].strip() else 0.0
                            
                            # Load accumulated watt-hours from previous batteries (index 10)
                            if len(last_row) > 10 and last_row[10].strip():
                                try:
                                    self.stats['previous_batteries_wh'] = float(last_row[10])
                                    self.stats['total_wh_consumed'] = self.stats['previous_batteries_wh']
                                except (ValueError, TypeError):
                                    self.stats['previous_batteries_wh'] = 0.0
                                    self.stats['total_wh_consumed'] = 0.0
                            else:
                                self.stats['previous_batteries_wh'] = 0.0
                                self.stats['total_wh_consumed'] = 0.0
                        else:
                            # Old format without dive_minutes - indices are different
                            self.stats['total_minutes'] = int(last_row[1]) if last_row[1].strip() else 0
                            self.stats['armed_minutes'] = int(last_row[2]) if last_row[2].strip() else 0
                            self.stats['disarmed_minutes'] = int(last_row[3]) if last_row[3].strip() else 0
                            self.stats['dive_minutes'] = 0  # Not tracked in old format
                            self.stats['battery_swaps'] = int(last_row[4]) if len(last_row) > 4 and last_row[4].strip() else 0
                            
                            if len(last_row) > 5 and last_row[5].strip():
                                self.stats['startups'] = int(last_row[5])
                            else:
                                self.stats['startups'] = 0
                            
                            if len(last_row) > 6:
                                self.stats['last_voltage'] = float(last_row[6]) if last_row[6].strip() else 0.0
                            else:
                                self.stats['last_voltage'] = 0.0
                            
                            self.stats['last_depth'] = 0.0  # Not tracked in old format
                            
                            if len(last_row) > 7 and last_row[7].strip():
                                try:
                                    self.stats['cpu_temp'] = float(last_row[7])
                                except (ValueError, TypeError):
                                    self.stats['cpu_temp'] = 0.0
                            else:
                                self.stats['cpu_temp'] = 0.0
                            
                            # Load accumulated watt-hours (index 8 in old format)
                            if len(last_row) > 8 and last_row[8].strip():
                                try:
                                    self.stats['previous_batteries_wh'] = float(last_row[8])
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
    
    def pwm_sample_loop(self):
        """Sample PWM values every 5 seconds when armed for duty-cycle averaging (thrusters + accessories)"""
        while not self.stop_event.is_set():
            try:
                time.sleep(PWM_SAMPLE_INTERVAL)
                if not self.get_armed_status():
                    continue
                servo_values = self.get_servo_output_raw()
                thruster_stats_changed = False
                with self.thruster_lock:
                    thruster_count = self.thruster_stats['thruster_count']
                    if thruster_count > 0:
                        for i in range(min(thruster_count, len(servo_values))):
                            pwm = servo_values[i]
                            if pwm > 0:
                                t = self.thruster_stats['thrusters'][i]
                                t['avg_pwm_sum'] = t.get('avg_pwm_sum', 0) + pwm
                                t['avg_pwm_count'] = t.get('avg_pwm_count', 0) + 1
                                thruster_stats_changed = True
                if thruster_stats_changed:
                    self.save_thruster_stats()
                # Sample accessory channels (avg PWM only; run_minutes updated per minute in update_stats)
                accessory_changed = False
                with self.accessory_lock:
                    for aid, acc in self.accessories.items():
                        ch = acc.get('channel', 1)
                        idx = max(0, min(ch - 1, len(servo_values) - 1))
                        pwm = servo_values[idx] if idx < len(servo_values) else 0
                        if pwm > 0:
                            acc['avg_pwm_sum'] = acc.get('avg_pwm_sum', 0) + pwm
                            acc['avg_pwm_count'] = acc.get('avg_pwm_count', 0) + 1
                            accessory_changed = True
                if accessory_changed:
                    self.save_accessories()
                # Session PWM for hard-use: max deviation from 1500 (neutral)
                max_dev = 0
                for pwm in servo_values:
                    if pwm > 0:
                        dev = abs(pwm - 1500)
                        if dev > max_dev:
                            max_dev = dev
                if max_dev > 0:
                    with self.stats_lock:
                        m = self.stats['current_mission']
                        cur = m.get('max_pwm_deviation', 0)
                        if max_dev > cur:
                            m['max_pwm_deviation'] = max_dev
            except Exception as e:
                logger.debug(f"PWM sample loop: {e}")
    
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
            
            # Get current voltage, armed status, current consumed, depth, and mav_type
            current_voltage, is_armed, current_consumed, current_depth, mav_type = self.get_vehicle_status()
            
            # Get current CPU temperature
            current_cpu_temp = self.get_cpu_temperature()
            
            # Update thruster stats: detect vehicle type, ensure thruster array exists, update run minutes and PWM
            thruster_count = self.get_thruster_count_from_mav_type(mav_type)
            thruster_stats_changed = False
            if thruster_count > 0:
                with self.thruster_lock:
                    if (self.thruster_stats['thruster_count'] != thruster_count or 
                        len(self.thruster_stats['thrusters']) != thruster_count):
                        self.thruster_stats['thruster_count'] = thruster_count
                        self.thruster_stats['mav_type'] = mav_type
                        # Preserve existing thruster data where indices overlap
                        old_thrusters = {i: t for i, t in enumerate(self.thruster_stats['thrusters'])}
                        self.thruster_stats['thrusters'] = [
                            old_thrusters.get(i, {'run_minutes': 0, 'avg_pwm_sum': 0, 'avg_pwm_count': 0})
                            for i in range(thruster_count)
                        ]
                        thruster_stats_changed = True
                    
                    if is_armed:
                        for i in range(min(thruster_count, 8)):
                            self.thruster_stats['thrusters'][i]['run_minutes'] = \
                                self.thruster_stats['thrusters'][i].get('run_minutes', 0) + 1
                        thruster_stats_changed = True
                
                if thruster_stats_changed:
                    self.save_thruster_stats()
            
            # Accessory run minutes (1 per real minute when armed)
            if is_armed:
                with self.accessory_lock:
                    acc_changed = False
                    for acc in self.accessories.values():
                        acc['run_minutes'] = acc.get('run_minutes', 0) + 1
                        acc_changed = True
                    if acc_changed:
                        self.save_accessories()
            
            with self.stats_lock:
                # Update total minutes
                self.stats['total_minutes'] += 1  # Always add 1 minute regardless of time jump
                
                # Update armed/disarmed minutes
                if is_armed:
                    self.stats['armed_minutes'] += 1
                else:
                    self.stats['disarmed_minutes'] += 1
                
                # Update dive minutes if depth exceeds threshold
                if current_depth >= DIVE_DEPTH_THRESHOLD:
                    self.stats['dive_minutes'] += 1
                    logger.info(f"Dive time incremented: depth={current_depth}m >= {DIVE_DEPTH_THRESHOLD}m threshold")
                
                # Store current depth
                self.stats['last_depth'] = current_depth
                
                # Check for battery swap on startup (voltage decreased last session, high now)
                if self.stats.get('pending_battery_swap_check') and current_voltage > 0:
                    last_v = self.stats['last_voltage']
                    if last_v > 0 and current_voltage > (last_v + BATTERY_SWAP_VOLTAGE_THRESHOLD):
                        self.stats['battery_swaps'] += 1
                        logger.info(f"Battery swap detected on reconnect: voltage was {last_v}V at shutdown, now {current_voltage}V")
                    self.stats['pending_battery_swap_check'] = False
                
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
                                'end_uptime': self.stats['total_minutes'],
                                'voltage_min': self.stats['current_mission'].get('voltage_min', 0) or 0,
                                'max_pwm_deviation': self.stats['current_mission'].get('max_pwm_deviation', 0) or 0
                            }
                            self.missions.append(mission)
                            self.save_mission(mission)
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
                            'end_uptime': self.stats['total_minutes'],
                            'voltage_min': current_voltage,
                            'max_pwm_deviation': 0.0
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
                            'end_uptime': self.stats['total_minutes'],
                            'voltage_min': current_voltage,
                            'max_pwm_deviation': 0.0
                        }
                    
                    # Update mission end values
                    self.stats['current_mission']['end_voltage'] = current_voltage
                    if current_cpu_temp > 0:
                        self.stats['current_mission']['end_cpu_temp'] = current_cpu_temp
                    self.stats['current_mission']['total_ah'] = abs(current_consumed) / 1000.0  # Convert mAh to Ah
                    self.stats['current_mission']['end_uptime'] = self.stats['total_minutes']
                    # Track session voltage min for hard-use detection
                    vmin = self.stats['current_mission'].get('voltage_min', 0) or 0
                    if vmin == 0 or current_voltage < vmin:
                        self.stats['current_mission']['voltage_min'] = current_voltage
                    
                    # Log energy consumption
                    logger.info(f"Energy - Current battery: {self.stats['current_battery_wh']:.2f}Wh, Previous batteries: {self.stats['previous_batteries_wh']:.2f}Wh, Lifetime total: {self.stats['total_wh_consumed']:.2f}Wh")
                    
                    # Update last values
                    self.stats['last_current_consumed'] = current_consumed
                    self.stats['last_voltage'] = current_voltage
                else:
                    # No vehicle/voltage (e.g. bench test without MAVLink) - still track session for usage history
                    if self.stats['current_mission']['start_time'] is None:
                        self.stats['current_mission'] = {
                            'start_time': self.get_local_time(),
                            'start_voltage': 0.0,
                            'start_cpu_temp': current_cpu_temp if current_cpu_temp > 0 else 0.0,
                            'end_voltage': 0.0,
                            'end_cpu_temp': current_cpu_temp if current_cpu_temp > 0 else 0.0,
                            'total_ah': 0.0,
                            'start_uptime': self.stats['total_minutes'],
                            'end_uptime': self.stats['total_minutes'],
                            'voltage_min': 0.0,
                            'max_pwm_deviation': 0.0
                        }
                    else:
                        self.stats['current_mission']['end_cpu_temp'] = current_cpu_temp if current_cpu_temp > 0 else self.stats['current_mission']['end_cpu_temp']
                        self.stats['current_mission']['end_uptime'] = self.stats['total_minutes']
                
                # Update CPU temperature if valid
                if current_cpu_temp > 0:
                    self.stats['cpu_temp'] = current_cpu_temp
                
                # Persist current session so it survives power-off (enables usage history on next boot)
                self.persist_current_session()
                
                # Write to CSV
                self.write_stats_to_csv(time_status)
            
            # Update the last update time
            self.last_update_time = current_time
            
            # Send stats to Mavlink
            self.send_stats_to_mavlink()
        
        except Exception as e:
            logger.error(f"Error updating stats: {e}")
    
    def get_local_time(self) -> datetime.datetime:
        """Get the local time from the system-information endpoint. Returns timezone-aware UTC."""
        try:
            response = requests.get('http://host.docker.internal/system-information/system/unix_time_seconds', timeout=2)
            if response.status_code == 200:
                unix_time = float(response.text)
                return datetime.datetime.fromtimestamp(unix_time, tz=timezone.utc)
        except Exception as e:
            logger.warning(f"Failed to get local time from system-information endpoint: {e}")
        
        # Fallback to system time if endpoint is not available
        return datetime.datetime.now(timezone.utc)

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
        # Format: timestamp, total_minutes, armed_minutes, disarmed_minutes, dive_minutes,
        # battery_swaps, startups, voltage, depth, cpu_temp, wh_consumed, current_ah, time_status
        row = [
            local_time.isoformat(),
            str(self.stats['total_minutes']),
            str(self.stats['armed_minutes']),
            str(self.stats['disarmed_minutes']),
            str(self.stats['dive_minutes']),
            str(self.stats['battery_swaps']),
            str(self.stats['startups']),
            str(self.stats['last_voltage']),
            str(self.stats['last_depth']),
            cpu_temp_value,
            str(self.stats['previous_batteries_wh']),
            str(self.stats['current_mission']['total_ah']),
            time_status + (" (startup)" if startup_detected else "")
        ]
        
        with open(ODOMETER_CSV, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(row)
    
    def _parse_mav_type(self, heartbeat: dict) -> int:
        """Extract MAV_TYPE from heartbeat. Returns -1 if unknown."""
        mavtype_obj = heartbeat.get('mavtype', heartbeat.get('type'))
        if mavtype_obj is None:
            return -1
        if isinstance(mavtype_obj, dict):
            type_val = mavtype_obj.get('type', mavtype_obj.get('value', -1))
        else:
            type_val = mavtype_obj
        if isinstance(type_val, int):
            return type_val
        if isinstance(type_val, str):
            type_map = {
                'MAV_TYPE_SUBMARINE': MAV_TYPE_SUBMARINE,
                'MAV_TYPE_GROUND_ROVER': MAV_TYPE_GROUND_ROVER,
                'MAV_TYPE_SURFACE_BOAT': MAV_TYPE_SURFACE_BOAT,
            }
            return type_map.get(type_val.upper(), -1)
        return -1
    
    def get_armed_status(self) -> bool:
        """Lightweight check: is vehicle armed? Fetches HEARTBEAT only."""
        for endpoint in MAVLINK_ENDPOINTS:
            try:
                resp = requests.get(f"{endpoint}/HEARTBEAT", timeout=2)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                heartbeat = data.get('message', data)
                base_mode_obj = heartbeat.get("base_mode", {})
                base_mode = base_mode_obj.get("bits", 0) if isinstance(base_mode_obj, dict) else base_mode_obj
                return bool(base_mode & ARMED_FLAG)
            except Exception:
                continue
        return False
    
    def get_servo_output_raw(self) -> List[int]:
        """Get PWM values from SERVO_OUTPUT_RAW (servo1_raw..servo16_raw in us). 
        Channels 1-8 from primary port; 9-16 from aux port if available."""
        values = [0] * 16
        for endpoint in MAVLINK_ENDPOINTS:
            try:
                # Primary port: servo1_raw..servo8_raw
                url = f"{endpoint}/SERVO_OUTPUT_RAW"
                resp = requests.get(url, timeout=2)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                msg = data.get('message', data)
                for i in range(1, 9):
                    key = f'servo{i}_raw'
                    val = msg.get(key, 0)
                    values[i - 1] = int(val) if val is not None else 0
                # Try aux port for channels 9-16 (SERVO_OUTPUT_RAW.port=1 typically)
                # Some MAVLink2Rest implementations include servo9_raw..servo16_raw in same message
                for i in range(9, 17):
                    key = f'servo{i}_raw'
                    val = msg.get(key, 0)
                    if val is not None:
                        values[i - 1] = int(val)
                return values
            except Exception as e:
                logger.debug(f"SERVO_OUTPUT_RAW from {endpoint}: {e}")
                continue
        return values
    
    def get_vehicle_status(self) -> Tuple[float, bool, float, float, int]:
        """Get the vehicle's current voltage, armed status, current consumed, depth, and mav_type from Mavlink2Rest"""
        voltage = 0.0
        is_armed = False
        current_consumed = 0.0
        depth = 0.0
        mav_type = -1
        
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
                        mav_type = self._parse_mav_type(heartbeat)
                    
                    # Get depth from VFR_HUD message (alt field)
                    # For underwater vehicles, alt is negative when submerged
                    vfr_hud_url = f"{endpoint}/VFR_HUD"
                    vfr_hud_response = requests.get(vfr_hud_url, timeout=2)
                    
                    if vfr_hud_response.status_code == 200:
                        vfr_hud_data = vfr_hud_response.json()
                        
                        # Try to handle different response formats
                        if 'message' in vfr_hud_data:
                            vfr_hud = vfr_hud_data.get("message", {})
                        else:
                            vfr_hud = vfr_hud_data
                        
                        # Get altitude - negative values indicate depth underwater
                        alt = float(vfr_hud.get("alt", 0.0))
                        # Convert to positive depth (negative altitude = positive depth)
                        depth = -alt if alt < 0 else 0.0
                        logger.info(f"VFR_HUD alt: {alt}m, depth: {depth}m")
                    
                    logger.info(f"Successfully got vehicle status from {endpoint}: voltage={voltage}V, armed={is_armed}, current_consumed={current_consumed}mAh, depth={depth}m, mav_type={mav_type}")
                    return voltage, is_armed, current_consumed, depth, mav_type
            
            except requests.exceptions.RequestException as e:
                logger.warning(f"Failed to connect to mavlink endpoint {endpoint}: {e}")
                continue
            except Exception as e:
                logger.warning(f"Error processing mavlink data from {endpoint}: {e}")
                continue
        
        logger.error(f"Could not get vehicle status from any mavlink endpoint")
        return voltage, is_armed, current_consumed, depth, mav_type
    
    def send_stats_to_mavlink(self):
        """Send odometer stats to Mavlink as named float values"""
        stats_to_send = {
            "ODO_UPTM": self.stats['total_minutes'],
            "ODO_WH": self.stats['total_wh_consumed'],
            "ODO_DIVE": self.stats['dive_minutes']  # Dive time in minutes
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

@app.route('/vehicle')
def get_vehicle():
    """Get vehicle name"""
    data = odometer_service.load_vehicle()
    return jsonify({"status": "success", "data": data})

@app.route('/vehicle', methods=['POST'])
def post_vehicle():
    """Update vehicle name"""
    data = request.json or {}
    name = data.get('name', '').strip()
    odometer_service.save_vehicle({'name': name})
    return jsonify({"status": "success", "message": "Vehicle name updated"})

@app.route('/accessories')
def get_accessories():
    """Get accessories list with run minutes and avg PWM"""
    with odometer_service.accessory_lock:
        acc_list = []
        for aid, acc in odometer_service.accessories.items():
            pwm_sum = acc.get('avg_pwm_sum', 0)
            pwm_count = acc.get('avg_pwm_count', 0)
            avg_pwm = round(pwm_sum / pwm_count, 1) if pwm_count > 0 else 0
            acc_list.append({
                'id': aid,
                'name': acc.get('name', ''),
                'channel': acc.get('channel', 1),
                'run_minutes': acc.get('run_minutes', 0),
                'avg_pwm_armed': avg_pwm
            })
        return jsonify({"status": "success", "data": acc_list})

@app.route('/accessories/<accessory_id>/rename', methods=['POST'])
def rename_accessory(accessory_id):
    """Rename an accessory"""
    data = request.json or {}
    new_name = data.get('name', '').strip()
    if not new_name:
        return jsonify({"status": "error", "message": "Name is required"}), 400
    if odometer_service.rename_accessory(accessory_id, new_name):
        return jsonify({"status": "success", "message": "Accessory renamed"})
    return jsonify({"status": "error", "message": "Accessory not found"}), 404

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
            headers = next(reader, [])
            has_thruster_cols = 'thruster_ids' in headers
            has_device_cols = 'device_id' in headers
            for row in reader:
                if len(row) >= 3:
                    rec = {
                        "timestamp": row[0],
                        "event_type": row[1],
                        "details": row[2]
                    }
                    if has_thruster_cols and len(row) >= 5:
                        try:
                            rec["thruster_ids"] = json.loads(row[3]) if row[3].strip() else []
                        except (json.JSONDecodeError, TypeError):
                            rec["thruster_ids"] = []
                        rec["reset_run_hours"] = (row[4].strip().lower() in ('true', '1', 'yes')) if len(row) > 4 else False
                    else:
                        rec["thruster_ids"] = []
                        rec["reset_run_hours"] = False
                    if has_device_cols and len(row) >= 9:
                        rec["device_id"] = row[5].strip() if len(row) > 5 else ''
                        rec["device_name"] = row[6].strip() if len(row) > 6 else ''
                        rec["device_channel"] = int(row[7]) if len(row) > 7 and row[7].strip() and str(row[7]).isdigit() else 0
                        rec["reset_accessory"] = (row[8].strip().lower() in ('true', '1', 'yes')) if len(row) > 8 else False
                    else:
                        rec["device_id"] = ''
                        rec["device_name"] = ''
                        rec["device_channel"] = 0
                        rec["reset_accessory"] = False
                    maintenance_records.append(rec)
    
    return jsonify({
        "status": "success",
        "data": maintenance_records
    })

@app.route('/maintenance', methods=['POST'])
def add_maintenance():
    """Add a new maintenance record"""
    data = request.json or {}
    event_type = data.get('event_type', '').strip()
    details = data.get('details', '').strip()
    thruster_ids = data.get('thruster_ids', [])
    reset_run_hours = bool(data.get('reset_run_hours', False))
    device_name = data.get('device_name', '').strip()
    device_channel = data.get('device_channel', 1)
    device_id = data.get('device_id', '').strip()
    reset_accessory = bool(data.get('reset_accessory', False))
    
    if not event_type:
        return jsonify({"status": "error", "message": "Event type is required"}), 400
    
    if event_type == 'Add Device':
        if not device_name:
            return jsonify({"status": "error", "message": "Device name is required for Add Device"}), 400
        device_channel = max(1, min(16, int(device_channel) if device_channel else 1))
        aid = odometer_service.add_accessory(device_name, device_channel)
        details = f"Added {device_name} on channel {device_channel}"
    elif not details:
        return jsonify({"status": "error", "message": "Details are required"}), 400
    
    # Sanitize thruster_ids: ensure list of ints, 1-indexed
    if not isinstance(thruster_ids, list):
        thruster_ids = []
    thruster_ids = [int(x) for x in thruster_ids if isinstance(x, (int, float, str)) and str(x).isdigit()]
    
    # Sanitize inputs to prevent CSV injection
    if details and details[0] in ('=', '+', '-', '@', '\t', '\r'):
        details = "'" + details
    
    # Get local time from system-information endpoint (use global odometer_service)
    timestamp = odometer_service.get_local_time().isoformat()
    
    # If reset_run_hours requested, reset thruster run hours for selected thrusters
    if reset_run_hours and thruster_ids:
        odometer_service.reset_thruster_run_hours(thruster_ids)
        logger.info(f"Reset run hours for thrusters {thruster_ids} via maintenance record")
    
    if reset_accessory and device_id:
        odometer_service.reset_accessory_run_hours(device_id)
        logger.info(f"Reset run hours for accessory {device_id} via maintenance record")
    
    thruster_ids_json = json.dumps(thruster_ids) if thruster_ids else ''
    reset_run_hours_str = 'true' if reset_run_hours else 'false'
    device_id_val = aid if event_type == 'Add Device' else device_id
    device_name_val = device_name if event_type == 'Add Device' else ''
    device_channel_val = str(device_channel) if event_type == 'Add Device' else ''
    reset_accessory_str = 'true' if reset_accessory else 'false'
    
    with open(MAINTENANCE_CSV, 'a', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([timestamp, event_type, details, thruster_ids_json, reset_run_hours_str,
                        device_id_val, device_name_val, device_channel_val, reset_accessory_str])
    
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

@app.route('/export/pdf')
def export_pdf():
    """Generate and download PDF report"""
    try:
        vehicle = odometer_service.load_vehicle()
        vehicle_name = vehicle.get('name', '')
        with odometer_service.stats_lock:
            stats = dict(odometer_service.stats)
        maintenance_records = []
        if MAINTENANCE_CSV.exists():
            with open(MAINTENANCE_CSV, 'r', newline='') as f:
                reader = csv.reader(f)
                headers = next(reader, [])
                for row in reader:
                    if len(row) >= 3:
                        rec = {"timestamp": row[0], "event_type": row[1], "details": row[2]}
                        maintenance_records.append(rec)
        with odometer_service.thruster_lock:
            thruster_data = dict(odometer_service.thruster_stats)
            layout = odometer_service.get_layout_config(
                thruster_data.get('mav_type', -1),
                thruster_data.get('thruster_count', 0)
            )
            thruster_data['layout'] = layout
            thrusters = []
            for i, t in enumerate(thruster_data.get('thrusters', [])):
                pwm_sum = t.get('avg_pwm_sum', 0)
                pwm_count = t.get('avg_pwm_count', 0)
                avg_pwm = round(pwm_sum / pwm_count, 1) if pwm_count > 0 else 0
                thrusters.append({
                    'id': i + 1,
                    'run_minutes': t.get('run_minutes', 0),
                    'avg_pwm_armed': avg_pwm
                })
            thruster_data['thrusters'] = thrusters
        with odometer_service.accessory_lock:
            accessories = []
            for aid, acc in odometer_service.accessories.items():
                pwm_sum = acc.get('avg_pwm_sum', 0)
                pwm_count = acc.get('avg_pwm_count', 0)
                avg_pwm = round(pwm_sum / pwm_count, 1) if pwm_count > 0 else 0
                accessories.append({
                    'id': aid,
                    'name': acc.get('name', ''),
                    'channel': acc.get('channel', 1),
                    'run_minutes': acc.get('run_minutes', 0),
                    'avg_pwm_armed': avg_pwm
                })
        with odometer_service.stats_lock:
            current_mission = dict(odometer_service.stats.get('current_mission', {}))
        missions = list(odometer_service.missions)
        pdf_bytes = generate_report(
            vehicle_name=vehicle_name,
            stats=stats,
            maintenance=maintenance_records,
            thrusters=thruster_data,
            accessories=accessories,
            missions=missions,
            current_mission=current_mission,
        )
        from io import BytesIO
        return send_file(
            BytesIO(pdf_bytes),
            mimetype='application/pdf',
            as_attachment=True,
            download_name=f'odometer_report_{odometer_service.get_local_time().strftime("%Y%m%d_%H%M")}.pdf'
        )
    except Exception as e:
        logger.error(f"Error generating PDF: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

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
            
            # Determine format based on headers
            has_dive_minutes = 'dive_minutes' in headers
            
            # Process each data row
            for row in reader:
                if has_dive_minutes:
                    # New format: voltage at index 7, depth at 8, cpu_temp at 9
                    if len(row) >= 10:
                        row[7] = "0.0"  # voltage
                        row[8] = "0.0"  # depth
                        row[9] = ""  # cpu_temp
                        rows.append(row)
                else:
                    # Old format: voltage at index 6, cpu_temp at 7
                    if len(row) >= 8:
                        row[6] = "0.0"  # voltage
                        row[7] = ""  # cpu_temp
                        rows.append(row)
        
        # Write back the modified data
        with open(ODOMETER_CSV, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerows(rows)
        
        # Also update the current stats
        with odometer_service.stats_lock:
            odometer_service.stats['last_voltage'] = 0.0
            odometer_service.stats['last_depth'] = 0.0
        
        return jsonify({"status": "success", "message": "Temperature, voltage, and depth history cleared successfully"})
    
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


@app.route('/missions/delete', methods=['POST'])
def delete_mission():
    """Delete a single completed mission by start_time"""
    data = request.json or {}
    start_time = data.get('start_time', '').strip()
    if not start_time:
        return jsonify({"status": "error", "message": "start_time is required"}), 400
    try:
        if not odometer_service.delete_mission(start_time):
            return jsonify({"status": "error", "message": "Mission not found"}), 404
        return jsonify({"status": "success", "message": "Mission deleted"})
    except Exception as e:
        logger.error(f"Error deleting mission: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/missions/clear', methods=['POST'])
def clear_missions():
    """Clear all completed missions (usage history). Does not affect current session."""
    try:
        odometer_service.clear_missions()
        return jsonify({"status": "success", "message": "Usage history cleared"})
    except Exception as e:
        logger.error(f"Error clearing missions: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/thrusters')
def get_thrusters():
    """Get per-thruster run hours and average PWM stats"""
    with odometer_service.thruster_lock:
        stats = odometer_service.thruster_stats
        thrusters = []
        for i, t in enumerate(stats['thrusters']):
            run_min = t.get('run_minutes', 0)
            pwm_sum = t.get('avg_pwm_sum', 0)
            pwm_count = t.get('avg_pwm_count', 0)
            avg_pwm = round(pwm_sum / pwm_count, 1) if pwm_count > 0 else 0
            thrusters.append({
                'id': i + 1,
                'run_minutes': run_min,
                'avg_pwm_armed': avg_pwm
            })
        mav_type = stats['mav_type']
        thruster_count = stats['thruster_count']
        layout = odometer_service.get_layout_config(mav_type, thruster_count)
        return jsonify({
            "status": "success",
            "data": {
                "thruster_count": thruster_count,
                "mav_type": mav_type,
                "layout": layout,
                "thrusters": thrusters
            }
        })


@app.route('/thrusters/reset', methods=['POST'])
def reset_thrusters():
    """Reset run hours for specified thruster IDs (1-indexed)"""
    data = request.json or {}
    thruster_ids = data.get('thruster_ids', [])
    if not thruster_ids:
        return jsonify({"status": "error", "message": "thruster_ids is required"}), 400
    thruster_ids = [int(x) for x in thruster_ids if isinstance(x, (int, float, str)) and str(x).isdigit()]
    odometer_service.reset_thruster_run_hours(thruster_ids)
    return jsonify({"status": "success", "message": f"Reset run hours for thrusters {thruster_ids}"})


# If run directly, start the app
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
