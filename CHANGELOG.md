# Changelog

Each round lives on its own branch (`v0.N`, branched from the previous round) and contains ~20
improvements focused on one goal: **a clogged chute or empty hopper must never go unnoticed**.
Constraints: 2 vCPU / 4 GB shared VPS, no heavy dependencies, no regressions (tests must pass).

## v0.7.0 — Standalone Native Home Assistant HACS Integration (`custom_components/molly_cody_feeder/`)
Branched from `v0.6`. Goal: deliver a **100% standalone, native Home Assistant HACS custom integration** (`molly_cody_feeder` v0.7.0) with zero external VPS or external watchdog server dependency, featuring an **Ultra-Lightweight Audio-Only Anti-Clog Verification Mode** optimized for small-compute HAOS boxes (Raspberry Pi / HA Green / Yellow) alongside individually toggleable camera, TTS, and vision features.

1. **Ultra-Lightweight Audio-Only Anti-Clog Verification for Small-Compute HAOS Boxes (`core/molly/camera.py`, `imm_transport.py`, `core/analysis/audio.py`, `ting.py`, `flow.py`)**:
   - **Zero-Video, Zero-FFmpeg Audio Capture (`capture_audio_clip` / `audio_only=True`)**: On the feeder (`imm` `AES/KCP 330`), mic audio (`conv=2`) is 8000 Hz mono G.711 A-law (`~8 KB/s` — only `~100–240 KB` for a full meal). In Audio-Only mode (`enable_verification=True`, `enable_vision_verification=False`, `enable_camera=False`), the integration opens only `conv=0` + `conv=2` (`cmd 0x00040006` start audio), **skips starting the `conv=1` H.264 video stream (`cmd 6`)**, and decodes G.711 A-law directly in Python via a 256-entry lookup table and stdlib `wave` writer with **zero `ffmpeg` subprocesses, zero H.264 video decoding, and no `opencv` or `Pillow` needed**.
   - **Milliseconds of CPU & `< 2 MB` RAM**: Allocation-frugal 1D signal processing (supporting both NumPy and pure-Python stdlib `math`/`struct` fallback) separates **Motor Only** (`motor_no_kibble` clog/empty hopper: sustained `200–900 Hz` motor hum with near-zero kibble spikes) from **Motor + Food Dispensed** (`verified`/`dispensed`: `200–900 Hz` motor hum + `>= 15` kibble impact spikes in `1500–3800 Hz` and metallic bowl tings `ting_rate_per_sec >= 4.6`, or `< 4.6` `weak_flow`) and **No Motor** (`no_motor`).
2. **100% Standalone Native HACS Package (`custom_components/molly_cody_feeder/`, `hacs.json`, `manifest.json`)**:
   - Self-contained `core/` engine (`core/molly/`, `core/analysis/`, `core/service/`, `core/db.py`, `core/schedule_decoder.py`) running directly inside Home Assistant with zero imports from `watchdog.*`.
   - Hybrid `auto` / `cloud` / `lan` transport combining Molly & Cody OEM Cloud API (`a1.tuyaus.com`, nybble-swapped MD5 + HMAC-SHA256 signing, MD5/RSA login, atomic `0o600` session storage in `.storage`), Cloud MQTT 2.2 (`m1-us.mollycody.tech:8883`), and Local LAN TCP 6668 (`tinytuya` v3.4) with `_never_sent` anti-double-feed protection.
3. **Config Flow, Options Flow & Modular Compute Toggles (`config_flow.py`)**:
   - Interactive UI setup for Cloud and/or Local LAN connection, duplicate device guard, and runtime `OptionsFlow` for polling/hopper/verification thresholds and 6 modular compute toggles: `enable_camera`, `enable_tts`, `enable_verification`, `enable_vision_verification`, `enable_mp4_recording`, and `enable_auto_retry` (with `enable_verification=True`, `enable_vision_verification=False`, `enable_camera=False` as the lightweight HAOS default/recommended profile).
