#!/usr/bin/env python3
"""One-time login/setup for MMM-Roborock."""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

from roborock.devices.device_manager import UserParams, create_device_manager
from roborock.devices.file_cache import FileCache, store_value
from roborock.web_api import RoborockApiClient


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Log in to Roborock and store a local session for MMM-Roborock")
    parser.add_argument("--email", required=True, help="Email address for your Roborock account")
    parser.add_argument(
        "--session-dir",
        default="data",
        help="Directory where login data and cache should be stored (default: data)"
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    session_dir = pathlib.Path(args.session_dir).expanduser().resolve()
    session_dir.mkdir(parents=True, exist_ok=True)

    user_params_path = session_dir / "user_params.pkl"
    cache_path = session_dir / "cache.pkl"

    print(f"Sending login code to {args.email} ...")
    web_api = RoborockApiClient(username=args.email)
    await web_api.request_code()
    code = input("Enter the code from the email: ").strip()

    if not code:
        print("No code entered.", file=sys.stderr)
        return 1

    print("Logging in ...")
    user_data = await web_api.code_login(code)
    base_url = await web_api.base_url

    user_params = UserParams(username=args.email, user_data=user_data, base_url=base_url)
    await store_value(user_params_path, user_params)

    print("Fetching device data ...")
    cache = FileCache(cache_path)
    manager = await create_device_manager(user_params, cache=cache)

    try:
        devices = await manager.get_devices()
        if not devices:
            print("No Roborock devices found on the account.", file=sys.stderr)
            return 2

        print("Found devices:")
        for device in devices:
            category = getattr(device.product.category, "value", str(device.product.category))
            print(
                f"- name='{device.name}' | duid='{device.duid}' | model='{device.product.model}' | "
                f"category='{category}' | firmware='{device.device_info.fv}'"
            )

        print()
        print(f"Login stored in: {user_params_path}")
        print(f"Cache stored in: {cache_path}")
        print("You can now configure MMM-Roborock in config.js.")
        return 0
    finally:
        await cache.flush()
        await manager.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
