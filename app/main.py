#!/usr/bin/env python3

import math
import os
import re
import csv
import shutil
import socket
import struct
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
from urllib.parse import urlparse
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
        logging.handlers.RotatingFileHandler(log_dir / 'odometer.log', maxBytes=2**16, backupCount=1),
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
STARTUP_MARKER = DATA_DIR / '.startup_marker'  # Legacy (never cleared, so startups stuck at 1); replaced by boot_id
LAST_BOOT_ID_FILE = DATA_DIR / 'last_boot_id'
BOOT_ID_PATH = Path('/proc/sys/kernel/random/boot_id')  # Kernel-wide, changes on every host reboot
THRUSTERS_FILE = DATA_DIR / 'thrusters.json'
ACCESSORIES_FILE = DATA_DIR / 'accessories.json'
VEHICLE_FILE = DATA_DIR / 'vehicle.json'
MODES_FILE = DATA_DIR / 'modes.json'
CPU_TEMP_PATH = Path('/sys/class/thermal/thermal_zone0/temp')
DISTANCE_SAMPLE_INTERVAL = 1  # Poll at 1 Hz when armed for distance tracking

# MAV_TYPE enum values for vehicle detection
MAV_TYPE_SUBMARINE = 12      # ArduSub: 4-8 thrusters
MAV_TYPE_GROUND_ROVER = 10   # ArduRover: 2 thrusters (BlueBoat)
MAV_TYPE_SURFACE_BOAT = 11   # Surface boat: 2 thrusters

# SERVOn_FUNCTION values used to map thrusters/motors to output channels
SERVO_FUNCTION_MOTORS = {33 + i: i + 1 for i in range(8)}  # Motor1..Motor8 -> thruster number
SERVO_FUNCTION_THROTTLE_LEFT = 73
SERVO_FUNCTION_THROTTLE_RIGHT = 74
THRUSTER_DETECT_INTERVAL = 600  # Re-read SERVOn_FUNCTION params every 10 minutes

# Hosts where BlueOS may be reachable from inside this container, in order of preference; the container's
# default gateway (the Docker host) is added at runtime. Not localhost: inside this container that is the
# extension itself, whose catch-all route answers every path with index.html.
BLUEOS_HOSTS = ['host.docker.internal', '192.168.2.2', 'blueos.local']
AUTOPILOT_COMPONENT_ID = 1
SYSID_DISCOVERY_INTERVAL = 10  # Seconds between attempts to find the autopilot's system ID
STALE_MESSAGE_SECONDS = 10.0  # mavlink2rest serves its last cached message forever; older than this = vehicle offline

UPDATE_INTERVAL = 60  # Update every 60 seconds (1 minute)
PWM_SAMPLE_INTERVAL = 5  # Sample PWM values every 5 seconds when armed
ARMED_FLAG = 128  # MAV_MODE_FLAG_SAFETY_ARMED (0b10000000)
MAX_TIME_JUMP_MINUTES = 5  # Maximum acceptable time jump in minutes
PORT = 80  # Port to run the server on
WEBSOCKET_PORT = 8765  # Port for Cockpit data lake streaming
WEBSOCKET_UPDATE_INTERVAL = 1.0  # Seconds between WebSocket updates
DIVE_DEPTH_THRESHOLD = 1.0  # Depth threshold in meters to count as diving
BATTERY_SWAP_VOLTAGE_THRESHOLD = 1.0  # Voltage increase (V) to detect battery swap on reconnect
MAX_PLAUSIBLE_SPEED_MS = 15.0  # Speed cap (m/s) to filter GPS glitches
MIN_SPEED_FOR_DISTANCE_MS = 0.2  # Ignore velocity noise below this when integrating distance (good GPS)
POOR_HDOP = 0.9  # Above this HDOP, GPS velocity noise grows (0.03-0.22 m/s seen at HDOP ~1.0-1.4 on a moored BlueBoat)
MAX_SPEED_FLOOR_MS = 0.5  # Cap on the HDOP-scaled speed floor
SCHEMA_FILE = DATA_DIR / 'schema.json'
SCHEMA_VERSION = 2  # v2: Ah history corrected for the cAh/mAh bug, per-session Wh, lifetime energy rebuilt
AH_INFLATION_RATIO = 10 ** 0.5  # Recorded/expected Ah above this (geometric midpoint of 1x and 10x) = inflated
MIN_AH_TO_JUDGE = 0.2  # Sessions expected to use less than this can't be judged on their own
PWM_NEUTRAL = 1500  # Thruster/motor neutral PWM (us)
PWM_ACTIVE_DEADBAND = 25  # |pwm - neutral| above this (us) counts as running
ACCESSORY_DEFAULT_OFF_PWM = 1100  # Typical "off" output for lights; configurable per accessory
MISSIONS_HEADERS = ['start_time', 'end_time', 'start_voltage', 'end_voltage',
                    'start_cpu_temp', 'end_cpu_temp', 'total_ah', 'start_uptime', 'end_uptime',
                    'voltage_min', 'max_pwm_deviation', 'hard_use',
                    'distance_m', 'max_current_a', 'avg_current_a', 'max_speed', 'avg_speed', 'wh']

# ArduSub flight mode numbers (custom_mode) -> display name
ARDUSUB_MODES = {
    0: "Stabilize", 1: "Acro", 2: "AltHold", 3: "Auto", 4: "Guided",
    7: "Circle", 9: "Surface", 16: "PosHold", 19: "Manual",
    20: "Motor Detect", 21: "SurfTrak"
}
# ArduRover flight mode numbers -> display name
ARDUROVER_MODES = {
    0: "Manual", 1: "Acro", 2: "Steering", 3: "Hold", 4: "Loiter",
    5: "Follow", 6: "Simple", 7: "Auto", 8: "RTL", 9: "SmartRTL",
    10: "Guided", 11: "Initializing"
}

app = Flask(__name__, static_folder='static')

