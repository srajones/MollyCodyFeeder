"""Molly & Cody (and.mollycody.app01) OEM cloud constants and the device DP name table."""



# -----------------------------------------------------------------------------
# Molly & Cody (and.mollycody.app01) OEM Constants
# -----------------------------------------------------------------------------
APP_ID = "wr7phe79kh4ntutc8g43"


APP_SECRET = "k893qxgpcesx7sdvhvchdtk797pw8cs9"


BMP_KEY = "c5xjxyfm738qpwka9ng9x5jjs3uqq5y3"


CERT_SHA256 = (
    "D8:EA:C4:82:81:DC:9F:B5:F2:99:64:69:76:B3:80:73:"
    "F4:DB:AC:6E:F1:35:B9:6D:E9:D9:3E:40:54:82:FD:97"
)


PKG_NAME = "and.mollycody.app01"


HMAC_KEY = f"{PKG_NAME}_{CERT_SHA256}_{BMP_KEY}_{APP_SECRET}"


API_URL = "https://a1.tuyaus.com/api.json"


MQTT_HOST = "m1-us.mollycody.tech"


MQTT_PORT = 8883


SIGNED_KEYS = {
    "chKey", "sp", "a", "v", "lat", "lon", "lang", "deviceId",
    "appVersion", "ttid", "isH5", "h5Token", "os", "clientId",
    "postData", "time", "requestId", "et", "n4h5", "sid",
}


DP_NAMES = {
    "101": "indicator_light",
    "103": "flip_screen",
    "104": "osd_watermark",
    "106": "motion_sensitivity",
    "108": "nightvision",
    "115": "movement_detect_pic",
    "134": "motion_switch",
    "201": "feed_num",
    "202": "food_weight_g",
    "203": "control",
    "204": "realtime_data",
    "205": "weight",
    "206": "history_data",
    "207": "schedule",
    "208": "feed_voice_record",
    "212": "initiative_message",
    "255": "feed_abnormal",
}
