#!/usr/bin/env python3
"""
`kibble` / `feeder_cli.py` — Unified 100% Cloud-ready CLI for the Molly & Cody Smart Feeder 6L Camera
(`and.mollycody.app01`).

Features:
- 100% Cloud operation (`FEEDER_MODE=cloud`) for remote Ubuntu VPS deployment, plus `auto` and `lan` modes
- Live 720x1280 upright HD camera snapshots & MP4+WAV audio/video recording (`imm` AES/KCP 330 over Cloud TURN)
- Two-Way Speaker Text-to-Speech (`kibble say "..."`) & audio file playback (`kibble play file.wav`)
- Full Visual + Acoustic + Telemetry anti-clog verification (`kibble verify` / `kibble feed 1 --verify`)
- Crash-safe WAL SQLite database (`kibble_history.db`) with automatic online backup (`kibble history`)
- Schedule management (`kibble schedule list` / `kibble schedule set ...`) & raw DP control (`kibble dp ...`)
"""
import argparse
import json
import re
import sys
from pathlib import Path
from typing import List

from watchdog.alerter import HouseholdAlerter
from watchdog.config import WatchdogConfig
from watchdog.db import KibbleDatabase
from watchdog.service.watchdog import PuppyFeederWatchdog
from watchdog.molly import MollyFeederClient
from watchdog.schedule_decoder import MealEntry, decode_molly_schedule, parse_days_spec


def _print_schedule_table(raw_str: str, meals: List[MealEntry]) -> None:
    print(f"\nFeeding Schedule (DP 207 raw: {raw_str or 'N/A'}):")
    print("-" * 66)
    print(f"{'Slot':<6} {'Time':<8} {'Portions':<10} {'Status':<10} {'Days'}")
    print("-" * 66)
    for m in meals:
        st = "ENABLED" if m.enabled else "disabled"
        print(f"#{m.slot:<5} {m.time_str:<8} {m.portions:<10} {st:<10} {m.days_str}")
    print("-" * 66)


def _print_history(db: KibbleDatabase, limit: int = 15, show_actions: bool = False, as_json: bool = False) -> None:
    stats = db.get_stats()
    events = db.get_recent_events(limit=limit)
    actions = db.get_recent_actions(limit=limit) if show_actions else []
    if as_json:
        print(
            json.dumps(
                {"stats": stats, "recent_events": events, "recent_actions": actions},
                indent=2,
            )
        )
        return

    print("\n=== Kibble Watchdog SQLite History (WAL + FULL Sync) ===")
    print(f"Database Path:       {db.db_path}")
    print(f"Backup Path:         {db.backup_path} (exists={db.backup_path.exists()})")
    print(
        f"Total Verifications: {stats['total_events']} "
        f"(Successful: {stats['successful_events']}, Clogs: {stats['clogs_detected']}, "
        f"Portions Dispensed: {stats['total_portions_dispensed']}, "
        f"Success Rate: {stats['success_rate_pct']}%)"
    )
    print("-" * 100)
    print(
        f"{'ID':<5} {'Timestamp':<20} {'Type':<23} {'Result':<8} {'Portions':<8} "
        f"{'Audio':<16} {'Fill (Before->After)':<22} {'Flags'}"
    )
    print("-" * 100)
    for ev in events:
        res_str = "OK" if ev["success"] else "CLOG!"
        fill_str = f"{ev['before_fill_pct']:.1f}% -> {ev['after_fill_pct']:.1f}%"
        flags = []
        if ev.get("infrared_mode"):
            flags.append("IR-NIGHT")
        if ev.get("dog_in_bowl"):
            flags.append("DOG-EATING")
        if ev.get("kibble_peaks"):
            flags.append(f"peaks={ev['kibble_peaks']}")
        flag_str = ",".join(flags) or "-"
        print(
            f"#{ev['id']:<4} {ev['timestamp']:<20} {ev['event_type']:<23} {res_str:<8} "
            f"{ev['portions']:<8} {ev['audio_state']:<16} {fill_str:<22} {flag_str}"
        )
    print("-" * 100)

    if show_actions and actions:
        print("\nRecent Device & CLI Actions:")
        print("-" * 90)
        for act in actions:
            ok_s = "OK" if act["success"] else "FAIL"
            print(f"#{act['id']:<4} {act['timestamp']:<20} {act['action']:<18} {ok_s:<6} {act['summary']}")
        print("-" * 90)


