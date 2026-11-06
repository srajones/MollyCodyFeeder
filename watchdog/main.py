"""Daemon / one-shot entry point:  python -m watchdog.main [--check-now | --dispense-and-verify N | --analyze-wav F]

The implementation lives in `watchdog.service`; names are re-exported here for backward compatibility.
"""
import argparse
import json
import logging

from watchdog.config import WatchdogConfig
from watchdog.service.decision import decide_passed  # noqa: F401  (re-export)
from watchdog.service.watchdog import PuppyFeederWatchdog

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

__all__ = ["PuppyFeederWatchdog", "decide_passed", "main"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Molly & Cody Dog Feeder Anti-Clog Watchdog")
    parser.add_argument("--check-now", action="store_true", help="Inspect the bowl immediately")
    parser.add_argument("--dispense-and-verify", type=int, metavar="PORTIONS", help="Dispense N portions and verify")
    parser.add_argument("--mode", type=str, choices=["auto", "cloud", "lan"], help="Override FEEDER_MODE")
    parser.add_argument("--analyze-wav", type=str, help="Run acoustic clog analysis on an existing WAV file")
    args = parser.parse_args()

    cfg = WatchdogConfig()
    if args.mode:
        cfg.feeder_mode = args.mode
    watchdog = PuppyFeederWatchdog(cfg)

    if args.analyze_wav:
        print(watchdog.analyzer.analyze_dispense_audio(args.analyze_wav))
        return
    if args.dispense_and_verify:
        ok = watchdog.verify_feed_event(trigger_dispense=True, portions=args.dispense_and_verify)
        print(json.dumps(watchdog.latest_state, indent=2, default=str))
        raise SystemExit(0 if ok else 1)
    if args.check_now:
        ok = watchdog.verify_feed_event(trigger_dispense=False)
        print(json.dumps(watchdog.latest_state, indent=2, default=str))
        raise SystemExit(0 if ok else 1)
    watchdog.start_daemon()


if __name__ == "__main__":
    main()
