"""Molly & Cody Cloud API calls, login, session persistence, and MQTT client factory."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import ssl
import time
import types
import uuid
from typing import Any, Dict, Tuple

import requests

from custom_components.molly_cody_feeder.core.molly.constants import (
    API_URL,
    APP_ID,
    PKG_NAME,
)
from custom_components.molly_cody_feeder.core.molly.crypto import (
    _get_ch_key,
    _hmac_sha256_sign,
    _rsa_encrypt_pkcs1,
)
from custom_components.molly_cody_feeder.core.molly.net import _retry_if_unreached

try:
    import paho.mqtt.client as mqtt  # type: ignore[import-not-found]
except ImportError:
    mqtt = types.ModuleType("paho.mqtt.client")  # type: ignore[assignment]

    class _DummyMqttClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.on_connect = None
            self.on_message = None

        def username_pw_set(self, *args: Any, **kwargs: Any) -> None:
            pass

        def tls_set(self, *args: Any, **kwargs: Any) -> None:
            pass

        def connect(self, *args: Any, **kwargs: Any) -> int:
            if callable(self.on_connect):
                self.on_connect(self, None, None, 0)
            return 0

        def loop_start(self) -> None:
            pass

        def loop_stop(self) -> None:
            pass

        def disconnect(self) -> None:
            pass

        def subscribe(self, *args: Any, **kwargs: Any) -> Tuple[int, int]:
            return (0, 1)

        def publish(self, *args: Any, **kwargs: Any) -> Any:
            return types.SimpleNamespace(rc=0)

    mqtt.Client = _DummyMqttClient  # type: ignore[attr-defined]

logger = logging.getLogger("molly_cody_feeder.molly.cloud")


class CloudAuthMixin:
    """OEM cloud session, login, and MQTT client creation."""

    def _load_session(self) -> bool:
        if not self.session_file.exists():
            return False
        try:
            data = json.loads(self.session_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not (data.get("sid") and data.get("uid") and data.get("ecode")):
                return False
            self.sid = str(data["sid"])
            self.uid = str(data["uid"])
            self.ecode = str(data["ecode"])
            self.partner_id = str(data.get("partner_id") or data.get("partnerIdentity") or "p2155085")
            self.gid = int(data.get("gid", 0))
            if not self.device_id:
                self.device_id = str(data.get("device_id") or data.get("devId") or "")
            if not self.local_key:
                self.local_key = str(data.get("local_key") or data.get("localKey") or "")
            if not self.local_ip:
                self.local_ip = str(data.get("local_ip") or data.get("localIp") or "")
            dev_name = data.get("device_name") or data.get("deviceName")
            if dev_name:
                self.device_name = str(dev_name)
            raw_dps = data.get("cached_dps") if isinstance(data.get("cached_dps"), dict) else data.get("dps")
            if isinstance(raw_dps, dict):
                self.cached_dps = {str(k): v for k, v in raw_dps.items()}
            return True
        except Exception as exc:
            logger.debug("Could not load session file %s: %s", self.session_file, exc)
            return False

    def _save_session(self) -> None:
        try:
            self.session_file.parent.mkdir(parents=True, exist_ok=True)
            now_ts = int(time.time())
            data = {
                "email": self.email,
                "sid": self.sid,
                "uid": self.uid,
                "ecode": self.ecode,
                "partner_id": self.partner_id,
                "partnerIdentity": self.partner_id,
                "gid": self.gid,
                "device_id": self.device_id,
                "devId": self.device_id,
                "device_name": self.device_name,
                "deviceName": self.device_name,
                "local_key": self.local_key,
                "localKey": self.local_key,
                "local_ip": self.local_ip,
                "localIp": self.local_ip,
                "cached_dps": self.cached_dps,
                "dps": self.cached_dps,
                "saved_at": now_ts,
                "updatedAt": now_ts,
            }
            payload = json.dumps(data, indent=2).encode("utf-8")
            tmp_path = self.session_file.with_suffix(".json.tmp")
            fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.chmod(str(tmp_path), 0o600)
            os.replace(str(tmp_path), str(self.session_file))
            try:
                os.chmod(str(self.session_file), 0o600)
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
        post_data = json.dumps(post_data_obj, separators=(",", ":"))
        params = {
            "a": action,
            "v": version,
            "lang": "en",
            "deviceId": self.mobile_device_id,
            "os": "Android",
            "osSystem": "12",
            "clientId": APP_ID,
            "appVersion": "1.0.5",
            "ttid": f"android@{PKG_NAME}",
            "isH5": "0",
            "postData": post_data,
            "time": str(int(time.time())),
            "requestId": str(uuid.uuid4()),
            "et": "0.0.1",
            "sdkVersion": "4.0.0",
            "sp": "0",
            "chKey": _get_ch_key(),
            "channel": "sdk",
            "timeZoneId": "America/New_York",
        }
        if sid:
            params["sid"] = sid
        if gid:
            params["gid"] = str(gid)
            params["bizData"] = json.dumps({"nd": "1", "gid": gid}, separators=(",", ":"))

        sig = _hmac_sha256_sign(params)
        query = {k: v for k, v in params.items() if k not in ("postData", "time", "sign")}
        form_body = dict(params)
        form_body["sign"] = sig
        resp = _retry_if_unreached(
            lambda: requests.post(
                API_URL,
                params=query,
                data=form_body,
                headers={"User-Agent": "TUYA_IOT_SDK"},
                timeout=12,
            )
        )
        if hasattr(resp, "raise_for_status"):
            resp.raise_for_status()
        return resp.json()

    def call_api(
        self,
        action: str,
        version: str,
        post_data_obj: Dict[str, Any],
        gid: int | None = None,
        allow_relogin: bool = True,
    ) -> Dict[str, Any]:
        with self._lock:
            if (not self.sid or not self.uid or not self.ecode) and allow_relogin and self.email and self.password:
                self.login(force=True)

            use_gid = gid if gid is not None else self.gid
            res = self._raw_api_call(action, version, post_data_obj, sid=self.sid, gid=use_gid)
            if not res.get("success") and allow_relogin:
                err_code = str(res.get("errorCode", "")).upper()
                if any(k in err_code for k in ("SESSION", "EXPIRE", "LOGIN", "AUTH", "USER")):
                    logger.info("Cloud session expired (%s), re-authenticating...", err_code)
                    if self.login(force=True):
                        use_gid = gid if gid is not None else self.gid
                        res = self._raw_api_call(action, version, post_data_obj, sid=self.sid, gid=use_gid)
            return res

    def login(self, force: bool = False) -> bool:
        with self._lock:
            if not force and self.sid and self.uid and self.ecode:
                if self.device_id and self.local_key:
                    return True
                if self.refresh_cloud_devices():
                    return True
            if not self.email or not self.password:
                raise RuntimeError("Missing email or password for Molly & Cody cloud login")

            tok_res = self._raw_api_call(
                "smartlife.m.user.username.token.get",
                "1.0",
                {"countryCode": self.country_code, "username": self.email, "isUid": False},
            )
            if not tok_res.get("success"):
                raise RuntimeError(f"Failed to acquire login token: {tok_res}")

            res_tok = tok_res["result"]
            token = res_tok["token"]
            pub_key = res_tok["publicKey"]
            exponent = res_tok.get("exponent", "65537")
            md5_pw = hashlib.md5(self.password.encode("utf-8")).hexdigest().lower()

            login_res = self._raw_api_call(
                "smartlife.m.user.email.password.login",
                "1.0",
                {
                    "countryCode": self.country_code,
                    "email": self.email,
                    "passwd": md5_pw,
                    "token": token,
                    "ifencrypt": 0,
                    "options": '{"group":1}',
                },
            )
            if not login_res.get("success"):
                rsa_pw = _rsa_encrypt_pkcs1(pub_key, exponent, md5_pw.encode("utf-8"))
                login_res = self._raw_api_call(
                    "smartlife.m.user.email.password.login",
                    "1.0",
                    {
                        "countryCode": self.country_code,
                        "email": self.email,
                        "passwd": rsa_pw,
                        "token": token,
                        "ifencrypt": 1,
                        "options": '{"group":1}',
                    },
                )

            if not login_res.get("success"):
                raise RuntimeError(f"Molly & Cody Cloud login failed: {login_res}")

            uinfo = login_res["result"]
            self.sid = uinfo["sid"]
            self.uid = uinfo["uid"]
            self.ecode = uinfo["ecode"]
            self.partner_id = uinfo.get("partnerIdentity", "p2155085")

            loc_res = self._raw_api_call("smartlife.m.location.list", "2.1", {}, sid=self.sid)
            if loc_res.get("success") and isinstance(loc_res.get("result"), list) and loc_res["result"]:
                self.gid = int(loc_res["result"][0].get("groupId") or loc_res["result"][0].get("id") or 0)

            self.refresh_cloud_devices()
            self._save_session()
            logger.info(
                "Authenticated to Molly & Cody Cloud (uid=%s, gid=%s, dev=%s)",
                self.uid,
                self.gid,
                self.device_id,
            )
            return True

    def refresh_cloud_devices(self, allow_relogin: bool = True) -> bool:
        if not self.sid:
            return False
        devs_res = self._raw_api_call("m.life.my.group.device.list", "2.2", {"gid": self.gid}, sid=self.sid, gid=self.gid)
        if not devs_res.get("success"):
            return False
        res_obj = devs_res.get("result")
        devs = res_obj if isinstance(res_obj, list) else (res_obj.get("devs", []) if isinstance(res_obj, dict) else [])
        for d in devs:
            if not isinstance(d, dict):
                continue
            dev_id = d.get("devId", "")
            if (
                not self.device_id
                or dev_id == self.device_id
                or "feeder" in str(d.get("name", "")).lower()
                or d.get("category") in ("cwwsq", "sp")
            ):
                self.device_id = dev_id
                self.local_key = d.get("localKey", self.local_key)
                self.device_name = d.get("name", self.device_name)
                self._cloud_online = bool(d.get("cloudOnline", d.get("isOnline", True)))
                self.is_online = self._cloud_online
                raw_dps = d.get("dps") or d.get("dataPointInfo", {}).get("dps")
                if isinstance(raw_dps, dict):
                    self.cached_dps = {str(k): v for k, v in raw_dps.items()}
                self._save_session()
                return True
        return False

    def _get_mqtt_credentials(self) -> Tuple[str, str, str]:
        tag = hashlib.md5((self.uid + self.mobile_device_id).encode("utf-8")).hexdigest()[8:24]
        client_id = f"{PKG_NAME}_mb_{tag}_DEFAULT"
        username = f"{self.partner_id}_v1_{APP_ID}_{self.uid}_{int(time.time())}_{self.sid}"
        password = hashlib.md5((self.ecode + self.sid).encode("utf-8")).hexdigest().lower()[8:24]
        return client_id, username, password

    def _create_mqtt_client(self) -> Any:
        cid, uname, pw = self._get_mqtt_credentials()
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=cid, clean_session=True)
        except AttributeError:
            client = mqtt.Client(client_id=cid, clean_session=True)
        client.username_pw_set(uname, pw)
        client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        return client
