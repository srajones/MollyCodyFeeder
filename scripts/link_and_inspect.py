import argparse
import json
import os
import sys
import time
from pathlib import Path

import qrcode
from dotenv import load_dotenv
from tuya_sharing.user import LoginControl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from watchdog.sharing_client import DEFAULT_SESSION_FILE, HA_CLIENT_ID, MC_SCHEMA, TuyaSharingFeederClient


def generate_and_wait_for_qr(
    user_code: str, schema: str, qr_out_paths: list[Path], wait_seconds: int = 240
) -> bool:
    lc = LoginControl()
    res = lc.qr_code(client_id=HA_CLIENT_ID, schema=schema, user_code=user_code)
    if not res.get("success"):
        print(f"Failed to request QR code for USER_CODE={user_code}, schema={schema}: {res}")
        return False

    token = res["result"]["qrcode"]
    qr_uri = f"tuyaSmart--qrLogin?token={token}"

    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(qr_uri)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    for p in qr_out_paths:
        p.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(p))
        print(f"Saved login QR code to: {p}")

    print(f"\nSchema : {schema}")
    print(f"QR URI : {qr_uri}")
    print("Waiting for you to scan the QR code in the Molly & Cody app (Me -> top-right Scan icon)...")

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        ok, info = lc.login_result(token=token, client_id=HA_CLIENT_ID, user_code=user_code)
        if ok:
            session_payload = {
                "user_code": user_code,
                "schema": schema,
                "terminal_id": info.get("terminal_id"),
                "endpoint": info.get("endpoint"),
                "token_info": {
                    "t": info.get("t"),
                    "uid": info.get("uid"),
                    "expire_time": info.get("expire_time"),
                    "access_token": info.get("access_token"),
                    "refresh_token": info.get("refresh_token"),
                },
            }
            DEFAULT_SESSION_FILE.write_text(json.dumps(session_payload, indent=2))
            print(f"\n✅ Successfully authenticated! Session saved to {DEFAULT_SESSION_FILE}")
            return True
        time.sleep(2)

    print("\n⏰ Timed out waiting for QR code scan.")
    return False


def inspect_account(user_code: str) -> None:
    client = TuyaSharingFeederClient(user_code=user_code)
    client.connect()
    print(f"\nTotal devices on account: {len(client.manager.device_map)}")
    for d in client.manager.device_map.values():
        print(f"\n================ DEVICE: {d.name} ================")
        print(f"Device ID  : {d.id}")
        print(f"Product ID : {d.product_id}")
        print(f"Category   : {d.category}")
        print(f"Online     : {d.online}")
        print(f"Local Key  : {d.local_key}")
        print(f"Public IP  : {d.ip}")
        print(f"Status DPs : {json.dumps(d.status, indent=2)}")
        print("Functions  :")
        for code, fn in (d.function or {}).items():
            print(f"  - {code} ({fn.type}): {fn.values}")

    meals = client.get_scheduled_meals()
    print("\n================ DECODED FEED SCHEDULE ================")
    if meals:
        for m in meals:
            print(f"  • {m.time_str} -> {m.portions} portion(s) (days_mask=0b{m.days_mask:07b})")
    else:
        print("  (No decodable meal_plan DP found in status; check raw Status DPs above)")

    stream_url = client.allocate_stream_url()
    print("\n================ LIVE STREAM ALLOCATION ================")
    print(f"Stream URL : {stream_url}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--qr-only", action="store_true", help="Generate QR code and exit immediately")
    parser.add_argument("--force-relink", action="store_true", help="Generate new QR code even if session exists")
    parser.add_argument("--schema", type=str, default=MC_SCHEMA, help="Tuya OEM app schema (default: mollycodyapp01)")
    parser.add_argument("--artifact-qr", type=str, default="", help="Additional path to save QR PNG")
    args = parser.parse_args()

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    user_code = os.getenv("USER_CODE", "").strip()
    if not user_code:
        print("ERROR: USER_CODE not found in .env")
        sys.exit(1)

    qr_paths = [Path(__file__).resolve().parent.parent / "tuya_login_qr.png"]
    if args.artifact_qr:
        qr_paths.append(Path(args.artifact_qr))

    if args.force_relink or not DEFAULT_SESSION_FILE.exists():
        if args.qr_only:
            generate_and_wait_for_qr(user_code, args.schema, qr_paths, wait_seconds=0)
            return
        if not generate_and_wait_for_qr(user_code, args.schema, qr_paths, wait_seconds=260):
            sys.exit(1)

    inspect_account(user_code)


if __name__ == "__main__":
    main()