4. **Coordinator, Scheduled Meal Engine, 14 Services & Diagnostics (`coordinator.py`, `__init__.py`, `diagnostics.py`, `services.yaml`)**:
   - `MollyCodyDataUpdateCoordinator` polling status/DPs/schedule, computing 25+ `ha_summary` metrics, enforcing a shared `asyncio.Lock` across `imm` camera/audio/TTS sessions, running scheduled meal pre-roll verifications, pre-meal online checks, post-meal eaten checks, `no_food_hours` timer mode, and anti-jam auto-retry with `max_daily_retry_portions` and `hopper_empty_suspected` stop.
   - 14 custom services (`feed`, `verify_feed`, `check_bowl`, `set_schedule_slot`, `set_schedule_raw`, `sync_schedules`, `refill_hopper`, `capture_snapshot`, `play_tts`, `play_audio`, `play_chime`, `set_dp`, `run_online_check`, `clear_clog_alert`) and secret-redacted Home Assistant diagnostics.
5. **All 9 Native Entity Platforms (77 Entities)**:
   - `sensor.py` (22 sensors), `binary_sensor.py` (9 binary sensors including `DP 255 > 1` hardware fault detection), `switch.py` (16 switches: Slots 1–8 enable, DPs 101/103/104/134, 4 compute toggles), `button.py` (8 action buttons), `number.py` (12 numbers: manual portions, hopper capacity, portion weight, TTS volume, Slots 1–8 portions), `select.py` (4 selects: connection mode, night vision DP 108, motion sensitivity DP 106, alert mode), `camera.py` (`MollyCodyBowlCamera`), `image.py` (3 verification images: `annotated_bowl`, `before_feed`, `after_feed`), and `event.py` (`feed_event`, `alert_event`).
6. **Lovelace Dashboard, Automation Blueprints & Documentation**:
   - Ready-to-use `lovelace/molly_cody_dashboard.yaml`, automation blueprints (`clog_alert_notification.yaml`, `hopper_low_reminder.yaml`), and `info.md`.

## v0.6 — ML round (in progress)
Branched from `v0.5`. Goal: use the newly collected feed audio/video captures to train and
evaluate ML models for clog / empty-hopper / successful-feed detection.

**Ground truth**
1. `ml/review_bundle.py` + `ml/review_bundle.html`: offline review/labelling tool for `export_ml_bundle.sh` bundles
   (stdlib only, 127.0.0.1, never touches the feeder/cloud). Synced video + audio + spectrogram, draggable timeline
   segments (motor, kibble, dog in view / at bowl, jam, empty hopper), auto-detected motor/kibble suggestions,
   pre-filled sound answers. Labels: `ml/labels/feed_event_labels.json` (29 hand-checked events from Oct 30–Nov 5).

**Verdict fixes (found in the real data)**
2. Recording coverage (`recorded_wall_sec`, `audio_sec`, `audio_last_rx_sec`, `video_last_rx_sec`); new outcome
   `recording_cut_short` = UNVERIFIED, not a clog (no escalation, no TTS, no auto-retry). #1/#16/#17 were false clogs.
3. Audio-first: with complete audio and no kibble heard, vision can no longer flip the verdict to "fed"
   (`VISION_CAN_OVERRIDE_AUDIO=false`). #25 was a false "OK".
