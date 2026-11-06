"""Cloud account session: persistence, signed API calls, self-healing login, MQTT credentials."""

import hashlib
import hmac
import json
import logging
import os
import random
import ssl
import string
import time
import uuid
from typing import Any, Dict, Optional, Tuple

import paho.mqtt.client as mqtt
import requests

from watchdog.molly.constants import API_URL, APP_ID, HMAC_KEY, PKG_NAME
from watchdog.molly.crypto import _build_sign_input, _get_ch_key, _md5_hex, _rsa_encrypt_password

logger = logging.getLogger("poor_puppy.molly.cloud")


class CloudSessionMixin:
    """Session persistence + signed cloud API calls with automatic re-login."""

    # -------------------------------------------------------------------------
    # Session Persistence & Self-Healing Cloud Login
    # -------------------------------------------------------------------------
    def _load_session(self) -> bool:
        if not self.session_file.exists():
            return False
        try:
            data = json.loads(self.session_file.read_text())
            if data.get("sid") and data.get("uid") and data.get("ecode"):
                self.sid = data["sid"]
                self.uid = data["uid"]
                self.ecode = data["ecode"]
                self.partner_id = data.get("partnerIdentity", self.partner_id)
                self.gid = int(data.get("gid", 0))
                self.device_id = data.get("devId", self.device_id)
                self.local_key = data.get("localKey", self.local_key)
                self.local_ip = data.get("localIp", self.local_ip)
                self.device_name = data.get("deviceName", self.device_name)
                self.cached_dps = data.get("dps", self.cached_dps)
                return True
        except Exception as exc:
            logger.warning("Could not load session file %s: %s", self.session_file, exc)
        return False

    def _save_session(self) -> None:
        try:
            self.session_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "email": self.email,
                "sid": self.sid,
                "uid": self.uid,
                "ecode": self.ecode,
                "partnerIdentity": self.partner_id,
                "gid": self.gid,
                "devId": self.device_id,
                "localKey": self.local_key,
                "localIp": self.local_ip,
                "deviceName": self.device_name,
                "dps": self.cached_dps,
                "updatedAt": int(time.time()),
            }
            data = json.dumps(payload, indent=2)
            # Atomic write (a crash mid-write must not destroy the session). A bind-mounted
            # single file (Docker) can't be replaced, so fall back to an in-place write.
            tmp = self.session_file.with_name(self.session_file.name + ".tmp")
            try:
                fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.session_file)
            except OSError:
                tmp.unlink(missing_ok=True)
                self.session_file.write_text(data)
            try:
                os.chmod(self.session_file, 0o600)  # holds sid + device local key
            except OSError:
                pass
        except Exception as exc:
            logger.warning("Failed to save session file %s: %s", self.session_file, exc)

    def _raw_api_call(
        self,
        action: str,
        version: str,
        post_data_obj: Dict[str, Any],
        sid: str = "",
        gid: int = 0,
    ) -> Dict[str, Any]:
        if action.startswith("thing."):
            action = "smartlife." + action[len("thing.") :]
        req_id = str(uuid.uuid4())
        post_data_json = json.dumps(post_data_obj, separators=(",", ":"))

        params: Dict[str, Any] = {
            "a": action,
            "v": version,
            "clientId": APP_ID,
            "appVersion": "1.2.0",
            "os": "Android",
            "lang": "en",
            "sdkVersion": "5.14.0",
            "deviceCoreVersion": "5.14.0",
            "ttid": "sdk_",
            "osSystem": "14",
            "requestId": req_id,
            "platform": "SM-S938U1",
            "timeZoneId": "America/New_York",
            "et": "0.0.1",
            "channel": "sdk",
            "appRnVersion": "5.49",
            "customDomainSupport": "1",
            "nd": "true",
            "bizData": json.dumps(
                {"customDomainSupport": "1", "nd": "true", "sdkInt": "34", "brand": "samsung"},
                separators=(",", ":"),
            ),
            "chKey": _get_ch_key(),
            "time": str(int(time.time())),
            "deviceId": _md5_hex(self.email or "mollycody") + "000000000000",
            "postData": post_data_json,
        }
        if sid:
            params["sid"] = sid
        if gid:
            params["gid"] = str(gid)

        sign_in = _build_sign_input(params)
        sig = hmac.new(HMAC_KEY.encode("utf-8"), sign_in.encode("utf-8"), hashlib.sha256).hexdigest().lower()

        query_params = {k: v for k, v in params.items() if k not in ("postData", "time", "sign")}
        form_body = dict(params)
        form_body["sign"] = sig

        resp = requests.post(
            API_URL,
            params=query_params,
            data=form_body,
            headers={"User-Agent": "okhttp/4.9.1"},
            timeout=12,
        )
        resp.raise_for_status()
        return resp.json()

    def login(self, force: bool = False) -> bool:
        """
        Authenticates with the Molly & Cody Tuya OEM cloud using USER and PASS,
        discovers the feeder device & home group ID, and persists the session.
        """
        with self._lock:
            if not force and self.sid and self.uid and self.ecode:
                # Verify existing session quickly
                if self.refresh_cloud_devices(allow_relogin=False):
                    return True

            if not self.email or not self.password:
                raise RuntimeError("Missing USER or PASS in .env for Molly & Cody cloud login")

            logger.info("Authenticating with Molly & Cody Cloud as %s...", self.email)
            tok_res = self._raw_api_call(
                "smartlife.m.user.username.token.get",
                "2.0",
                {"countryCode": self.country_code, "username": self.email, "isUid": False},
            )
            if not tok_res.get("success"):
                raise RuntimeError(f"Token fetch failed: {tok_res}")

            t_info = tok_res["result"]
            token = t_info["token"]
            md5_pwd = _md5_hex(self.password)

            login_res = self._raw_api_call(
                "smartlife.m.user.email.password.login",
                "3.0",
                {
                    "countryCode": self.country_code,
                    "email": self.email,
                    "passwd": md5_pwd,
                    "options": '{"group": 1,"mfaCode": ""}',
                    "token": token,
                    "ifencrypt": 0,
                },
            )

            if not login_res.get("success"):
                time.sleep(1.0)
                tok_res2 = self._raw_api_call(
                    "smartlife.m.user.username.token.get",
                    "2.0",
                    {"countryCode": self.country_code, "username": self.email, "isUid": False},
                )
                if tok_res2.get("success"):
                    t2 = tok_res2["result"]
                    enc_pwd = _rsa_encrypt_password(self.password, t2["publicKey"], t2["exponent"])
                    login_res = self._raw_api_call(
                        "smartlife.m.user.email.password.login",
                        "3.0",
                        {
                            "countryCode": self.country_code,
                            "email": self.email,
                            "passwd": enc_pwd,
                            "options": '{"group": 1,"mfaCode": ""}',
                            "token": t2["token"],
                            "ifencrypt": 1,
                        },
                    )

            if not login_res.get("success"):
                raise RuntimeError(f"Molly & Cody Cloud login failed: {login_res}")

            user_info = login_res["result"]
            self.sid = user_info["sid"]
            self.uid = user_info["uid"]
            self.ecode = user_info["ecode"]
            self.partner_id = user_info.get("partnerIdentity", self.partner_id)
            logger.info("Successfully logged into Molly & Cody Cloud (uid=%s)", self.uid)

            self.refresh_cloud_devices(allow_relogin=False)
            self._save_session()
            return True

    def call_api(
        self,
        action: str,
        version: str,
        post_data_obj: Dict[str, Any],
        gid: Optional[int] = None,
        allow_relogin: bool = True,
    ) -> Dict[str, Any]:
        """Calls a Molly & Cody Cloud API endpoint with automatic self-healing re-login on session expiry."""
        with self._lock:
            if not self.sid or not self.uid or not self.ecode:
                self.login(force=True)

            use_gid = gid if gid is not None else self.gid
            res = self._raw_api_call(action, version, post_data_obj, sid=self.sid, gid=use_gid)
            if not res.get("success") and allow_relogin:
                err_code = str(res.get("errorCode", "")).upper()
                if any(k in err_code for k in ("SESSION", "EXPIRE", "LOGIN", "AUTH", "USER")):
                    logger.warning("Cloud session expired (%s); re-authenticating automatically...", err_code)
                    self.login(force=True)
                    use_gid = gid if gid is not None else self.gid
                    res = self._raw_api_call(action, version, post_data_obj, sid=self.sid, gid=use_gid)
            return res

    def refresh_cloud_devices(self, allow_relogin: bool = True) -> bool:
        """Refreshes home groups and feeder device metadata/DPs from Molly & Cody Cloud."""
        with self._lock:
            if not self.gid:
                h_res = self.call_api("smartlife.m.location.list", "2.1", {}, gid=0, allow_relogin=allow_relogin)
                if h_res.get("success") and isinstance(h_res.get("result"), list) and h_res["result"]:
                    self.gid = int(h_res["result"][0].get("groupId") or h_res["result"][0].get("id") or 0)

            if not self.gid:
                self.gid = 313530733

            d_res = self.call_api(
                "m.life.my.group.device.list",
                "2.2",
                {"gid": self.gid},
                gid=self.gid,
                allow_relogin=allow_relogin,
            )
            if not d_res.get("success"):
                return False

            res_obj = d_res.get("result")
            devs = res_obj if isinstance(res_obj, list) else (res_obj.get("devs", []) if isinstance(res_obj, dict) else [])
            for dev in devs:
                if not isinstance(dev, dict):
                    continue
                if dev.get("devId") == self.device_id or "feeder" in dev.get("name", "").lower() or dev.get("category") == "sp":
                    self.device_id = dev.get("devId", self.device_id)
                    self.local_key = dev.get("localKey", self.local_key)
                    self.device_name = dev.get("name", self.device_name)
                    self.is_online = bool(dev.get("cloudOnline", dev.get("isOnline", True)))
                    raw_dps = dev.get("dps") or dev.get("dataPointInfo", {}).get("dps")
                    if isinstance(raw_dps, dict):
                        self.cached_dps = {str(k): v for k, v in raw_dps.items()}
                    self._save_session()
                    return True
            return False

    # -------------------------------------------------------------------------
    # MQTT Credentials Helper (for Cloud Control & WebRTC Signaling)
    # -------------------------------------------------------------------------
    def _get_mqtt_credentials(self) -> Tuple[str, str, str]:
        if not self.sid or not self.uid or not self.ecode:
            self.login()
        ch_key = _get_ch_key()
        u_h1 = _md5_hex(APP_ID)
        u_h2 = _md5_hex(u_h1 + self.ecode)
        username = f"{self.partner_id}_v1_{APP_ID}_{ch_key}_mb_{self.sid}{u_h2[-16:]}"

        p_h1 = _md5_hex(HMAC_KEY)
        p_h2 = _md5_hex(p_h1 + self.ecode)
        password = p_h2[8:24]

        rand_suffix = "".join(random.choices(string.hexdigits.lower(), k=8))
        dev_hash = _md5_hex(self.uid + "sdkfasodifca")
        client_id = f"{PKG_NAME}_mb_{rand_suffix}_{dev_hash}_DEFAULT"
        return client_id, username, password

    def _create_mqtt_client(self) -> mqtt.Client:
        client_id, username, password = self._get_mqtt_credentials()
        mclient = mqtt.Client(client_id=client_id)
        mclient.username_pw_set(username, password)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        mclient.tls_set_context(ctx)
        return mclient
