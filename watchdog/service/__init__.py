"""Watchdog service layer.

    models       VerifyRequest / CaptureFiles / CaptureResult data objects
    decision     pure pass/fail + outcome rules
    verifier     FeedVerifier pipeline (capture -> analyze -> decide -> persist -> notify)
    scheduling   SchedulingMixin   meal jobs, retry/escalation, schedule-change alerts
    maintenance  MaintenanceMixin  hopper, eaten check, summary, retention, config checks
    http_api     HttpApiMixin      local HTTP API
    watchdog     PuppyFeederWatchdog composition root + daemon loop
"""
