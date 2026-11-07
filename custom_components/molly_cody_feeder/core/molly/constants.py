"""Molly & Cody Smart Feeder OEM Cloud & Device DP constants."""
from __future__ import annotations

APP_ID = "wr7phe79kh4ntutc8g43"
APP_SECRET = "48f5v77xxf7354533u5r3c3t8vpxfuuv"
CERT_SHA256 = "EA:2C:70:C3:8E:06:B2:46:4F:51:F9:C4:F9:33:E6:75:B1:D3:53:46:89:35:0A:A7:91:70:D4:1B:E4:5E:EC:CD"
PKG_NAME = "and.mollycody.app01"
HMAC_KEY = f"A_3347896d734525a1983f3a0496303d85_{PKG_NAME}_{CERT_SHA256}_{APP_SECRET}"

API_URL = "https://a1.tuyaus.com/api.json"
MQTT_HOST = "m1-us.mollycody.tech"
MQTT_PORT = 8883

SIGNED_KEYS = [
    "a",
    "v",
    "lat",
    "lon",
    "lang",
    "deviceId",
    "appVersion",
    "ttid",
    "isH5",
    "h5Token",
    "os",
    "clientId",
    "postData",
    "time",
    "requestId",
    "et",
    "n4h5",
    "sid",
    "sp",
    "chKey",
]

DP_NAMES = {
    "101": "indicator_light",
    "103": "flip_screen",
    "104": "osd_watermark",
    "106": "motion_sensitivity",
    "108": "nightvision",
    "109": "sd_card_status",
    "110": "sd_card_capacity",
    "134": "motion_switch",
    "161": "doorbell_pic",
    "201": "feed_num",
    "202": "food_weight_g",
    "207": "schedule",
    "255": "feed_abnormal",
}