def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON so a power cut leaves either the old or the new file, never a truncated one."""
    tmp = path.with_name(path.name + '.tmp')
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _default_gateway() -> Optional[str]:
    """IPv4 default gateway of this container (the Docker host running BlueOS), from /proc/net/route."""
    try:
        with open('/proc/net/route') as f:
            next(f)  # Header
            for line in f:
                fields = line.split()
                # Destination 0.0.0.0 with the RTF_GATEWAY flag is the default route; addresses are little-endian hex
                if len(fields) > 3 and fields[1] == '00000000' and int(fields[3], 16) & 0x2:
                    return socket.inet_ntoa(struct.pack('<L', int(fields[2], 16)))
    except (OSError, ValueError, StopIteration):
        pass
    return None


def _parse_mavlink_time(value: Any) -> Optional[datetime.datetime]:
    """Parse mavlink2rest's RFC 3339 timestamps (may carry nanoseconds and a trailing Z)."""
    if not isinstance(value, str):
        return None
    try:
        value = re.sub(r'(\.\d{6})\d+', r'\1', value.replace('Z', '+00:00'))
        parsed = datetime.datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


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
            'total_distance_m': 0.0,  # Lifetime distance traveled (meters)
            'mode_minutes': {},  # {mode_name: minutes} per flight mode
            'current_mission': {
                'start_time': None,
                'start_voltage': 0.0,
                'start_cpu_temp': 0.0,
                'end_voltage': 0.0,
                'end_cpu_temp': 0.0,
                'min_cpu_temp': 0.0,
                'max_cpu_temp': 0.0,
                'total_ah': 0.0,
                'start_uptime': 0,
                'end_uptime': 0,
                'voltage_min': 0.0,
                'max_pwm_deviation': 0.0,
                'distance_m': 0.0,
                'max_current_a': 0.0,
                'current_sum': 0.0,
                'current_count': 0,
                'max_speed': 0.0,
                'speed_sum': 0.0,
                'speed_count': 0
            },
            'pending_battery_swap_check': False  # Set on startup when previous session had voltage drop
        }
        self.missions = []  # List to store completed missions
        self.thruster_stats = {
            'thruster_count': 0,
            'mav_type': -1,
            'channels': [],  # Servo output channel for thruster 1, 2, ...
            'channel_source': 'default',  # 'params' once read from SERVOn_FUNCTION
            'thrusters': []  # [{'run_seconds': 0.0, 'run_minutes': 0, 'avg_pwm_sum': 0, 'avg_pwm_count': 0}, ...]
        }
        self.thruster_lock = threading.RLock()
        self.accessories = {}  # {id: {'name', 'channel', 'off_pwm', 'run_seconds', 'run_minutes', 'avg_pwm_sum', 'avg_pwm_count'}}
        self.accessory_lock = threading.RLock()
        self._next_accessory_id = 1
        self.last_update_time = time.time()
        self.minutes_since_update = 0
        # Connection discovery: which BlueOS host/mavlink2rest URL answers, and the autopilot's system ID
        hosts = BLUEOS_HOSTS[:1] + [h for h in [_default_gateway()] if h] + BLUEOS_HOSTS[1:]
        self._blueos_hosts = list(dict.fromkeys(hosts))
        self._mavlink_endpoints = [url for h in self._blueos_hosts
                                   for url in (f'http://{h}/mavlink2rest/mavlink', f'http://{h}:6040/mavlink')]
        self._endpoint_index = 0  # Index into _mavlink_endpoints of the endpoint that last answered
        self._active_endpoint = None  # URL of the mavlink2rest endpoint in use; None while unreachable
        self._unreachable_logged = False
        self._sysid = None  # Autopilot MAVLink system ID (SYSID_THISMAV), discovered from mavlink2rest
        self._sysid_checked = 0.0
        self._autopilot_missing_logged = False
        self._vehicle_online = None  # Last logged online state, to log transitions only
        logger.info(f"BlueOS host candidates: {self._blueos_hosts}")
        self._message_seen = {}  # {(sysid, msg_type): (counter, monotonic time the counter last changed)}
        self._freshness_lock = threading.Lock()
        self.setup_csv_files()
        self.load_stats()
        self.load_modes()
        self.load_missions()
        self.load_thruster_stats()
        self.load_accessories()
        self._auto_populate_vehicle_name()
        self.close_previous_session_on_startup()
        self.migrate_energy_history()
        self.detect_startup()
        
        # Start the update thread
        self.update_thread = threading.Thread(target=self.update_loop)
        self.update_thread.daemon = True
        self.update_thread.start()
        
        # Start PWM sampling thread (runs every 5 seconds when armed)
        self.pwm_sample_thread = threading.Thread(target=self.pwm_sample_loop)
        self.pwm_sample_thread.daemon = True
        self.pwm_sample_thread.start()

        # Start distance sampling thread (runs at 1 Hz when armed for ArduRover/boats)
        self.distance_sample_thread = threading.Thread(target=self.distance_sample_loop)
        self.distance_sample_thread.daemon = True
        self.distance_sample_thread.start()

        # Map thrusters to output channels from autopilot params
        self.thruster_detect_thread = threading.Thread(target=self.thruster_detect_loop)
        self.thruster_detect_thread.daemon = True
        self.thruster_detect_thread.start()
    
    def detect_startup(self):
        """Count a startup when the vehicle has rebooted since the extension last ran.

        Uses the kernel boot_id, which changes on every host reboot but not on container restarts.
        """
        try:
            boot_id = BOOT_ID_PATH.read_text().strip()
        except OSError:
            boot_id = ''
        try:
            last_boot_id = LAST_BOOT_ID_FILE.read_text().strip()
        except OSError:
            last_boot_id = ''
        STARTUP_MARKER.unlink(missing_ok=True)

        if boot_id and boot_id == last_boot_id:
            logger.info("Extension restarted without a vehicle reboot; not counting a startup")
            return

        logger.info("Detected new vehicle startup")
        with self.stats_lock:
            self.stats['startups'] += 1
        # Write the updated stats to CSV right away, then remember this boot
        self.write_stats_to_csv(startup_detected=True)
        if boot_id:
            LAST_BOOT_ID_FILE.write_text(boot_id)

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
                        # Write new header row with dive_minutes, depth, total_distance_m
                        writer.writerow(['timestamp', 'total_minutes', 'armed_minutes', 'disarmed_minutes', 
                                       'dive_minutes', 'battery_swaps', 'startups', 'voltage', 'depth',
                                       'cpu_temp', 'wh_consumed', 'current_ah', 'total_distance_m', 'time_status'])
                        
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
                                '0.0',  # total_distance_m (new)
                                row[10] if len(row) > 10 else 'normal'  # time_status
                            ]
                            writer.writerow(new_row)
                    
                    logger.info("Successfully upgraded CSV file to new format with dive tracking")
        except Exception as e:
            logger.error(f"Error upgrading CSV format: {e}")

        # Separate upgrade: add total_distance_m column if missing (before time_status)
        try:
            if ODOMETER_CSV.exists():
                with open(ODOMETER_CSV, 'r', newline='') as f:
                    reader = csv.reader(f)
                    headers = next(reader)
                    if 'total_distance_m' not in headers:
                        rows = list(reader)
                        new_headers = headers[:-1] + ['total_distance_m'] + headers[-1:]
                        with open(ODOMETER_CSV, 'w', newline='') as fw:
                            w = csv.writer(fw)
                            w.writerow(new_headers)
                            for row in rows:
                                if len(row) >= len(headers) - 1:
                                    new_row = row[:len(headers) - 1] + ['0.0'] + row[len(headers) - 1:]
                                    w.writerow(new_row)
                                else:
                                    w.writerow(row)
                        logger.info("Added total_distance_m column to odometer CSV")
        except Exception as e:
            logger.error(f"Error upgrading CSV for total_distance_m: {e}")

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
                               'cpu_temp', 'wh_consumed', 'current_ah', 'total_distance_m', 'time_status'])
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
                writer.writerow(MISSIONS_HEADERS)
        else:
            self.upgrade_missions_csv_format()
    
    def upgrade_missions_csv_format(self):
        """Append any MISSIONS_HEADERS columns missing from an older missions CSV."""
        try:
            if not MISSIONS_CSV.exists():
                return
            with open(MISSIONS_CSV, 'r', newline='') as f:
                reader = csv.reader(f)
                headers = next(reader, [])
                missing = [h for h in MISSIONS_HEADERS if h not in headers]
                if not missing:
                    return
                rows = list(reader)
            new_headers = headers + missing
            tmp = MISSIONS_CSV.with_name(MISSIONS_CSV.name + '.tmp')
            with open(tmp, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(new_headers)
                for row in rows:
                    # Pre-wh rows get an empty wh so the UI falls back to estimating from Ah
                    pad = ['' if h == 'wh' else '0' for h in new_headers[len(row):]]
                    writer.writerow(row + pad)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, MISSIONS_CSV)
            logger.info(f"Upgraded missions CSV with columns: {missing}")
        except Exception as e:
            logger.error(f"Error upgrading missions CSV: {e}")

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
                                if len(row) >= 17:
                                    m['distance_m'] = float(row[12]) if row[12].strip() else 0.0
                                    m['max_current_a'] = float(row[13]) if row[13].strip() else 0.0
                                    m['avg_current_a'] = float(row[14]) if row[14].strip() else 0.0
                                    m['max_speed'] = float(row[15]) if row[15].strip() else 0.0
                                    m['avg_speed'] = float(row[16]) if row[16].strip() else 0.0
                                if len(row) >= 18 and row[17].strip():
                                    m['wh'] = float(row[17])
                                self.missions.append(m)
                logger.info(f"Loaded {len(self.missions)} missions from {MISSIONS_CSV}")
            except Exception as e:
                logger.error(f"Error loading missions: {e}")

    def load_modes(self):
        """Load mode_minutes from JSON file"""
        if MODES_FILE.exists():
            try:
                with open(MODES_FILE, 'r') as f:
                    data = json.load(f)
                with self.stats_lock:
                    self.stats['mode_minutes'] = data.get('mode_minutes', {})
                logger.info(f"Loaded mode_minutes: {list(self.stats['mode_minutes'].keys())}")
            except Exception as e:
                logger.error(f"Error loading modes: {e}")

    def save_modes(self):
        """Save mode_minutes to JSON file"""
        try:
            with self.stats_lock:
                data = {'mode_minutes': dict(self.stats.get('mode_minutes', {}))}
            _atomic_write_json(MODES_FILE, data)
        except Exception as e:
            logger.error(f"Error saving modes: {e}")

    def load_thruster_stats(self):
        """Load thruster stats from JSON file"""
        if THRUSTERS_FILE.exists():
            try:
                with open(THRUSTERS_FILE, 'r') as f:
                    data = json.load(f)
                with self.thruster_lock:
                    count = data.get('thruster_count', 0)
                    self.thruster_stats['thruster_count'] = count
                    self.thruster_stats['mav_type'] = data.get('mav_type', -1)
                    self.thruster_stats['channels'] = data.get('channels') or list(range(1, count + 1))
                    self.thruster_stats['channel_source'] = data.get('channel_source', 'default')
                    self.thruster_stats['thrusters'] = data.get('thrusters', [])
                    for t in self.thruster_stats['thrusters']:
                        t.setdefault('run_seconds', float(t.get('run_minutes', 0)) * 60)
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
                    'channels': list(self.thruster_stats.get('channels', [])),
                    'channel_source': self.thruster_stats.get('channel_source', 'default'),
                    'thrusters': [dict(t) for t in self.thruster_stats['thrusters']]
                }
            _atomic_write_json(THRUSTERS_FILE, data)
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
            _atomic_write_json(VEHICLE_FILE, data)
        except Exception as e:
            logger.error(f"Error saving vehicle: {e}")
    
    def _auto_populate_vehicle_name(self) -> None:
        """On startup, sync vehicle name from BlueOS beacon to local storage."""
        name = self.get_blueos_vehicle_name()
        if name:
            self.save_vehicle({'name': name})
            logger.info(f"Synced vehicle name from BlueOS: {name}")

    def get_blueos_vehicle_name(self) -> str:
        """Fetch vehicle name from the BlueOS Beacon service. Returns name or empty string."""
        beacon_hosts = list(dict.fromkeys([self._blueos_base_url()] + [f'http://{h}' for h in self._blueos_hosts]))
        for host in beacon_hosts:
            try:
                resp = requests.get(f"{host}/beacon/v1.0/vehicle_name", timeout=2)
                if resp.status_code == 200:
                    name = resp.text.strip().strip('"')
                    if name:
                        logger.info(f"Got vehicle name from BlueOS beacon ({host}): {name}")
                        return name
            except Exception:
                continue
        return ''

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
                            'off_pwm': int(v.get('off_pwm', ACCESSORY_DEFAULT_OFF_PWM)),
                            'run_seconds': float(v.get('run_seconds', int(v.get('run_minutes', 0)) * 60)),
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
                data = {'accessories': {k: dict(v) for k, v in self.accessories.items()}}
            _atomic_write_json(ACCESSORIES_FILE, data)
        except Exception as e:
            logger.error(f"Error saving accessories: {e}")
    
    def _apply_thruster_layout(self, mav_type: int, channels: List[int], source: str) -> None:
        """Resize the thruster list to match channels, preserving existing per-thruster data by index."""
        with self.thruster_lock:
            ts = self.thruster_stats
            if (ts['mav_type'] == mav_type and ts.get('channels') == channels and
                    ts.get('channel_source') == source and len(ts['thrusters']) == len(channels)):
                return
            ts['mav_type'] = mav_type
            ts['thruster_count'] = len(channels)
            ts['channels'] = list(channels)
            ts['channel_source'] = source
            old = ts['thrusters']
            ts['thrusters'] = [
                old[i] if i < len(old) else {'run_seconds': 0.0, 'run_minutes': 0, 'avg_pwm_sum': 0, 'avg_pwm_count': 0}
                for i in range(len(channels))
            ]
            logger.info(f"Thruster layout: {len(channels)} on channels {channels} (from {source})")
        self.save_thruster_stats()

    def get_thruster_count_from_mav_type(self, mav_type: int) -> int:
        """Default thruster count until SERVOn_FUNCTION params are read. ArduSub: 8, Boat/Rover: 2"""
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
                    {'label': 'Port / Starboard', 'rows': [[2, 1]], 'names': {2: 'Port', 1: 'Starboard'}}
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
                        {'label': 'Horizontal', 'rows': [[2, 1], [4, 3]]},
                        {'label': 'Vertical', 'rows': [[6, 5], [8, 7]]}
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
    
    def add_accessory(self, name: str, channel: int, off_pwm: int = ACCESSORY_DEFAULT_OFF_PWM) -> str:
        """Add a new accessory. Run time counts while its output is away from off_pwm. Returns the new ID."""
        channel = max(1, min(16, int(channel)))
        with self.accessory_lock:
            aid = str(self._next_accessory_id)
            self._next_accessory_id += 1
            self.accessories[aid] = {
                'name': str(name).strip() or f'Device {aid}',
                'channel': channel,
                'off_pwm': int(off_pwm),
                'run_seconds': 0.0,
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
                self.accessories[accessory_id]['run_seconds'] = 0.0
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
                    self.thruster_stats['thrusters'][idx]['run_seconds'] = 0.0
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

    def _mission_row(self, m: dict) -> List[str]:
        """Serialize a mission in MISSIONS_HEADERS order."""
        return [
            str(m.get('start_time', '')),
            str(m.get('end_time', '')),
            str(m.get('start_voltage', 0)),
            str(m.get('end_voltage', 0)),
            str(m.get('start_cpu_temp', 0)),
            str(m.get('end_cpu_temp', 0)),
            str(m.get('total_ah', 0)),
            str(m.get('start_uptime', 0)),
            str(m.get('end_uptime', 0)),
            str(m.get('voltage_min', 0) or 0),
            str(m.get('max_pwm_deviation', 0) or 0),
            'true' if m.get('hard_use') else 'false',
            str(m.get('distance_m', 0)),
            str(m.get('max_current_a', 0)),
            str(m.get('avg_current_a', 0)),
            str(m.get('max_speed', 0)),
            str(m.get('avg_speed', 0)),
            str(m['wh']) if m.get('wh') is not None else ''
        ]

    def _rewrite_missions_csv(self) -> None:
        """Rewrite MISSIONS_CSV with current missions list."""
        tmp = MISSIONS_CSV.with_name(MISSIONS_CSV.name + '.tmp')
        with open(tmp, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(MISSIONS_HEADERS)
            for m in self.missions:
                writer.writerow(self._mission_row(m))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, MISSIONS_CSV)

    def save_mission(self, mission: dict):
        """Append a single mission to persistent storage"""
        try:
            mission['hard_use'] = self._is_hard_use(mission)
            with open(MISSIONS_CSV, 'a', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(self._mission_row(mission))
        except Exception as e:
            logger.error(f"Error saving mission: {e}")

    def persist_current_session(self):
        """Save current mission/session to disk so it survives power-off"""
        try:
            mission = self.stats['current_mission']
            if mission.get('start_time') is not None:
                _atomic_write_json(CURRENT_SESSION_FILE, mission)
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
            
            if any(m.get('start_time') == str(start_time) for m in self.missions):
                logger.info(f"Previous session {start_time} already in usage history; not recording it again")
            else:
                with self.stats_lock:
                    self._close_session(prev_session, end_time, end_voltage, end_cpu_temp, end_uptime)
                    # Persist the lifetime energy that now includes this session
                    self.write_stats_to_csv("session_closed", timestamp=end_time)

            # Check if voltage decreased during that session - if so, battery swap likely on reconnect
            start_voltage = float(prev_session.get('start_voltage', 0) or 0)
            if start_voltage > 0 and end_voltage > 0 and end_voltage < start_voltage:
                with self.stats_lock:
                    self.stats['pending_battery_swap_check'] = True
                    self.stats['last_voltage'] = end_voltage  # For comparison when we get first reading
                logger.info(f"Voltage decreased during previous session ({start_voltage}V -> {end_voltage}V); will check for battery swap on first voltage reading")
            
            # Remove the session file so we don't process it again
            CURRENT_SESSION_FILE.unlink(missing_ok=True)
            
        except Exception as e:
            logger.error(f"Error closing previous session on startup: {e}")
    
    def migrate_energy_history(self) -> None:
        """One-time (schema v2) fix for history recorded while current_consumed was read as cAh (10x the Ah).

        Vehicles installed the affected build at different times, so each session is judged on its own:
        recorded Ah vs. average current x duration (current was always decoded correctly). Sessions too
        small to judge take the verdict of the nearest judged session. Per-session Wh is then filled in and
        lifetime energy rebuilt from all sessions. Original files are backed up first.
        """
        try:
            schema = json.loads(SCHEMA_FILE.read_text()) if SCHEMA_FILE.exists() else {}
        except (OSError, ValueError):
            schema = {}
        if schema.get('schema_version', 1) >= SCHEMA_VERSION:
            return

        legacy = [i for i, m in enumerate(self.missions) if m.get('wh') is None]
        record = {'schema_version': SCHEMA_VERSION, 'migrated_at': datetime.datetime.now(timezone.utc).isoformat()}
        if legacy:
            backup_dir = DATA_DIR / f"backup-v1-{datetime.datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
            backup_dir.mkdir(exist_ok=True)
            for f in DATA_DIR.iterdir():
                if f.is_file() and f.suffix in ('.csv', '.json'):
                    shutil.copy2(f, backup_dir / f.name)

            verdicts = {}  # mission index -> True if Ah was inflated 10x
            for i in legacy:
                m = self.missions[i]
                hours = max(0, int(m.get('end_uptime', 0) or 0) - int(m.get('start_uptime', 0) or 0)) / 60.0
                expected_ah = float(m.get('avg_current_a', 0) or 0) * hours
                if expected_ah >= MIN_AH_TO_JUDGE and (m.get('total_ah') or 0) > 0:
                    verdicts[i] = m['total_ah'] / expected_ah > AH_INFLATION_RATIO
            corrected = []
            lifetime_wh = 0.0
            for i, m in enumerate(self.missions):
                if i in legacy:
                    inflated = verdicts.get(i)
                    if inflated is None and verdicts:
                        inflated = verdicts[min(verdicts, key=lambda j: abs(j - i))]
                    if inflated and (m.get('total_ah') or 0) > 0:
                        m['total_ah'] = m['total_ah'] / 10.0
                        corrected.append(m.get('start_time'))
                    volts = [v for v in (m.get('start_voltage', 0), m.get('end_voltage', 0)) if 1.0 < (v or 0) < 65.0]
                    m['wh'] = (m.get('total_ah') or 0) * (sum(volts) / len(volts)) if volts else 0.0
                lifetime_wh += m.get('wh') or 0.0
            self._rewrite_missions_csv()

            with self.stats_lock:
                self.stats['previous_batteries_wh'] = lifetime_wh
                self.stats['total_wh_consumed'] = lifetime_wh
                self.write_stats_to_csv("migrated")
            note = (f"Odometer v2 data migration: corrected Ah on {len(corrected)} of {len(legacy)} older sessions "
                    f"(recorded 10x too high), filled in per-session Wh, and rebuilt lifetime energy to "
                    f"{lifetime_wh:.0f} Wh. Original files backed up to {backup_dir.name}.")
            with open(MAINTENANCE_CSV, 'a', newline='') as f:
                csv.writer(f).writerow([self.get_local_time().isoformat(), 'Note', note, '', 'false', '', '', '', 'false'])
            logger.info(note)
            record.update({'backup': backup_dir.name, 'legacy_sessions': len(legacy),
                           'ah_corrected_sessions': corrected, 'lifetime_wh': lifetime_wh})
        _atomic_write_json(SCHEMA_FILE, record)

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
                has_total_distance = 'total_distance_m' in headers
                min_columns = 14 if (has_dive_minutes and has_total_distance) else (11 if has_dive_minutes else 9)
                
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
                            if len(row) > 12 and row[12].strip(): float(row[12])  # total_distance_m
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

                            # Load total_distance_m (index 12) if present
                            if len(last_row) > 12 and last_row[12].strip():
                                try:
                                    self.stats['total_distance_m'] = float(last_row[12])
                                except (ValueError, TypeError):
                                    self.stats['total_distance_m'] = 0.0
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
        """Sample servo outputs every PWM_SAMPLE_INTERVAL seconds.

        Thruster run time accrues only while armed and that thruster's output is outside the neutral deadband.
        Accessory run time accrues while its output is away from its configured off PWM. Both are persisted
        once a minute by update_stats.
        """
        last_sample = time.monotonic()
        while not self.stop_event.is_set():
            time.sleep(PWM_SAMPLE_INTERVAL)
            now = time.monotonic()
            dt = min(now - last_sample, PWM_SAMPLE_INTERVAL * 2)
            last_sample = now
            try:
                servo_values = self.get_servo_output_raw()
                if servo_values is None:
                    continue
                is_armed = self.get_armed_status()
                max_dev = 0
                if is_armed:
                    with self.thruster_lock:
                        channels = self.thruster_stats.get('channels') or []
                        for t, ch in zip(self.thruster_stats['thrusters'], channels):
                            pwm = servo_values[ch - 1] if 1 <= ch <= len(servo_values) else 0
                            if pwm <= 0:
                                continue
                            t['avg_pwm_sum'] = t.get('avg_pwm_sum', 0) + pwm
                            t['avg_pwm_count'] = t.get('avg_pwm_count', 0) + 1
                            dev = abs(pwm - PWM_NEUTRAL)
                            max_dev = max(max_dev, dev)
                            if dev > PWM_ACTIVE_DEADBAND:
                                t['run_seconds'] = t.get('run_seconds', 0.0) + dt
                                t['run_minutes'] = int(t['run_seconds'] // 60)
                with self.accessory_lock:
                    for acc in self.accessories.values():
                        ch = acc.get('channel', 1)
                        pwm = servo_values[ch - 1] if 1 <= ch <= len(servo_values) else 0
                        if pwm <= 0:
                            continue
                        if is_armed:
                            acc['avg_pwm_sum'] = acc.get('avg_pwm_sum', 0) + pwm
                            acc['avg_pwm_count'] = acc.get('avg_pwm_count', 0) + 1
                        if abs(pwm - acc.get('off_pwm', ACCESSORY_DEFAULT_OFF_PWM)) > PWM_ACTIVE_DEADBAND:
                            acc['run_seconds'] = acc.get('run_seconds', 0.0) + dt
                            acc['run_minutes'] = int(acc['run_seconds'] // 60)
                # Session hard-use metric: thruster/motor channels only (lights etc. sit far from 1500 at rest)
                if max_dev > 0:
                    with self.stats_lock:
                        m = self.stats['current_mission']
                        if max_dev > m.get('max_pwm_deviation', 0):
                            m['max_pwm_deviation'] = max_dev
            except Exception as e:
                logger.debug(f"PWM sample loop: {e}")

    def distance_sample_loop(self):
        """Sample ground speed at 1 Hz when armed; integrate over the real elapsed time for distance."""
        last_sample = time.monotonic()
        while not self.stop_event.is_set():
            time.sleep(DISTANCE_SAMPLE_INTERVAL)
            now = time.monotonic()
            dt = min(now - last_sample, DISTANCE_SAMPLE_INTERVAL * 5)
            last_sample = now
            try:
                if not self.get_armed_status():
                    continue
                speed_m_s, hdop = self.get_ground_speed_m_s()
                if speed_m_s < self._distance_speed_floor(hdop):
                    continue  # Velocity noise while holding station would otherwise accumulate
                distance_m = speed_m_s * dt
                with self.stats_lock:
                    self.stats['total_distance_m'] = self.stats.get('total_distance_m', 0) + distance_m
                    m = self.stats['current_mission']
                    m['distance_m'] = m.get('distance_m', 0) + distance_m
            except Exception as e:
                logger.debug(f"Distance sample loop: {e}")

    def _start_session(self, start_time: str, voltage: float, cpu_temp: float) -> dict:
        """Begin a new usage session. Caller holds stats_lock."""
        cpu = cpu_temp if cpu_temp > 0 else 0.0
        self.stats['current_mission'] = {
            'start_time': start_time,
            'start_voltage': voltage,
            'start_cpu_temp': cpu,
            'end_voltage': voltage,
            'end_cpu_temp': cpu,
            'min_cpu_temp': cpu,
            'max_cpu_temp': cpu,
            'total_ah': 0.0,
            'wh': 0.0,
            'start_uptime': self.stats['total_minutes'],
            'end_uptime': self.stats['total_minutes'],
            'voltage_min': voltage,
            'max_pwm_deviation': 0.0,
            'distance_m': 0.0,
            'max_current_a': 0.0,
            'current_sum': 0.0,
            'current_count': 0,
            'max_speed': 0.0,
            'speed_sum': 0.0,
            'speed_count': 0,
            # Battery-monitor readings when this session started; energy is measured from here so an
            # extension restart mid-battery doesn't count the same energy again
            'base_mah': None,
            'base_hj': None,
            # Energy counted before a battery-monitor reset within this session
            'carried_ah': 0.0,
            'carried_wh': 0.0
        }
        self.stats['voltage_sum'] = 0.0
        self.stats['voltage_count'] = 0
        return self.stats['current_mission']

    def _update_session_energy(self, m: dict, consumed_mah: Optional[float], energy_hj: Optional[float],
                               avg_voltage: float) -> None:
        """Set the session's total_ah and wh from the battery monitor counters, relative to its baseline."""
        if consumed_mah is not None:
            if m.get('base_mah') is None:
                m['base_mah'] = consumed_mah
            m['total_ah'] = m.get('carried_ah', 0.0) + max(0.0, consumed_mah - m['base_mah']) / 1000.0
        if energy_hj is not None:
            # Prefer the autopilot's own energy integration (1 Wh = 36 hJ)
            if m.get('base_hj') is None:
                m['base_hj'] = energy_hj
            m['wh'] = m.get('carried_wh', 0.0) + max(0.0, energy_hj - m['base_hj']) / 36.0
        elif consumed_mah is not None:
            m['wh'] = m.get('carried_wh', 0.0) + max(0.0, consumed_mah - m['base_mah']) / 1000.0 * avg_voltage

    def _close_session(self, session: dict, end_time: str, end_voltage: float, end_cpu_temp: float,
                       end_uptime: int) -> dict:
        """Record a finished session in usage history and fold its energy into the lifetime total.

        Caller holds stats_lock (or is running single-threaded during startup).
        """
        c_count = int(session.get('current_count', 0) or 0)
        s_count = int(session.get('speed_count', 0) or 0)
        mission = {
            'start_time': str(session['start_time']),
            'end_time': str(end_time),
            'start_voltage': float(session.get('start_voltage', 0) or 0),
            'end_voltage': end_voltage,
            'start_cpu_temp': float(session.get('start_cpu_temp', 0) or 0),
            'end_cpu_temp': end_cpu_temp,
            'total_ah': float(session.get('total_ah', 0) or 0),
            'start_uptime': int(session.get('start_uptime', 0) or 0),
            'end_uptime': end_uptime,
            'voltage_min': float(session.get('voltage_min', 0) or 0),
            'max_pwm_deviation': float(session.get('max_pwm_deviation', 0) or 0),
            'distance_m': float(session.get('distance_m', 0) or 0),
            'max_current_a': float(session.get('max_current_a', 0) or 0),
            'avg_current_a': (float(session.get('current_sum', 0) or 0) / c_count) if c_count > 0 else 0.0,
            'max_speed': float(session.get('max_speed', 0) or 0),
            'avg_speed': (float(session.get('speed_sum', 0) or 0) / s_count) if s_count > 0 else 0.0
        }
        # A session persisted by a pre-v2 version has no Wh, and its Ah may be inflated: leave both to
        # migrate_energy_history, which runs right after this at startup
        mission['wh'] = float(session['wh']) if session.get('wh') is not None else None
        mission['hard_use'] = self._is_hard_use(mission)
        self.missions.append(mission)
        self.save_mission(mission)
        self.stats['previous_batteries_wh'] += mission['wh'] or 0.0
        logger.info(f"Session closed: {mission}")
        return mission

    def update_stats(self):
        """Update the statistics and write to CSV"""
        try:
            # Check for time jumps (system time corrections)
            current_time = time.time()
            time_diff_seconds = current_time - self.last_update_time
            if abs(time_diff_seconds - UPDATE_INTERVAL) > (MAX_TIME_JUMP_MINUTES * 60):
                logger.warning(f"Time jump detected! Diff: {time_diff_seconds/60:.2f} minutes.")
                time_status = "corrected"
            else:
                time_status = "normal"

            vehicle = self.get_vehicle_status()
            current_voltage = vehicle['voltage']
            is_armed = vehicle['is_armed']
            current_depth = vehicle['depth']
            mav_type = vehicle['mav_type']
            current_battery_a = vehicle['current_battery_a']
            groundspeed = vehicle['groundspeed']
            consumed_mah = vehicle['consumed_mah']
            current_cpu_temp = self.get_cpu_temperature()
            # Fetch time before taking the lock (HTTP call)
            now_iso = self.get_local_time().isoformat()

            # Fall back to a default 1..N channel layout until autopilot params give the real mapping
            default_count = self.get_thruster_count_from_mav_type(mav_type)
            if default_count > 0:
                with self.thruster_lock:
                    use_default = (self.thruster_stats.get('channel_source') != 'params' or
                                   self.thruster_stats['mav_type'] != mav_type)
                if use_default:
                    self._apply_thruster_layout(mav_type, list(range(1, default_count + 1)), 'default')

            # Run time accumulates in pwm_sample_loop; persist it once a minute
            self.save_thruster_stats()
            self.save_accessories()

            modes_changed = False
            with self.stats_lock:
                # Update total minutes
                self.stats['total_minutes'] += 1

                # Update armed/disarmed minutes
                if is_armed:
                    self.stats['armed_minutes'] += 1
                else:
                    self.stats['disarmed_minutes'] += 1

                # Update dive minutes if depth exceeds threshold (depth is only reported for submarines)
                if current_depth >= DIVE_DEPTH_THRESHOLD:
                    self.stats['dive_minutes'] += 1
                    logger.info(f"Dive time incremented: depth={current_depth}m >= {DIVE_DEPTH_THRESHOLD}m threshold")
                self.stats['last_depth'] = current_depth

                # Flight mode tracking: increment mode_minutes for this minute
                if vehicle['online']:
                    mode_name = self._mode_name(vehicle['custom_mode'], mav_type)
                    mode_min = self.stats.get('mode_minutes', {})
                    mode_min[mode_name] = mode_min.get(mode_name, 0) + 1
                    self.stats['mode_minutes'] = mode_min
                    modes_changed = True

                m = self.stats['current_mission']
                if m.get('start_time') is None:
                    m = self._start_session(now_iso, current_voltage, current_cpu_temp)

                # Peak/avg current and speed
                if current_battery_a > 0:
                    m['max_current_a'] = max(m.get('max_current_a', 0), current_battery_a)
                    m['current_sum'] = m.get('current_sum', 0) + current_battery_a
                    m['current_count'] = m.get('current_count', 0) + 1
                if groundspeed > 0:
                    m['max_speed'] = max(m.get('max_speed', 0), groundspeed)
                    m['speed_sum'] = m.get('speed_sum', 0) + groundspeed
                    m['speed_count'] = m.get('speed_count', 0) + 1

                # Check for battery swap on startup (voltage decreased last session, high now)
                if self.stats.get('pending_battery_swap_check') and current_voltage > 0:
                    last_v = self.stats['last_voltage']
                    if last_v > 0 and current_voltage > (last_v + BATTERY_SWAP_VOLTAGE_THRESHOLD):
                        self.stats['battery_swaps'] += 1
                        logger.info(f"Battery swap detected on reconnect: voltage was {last_v}V at shutdown, now {current_voltage}V")
                    self.stats['pending_battery_swap_check'] = False

                if current_voltage > 0:
                    self.stats['voltage_sum'] += current_voltage
                    self.stats['voltage_count'] += 1
                    avg_voltage = self.stats['voltage_sum'] / self.stats['voltage_count']

                    last_consumed = self.stats['last_current_consumed']
                    if consumed_mah is not None and last_consumed > 0 and consumed_mah < last_consumed:
                        # The battery monitor's counters reset (hot swap or autopilot reboot)
                        last_v = self.stats['last_voltage']
                        if last_v > 0 and current_voltage > last_v + BATTERY_SWAP_VOLTAGE_THRESHOLD:
                            self.stats['battery_swaps'] += 1
                            logger.info("Battery swap detected! Starting new session.")
                            self._close_session(m, now_iso, last_v, self.stats['cpu_temp'], self.stats['total_minutes'])
                            m = self._start_session(now_iso, current_voltage, current_cpu_temp)
                            self.stats['voltage_sum'] = current_voltage
                            self.stats['voltage_count'] = 1
                            avg_voltage = current_voltage
                        else:
                            # Same session continues: keep what was counted and measure from zero
                            m['carried_ah'] = m.get('total_ah', 0.0)
                            m['carried_wh'] = m.get('wh', 0.0)
                        m['base_mah'] = 0.0
                        m['base_hj'] = 0.0

                    self._update_session_energy(m, consumed_mah, vehicle['energy_hj'], avg_voltage)

                    if (m.get('start_voltage') or 0) <= 0:
                        m['start_voltage'] = current_voltage  # Session began before voltage was available
                    m['end_voltage'] = current_voltage
                    vmin = m.get('voltage_min', 0) or 0
                    if vmin == 0 or current_voltage < vmin:
                        m['voltage_min'] = current_voltage

                    if consumed_mah is not None:
                        self.stats['last_current_consumed'] = consumed_mah
                    self.stats['last_voltage'] = current_voltage

                if current_cpu_temp > 0:
                    m['end_cpu_temp'] = current_cpu_temp
                    cur_min = m.get('min_cpu_temp', 0)
                    if cur_min <= 0 or current_cpu_temp < cur_min:
                        m['min_cpu_temp'] = current_cpu_temp
                    if current_cpu_temp > m.get('max_cpu_temp', 0):
                        m['max_cpu_temp'] = current_cpu_temp
                    self.stats['cpu_temp'] = current_cpu_temp
                m['end_uptime'] = self.stats['total_minutes']

                # Lifetime energy = all closed sessions + this one
                self.stats['current_battery_wh'] = m.get('wh', 0.0)
                self.stats['total_wh_consumed'] = self.stats['previous_batteries_wh'] + self.stats['current_battery_wh']
                logger.info(f"Energy - Session: {self.stats['current_battery_wh']:.2f}Wh, Closed sessions: {self.stats['previous_batteries_wh']:.2f}Wh, Lifetime total: {self.stats['total_wh_consumed']:.2f}Wh")

                # Persist current session so it survives power-off (enables usage history on next boot)
                self.persist_current_session()

                # Write to CSV
                self.write_stats_to_csv(time_status, timestamp=now_iso)

            if modes_changed:
                self.save_modes()

            # Update the last update time
            self.last_update_time = current_time

            # Send stats to Mavlink
            self.send_stats_to_mavlink()

        except Exception as e:
            logger.error(f"Error updating stats: {e}")

    def get_local_time(self) -> datetime.datetime:
        """Get the local time from the system-information endpoint. Returns timezone-aware UTC."""
        try:
            response = requests.get(f'{self._blueos_base_url()}/system-information/system/unix_time_seconds', timeout=2)
            if response.status_code == 200:
                unix_time = float(response.text)
                return datetime.datetime.fromtimestamp(unix_time, tz=timezone.utc)
        except Exception as e:
            logger.warning(f"Failed to get local time from system-information endpoint: {e}")
        
        # Fallback to system time if endpoint is not available
        return datetime.datetime.now(timezone.utc)

    def write_stats_to_csv(self, time_status="normal", startup_detected=False, timestamp: Optional[str] = None):
        """Write the current stats to the CSV file.
        
        Note: This method should be called while holding self.stats_lock or with
        a copy of the stats values to ensure thread safety.
        """
        # Only write valid CPU temperature values to CSV
        cpu_temp_value = str(self.stats['cpu_temp']) if self.stats['cpu_temp'] > 0 else ''
        
        if timestamp is None:
            timestamp = self.get_local_time().isoformat()
        
        # Create row with all fields, converting all values to strings
        # Format: timestamp, total_minutes, armed_minutes, disarmed_minutes, dive_minutes,
        # battery_swaps, startups, voltage, depth, cpu_temp, wh_consumed, current_ah, total_distance_m, time_status
        row = [
            timestamp,
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
            str(self.stats.get('total_distance_m', 0.0)),
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
    
    def _request(self, method: str, path: str, **kwargs) -> Optional[requests.Response]:
        """Send a request to mavlink2rest, starting with the endpoint that last answered."""
        endpoints = self._mavlink_endpoints
        start = self._endpoint_index
        for idx in list(range(start, len(endpoints))) + list(range(0, start)):
            try:
                resp = requests.request(method, endpoints[idx] + path, timeout=2, **kwargs)
            except requests.exceptions.RequestException:
                continue
            self._endpoint_index = idx
            self._unreachable_logged = False
            if self._active_endpoint != endpoints[idx]:
                logger.info(f"Using mavlink2rest at {endpoints[idx]}"
                            + (f" (was {self._active_endpoint})" if self._active_endpoint else ""))
                self._active_endpoint = endpoints[idx]
            return resp
        if not self._unreachable_logged:
            logger.warning(f"mavlink2rest unreachable; tried {endpoints}")
            self._unreachable_logged = True
        self._active_endpoint = None
        return None

    def _blueos_base_url(self) -> str:
        """Web root of the BlueOS host mavlink2rest answered on (for beacon, system-information)."""
        if self._active_endpoint:
            parsed = urlparse(self._active_endpoint)
            return f"{parsed.scheme}://{parsed.hostname}"
        return f"http://{self._blueos_hosts[0]}"

    def _autopilot_sysid(self) -> Optional[int]:
        """The autopilot's MAVLink system ID. Not always 1 (SYSID_THISMAV is user-configurable), so it's
        discovered from mavlink2rest: the system whose component 1 sends a fresh, non-INVALID autopilot
        HEARTBEAT, preferring the most recently updated one."""
        if self._sysid is not None:
            return self._sysid
        now = time.monotonic()
        if now - self._sysid_checked < SYSID_DISCOVERY_INTERVAL:
            return None
        self._sysid_checked = now
        resp = self._request('GET', '/vehicles')
        if resp is None or resp.status_code != 200:
            return None
        try:
            vehicles = resp.json()
        except ValueError:
            return None
        best = None
        for sysid, vehicle in vehicles.items():
            component = (vehicle.get('components') or {}).get(str(AUTOPILOT_COMPONENT_ID)) or {}
            heartbeat = (component.get('messages') or {}).get('HEARTBEAT')
            if not heartbeat:
                continue
            autopilot = (heartbeat.get('message') or {}).get('autopilot')
            if isinstance(autopilot, dict):
                autopilot = autopilot.get('type')
            if autopilot in ('MAV_AUTOPILOT_INVALID', 8, None):
                continue
            last_update = _parse_mavlink_time(((heartbeat.get('status') or {}).get('time') or {}).get('last_update'))
            if last_update is not None and \
                    (datetime.datetime.now(timezone.utc) - last_update).total_seconds() > STALE_MESSAGE_SECONDS:
                continue  # mavlink2rest still lists systems that have gone quiet
            if best is None or (last_update and (best[1] is None or last_update > best[1])):
                best = (int(sysid), last_update)
        if best is None:
            if not self._autopilot_missing_logged:
                logger.warning(f"No autopilot HEARTBEAT on mavlink2rest at {self._active_endpoint}; "
                               f"systems present: {sorted(vehicles, key=int)}")
                self._autopilot_missing_logged = True
            return None
        self._sysid = best[0]
        self._autopilot_missing_logged = False
        logger.info(f"Autopilot found at MAVLink system ID {self._sysid} via {self._active_endpoint}")
        return self._sysid

    def _lose_autopilot(self, reason: str) -> None:
        """Forget the autopilot's system ID so it is rediscovered (it may have been renumbered)."""
        if self._sysid is not None:
            logger.warning(f"Autopilot at MAVLink system ID {self._sysid} {reason}; rediscovering")
            self._sysid = None

    def connection_status(self) -> Dict[str, Any]:
        """How the extension is reaching the vehicle, for the API/UI."""
        return {
            'mavlink_endpoint': self._active_endpoint,
            'autopilot_sysid': self._sysid,
            'vehicle_online': bool(self._vehicle_online),
            'blueos_hosts': self._blueos_hosts,
        }

    def _get_message(self, msg_type: str) -> Optional[dict]:
        """Fetch the latest msg_type from the autopilot via mavlink2rest. Returns None if unavailable or stale."""
        msg = self._fetch_message(msg_type)
        if msg_type == 'HEARTBEAT':
            self._set_vehicle_online(msg is not None)
        return msg

    def _set_vehicle_online(self, online: bool) -> None:
        """Record online state, logging only transitions."""
        if online == self._vehicle_online:
            return
        if self._vehicle_online is None and not online:
            self._vehicle_online = False  # Still starting up; discovery logs if no autopilot is found
            return
        if online:
            logger.info(f"Vehicle online: autopilot system ID {self._sysid} via {self._active_endpoint}")
        else:
            logger.warning("Vehicle offline: no fresh autopilot HEARTBEAT; counting time as disarmed")
        self._vehicle_online = online

    def _fetch_message(self, msg_type: str) -> Optional[dict]:
        sysid = self._autopilot_sysid()
        if sysid is None:
            return None
        resp = self._request('GET', f"/vehicles/{sysid}/components/{AUTOPILOT_COMPONENT_ID}/messages/{msg_type}")
        if resp is None or resp.status_code != 200:
            if msg_type == 'HEARTBEAT' and resp is not None:
                self._lose_autopilot(f"has no HEARTBEAT (HTTP {resp.status_code})")
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        msg = self._fresh_payload((sysid, msg_type), data)
        if msg is None and msg_type == 'HEARTBEAT':
            self._lose_autopilot(f"stopped sending HEARTBEAT for over {STALE_MESSAGE_SECONDS:.0f} s")
        return msg

    def _fresh_payload(self, key: Tuple[int, str], data: dict) -> Optional[dict]:
        """Unwrap a mavlink2rest response, returning None if the message has stopped updating.

        mavlink2rest keeps serving the last copy of every message after the autopilot goes quiet, so a
        disconnected vehicle would otherwise look armed forever. Freshness is judged by the message counter
        advancing (immune to clock jumps), falling back to the last_update timestamp on first sighting.
        """
        msg = data.get('message', data)
        status_time = (data.get('status') or {}).get('time') or {}
        counter = status_time.get('counter')
        if counter is None:
            return msg  # Endpoint format without freshness info; assume live
        now = time.monotonic()
        with self._freshness_lock:
            seen = self._message_seen.get(key)
            if seen is None or seen[0] != counter:
                changed_at = now
                if seen is None:
                    last_update = _parse_mavlink_time(status_time.get('last_update'))
                    if last_update is not None:
                        age = (datetime.datetime.now(timezone.utc) - last_update).total_seconds()
                        changed_at = now - max(0.0, age)
                seen = (counter, changed_at)
                self._message_seen[key] = seen
        return msg if now - seen[1] <= STALE_MESSAGE_SECONDS else None

    @staticmethod
    def _is_armed(heartbeat: dict) -> bool:
        base_mode_obj = heartbeat.get("base_mode", {})
        base_mode = base_mode_obj.get("bits", 0) if isinstance(base_mode_obj, dict) else base_mode_obj
        return bool(int(base_mode or 0) & ARMED_FLAG)

    @staticmethod
    def _has_3d_fix(gps: dict) -> bool:
        fix = gps.get('fix_type', 0)
        if isinstance(fix, dict):
            fix = fix.get('type', '')
        if isinstance(fix, str):
            return any(k in fix for k in ('3D_FIX', 'DGPS', 'RTK', 'STATIC', 'PPP'))
        return int(fix or 0) >= 3

    def get_ground_speed_m_s(self) -> Tuple[float, Optional[float]]:
        """Ground speed in m/s from GLOBAL_POSITION_INT (vx,vy in cm/s), falling back to GPS_RAW_INT (vel in
        cm/s), plus the GPS HDOP (None if there's no GPS or it doesn't report one)."""
        gps = self._get_message('GPS_RAW_INT')
        eph = gps.get('eph') if gps is not None else None
        hdop = eph / 100.0 if eph not in (None, 65535) else None  # eph is HDOP x 100; UINT16_MAX = unknown
        msg = self._get_message('GLOBAL_POSITION_INT')
        if msg is not None:
            vx = int(msg.get('vx', 0) or 0)  # cm/s
            vy = int(msg.get('vy', 0) or 0)  # cm/s
            return min(math.sqrt(vx * vx + vy * vy) / 100.0, MAX_PLAUSIBLE_SPEED_MS), hdop
        if gps is not None and self._has_3d_fix(gps):
            vel = int(gps.get('vel', 0) or 0)  # cm/s; 65535 = unknown
            if vel != 65535:
                return min(vel / 100.0, MAX_PLAUSIBLE_SPEED_MS), hdop
        return 0.0, hdop

    @staticmethod
    def _distance_speed_floor(hdop: Optional[float]) -> float:
        """Speed below which velocity is treated as noise. GPS velocity noise grows with HDOP, so the floor
        rises in proportion above POOR_HDOP; without GPS (e.g. a DVL-only sub) the base floor applies."""
        if hdop is None or hdop <= POOR_HDOP:
            return MIN_SPEED_FOR_DISTANCE_MS
        return min(MIN_SPEED_FOR_DISTANCE_MS * hdop / POOR_HDOP, MAX_SPEED_FLOOR_MS)

    def get_armed_status(self) -> bool:
        """Lightweight check: is vehicle armed? Fetches HEARTBEAT only; a stale heartbeat counts as disarmed."""
        heartbeat = self._get_message('HEARTBEAT')
        return heartbeat is not None and self._is_armed(heartbeat)

    def get_servo_output_raw(self) -> Optional[List[int]]:
        """Get PWM values (us) for channels 1-16 from SERVO_OUTPUT_RAW, or None if unavailable/stale."""
        msg = self._get_message('SERVO_OUTPUT_RAW')
        if msg is None:
            return None
        values = []
        for i in range(1, 17):
            val = msg.get(f'servo{i}_raw')
            values.append(int(val) if val is not None else 0)
        return values

    def _mode_name(self, custom_mode: int, mav_type: int) -> str:
        """Map custom_mode to display name based on vehicle type."""
        if mav_type == MAV_TYPE_SUBMARINE:
            return ARDUSUB_MODES.get(custom_mode, f"MODE_{custom_mode}")
        if mav_type in (MAV_TYPE_GROUND_ROVER, MAV_TYPE_SURFACE_BOAT):
            return ARDUROVER_MODES.get(custom_mode, f"MODE_{custom_mode}")
        return f"MODE_{custom_mode}"

    def get_vehicle_status(self) -> Dict[str, Any]:
        """Read the current vehicle state from mavlink2rest.

        HEARTBEAT drives online/armed state, vehicle type and mode. BATTERY_STATUS and VFR_HUD are optional,
        so vehicles without a battery monitor are still tracked. consumed_mah/energy_hj are None when the
        autopilot doesn't report them.
        """
        status = {
            'online': False, 'is_armed': False, 'mav_type': -1, 'custom_mode': 0,
            'voltage': 0.0, 'consumed_mah': None, 'energy_hj': None, 'current_battery_a': 0.0,
            'depth': 0.0, 'groundspeed': 0.0,
        }
        heartbeat = self._get_message('HEARTBEAT')
        if heartbeat is None:
            return status
        status['online'] = True
        status['is_armed'] = self._is_armed(heartbeat)
        status['mav_type'] = self._parse_mav_type(heartbeat)
        status['custom_mode'] = int(heartbeat.get("custom_mode", 0) or 0)

        battery = self._get_message('BATTERY_STATUS')
        if battery is not None:
            voltages = battery.get('voltages') or []
            if voltages and 0 < voltages[0] < 65535:  # UINT16_MAX = not reported
                status['voltage'] = voltages[0] / 1000.0  # mV -> V
            # MAVLink units: current_consumed mAh, energy_consumed hJ, current_battery cA; -1 = not provided
            consumed = battery.get('current_consumed', -1)
            if consumed is not None and consumed >= 0:
                status['consumed_mah'] = float(consumed)
            energy = battery.get('energy_consumed', -1)
            if energy is not None and energy >= 0:
                status['energy_hj'] = float(energy)
            current = battery.get('current_battery', -1)
            if current is not None and current != -1:
                status['current_battery_a'] = abs(int(current)) / 100.0

        vfr_hud = self._get_message('VFR_HUD')
        if vfr_hud is not None:
            # VFR_HUD.alt is depth (negative underwater) only on ArduSub; on boats it's GPS altitude,
            # which routinely dips below zero near sea level and must not count as diving
            alt = float(vfr_hud.get("alt", 0.0) or 0.0)
            if status['mav_type'] == MAV_TYPE_SUBMARINE and alt < 0:
                status['depth'] = -alt
            status['groundspeed'] = min(abs(float(vfr_hud.get("groundspeed", 0.0) or 0.0)), MAX_PLAUSIBLE_SPEED_MS)

        logger.debug(f"Vehicle status: {status}")
        return status

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
                # Exactly 10 characters, NUL-padded (as required by MAVLink)
                "name": [name[i] if i < len(name) else '\u0000' for i in range(10)]
            }
        }
        if self._post_message(payload):
            logger.info(f"Successfully sent {name}={value} to Mavlink2Rest")
            return True
        logger.error(f"Could not send {name}={value} to any Mavlink2Rest endpoint")
        return False

    def _post_message(self, payload: dict) -> bool:
        """POST a MAVLink message through mavlink2rest. Returns True on success."""
        resp = self._request('POST', '', json=payload)
        if resp is not None and resp.status_code == 200:
            return True
        logger.warning(f"Failed to send {payload['message']['type']}: "
                       f"{resp.status_code if resp is not None else 'no mavlink2rest endpoint reachable'}")
        return False

    def _read_param(self, param_id: str, timeout: float = 3.0) -> Optional[float]:
        """Read one autopilot parameter via PARAM_REQUEST_READ / PARAM_VALUE. Returns None on timeout."""
        payload = {
            "header": {"system_id": 255, "component_id": 0, "sequence": 0},
            "message": {
                "type": "PARAM_REQUEST_READ",
                "target_system": self._sysid or 1,
                "target_component": AUTOPILOT_COMPONENT_ID,
                "param_id": [param_id[i] if i < len(param_id) else '\u0000' for i in range(16)],
                "param_index": -1
            }
        }
        if not self._post_message(payload):
            return None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.2)
            msg = self._get_message('PARAM_VALUE')
            if msg is None:
                continue
            name = msg.get('param_id', '')
            if isinstance(name, list):
                name = ''.join(name)
            if name.rstrip('\u0000') == param_id:
                return float(msg.get('param_value', 0))
        return None

    def detect_thruster_channels(self, mav_type: int) -> Optional[List[int]]:
        """Map thrusters/motors to servo output channels from SERVOn_FUNCTION params.

        Returns the channel for thruster 1, 2, ... or None if params can't be read or no motors are found.
        Sub: thruster n is the channel set to Motor n. Boat: thruster 1 = starboard (ThrottleRight),
        thruster 2 = port (ThrottleLeft), matching get_layout_config.
        """
        functions = {}
        for ch in range(1, 17):
            # mavlink2rest keeps only the latest PARAM_VALUE, so a GCS fetching params at the same time can
            # overwrite our reply before it's polled; retry before giving up on this pass
            value = None
            for _ in range(3):
                value = self._read_param(f'SERVO{ch}_FUNCTION')
                if value is not None:
                    break
            if value is None:
                logger.info(f"Could not read SERVO{ch}_FUNCTION; keeping current thruster mapping")
                return None
            functions[ch] = int(value)
        if mav_type == MAV_TYPE_SUBMARINE:
            motors = {SERVO_FUNCTION_MOTORS[f]: ch for ch, f in functions.items() if f in SERVO_FUNCTION_MOTORS}
            return [motors[n] for n in sorted(motors)] or None
        if mav_type in (MAV_TYPE_GROUND_ROVER, MAV_TYPE_SURFACE_BOAT):
            right = next((ch for ch, f in functions.items() if f == SERVO_FUNCTION_THROTTLE_RIGHT), None)
            left = next((ch for ch, f in functions.items() if f == SERVO_FUNCTION_THROTTLE_LEFT), None)
            return [right, left] if right and left else None
        return None

    def thruster_detect_loop(self):
        """Periodically refresh the thruster -> channel mapping from autopilot params."""
        while not self.stop_event.is_set():
            delay = 30
            try:
                heartbeat = self._get_message('HEARTBEAT')
                mav_type = self._parse_mav_type(heartbeat) if heartbeat else -1
                if self.get_thruster_count_from_mav_type(mav_type) > 0:
                    channels = self.detect_thruster_channels(mav_type)
                    if channels:
                        self._apply_thruster_layout(mav_type, channels, 'params')
                        delay = THRUSTER_DETECT_INTERVAL
            except Exception as e:
                logger.warning(f"Thruster detection: {e}")
            self.stop_event.wait(delay)

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

@app.route('/blueos_vehicle_name')
def get_blueos_vehicle_name():
    """Fetch vehicle name from the BlueOS Beacon service"""
    name = odometer_service.get_blueos_vehicle_name()
    return jsonify({"status": "success", "name": name})

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
                'off_pwm': acc.get('off_pwm', ACCESSORY_DEFAULT_OFF_PWM),
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
            "data": {**odometer_service.stats, 'connection': odometer_service.connection_status()}
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
    device_off_pwm = data.get('device_off_pwm', ACCESSORY_DEFAULT_OFF_PWM)
    device_id = data.get('device_id', '').strip()
    reset_accessory = bool(data.get('reset_accessory', False))
    
    if not event_type:
        return jsonify({"status": "error", "message": "Event type is required"}), 400
    
    if event_type == 'Add Device':
        if not device_name:
            return jsonify({"status": "error", "message": "Device name is required for Add Device"}), 400
        device_channel = max(1, min(16, int(device_channel) if device_channel else 1))
        try:
            device_off_pwm = max(800, min(2200, int(device_off_pwm)))
        except (TypeError, ValueError):
            device_off_pwm = ACCESSORY_DEFAULT_OFF_PWM
        aid = odometer_service.add_accessory(device_name, device_channel, device_off_pwm)
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
    
    # Ensure maintenance CSV exists with headers (e.g. if manually deleted)
    if not MAINTENANCE_CSV.exists() or MAINTENANCE_CSV.stat().st_size == 0:
        odometer_service.setup_csv_files()
    
    with open(MAINTENANCE_CSV, 'a', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([timestamp, event_type, details, thruster_ids_json, reset_run_hours_str,
                        device_id_val, device_name_val, device_channel_val, reset_accessory_str])
    
    return jsonify({"status": "success", "message": "Maintenance record added"})

@app.route('/maintenance/update', methods=['POST'])
def update_maintenance():
    """Update an existing maintenance record timestamp"""
    data = request.json or {}
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
    data = request.json or {}
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
    static_folder_abs = os.path.abspath(app.static_folder)
    requested_path = os.path.abspath(os.path.normpath(os.path.join(app.static_folder, path)))
    # Prevent path traversal: ensure requested path is within static folder
    try:
        if os.path.commonpath([static_folder_abs, requested_path]) != static_folder_abs:
            return send_from_directory(app.static_folder, 'index.html')
    except ValueError:
        return send_from_directory(app.static_folder, 'index.html')
    if os.path.exists(requested_path) and os.path.isfile(requested_path):
        return send_from_directory(app.static_folder, path)
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
