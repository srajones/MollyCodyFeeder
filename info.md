# 🐶 Molly & Cody Smart Pet Feeder (`molly_cody_feeder` v0.7.0)

**Molly & Cody Smart Pet Feeder** (`molly_cody_feeder`) is a **100% standalone, native Home Assistant HACS integration** for the **Molly & Cody 6L Smart Pet Feeder with Camera** (`and.mollycody.app01` / Tuya OEM `yv8xga2javjxtqdc`).

> **Zero External VPS or Watchdog Server Required:** Everything runs natively inside Home Assistant (`custom_components/molly_cody_feeder/`). Camera streaming, speaker TTS, and acoustic/visual anti-clog verification are modular and individually toggleable in the integration's Config & Options Flow so low-compute Home Assistant hardware (such as Raspberry Pi, Home Assistant Green, or Home Assistant Yellow) only runs the features you choose.

---

## ⚡ Ultra-Lightweight Audio-Only Anti-Clog Verification for Small-Compute HAOS Boxes

- **Small-Compute HAOS Ready (Raspberry Pi / HA Green / Yellow)**:
  - On the feeder (`imm` `AES/KCP 330` protocol), microphone audio (`conv=2`) is **8000 Hz mono G.711 A-law** (`~8 KB/s` — only `~100–240 KB` for a full meal).
  - Recommended lightweight HAOS profile: `enable_verification=True`, `enable_vision_verification=False`, `enable_camera=False`.
  - **Zero-Video, Zero-FFmpeg Capture (`audio_only=True`)**: Opens only `conv=0` + `conv=2` (`cmd 0x00040006` start audio), **skips starting the `conv=1` H.264 video stream (`cmd 6`)**, and decodes G.711 A-law directly in Python via a 256-entry lookup table and stdlib `wave` writer with **zero `ffmpeg` subprocesses, zero H.264 video decoding, and no `opencv` or `Pillow` required**.
  - **Milliseconds of CPU & `< 2 MB` RAM**: Fast 1D acoustic signal processing (supporting both NumPy and pure-Python stdlib `math`/`struct` fallback) accurately separates:
    - **Motor Only (`motor_no_kibble` / Clog or Empty Hopper)**: sustained `200–900 Hz` motor hum (`motor_detected=True`) with near-zero kibble impact spikes/tings (`kibble_dispensed_audio=False`).
    - **Motor + Food Dispensed (`verified` / `dispensed` / Fed)**: sustained `200–900 Hz` motor hum + clear kibble impact clatter (`>= 15` spikes in `1500–3800 Hz`) and metallic bowl tings (`ting_rate_per_sec >= 4.6` normal, `< 4.6` `weak_flow`).
    - **No Motor (`no_motor`)**: feeder never ran (`motor_detected=False`).

---

## ✨ Highlights

- **Hybrid Cloud + Local LAN Transport (`auto` / `cloud` / `lan`)**:
  - Direct **Molly & Cody OEM Cloud API** (`a1.tuyaus.com`, HMAC-SHA256 signing, MD5/RSA login, automatic session persistence in `.storage`) + **Cloud MQTT 2.2** (`m1-us.mollycody.tech:8883`) + **Local LAN TCP 6668** (`tinytuya` v3.4) with automatic failover and `_never_sent` anti-double-feed protection.
- **Modular Compute Feature Toggles (Config Flow & Options Flow)**:
  - `enable_verification` — Ultra-lightweight Audio-Only anti-clog verification (`conv=2` 8 kHz G.711 A-law, 10ms RMS `200–900 Hz` motor window + `1500–3800 Hz` kibble clatter `>= 15` spikes + 256-pt STFT spectral-flux `ting` detector `ting_rate_per_sec >= 4.6` + learned per-feeder baselines; default `True`).
  - `enable_camera` — Optional live 720×1280 portrait snapshots via Cloud `imm` (`AES/KCP 330`) protocol (`False` on lightweight HAOS profile).
  - `enable_tts` — Two-way speaker talkback, custom audio playback, and meal chime (`8000 Hz` mono **G.711 μ-law**, `codec=0x86`, `reqId=7`; default `True`).
  - `enable_vision_verification` — Optional OpenCV/Pillow `BowlAnalyzer` inner-bowl kibble fill analysis (daylight & infrared night-vision modes, dog-at-bowl guard, and annotated ROI images; default `False` on lightweight HAOS profile).
  - `enable_mp4_recording` — Optional full-feed H.264 MP4 + AAC clip recording and automated media retention pruning (default `False`).
  - `enable_auto_retry` — 1-portion anti-jam auto-retry on `motor_no_kibble`, guarded by `max_daily_retry_portions` and `hopper_empty_suspected` escalation (default `True`).
- **Complete Entity Suite across 9 Platforms (77 Entities)**:
  - **22 Sensors**, **9 Binary Sensors**, **16 Switches** (Schedule Slots 1–8 enable, Hardware DPs 101/103/104/134, and runtime compute toggles), **8 Buttons**, **12 Numbers** (Manual feed portions, Hopper capacity, Portion weight, TTS volume, and Slots 1–8 portions), **4 Selects** (Connection mode, Night vision DP 108, Motion sensitivity DP 106, Alert mode), **1 Camera**, **3 Verification Images** (`before_feed`, `after_feed`, `annotated_bowl`), and **2 Event Entities** (`feed_event`, `alert_event`).
- **14 Native Home Assistant Services, Lovelace Dashboard & Automation Blueprints**:
  - Includes ready-to-import `lovelace/molly_cody_dashboard.yaml` and automation blueprints in `blueprints/automation/molly_cody_feeder/`.

---

## 🚀 Quick Setup via HACS

1. In Home Assistant, open **HACS → Integrations → ⋮ (top right) → Custom repositories**.
2. Add this repository URL with category **Integration** and click **Download**.
3. Restart Home Assistant.
4. Go to **Settings → Devices & Services → Add Integration** and search for **Molly & Cody Smart Feeder**.
5. Enter your Molly & Cody Cloud credentials (or Local LAN IP + Local Key) and select which optional camera/audio/vision verification features to enable for your Home Assistant hardware.