4. MP4 mux no longer uses `-shortest`: the audio can drop out while video continues; cutting the video to the audio
   length hid the rest of the meal (the AFTER photo showed footage that wasn't in the clip).
5. Notifications: plain text (no `**` in Home Assistant), sound first, camera marked secondary, no meaningless IR
   "0.0% → 0.0%" fill numbers, constant DP202/DP255 "weight=10g, abnormal=1" line removed.
6. Home Assistant payload: only real clog/no-food events set `clog_detected=true`; informational alerts send
   `food_dispensed=null`; new `verified` + `outcome` fields.

**Label-driven algorithm changes (29 hand-checked feeds; `tests/test_owner_labels.py` replays them)**
8. Fed vs. not fed: the production audio analyzer already matches the owner on every labelled feed (26 fed incl. 6 jams,
   2 "nothing happened"); locked in by a regression test. Motor start/stop match the owner's marks within ~0.3 s.
9. New `weak_flow` warning (`WEAK_FLOW_TING_RATE=4.6` tings per motor-second, soft "feed went through, but…" alert):
   catches all 6 owner-marked partial jams (3.2–4.6/s) with 3/17 false alarms on normal feeds. Plus the existing
   `short_run` (motor stopped early) which also covers #20/#25.
10. `watchdog/analysis/flow.py`: kibble-flow timeline (coverage, gaps, tail gap) stored with every event for future
    training. Not used for verdicts: on jams the motor keeps grinding loudly, so a level-dip gap detector misses them
    (findings in `ml/experiments/`).
11. Vision: the "dog at bowl" detector was wrong 14/15 times vs the owner → alerts no longer claim a dog (just "photos
    differ a lot"). The IR flag was right 20/20 (2 "glare" clips also read as IR).

**Web control page (`vps_install.sh`)**
12. Phone-friendly page served by the watchdog (`/`): camera photo on demand, speak through the feeder, feed 1-20
    portions (confirm), bowl check, last result / hopper / next meal. Uses the daemon's existing Tuya session.
13. `sudo ./vps_install.sh [--check|--uninstall]`: picks a free loopback port, publishes the API on 127.0.0.1 only via a
    git-ignored `docker-compose.override.yml`, adds a new nginx site (HTTPS, login, per-IP rate limits, token injected
    server-side, admin endpoints blocked), gets a Let's Encrypt cert with the same auto-renewal; `nginx -t` before every
    reload, automatic rollback, never touches other sites or compose projects.
15. Control page features: play each feed's recorded video (with sound, seekable) from the timeline; 👍/👎 "was this
    right?" per feed; Today card; 30-day trends chart (kibble flow per feed, confirmed feeds, motor s/portion, "dropping"
    warning); hopper bar + "I refilled it" + days of food left; feeding schedule; alert history (alerts are now logged).
16. Labelling on the server: `/label` serves the same labelling page against the live footage; labels are stored in the
    DB (`feed_labels`), the repo's 29 labels are imported once, and ML exports carry them automatically.
17. "⬇ Export for analysis" on the control page: streams a `.tar.gz` (labels + measurements / + audio / everything,
    last 7 / 30 days / all time) in the same layout as `export_ml_bundle.sh`, with the owner's labels inside.
18. Home Assistant setup page (`/ha`) + flat RESTful endpoint (`/ha/summary.json`): live data test table, checkboxes for
    which sensors to create (last meal, today's meals/portions/failed, hopper %/portions/days left, next meal, OK streak,
    problem binary sensor, feeder online), and copy-ready `secrets.yaml`, `configuration.yaml`, Lovelace card, Generic
    Camera, and low-hopper automation YAML.
19. Settings & Security page (`/system`) + host helper (`scripts/pp_agent.py`, `pp-agent.path`): edit `.env` settings
    (with effective defaults shown and secrets masked), change the web login username/password, rotate `WEBHOOK_SECRET`
    across `.env` and nginx atomically with automatic rollback, and run `update-pp --latest`, connection self-test,
    status check, or container restart from the browser.
20. Shared top navigation bar (`watchdog/web/static/nav.js`) on every web UI page (`🐶 Feeder`, `🏷 Labels`,
    `🏠 Home Assistant`, `⚙️ Settings`).
14. API hardening: feed portions validated (1-20), speaker/snapshot never interrupt a meal recording, fresh photos
    rate-limited to one per 10 s.

**Updates (`update-pp`)**

7. Meal-window/busy guard, build-before-switch, offline smoke test, pre-update DB backup, post-switch `/selftest`
   (existing session only, never logs in, never dispenses), automatic rollback to the previous image on the original
   branch, `--status` / `--selftest`, `data/update-history.log`; pp-settings Tools m/n/o. `/health` reports busy + next meal.

## v0.5 — configurable retention, ML JSONL export, kibble "ting" detector, mulaw speaker & pp-settings
1. **Configurable media vs. DB retention (`watchdog/service/retention.py`, `watchdog/db_retention.py`)**:
   - `KEEP_FOOTAGE=true|false` (`false` deletes media right after analysis while keeping all DB/JSONL metrics).
   - `CAPTURE_RETENTION_DAYS=30` (normal feeds) and `KEEP_FAILED_FOOTAGE_DAYS=90` (longer keep for clog/failed feeds; falls back to `CAPTURE_RETENTION_DAYS` if unset).
   - `DB_RETENTION_DAYS=0` keeps `feed_events` metric rows in SQLite forever by default.
   - Purging media nulls out `video_clip`, `audio_clip`, `before_image`, `after_image`, `annotated_image` and stamps `media_purged_at` so the DB stays honest.
2. **ML training JSONL export (`EXPORT_ML_ON_PRUNE=true`, `ML_EXPORT_PATH=data/ml_events.jsonl`)**:
   - Appends one training-ready JSON object per pruned event (timestamp, portions, success, clog, outcome, motor duration, `kibble_peaks`, `kibble_rate`, `ting_count`, `ting_rate_per_sec`, fill deltas, diagnosis, warnings) and stamps `ml_exported_at` so events are never exported twice.
3. **Scheduled cleanup (`PuppyFeederWatchdog`)**:
   - Runs `prune_captures()` once at daemon startup and every hour (`schedule.every().hour.do(self.prune_captures)`), plus immediately after each verification when `KEEP_FOOTAGE=false`.
4. **Spectral kibble "ting" detector (`watchdog/analysis/ting.py`)**:
   - Detects metallic pellet-on-steel impacts (`600–1500 Hz` + `>2500 Hz` over `1500–2500 Hz` auger grind) achieving 11/11 (100%) accuracy across all user-labeled clips.
   - Escalates to an urgent clog/no-food alert after `NO_TING_ESCALATE_AFTER=2` consecutive feeds without the ting (`1` miss = heads-up).
5. **Speaker TTS hardware codec fix (`watchdog/molly/speaker.py`, `imm_codec.py`)**:
   - Switched speaker TX from A-law to **G.711 μ-law (`mulaw`, `0xFF` silence)** with `dynaudnorm` + presence EQ + soft limiter (`+4.5 dB` louder, zero static/clipping).
6. **Quiet "no food for X hours" alert mode & `pp-settings` (`scripts/pp_settings.py`, `install_ubuntu.sh`)**:
   - Added `ALERT_MODE=no_food_hours` (`NO_FOOD_ALERT_HOURS`, `FOOD_CHECK_INTERVAL_MIN`) and the interactive `pp-settings` CLI menu on the VPS covering alerts, `HA_WEBHOOK_URL`, voice volume, and retention/ML export.

## v0.4 — refactor round (no behavior changes except the fixes listed)
**Structure**
1. `molly_client.py` (2,484 lines) → `watchdog/molly/` package: 13 modules, `MollyFeederClient` facade composed of mixins.
2. `main.py` (953 lines) → `watchdog/service/` package; `main.py` is now a 46-line entry point.
3. The 330-line `_verify_feed_event` → `FeedVerifier` pipeline (capture → analyze → decide → persist → notify) with injected dependencies.
4. Pure decision rules + outcome constants in `service/decision.py`; plain dataclasses in `service/models.py`.
5. HTTP API: nested 200-line handler → top-level `WatchdogApiHandler` with route tables (one method per endpoint).
6. `analyzer.py` (637 lines) → `watchdog/analysis/` (audio, vision, llm, stream_capture, learning, results).
7. `review_server.py` (929 lines) → `watchdog/review/` (manager, server) + `web/templates/review.html`.
8. `watchdog/paths.py` and `watchdog/env.py` replace scattered `__file__` path math and dotenv calls.
9. Backward-compatible shims keep every old import path working.
10. `docs/ARCHITECTURE.md`: design rules + where a future logged-in web UI plugs in.

**Fixes found while refactoring/testing**
11. `.env` overrode real environment variables (Docker/CLI overrides were ignored) — real env now wins; legacy `USER=`/`PASS=` are mapped to `MOLLY_USER`/`MOLLY_PASS` so the OS `$USER` can't hijack the login.
12. On-demand checks are logged as `bowl_check` (were `scheduled_verify`) and log "BOWL CHECK OK" instead of "FEED VERIFIED".
13. Lint-clean (`ruff` F-rules) across the repo; dead code / unused imports removed.

**Verification**: 22 unit/integration tests (incl. FeedVerifier pipeline with fakes and HTTP busy/refill),
plus live hardware checks on the refactored code: status, snapshot, A/V clip, speaker TTS, CLI commands,
daemon start → HTTP API (auth 401/200, 409 busy guard) → live on-demand verification → graceful SIGTERM, Review Lab.

## v0.3
1. **Timezone bug**: the Docker image had no `tzdata`/`TZ`, so on a UTC VPS every meal check fired hours off. Added `tzdata`, `TZ` in `.env.example`, startup warning.
2. Startup config validation (device id, session/login, no alert channel configured, TZ).
3. Dispense DP publish retried only when the request provably never left (DNS/connect failure) — never on ambiguous timeouts (no double feeding).
4. On-demand checks (`/webhook/check`, `kibble verify`) no longer raise "FEEDER DID NOT RUN"; they answer "is there food?" (`bowl_check`).
5. `clog_detected` / status no longer set for `bowl_check` and `camera_unavailable` outcomes.
6. Graceful SIGTERM/SIGINT shutdown (Docker stop finishes cleanly).
7. Schedule-change alerts (meal removed/disabled = urgent; changed/added = info).
8. "No enabled meals" alert.
9. One verification at a time (lock); HTTP returns 409 instead of piling up camera sessions.
10. Daily summary queries the whole day (was last 50 events).
11. SQLite hot backup throttled to every 10 min (was a full copy per event).
12. "Puppy may not be eating" daylight re-check (`EATEN_CHECK_MINUTES`).
13. `scripts/calibrate.py`: label-driven report (agreement, impacts/s margin).
14. Tiny numpy logistic-regression model trained from labels (with leave-one-out accuracy), only when enough labels exist.
15. `ml_prob_dispensed` + "learned model disagrees" note in diagnoses (rules stay authoritative).
16. Review Lab: "classifier disagrees with your label" / "model unsure" badges (active learning).
17. Review Lab: night-vision verdict and kibble flow shown per clip.
18. **Leftovers no longer mask a clog**: when a dispense is expected and audio was captured, visible old food can't pass a meal.
19. Pass/fail logic extracted to `decide_passed()` with a decision-matrix test.
20. Motor window = longest sustained sound block (a bark/voice before the meal in the 70 s scheduled recording was taken as the motor → false clog).

## v0.2
1. Kibble flow metric: impacts per motor-second (`kibble_rate_per_sec`) on every analysis.
2. `short_run` warning — motor stopped far earlier than expected for the requested portions (jam / partial dispense).
3. `low_flow` warning — kibble flow far below normal (partial clog, hopper running low, overfull bowl). Warnings never flip a pass into a fail.
4. Learned per-feeder baselines (median seconds-per-portion and impact rate from recent verified feeds) replace fixed constants once 3+ samples exist.
5. Additive DB migration: `outcome`, `motor_duration_sec`, `kibble_rate` columns (old DBs keep working).
6. Hopper estimate (portions used since last refill) + low-hopper alert (12 h cooldown).
7. CLI `kibble hopper` / `kibble refill [--capacity N]`.
8. HTTP `GET /hopper` and `POST /webhook/refill`.
9. Unverified CLI feeds (`feed --no-verify`) are logged as feed events so hopper/daily totals include them.
10. Scheduled checks log the meal's real portions from DP 207 (were logged as 0 → stats/hopper undercounted).
11. Pre-meal online check (`PRE_MEAL_ONLINE_CHECK_SECONDS`) warns while there is still time to fix power/Wi-Fi.
12. Dead-man heartbeat (`HEARTBEAT_URL`, every 5 min).
13. Daily summary push (`DAILY_SUMMARY_TIME`), non-urgent unless something failed.
14. Alert cooldown per event type (`ALERT_COOLDOWN_MINUTES`) + urgent/normal priority (ntfy).
15. `/snapshot.jpg?fresh=1` rate-limited to one live capture per 30 s.
16. Camera session retried once on setup failure — never after the dispense command was sent.
17. A camera failure no longer blocks feeding: the dispense still happens (unverified).
18. `camera_unavailable` outcome: no false clog alert and no blind auto-retry when nothing could be captured.
19. TTS audio cache (`data/tts_cache`, 50 phrases): instant repeat alerts, works when the TTS service is down.
20. Review Lab: optional `REVIEW_TOKEN`, request-size cap, portions/duration clamped.

## v0.1
1. **Speaker TTS fixed** — talk frames must carry the talk-open reqId (was 0 → camera silently dropped audio). Verified on-device via mic loopback.
2. TTS soft limiter (`alimiter`) — a high `TTS_VOLUME` can no longer hard-clip into garbled audio.
3. Regression test suite (`tests/`, stdlib `unittest`): talk framing, DP 207 codec, labeled-clip verdicts, webhook auth, retention, pre-roll.
4. Removed hard-coded device id / local key / uid / user code defaults from source; `MOLLY_USER` / `MOLLY_PASS` (bare `USER` collides with the OS username).
5. Session file written atomically with `chmod 600` (bind-mount-safe fallback).
6. Webhook API: constant-time token check (old substring match accepted any header containing the secret), auth on all GETs except `/health`, 64 KB body cap, threaded server, default bind `127.0.0.1` + warning when exposed without a secret.
7. `/health` is liveness-only (no cloud API call on every Docker probe).
8. Gemini API key sent in the `x-goog-api-key` header instead of the URL.
9. ntfy: one push with the photo attached (was two); Telegram: HTML escaping (Markdown mode rejected `_`/`*`).
10. `.env.example` rebuilt from the keys the code actually reads; `MAX_RETRY_Attempts` typo removed.
11. Global camera-session lock — schedule, webhooks and the review server can't open overlapping sessions.
12. Outcome classes `dispensed` / `motor_no_kibble` / `no_motor` with specific alert titles (feeder never ran vs. clog/empty).
13. Overfeed guard (`MAX_DAILY_RETRY_PORTIONS`) + "hopper likely empty" escalation that stops futile retries.
14. **IR night-vision fix** — the daylight metric read 0 % for every night frame, so every night feed was failed as "empty bowl". At night vision now only confirms new kibble (exposure-normalized change / reflection occlusion) and the audio decides otherwise.
15. Recording keeps JPEG-compressed frames instead of raw RGB (~2.7 MB → ~0.1 MB per frame; long feeds could OOM a 4 GB VPS).
16. **Recording auto-extends while the motor is still running** (multi-portion feeds; 3 of 8 labeled clips were cut off mid-dispense).
17. **Event-relative BEFORE/AFTER frames** chosen around the motor window detected in the audio (dispense start delay varies — one clip started 9 s late).
18. **Scheduled-meal pre-roll** (`MEAL_PREROLL_SECONDS`, default 40) so the feeder's own dispense is inside the recording.
19. Capture retention (`CAPTURE_RETENTION_DAYS`, `CAPTURE_MAX_MB`).
20. Docker: DB inside the mounted volume (was lost on rebuild), non-root user, memory/CPU/pids limits, log rotation, `edge-tts` + `espeak-ng` so TTS works in the container at all.

Docs: README protocol section corrected (DP 207 is hex/decimal fields, transport is KCP + HMAC-SHA1 + AES-CBC, control messages are binary).
