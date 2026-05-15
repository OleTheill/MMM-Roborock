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
