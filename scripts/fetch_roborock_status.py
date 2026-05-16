#!/usr/bin/env python3
"""Fetch status for a Roborock device and print JSON for MMM-Roborock."""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from dataclasses import is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from roborock.devices.cache import InMemoryCache
from roborock.devices.device_manager import create_device_manager
from roborock.devices.file_cache import load_value
from roborock.exceptions import RoborockException
from roborock.protocols.v1_protocol import decode_rpc_response


REMOTE_MSG_TYPE_REQUEST_MAINTENANCE_STATUS = 114

MAINTENANCE_TYPES = {
    0: "UNKNOWN",
    1: "CAMERA_CLEANING",
    2: "CHASSIS_CLEANING",
    3: "CUTTING",
    4: "EDGING",
    5: "BATTERY",
}

MAINTENANCE_STATUS_RESPONSE_FIELDS = {
    1: "type",
    2: "used_time",
    3: "threshold",
    4: "needs_maintenance",
    5: "battery_degradation",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Roborock status for MMM-Roborock")
    parser.add_argument("--session-dir", required=True, help="Directory containing user_params.pkl and cache.pkl")
    parser.add_argument("--device-name", default=None, help="Filter by device name")
    parser.add_argument("--device-duid", default=None, help="Filter by DUID")
    parser.add_argument("--prefer-category", default=None, help="Preferred category, for example mower or vacuum")
    return parser.parse_args()


def normalize(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, pathlib.Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return {k: normalize(v) for k, v in value.__dict__.items() if not k.startswith("_") and v is not None}
    if isinstance(value, dict):
        return {str(k): normalize(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple, set)):
        return [normalize(v) for v in value]
    return value


def decode_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    shift = 0
    value = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValueError("Truncated protobuf varint")


def get_length_fields(data: bytes, field_number: int) -> list[bytes]:
    values = []
    offset = 0
    while offset < len(data):
        tag, offset = decode_varint(data, offset)
        current_field = tag >> 3
        wire_type = tag & 0x07

        if wire_type == 0:
            _, offset = decode_varint(data, offset)
        elif wire_type == 1:
            offset += 8
        elif wire_type == 2:
            length, offset = decode_varint(data, offset)
            value = data[offset : offset + length]
            offset += length
            if current_field == field_number:
                values.append(value)
        elif wire_type == 5:
            offset += 4
        else:
            raise ValueError(f"Unsupported protobuf wire type {wire_type}")
    return values


def decode_status_message(data: bytes) -> dict[str, Any]:
    status: dict[str, Any] = {}
    offset = 0
    while offset < len(data):
        tag, offset = decode_varint(data, offset)
        field_number = tag >> 3
        wire_type = tag & 0x07
        name = MAINTENANCE_STATUS_RESPONSE_FIELDS.get(field_number, f"field_{field_number}")

        if wire_type == 0:
            value, offset = decode_varint(data, offset)
            if name == "type":
                status[name] = value
                status["typeName"] = MAINTENANCE_TYPES.get(value, f"UNKNOWN_{value}")
            elif name in {"needs_maintenance", "battery_degradation"}:
                status[name] = bool(value)
            else:
                status[name] = value
                if name in {"used_time", "threshold"}:
                    status[f"{name}_hours"] = round(value / 3600, 2)
        elif wire_type == 1:
            offset += 8
        elif wire_type == 2:
            length, offset = decode_varint(data, offset)
            offset += length
        elif wire_type == 5:
            offset += 4
        else:
            raise ValueError(f"Unsupported protobuf wire type {wire_type}")
    return status


def decode_mower_maintenance_payload(payload: bytes | None) -> list[dict[str, Any]]:
    if not payload or not payload.startswith(b"PB"):
        return []

    data = payload[2:]
    statuses = []
    for wrapper in get_length_fields(data, 5):
        for maintenance_response in get_length_fields(wrapper, 94):
            for status_message in get_length_fields(maintenance_response, 1):
                statuses.append(decode_status_message(status_message))
    return statuses


def build_maintenance_request_object(maintenance_type: int = 0) -> dict[str, Any]:
    return {
        "id": int(datetime.now(timezone.utc).timestamp() * 1000),
        "type": REMOTE_MSG_TYPE_REQUEST_MAINTENANCE_STATUS,
        "maintenance_status_request": {
            "type": MAINTENANCE_TYPES[maintenance_type],
        },
    }


def lower_or_none(value: Any) -> str | None:
    return str(value).lower() if value is not None else None


def choose_device(devices: list[Any], device_name: str | None, device_duid: str | None, prefer_category: str | None) -> Any:
    if device_duid:
        for device in devices:
            if device.duid == device_duid:
                return device
        raise RuntimeError(f"No device found with DUID '{device_duid}'.")

    if device_name:
        for device in devices:
            if device.name.lower() == device_name.lower():
                return device
        for device in devices:
            if device_name.lower() in device.name.lower():
                return device
        raise RuntimeError(f"No device found with name '{device_name}'.")

    if prefer_category:
        wanted = prefer_category.lower()
        for device in devices:
            category = lower_or_none(getattr(device.product, "category", None))
            if category and wanted in category:
                return device

    if len(devices) == 1:
        return devices[0]

    online_devices = [device for device in devices if getattr(device.device_info, "online", False)]
    if len(online_devices) == 1:
        return online_devices[0]

    return devices[0]


def get_schema_values(device: Any) -> tuple[dict[str, Any], dict[str, int]]:
    raw_status = normalize(getattr(device.device_info, "device_status", None)) or {}
    code_values: dict[str, Any] = {}
    code_to_id: dict[str, int] = {}

    for item in getattr(device.product, "schema", None) or []:
        code = getattr(item, "code", None)
        schema_id = getattr(item, "id", None)

        if not code or schema_id is None:
            continue

        try:
            schema_id_int = int(schema_id)
        except (TypeError, ValueError):
            continue

        code_to_id[code] = schema_id_int

        if str(schema_id_int) in raw_status:
            code_values[code] = raw_status[str(schema_id_int)]
        elif schema_id_int in raw_status:
            code_values[code] = raw_status[schema_id_int]

    return code_values, code_to_id


def build_safe_device_raw(device: Any) -> dict[str, Any]:
    return {
        "device_status": normalize(getattr(device.device_info, "device_status", None)) or {},
    }


def build_safe_product_raw(device: Any) -> dict[str, Any]:
    return {
        "schema": normalize(getattr(device.product, "schema", None)) or [],
    }


def remove_empty_cache(cache_path: pathlib.Path) -> None:
    if cache_path.exists() and cache_path.stat().st_size == 0:
        cache_path.unlink()


def debug(message: str) -> None:
    print(f"[MMM-Roborock] {message}", file=sys.stderr, flush=True)


async def create_manager_with_cache(user_params: Any, cache_path: pathlib.Path) -> Any:
    remove_empty_cache(cache_path)
    cache = InMemoryCache()

    try:
        debug("creating device manager")
        manager = await asyncio.wait_for(
            create_device_manager(user_params, cache=cache),
            timeout=25,
        )
    except EOFError:
        debug("cache EOFError, deleting cache and retrying")
        cache_path.unlink(missing_ok=True)
        cache = InMemoryCache()
        manager = await asyncio.wait_for(
            create_device_manager(user_params, cache=cache),
            timeout=25,
        )

    return manager


def build_mower_status(device: Any) -> dict[str, Any]:
    values, schema_map = get_schema_values(device)

    mow_state = values.get("mow_state")
    mapping_state = values.get("mapping_state")
    charge_state = values.get("charge_state")
    ota_state = values.get("ota_state")
    offline_status = values.get("offline_status")

    state_name = "Ready"
    if mow_state not in (None, 0):
        state_name = f"Mowing ({mow_state})"
    elif mapping_state not in (None, 0):
        state_name = f"Mapping ({mapping_state})"
    elif charge_state not in (None, 0):
        state_name = f"Charging ({charge_state})"
    elif ota_state not in (None, 0):
        state_name = f"Updating ({ota_state})"
    elif offline_status not in (None, 0, ""):
        state_name = "Offline/warning"

    error_code = values.get("error_code")

    return {
        "state": mow_state if mow_state is not None else 0,
        "stateName": state_name,
        "battery": values.get("battery"),
        "errorCode": error_code,
        "errorCodeName": None if error_code in (None, 0) else f"Code {error_code}",
        "mowType": values.get("mow_type"),
        "mowState": mow_state,
        "mappingType": values.get("mapping_type"),
        "mappingState": mapping_state,
        "otaState": ota_state,
        "chargeState": charge_state,
        "chargeType": values.get("charge_type"),
        "mowStartType": values.get("mow_start_type"),
        "mowEffMode": values.get("mow_eff_mode"),
        "mowHeight": values.get("mow_height"),
        "mowDirectionAngle": values.get("mow_direction_angle"),
        "offlineStatus": offline_status,
        "mowProgress": values.get("mow_progress"),
        "gpsCoordinate": values.get("gps_coordinate"),
        "offDockNoTaskStatus": values.get("off_dock_no_task_status"),
        "afsStatus": values.get("afs_status"),
        "networkChannel": values.get("network_channel"),
        "rawByCode": values,
        "schemaMap": schema_map,
    }


async def fetch_mower_maintenance(device: Any) -> dict[str, Any] | None:
    if not device.v1_properties or not device.v1_properties.command:
        return None

    mqtt_channel = getattr(device._channel, "_mqtt_channel", None)
    if mqtt_channel is None:
        return None

    statuses: list[dict[str, Any]] = []

    def capture(message: Any) -> None:
        try:
            decoded = decode_rpc_response(message)
        except RoborockException:
            decoded = None

        if decoded is not None:
            return

        statuses.extend(decode_mower_maintenance_payload(getattr(message, "payload", None)))

    unsub = await mqtt_channel.subscribe(capture)
    original_rpc_channel = device.v1_properties.command._rpc_channel
    try:
        device.v1_properties.command._rpc_channel = device._channel.mqtt_rpc_channel
        try:
            await asyncio.wait_for(
                device.v1_properties.command.send("remote_pb", params=build_maintenance_request_object(0)),
                timeout=12,
            )
        except Exception as err:  # noqa: BLE001
            # The current python-roborock V1 decoder times out because the mower
            # answers with a PB payload. The capture callback above still receives
            # and decodes that PB response.
            debug(f"mower maintenance command ended without V1 response: {type(err).__name__}: {err}")
        await asyncio.sleep(1)
    finally:
        device.v1_properties.command._rpc_channel = original_rpc_channel
        unsub()

    if not statuses:
        return None

    return {
        "statuses": statuses,
        "needsMaintenance": [status for status in statuses if status.get("needs_maintenance")],
    }


def build_schema_status(device: Any) -> dict[str, Any]:
    values, schema_map = get_schema_values(device)
    error_code = values.get("error_code")

    return {
        "state": values.get("state"),
        "stateName": None,
        "battery": values.get("battery"),
        "errorCode": error_code,
        "errorCodeName": None if error_code in (None, 0) else f"Code {error_code}",
        "chargeStatus": values.get("charge_status"),
        "rawByCode": values,
        "schemaMap": schema_map,
    }


async def main() -> int:
    args = parse_args()
    session_dir = pathlib.Path(args.session_dir).expanduser().resolve()
    user_params_path = session_dir / "user_params.pkl"
    cache_path = session_dir / "cache.pkl"

    if not user_params_path.exists():
        print(
            "Missing login data. Run setup_roborock.py first to create user_params.pkl.",
            file=sys.stderr,
        )
        return 2

    user_params = await load_value(user_params_path)
    if user_params is None:
        print("Could not read user_params.pkl.", file=sys.stderr)
        return 3

    manager = await create_manager_with_cache(user_params, cache_path)

    try:
        debug("fetching devices")
        devices = await asyncio.wait_for(manager.get_devices(), timeout=25)
        if not devices:
            print("No Roborock devices found.", file=sys.stderr)
            return 4

        device = choose_device(devices, args.device_name, args.device_duid, args.prefer_category)
        debug(f"selected device {device.name} ({device.duid})")

        category = lower_or_none(getattr(device.product, "category", None))
        status_payload: dict[str, Any] | None = None
        status_error: str | None = None

        if category and "mower" in category:
            status_payload = build_mower_status(device)
            try:
                maintenance = await fetch_mower_maintenance(device)
                if maintenance:
                    status_payload["maintenance"] = maintenance
            except Exception as err:  # noqa: BLE001
                status_payload["maintenanceError"] = str(err)
        elif device.v1_properties is not None:
            try:
                await asyncio.wait_for(device.v1_properties.status.refresh(), timeout=15)
                status = device.v1_properties.status
                status_payload = status.as_dict()
                status_payload.update(
                    {
                        "stateName": getattr(status, "state_name", None),
                        "errorCodeName": getattr(status, "error_code_name", None),
                        "fanPowerName": getattr(status, "fan_power_name", None),
                        "waterBoxModeName": getattr(status, "water_box_mode_name", None),
                        "mopModeName": getattr(status, "mop_mode_name", None),
                        "squareMeterCleanArea": getattr(status, "square_meter_clean_area", None),
                    }
                )
            except Exception as err:  # noqa: BLE001
                status_error = str(err)
                status_payload = build_schema_status(device)
        else:
            status_error = "The device does not expose v1_properties in python-roborock yet."
            status_payload = build_schema_status(device)

        payload = {
            "fetchedAt": datetime.now(timezone.utc).isoformat(),
            "connected": bool(getattr(device, "is_connected", False)),
            "localConnected": bool(getattr(device, "is_local_connected", False)),
            "device": {
                "name": device.name,
                "duid": device.duid,
                "firmware": getattr(device.device_info, "fv", None),
                "online": getattr(device.device_info, "online", None),
                "pv": getattr(device.device_info, "pv", None),
                "raw": build_safe_device_raw(device),
            },
            "product": {
                "name": getattr(device.product, "name", None),
                "model": getattr(device.product, "model", None),
                "category": category,
                "raw": build_safe_product_raw(device),
            },
            "status": status_payload,
            "statusError": status_error,
        }

        print(json.dumps(normalize(payload), ensure_ascii=False))
        return 0
    finally:
        try:
            await asyncio.wait_for(manager.close(), timeout=5)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