def _update_env_key(env_path: Path, key: str, value: str) -> None:
    if not env_path.exists():
        env_path.write_text(f"{key}={value}\n", encoding="utf-8")
        return
    content = env_path.read_text(encoding="utf-8")
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(content):
        content = pattern.sub(f"{key}={value}", content)
    else:
        if not content.endswith("\n"):
            content += "\n"
        content += f"{key}={value}\n"
    env_path.write_text(content, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="kibble",
        description="Molly & Cody Smart Dog Feeder CLI & Anti-Clog Watchdog (100% Cloud + Two-Way Speaker TTS)",
    )
    parser.add_argument(
        "--mode",
        choices=["auto", "cloud", "lan"],
        default=None,
        help="Connection mode override: 'cloud' (100%% Cloud for VPS), 'auto', or 'lan'",
    )

    sub = parser.add_subparsers(dest="cmd", required=True)

    # status
    sub.add_parser("status", help="Show live feeder status, DPs, weight, decoded schedule, and last DB event")

    # feed
    p_feed = sub.add_parser("feed", help="Dispense N portions of food (1-20), optionally with full AV verification")
    p_feed.add_argument("portions", type=int, nargs="?", default=1, help="Number of portions (1-20)")
    p_feed.add_argument(
        "--verify",
        action="store_true",
        help="Capture Before/After 720x1280 HD snapshots + live MP4/WAV audio during dispense and verify bowl fill",
    )

    # verify / check
    for alias in ("verify", "check"):
        p_ver = sub.add_parser(alias, help="Run a live visual + acoustic + telemetry bowl verification right now")
        p_ver.add_argument(
            "--dispense",
            type=int,
            default=0,
            metavar="PORTIONS",
            help="Optionally dispense N portions during the verification window",
        )
        p_ver.add_argument(
            "--no-tts",
            action="store_true",
            help="Suppress feeder speaker TTS alert if a clog/empty bowl is detected during this check",
        )

    # say / tts / speak
    for alias in ("say", "tts", "speak"):
        p_say = sub.add_parser(
            alias,
            help="Speak a text-to-speech message through the smart feeder's built-in speaker over Cloud",
        )
        p_say.add_argument("text", nargs="+", help="Message text to speak through the feeder speaker")
        p_say.add_argument(
            "--voice",
            type=str,
            default=None,
            help="Neural TTS voice (default: en-US-AriaNeural; e.g. en-US-GuyNeural, en-GB-SoniaNeural)",
        )
        p_say.add_argument("--volume", type=float, default=None, help="Speaker volume multiplier (default: 2.2)")
        p_say.add_argument("--repeat", type=int, default=1, help="Number of times to repeat the message (1-5)")
        p_say.add_argument("--no-chime", action="store_true", help="Disable the two-tone attention chime before speech")

    # play
    p_play = sub.add_parser("play", help="Play an audio file (.wav, .mp3, .m4a) through the feeder's speaker")
    p_play.add_argument("audio_file", type=str, help="Path to audio file")
    p_play.add_argument("--volume", type=float, default=2.0, help="Volume multiplier (default: 2.0)")
    p_play.add_argument("--chime", action="store_true", help="Prepend two-tone attention chime")

    # snapshot
    p_snap = sub.add_parser("snapshot", help="Capture a live upright 720x1280 HD JPEG snapshot over Cloud")
    p_snap.add_argument("output", nargs="?", default="captures/snapshot.jpg", help="Output JPEG path")

    # record
    p_rec = sub.add_parser(
        "record",
        help="Record a live upright 720x1280 HD MP4 video + 8kHz WAV audio clip from the feeder camera",
    )
    p_rec.add_argument("seconds", type=int, nargs="?", default=8, help="Duration in seconds (default: 8)")
    p_rec.add_argument("output", nargs="?", default="captures/clip.mp4", help="Output MP4 path")

    # schedule
    p_sched = sub.add_parser("schedule", help="List or modify feeding schedule slots (DP 207)")
    sched_sub = p_sched.add_subparsers(dest="sched_cmd", required=True)
    sched_sub.add_parser("list", help="List all schedule slots")

    p_sched_set = sched_sub.add_parser("set", help="Modify or enable/disable a schedule slot (1-8)")
    p_sched_set.add_argument("slot", type=int, help="Slot number (1-8)")
    p_sched_set.add_argument("--time", type=str, help="Time in HH:MM (24-hour format, e.g. 07:00)")
    p_sched_set.add_argument("--portions", type=int, help="Number of portions (1-20)")
    p_sched_set.add_argument(
        "--days",
        type=str,
        help="Days: 'everyday', 'weekdays', 'weekends', 'once', or comma list e.g. 'mon,wed,fri'",
    )
    en_group = p_sched_set.add_mutually_exclusive_group()
    en_group.add_argument("--enable", action="store_true", help="Enable this schedule slot")
    en_group.add_argument("--disable", action="store_true", help="Disable this schedule slot")

    # dp
    p_dp = sub.add_parser(
        "dp",
        help="Set a raw Data Point (101 indicator, 103 flip, 104 osd, 108 nightvision [0=auto,1=off,2=on], 201 feed)",
    )
    p_dp.add_argument("dp_id", type=int, help="DP ID (e.g. 101, 103, 104, 108, 134, 201, 207)")
    p_dp.add_argument("value", type=str, help="Value ('true', 'false', integer, or string)")

    # history
    p_hist = sub.add_parser("history", help="View crash-safe SQLite feed verification history & statistics")
    p_hist.add_argument("--limit", type=int, default=15, help="Number of rows to display (default: 15)")
    p_hist.add_argument("--actions", action="store_true", help="Also display device/CLI action audit logs")
    p_hist.add_argument("--json", action="store_true", help="Output raw JSON")

    # webhook
    p_wh = sub.add_parser("webhook", help="Show or test Home Assistant / outbound webhook configuration")
    p_wh.add_argument("--url", type=str, default=None, help="Save a new HA_WEBHOOK_URL to .env")
    p_wh.add_argument("--test", action="store_true", help="Send a test webhook event to HA_WEBHOOK_URL")

    # mode
    p_mode = sub.add_parser("mode", help="Show or persist FEEDER_MODE (cloud, auto, lan) in .env")
    p_mode.add_argument("new_mode", nargs="?", choices=["cloud", "auto", "lan"], help="New FEEDER_MODE to save")

    # hopper / refill
    sub.add_parser("hopper", help="Show the estimated kibble left in the hopper since the last refill")
    p_refill = sub.add_parser("refill", help="Mark the hopper as refilled (restarts the low-kibble estimate)")
    p_refill.add_argument("--capacity", type=int, default=None, help="Portions a full hopper holds")

    # export-ml
    p_exp = sub.add_parser("export-ml", help="Package SQLite metrics + JSONL + captures (or --no-media) into exports/*.tar.gz")
    p_exp.add_argument("--with-media", action="store_true", help="Include captured MP4/WAV/JPG files from ./captures (default)")
    p_exp.add_argument("--no-media", action="store_true", help="Metrics only (skip MP4/WAV/JPG capture files)")
    p_exp.add_argument("--days", type=float, default=0, help="Only include capture media newer than N days")
    p_exp.add_argument("--with-review", action="store_true", help="Also include captures/review_clips/ media")
    p_exp.add_argument("--with-backups", action="store_true", help="Include up to 3 recent DB backups from data/backups/")
    p_exp.add_argument("--out", type=str, default="", help="Custom output .tar.gz path")

    # daemon
    sub.add_parser("daemon", help="Run the 24/7 anti-clog watchdog daemon + HTTP webhook server")

    args = parser.parse_args()
    if args.cmd == "export-ml":
        import subprocess

        script = Path(__file__).resolve().parent / "scripts" / "export_ml_bundle.sh"
        cmd = [str(script)]
        if args.no_media:
            cmd.append("--no-media")
        elif args.with_media:
            cmd.append("--with-media")
        if args.days and args.days > 0:
            cmd += ["--days", str(args.days)]
        if args.with_review:
            cmd.append("--with-review")
        if args.with_backups:
            cmd.append("--with-backups")
        if args.out:
            cmd += ["--out", args.out]
        sys.exit(subprocess.call(cmd))
    cfg = WatchdogConfig()
    if args.mode:
        cfg.feeder_mode = args.mode

    db = KibbleDatabase(cfg.db_path)

    if args.cmd == "history":
        _print_history(db, limit=args.limit, show_actions=args.actions, as_json=args.json)
        return

    if args.cmd in ("hopper", "refill"):
        if args.cmd == "refill":
            cap = args.capacity or cfg.hopper_capacity_portions
            db.record_refill(cap, note="cli")
            print(f"Hopper marked as refilled ({cap} portions).")
        hop = db.hopper_status(cfg.hopper_capacity_portions)
        if not hop["known"]:
            print("No refill recorded yet — run `./kibble refill` right after filling the hopper.")
        else:
            print(
                f"Hopper: ~{hop['percent_left']:.0f}% left — {hop['portions_left']}/{hop['capacity_portions']} portions "
                f"(used {hop['portions_used']} since refill at {hop['refilled_at']})"
            )
        return

    if args.cmd == "mode":
        env_file = Path(__file__).resolve().parent / ".env"
        if args.new_mode:
            _update_env_key(env_file, "FEEDER_MODE", args.new_mode)
            db.log_action("set_mode", args.new_mode, True, f"Updated FEEDER_MODE={args.new_mode} in .env")
            print(f"Updated FEEDER_MODE={args.new_mode} in {env_file}")
        else:
            print(f"Current FEEDER_MODE: {cfg.feeder_mode.upper()}")
        return

    if args.cmd == "webhook":
        def _mask_url(val: str) -> str:
            if not val:
                return "(not set)"
            return ("••••" + val[-4:]) if len(val) > 4 else "••••"

        env_file = Path(__file__).resolve().parent / ".env"
        if args.url is not None:
            _update_env_key(env_file, "HA_WEBHOOK_URL", args.url)
            cfg.ha_webhook_url = args.url
            print(f"Saved HA_WEBHOOK_URL={_mask_url(args.url)} to {env_file}")
        print(f"HA_WEBHOOK_URL:      {_mask_url(cfg.ha_webhook_url)}")
        print(f"Inbound HTTP Server: http://{cfg.webhook_listen_host}:{cfg.webhook_listen_port}")
        if args.test:
            alerter = HouseholdAlerter(cfg)
            ok = alerter.send_ha_webhook(
                event="test_webhook",
                clog_detected=False,
                title="Kibble Watchdog Test Webhook",
                message="Test notification from kibble CLI.",
                details={"mode": cfg.feeder_mode},
                force=True,
            )
            print(f"Webhook test sent -> ok={ok}")
            sys.exit(0 if ok else 1)
        return

    if args.cmd == "daemon":
        watchdog = PuppyFeederWatchdog(cfg)
        watchdog.start_daemon()
        return

    client = MollyFeederClient(
        email=cfg.molly_user,
        password=cfg.molly_pass,
        mode=cfg.feeder_mode,
        device_id=cfg.tuya_device_id,
        local_ip=cfg.feeder_local_ip,
        local_key=cfg.feeder_local_key,
    )

    if args.cmd == "status":
        st = client.get_status()
        last_ev = db.get_last_feed()
        print(f"Device:           {st['device_name']} ({st['device_id']})")
        print(f"Configured Mode:  {st['configured_mode'].upper()}")
        print(f"Active Transport: {st['active_transport'].upper()}")
        print(f"Online:           {st['online']}")
        print(f"Bowl Weight (g):  {st['food_weight_g']}")
        print(f"Last Feed (201):  {st['last_feed_portions']} portion(s)")
        print(f"Abnormal (255):   {st['feed_abnormal']}")
        print(f"TTS Clog Alert:   {'ENABLED' if cfg.tts_alert_on_clog else 'disabled'} ({cfg.tts_voice})")
        if last_ev:
            print(
                f"Last DB Event:    #{last_ev['id']} at {last_ev['timestamp']} "
                f"[{'OK' if last_ev['success'] else 'CLOG!'}] ({last_ev['diagnosis']})"
            )
        print(f"Named DPs:        {json.dumps(st['named_dps'], indent=2)}")
        meals = decode_molly_schedule(st["schedule_raw"])
        _print_schedule_table(st["schedule_raw"], meals)
        return

    if args.cmd == "feed":
        if args.verify:
            watchdog = PuppyFeederWatchdog(cfg)
            ok = watchdog.verify_feed_event(trigger_dispense=True, portions=args.portions)
            print(json.dumps(watchdog.latest_state, indent=2))
            sys.exit(0 if ok else 1)
        else:
            ok, transport = client.dispense_food(args.portions)
            # Also a feed event so hopper accounting / daily totals include unverified feeds
            db.log_feed_event(
                event_type="manual_feed_unverified",
                mode=client.mode,
                transport=transport,
                portions=args.portions if ok else 0,
                success=ok,
                clog_detected=False,
                diagnosis="Dispensed without camera verification",
                outcome="unverified",
            )
            db.log_action(
                action="manual_feed",
                transport=transport,
                success=ok,
                summary=f"Dispensed {args.portions} portion(s) without camera verification",
                payload={"portions": args.portions},
            )
            if ok:
                print(f"Successfully dispensed {args.portions} portion(s) via {transport.upper()}.")
            else:
                print(f"Failed to dispense food (mode={client.mode}).", file=sys.stderr)
                sys.exit(1)
            return

    if args.cmd in ("verify", "check"):
        watchdog = PuppyFeederWatchdog(cfg)
        trigger = args.dispense > 0
        portions = max(1, args.dispense) if trigger else 1
        ok = watchdog.verify_feed_event(
            trigger_dispense=trigger,
            portions=portions,
            announce_clog_tts=not args.no_tts,
        )
        print(json.dumps(watchdog.latest_state, indent=2))
        sys.exit(0 if ok else 1)

    if args.cmd in ("say", "tts", "speak"):
        msg_text = " ".join(args.text).strip()
        voice = args.voice or cfg.tts_voice
        vol = args.volume if args.volume is not None else cfg.tts_volume
        print(
            f"Streaming TTS over 100% Cloud speaker (voice={voice}, volume={vol}, repeat={args.repeat}): "
            f"{msg_text!r}..."
        )
        res = client.speak_tts(
            text=msg_text,
            voice=voice,
            volume=vol,
            repeat=args.repeat,
            prepend_chime=not args.no_chime,
        )
        db.log_action(
            action="cli_say",
            transport="cloud_imm",
            success=bool(res.get("ok")),
            summary=f"Spoke on feeder speaker: {msg_text}",
            payload=res,
        )
        print(json.dumps(res, indent=2))
        sys.exit(0 if res.get("ok") else 1)

    if args.cmd == "play":
        print(f"Streaming audio file {args.audio_file} to feeder speaker over Cloud...")
        res = client.play_audio_file(
            audio_path=args.audio_file,
            volume=args.volume,
            prepend_chime=args.chime,
        )
        db.log_action(
            action="cli_play_audio",
            transport="cloud_imm",
            success=bool(res.get("ok")),
            summary=f"Played audio file {args.audio_file} on feeder speaker",
            payload=res,
        )
        print(json.dumps(res, indent=2))
        sys.exit(0 if res.get("ok") else 1)

    if args.cmd == "schedule":
        if args.sched_cmd == "list":
            raw, meals = client.get_schedule()
            _print_schedule_table(raw, meals)
            return
        if args.sched_cmd == "set":
            hour = None
            minute = None
            if args.time:
                if ":" not in args.time:
                    parser.error("--time must be in HH:MM format")
                h_s, m_s = args.time.split(":", 1)
                hour, minute = int(h_s), int(m_s)
            days_mask = parse_days_spec(args.days) if args.days else None
            enabled = True if args.enable else (False if args.disable else None)
            ok, transport, updated = client.update_schedule_slot(
                slot=args.slot,
                hour=hour,
                minute=minute,
                portions=args.portions,
                days_mask=days_mask,
                enabled=enabled,
            )
            db.log_action(
                action="schedule_set",
                transport=transport,
                success=ok,
                summary=f"Updated schedule slot #{args.slot}",
                payload={"slot": args.slot, "time": args.time, "portions": args.portions, "enabled": enabled},
            )
            if ok:
                print(f"Updated slot #{args.slot} via {transport.upper()}.")
                raw_new = "".join(m.to_molly_slot() for m in updated)
                _print_schedule_table(raw_new, updated)
            else:
                print("Failed to update schedule.", file=sys.stderr)
                sys.exit(1)
            return

    if args.cmd == "snapshot":
        out = str(Path(args.output).resolve())
        print(f"Capturing live 720x1280 upright HD snapshot via Cloud imm (mode={client.mode.upper()})...")
        ok = client.capture_snapshot(out)
        db.log_action("snapshot", client.mode, ok, f"Captured snapshot to {out}")
        if ok:
            print(f"Saved upright 720x1280 HD snapshot to: {out}")
        else:
            print("Failed to capture snapshot.", file=sys.stderr)
            sys.exit(1)
        return

    if args.cmd == "record":
        out_mp4 = str(Path(args.output).resolve())
        out_wav = str(Path(out_mp4).with_suffix(".wav"))
        print(
            f"Recording {args.seconds}s live 720x1280 upright HD video + 8kHz audio via Cloud imm "
            f"(mode={client.mode.upper()})..."
        )
        res = client.capture_av_clip(duration_sec=float(args.seconds), output_mp4=out_mp4, output_wav=out_wav)
        db.log_action(
            "record_av",
            client.mode,
            bool(res.get("video_ok") and res.get("audio_ok")),
            f"Recorded {args.seconds}s AV clip to {out_mp4} and {out_wav}",
            payload=res,
        )
        print(json.dumps(res, indent=2))
        if not res.get("video_ok"):
            sys.exit(1)
        return

    if args.cmd == "dp":
        val_str = args.value.strip()
        if val_str.lower() == "true":
            val: object = True
        elif val_str.lower() == "false":
            val = False
        elif args.dp_id == 108:
            # DP 108 (nightvision) is an enum string: "0" (Auto), "1" (Off), "2" (On)
            val = str(val_str)
        else:
            try:
                val = int(val_str)
            except ValueError:
                val = val_str
        ok, transport = client.set_dp(args.dp_id, val)
        db.log_action(
            action=f"set_dp_{args.dp_id}",
            transport=transport,
            success=ok,
            summary=f"Set DP {args.dp_id} = {val!r}",
            payload={"dp_id": args.dp_id, "value": val},
        )
        print(f"Set DP {args.dp_id} = {val!r} -> ok={ok} (transport={transport})")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
