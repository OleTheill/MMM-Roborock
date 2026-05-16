#!/usr/bin/env python3
"""Probe mower maintenance commands exposed by the Roborock app plugin.

This script is intentionally diagnostic. It prints sanitized JSON and does not
reset maintenance records or send control commands.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
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


DEFAULT_COMMANDS = [
    "NRTK_REQUEST_MAINTENANCE_STATUS",
    "maintenance_status_request",
    "get_maintenance_status",
    "request_maintenance_status",
]

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
    parser = argparse.ArgumentParser(description="Probe Roborock mower maintenance status commands")
    parser.add_argument("--session-dir", required=True, help="Directory containing user_params.pkl")
    parser.add_argument("--device-name", default=None, help="Filter by device name")
    parser.add_argument("--device-duid", default=None, help="Filter by DUID")
    parser.add_argument(
        "--command",
        action="append",
        default=[],
        help="Command candidate to try. Can be passed multiple times.",
    )
    parser.add_argument("--timeout", type=float, default=8.0, help="Timeout per command in seconds")
    return parser.parse_args()


def encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("encode_varint only supports non-negative integers")
    encoded = bytearray()
    while value > 0x7F:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def encode_int32(field_number: int, value: int) -> bytes:
    return encode_varint(field_number << 3) + encode_varint(value)


def encode_int64(field_number: int, value: int) -> bytes:
    return encode_int32(field_number, value)


def encode_message(field_number: int, value: bytes) -> bytes:
    return encode_varint((field_number << 3) | 2) + encode_varint(len(value)) + value


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


def decode_protobuf_fields(data: bytes) -> list[dict[str, Any]]:
    fields = []
    offset = 0
    while offset < len(data):
        tag, offset = decode_varint(data, offset)
        field_number = tag >> 3
        wire_type = tag & 0x07
        field: dict[str, Any] = {"field": field_number, "wireType": wire_type}

        if wire_type == 0:
            value, offset = decode_varint(data, offset)
            field["value"] = value
        elif wire_type == 1:
            value = data[offset : offset + 8]
            offset += 8
            field["value"] = normalize(value)
        elif wire_type == 2:
            length, offset = decode_varint(data, offset)
            value = data[offset : offset + length]
            offset += length
            field["value"] = normalize(value)
            try:
                text = value.decode()
            except UnicodeDecodeError:
                text = None
            if text and all(char.isprintable() for char in text):
                field["text"] = text
        elif wire_type == 5:
            value = data[offset : offset + 4]
            offset += 4
            field["value"] = normalize(value)
        else:
            raise ValueError(f"Unsupported protobuf wire type {wire_type}")

        fields.append(field)
    return fields


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


def decode_pb_payload(payload: bytes | None) -> dict[str, Any] | None:
    if not payload or not payload.startswith(b"PB"):
        return None

    data = payload[2:]
    decoded: dict[str, Any] = {
        "protobufFields": decode_protobuf_fields(data),
        "maintenanceStatuses": [],
    }

    # Current mower responses wrap RobotMsg content in field 5. The maintenance
    # response itself is field 94 within that wrapper.
    for wrapper in get_length_fields(data, 5):
        for maintenance_response in get_length_fields(wrapper, 94):
            for status_message in get_length_fields(maintenance_response, 1):
                decoded["maintenanceStatuses"].append(decode_status_message(status_message))

    return normalize(decoded)


def build_maintenance_request_payload(maintenance_type: int = 0) -> bytes:
    """Build the RemoteMsg protobuf used by the official mower plugin.

    Reverse-engineered from the official Roborock mower app plugin:
    - RemoteMsg.id: field 1, int64
    - RemoteMsg.type: field 2, enum REQUEST_MAINTENANCE_STATUS = 114
    - RemoteMsg.maintenance_status_request: field 43, MaintenanceInfo
    - MaintenanceInfo.type: field 1, enum UNKNOWN/CAMERA/CHASSIS/CUTTING/EDGING/BATTERY
    """
    request_id = int(datetime.now(timezone.utc).timestamp() * 1000)
    maintenance_info = encode_int32(1, maintenance_type)
    return b"".join(
        [
            encode_int64(1, request_id),
            encode_int32(2, REMOTE_MSG_TYPE_REQUEST_MAINTENANCE_STATUS),
            encode_message(43, maintenance_info),
        ]
    )


def build_maintenance_request_object(maintenance_type: int = 0) -> dict[str, Any]:
    return {
        "id": int(datetime.now(timezone.utc).timestamp() * 1000),
        "type": REMOTE_MSG_TYPE_REQUEST_MAINTENANCE_STATUS,
        "maintenance_status_request": {
            "type": MAINTENANCE_TYPES[maintenance_type],
        },
    }


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
    if isinstance(value, bytes):
        return {
            "type": "bytes",
            "length": len(value),
            "hexPrefix": value[:64].hex(),
        }
    if isinstance(value, dict):
        return {str(k): normalize(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple, set)):
        return [normalize(v) for v in value]
    return value


def summarize_roborock_message(message: Any) -> dict[str, Any]:
    payload = getattr(message, "payload", None)
    summary = {
        "protocol": normalize(getattr(message, "protocol", None)),
        "seq": getattr(message, "seq", None),
        "version": normalize(getattr(message, "version", None)),
        "random": getattr(message, "random", None),
        "timestamp": getattr(message, "timestamp", None),
        "payload": normalize(payload),
    }

    if isinstance(payload, bytes):
        summary["payloadHex"] = payload.hex()
        try:
            summary["payloadText"] = payload.decode()
        except UnicodeDecodeError:
            pass
        else:
            try:
                summary["payloadJson"] = json.loads(summary["payloadText"])
            except json.JSONDecodeError:
                pass
        pb_decoded = decode_pb_payload(payload)
        if pb_decoded is not None:
            summary["pbDecoded"] = pb_decoded

    try:
        decoded = decode_rpc_response(message)
    except RoborockException as err:
        summary["v1DecodeError"] = str(err)
    except Exception as err:  # noqa: BLE001
        summary["v1DecodeError"] = f"{type(err).__name__}: {err}"
    else:
        summary["v1Decoded"] = normalize(decoded)

    return normalize(summary)


def lower_or_none(value: Any) -> str | None:
    return str(value).lower() if value is not None else None


def choose_device(devices: list[Any], device_name: str | None, device_duid: str | None) -> Any:
    if device_duid:
        for device in devices:
            if device.duid == device_duid:
                return device
        raise RuntimeError("No device found with the configured DUID.")

    if device_name:
        for device in devices:
            if device.name.lower() == device_name.lower():
                return device
        for device in devices:
            if device_name.lower() in device.name.lower():
                return device
        raise RuntimeError(f"No device found with name '{device_name}'.")

    for device in devices:
        category = lower_or_none(getattr(device.product, "category", None))
        if category and "mower" in category:
            return device

    raise RuntimeError("No mower device found.")


def schema_codes(device: Any) -> list[str]:
    return [
        str(getattr(item, "code", ""))
        for item in getattr(device.product, "schema", None) or []
        if getattr(item, "code", None)
    ]


async def try_command(
    device: Any,
    command: str,
    timeout: float,
    params: Any = None,
    *,
    force_mqtt: bool = False,
) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    original_rpc_channel = None
    try:
        if not device.v1_properties or not device.v1_properties.command:
            raise RuntimeError("Device does not expose v1_properties.command.")
        if force_mqtt:
            original_rpc_channel = device.v1_properties.command._rpc_channel
            device.v1_properties.command._rpc_channel = device._channel.mqtt_rpc_channel
        response = await asyncio.wait_for(
            device.v1_properties.command.send(command, params=params),
            timeout=timeout,
        )
        return {
            "command": command,
            "params": normalize(params),
            "ok": True,
            "startedAt": started.isoformat(),
            "response": normalize(response),
        }
    except Exception as err:  # noqa: BLE001
        return {
            "command": command,
            "params": normalize(params),
            "ok": False,
            "startedAt": started.isoformat(),
            "errorType": type(err).__name__,
            "error": str(err),
        }
    finally:
        if original_rpc_channel is not None:
            device.v1_properties.command._rpc_channel = original_rpc_channel


async def try_command_with_raw_capture(device: Any, command: str, timeout: float, params: Any = None) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    mqtt_channel = getattr(device._channel, "_mqtt_channel", None)
    if mqtt_channel is None:
        result = await try_command(device, command, timeout, params=params)
        result["rawCaptureError"] = "Device does not expose an MQTT channel."
        return result

    def capture(message: Any) -> None:
        messages.append(summarize_roborock_message(message))

    unsub = await mqtt_channel.subscribe(capture)
    try:
        result = await try_command(device, command, timeout, params=params, force_mqtt=True)
        await asyncio.sleep(1)
    finally:
        unsub()

    result["rawMessages"] = messages
    return result


async def main() -> int:
    args = parse_args()
    session_dir = pathlib.Path(args.session_dir).expanduser().resolve()
    user_params_path = session_dir / "user_params.pkl"
    if not user_params_path.exists():
        print("Missing login data. Run setup_roborock.py first.", file=sys.stderr)
        return 2

    user_params = await load_value(user_params_path)
    if user_params is None:
        print("Could not read user_params.pkl.", file=sys.stderr)
        return 3

    manager = await asyncio.wait_for(
        create_device_manager(user_params, cache=InMemoryCache()),
        timeout=25,
    )

    try:
        devices = await asyncio.wait_for(manager.get_devices(), timeout=25)
        device = choose_device(devices, args.device_name, args.device_duid)
        commands = args.command or DEFAULT_COMMANDS

        payload = {
            "fetchedAt": datetime.now(timezone.utc).isoformat(),
            "device": {
                "name": device.name,
                "firmware": getattr(device.device_info, "fv", None),
                "online": getattr(device.device_info, "online", None),
                "pv": getattr(device.device_info, "pv", None),
            },
            "product": {
                "name": getattr(device.product, "name", None),
                "model": getattr(device.product, "model", None),
                "category": lower_or_none(getattr(device.product, "category", None)),
                "schemaCodes": schema_codes(device),
            },
            "hasV1CommandTrait": bool(device.v1_properties and device.v1_properties.command),
            "officialPluginMaintenanceRequest": {
                "note": "Encoded RemoteMsg payload only; not sent by this script.",
                "remoteMsgType": {
                    "REQUEST_MAINTENANCE_STATUS": REMOTE_MSG_TYPE_REQUEST_MAINTENANCE_STATUS,
                },
                "maintenanceTypes": MAINTENANCE_TYPES,
                "responseStatusFields": MAINTENANCE_STATUS_RESPONSE_FIELDS,
                "requestAllStatusesObject": build_maintenance_request_object(0),
                "requestAllStatuses": normalize(build_maintenance_request_payload(0)),
                "requestAllStatusesBase64": base64.b64encode(build_maintenance_request_payload(0)).decode(),
            },
            "results": [],
        }

        payload["results"].append(
            await try_command_with_raw_capture(
                device,
                "remote_pb",
                args.timeout,
                params=build_maintenance_request_object(0),
            )
        )

        for command in commands:
            payload["results"].append(await try_command(device, command, args.timeout))

        print(json.dumps(normalize(payload), ensure_ascii=False))
        return 0
    finally:
        try:
            await asyncio.wait_for(manager.close(), timeout=5)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
