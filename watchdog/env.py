"""Environment bootstrap (idempotent) + stdlib fallbacks for bare Python environments.

Loads `.env` without overriding real environment variables, and provides genuine stdlib
fallbacks for `python-dotenv`, `requests`, and `schedule` when running on a minimal host
without third-party wheels installed.
"""
from __future__ import annotations

import datetime as _dt
import functools
import http.client
import json as _json
import os
import re
import socket
import sys
import types
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Union

from watchdog.paths import PROJECT_ROOT


# ---------------------------------------------------------------------------
# 1. stdlib .env parser fallback (when python-dotenv is not installed)
# ---------------------------------------------------------------------------
def _stdlib_dotenv_values(dotenv_path: Optional[Union[str, Path]] = None) -> Dict[str, Optional[str]]:
    path = Path(dotenv_path) if dotenv_path else (PROJECT_ROOT / ".env")
    values: Dict[str, Optional[str]] = {}
    if not path.is_file():
        return values
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return values
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            quote = val[0]
            val = val[1:-1]
            if quote == '"':
                val = val.replace(r"\n", "\n").replace(r"\"", '"').replace(r"\\", "\\")
        else:
            val = re.sub(r"\s+#.*$", "", val).strip()
        values[key] = val
    return values


def _stdlib_load_dotenv(
    dotenv_path: Optional[Union[str, Path]] = None,
    override: bool = False,
    **kwargs: Any,
) -> bool:
    vals = _stdlib_dotenv_values(dotenv_path)
    if not vals:
        return False
    for k, v in vals.items():
        if v is not None and (override or k not in os.environ):
            os.environ[k] = str(v)
    return True


try:
    from dotenv import dotenv_values, load_dotenv  # type: ignore[import-not-found]
except ImportError:
    dotenv_values = _stdlib_dotenv_values
    load_dotenv = _stdlib_load_dotenv
    _dotenv_mod = types.ModuleType("dotenv")
    _dotenv_mod.dotenv_values = dotenv_values  # type: ignore[attr-defined]
    _dotenv_mod.load_dotenv = load_dotenv  # type: ignore[attr-defined]
    sys.modules.setdefault("dotenv", _dotenv_mod)


# ---------------------------------------------------------------------------
# 2. stdlib `requests` fallback (when requests is not installed)
# ---------------------------------------------------------------------------
def _install_requests_fallback() -> None:
    try:
        import requests  # noqa: F401
        return
    except ImportError:
        pass

    req_mod = types.ModuleType("requests")
    exc_mod = types.ModuleType("requests.exceptions")

    class RequestException(IOError):
        def __init__(self, *args: Any, response: Any = None, request: Any = None) -> None:
            super().__init__(*args)
            self.response = response
            self.request = request

    class HTTPError(RequestException):
        pass

    class ConnectionError(RequestException):
        pass

    class Timeout(RequestException):
        pass

    class ConnectTimeout(ConnectionError, Timeout):
        pass

    class ReadTimeout(Timeout):
        pass

    exc_mod.RequestException = RequestException  # type: ignore[attr-defined]
    exc_mod.HTTPError = HTTPError  # type: ignore[attr-defined]
    exc_mod.ConnectionError = ConnectionError  # type: ignore[attr-defined]
    exc_mod.Timeout = Timeout  # type: ignore[attr-defined]
    exc_mod.ConnectTimeout = ConnectTimeout  # type: ignore[attr-defined]
    exc_mod.ReadTimeout = ReadTimeout  # type: ignore[attr-defined]

    class Response:
        def __init__(
            self,
            status_code: int = 200,
            content: bytes = b"",
            headers: Optional[Dict[str, str]] = None,
            url: str = "",
        ) -> None:
            self.status_code = int(status_code)
            self.content = content or b""
            self.headers = headers or {}
            self.url = url

        @property
        def ok(self) -> bool:
            return 200 <= self.status_code < 400

        @property
        def text(self) -> str:
            return self.content.decode("utf-8", errors="replace")

        def json(self) -> Any:
            return _json.loads(self.text)

        def raise_for_status(self) -> None:
            if not self.ok:
                raise HTTPError(f"HTTP {self.status_code} Error for url: {self.url}", response=self)

    def _encode_multipart(data: Dict[str, Any], files: Dict[str, Any]) -> tuple[bytes, str]:
        boundary = "----MollyCodyBoundary7MA4YWxkTrZu0gW"
        parts: List[bytes] = []
        for k, v in (data or {}).items():
            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{k}"\r\n\r\n'
                f"{v}\r\n".encode("utf-8")
            )
        for field_name, file_info in (files or {}).items():
            if isinstance(file_info, tuple):
                filename = file_info[0]
                fobj = file_info[1]
                ctype = file_info[2] if len(file_info) > 2 else "application/octet-stream"
            else:
                filename = getattr(file_info, "name", field_name)
                fobj = file_info
                ctype = "application/octet-stream"
            raw = fobj.read() if hasattr(fobj, "read") else bytes(file_info)
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            header = (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{field_name}"; filename="{Path(str(filename)).name}"\r\n'
                f"Content-Type: {ctype}\r\n\r\n"
            ).encode("utf-8")
            parts.append(header + raw + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        return b"".join(parts), f"multipart/form-data; boundary={boundary}"

    def request(
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        data: Any = None,
        json: Any = None,
        headers: Optional[Dict[str, str]] = None,
        files: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = 12.0,
        **kwargs: Any,
    ) -> Response:
        full_url = url
        if params:
            qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
            if qs:
                sep = "&" if "?" in full_url else "?"
                full_url = f"{full_url}{sep}{qs}"
        req_headers = dict(headers or {})
        body: Optional[bytes] = None
        if files:
            body, ctype = _encode_multipart(data if isinstance(data, dict) else {}, files)
            req_headers.setdefault("Content-Type", ctype)
        elif json is not None:
            body = _json.dumps(json).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        elif data is not None:
            if isinstance(data, (bytes, bytearray)):
                body = bytes(data)
            elif isinstance(data, str):
                body = data.encode("utf-8")
            elif isinstance(data, dict):
                body = urllib.parse.urlencode(data, doseq=True).encode("utf-8")
                req_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        req = urllib.request.Request(full_url, data=body, headers=req_headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return Response(
                    status_code=resp.status,
                    content=resp.read(),
                    headers=dict(resp.headers),
                    url=full_url,
                )
        except urllib.error.HTTPError as e:
            return Response(
                status_code=e.code,
                content=e.read() if e.fp else b"",
                headers=dict(e.headers) if e.headers else {},
                url=full_url,
            )
        except urllib.error.URLError as e:
            if isinstance(e.reason, socket.timeout):
                raise ConnectTimeout(str(e)) from e
            raise ConnectionError(f"NewConnectionError: {e}") from e
        except (socket.timeout, TimeoutError) as e:
            raise ReadTimeout(str(e)) from e
        except http.client.HTTPException as e:
            raise RequestException(str(e)) from e

    def get(url: str, params: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Response:
        return request("GET", url, params=params, **kwargs)

    def post(url: str, data: Any = None, json: Any = None, **kwargs: Any) -> Response:
        return request("POST", url, data=data, json=json, **kwargs)

    class Session:
        def __init__(self) -> None:
            self.headers: Dict[str, str] = {}

        def request(self, method: str, url: str, **kwargs: Any) -> Response:
            merged_headers = {**self.headers, **(kwargs.pop("headers", None) or {})}
            return request(method, url, headers=merged_headers, **kwargs)

        def get(self, url: str, **kwargs: Any) -> Response:
            return self.request("GET", url, **kwargs)

        def post(self, url: str, **kwargs: Any) -> Response:
            return self.request("POST", url, **kwargs)

    req_mod.exceptions = exc_mod  # type: ignore[attr-defined]
    req_mod.RequestException = RequestException  # type: ignore[attr-defined]
    req_mod.HTTPError = HTTPError  # type: ignore[attr-defined]
    req_mod.ConnectionError = ConnectionError  # type: ignore[attr-defined]
    req_mod.Timeout = Timeout  # type: ignore[attr-defined]
    req_mod.ConnectTimeout = ConnectTimeout  # type: ignore[attr-defined]
    req_mod.ReadTimeout = ReadTimeout  # type: ignore[attr-defined]
    req_mod.Response = Response  # type: ignore[attr-defined]
    req_mod.Session = Session  # type: ignore[attr-defined]
    req_mod.request = request  # type: ignore[attr-defined]
    req_mod.get = get  # type: ignore[attr-defined]
    req_mod.post = post  # type: ignore[attr-defined]

    sys.modules["requests"] = req_mod
    sys.modules["requests.exceptions"] = exc_mod


# ---------------------------------------------------------------------------
# 3. stdlib `schedule` fallback (when schedule is not installed)
# ---------------------------------------------------------------------------
def _install_schedule_fallback() -> None:
    try:
        import schedule  # noqa: F401
        return
    except ImportError:
        pass

    sched_mod = types.ModuleType("schedule")

    class CancelJob:
        pass

    class Job:
        def __init__(self, interval: int = 1, scheduler: Optional["Scheduler"] = None) -> None:
            self.interval: int = interval
            self.unit: Optional[str] = None
            self.at_time: Optional[_dt.time] = None
            self.job_func: Optional[functools.partial] = None
            self.last_run: Optional[_dt.datetime] = None
            self.next_run: Optional[_dt.datetime] = None
            self.tags: Set[Any] = set()
            self.scheduler: Optional["Scheduler"] = scheduler

        @property
        def second(self) -> "Job":
            return self.seconds

        @property
        def seconds(self) -> "Job":
            self.unit = "seconds"
            return self

        @property
        def minute(self) -> "Job":
            return self.minutes

        @property
        def minutes(self) -> "Job":
            self.unit = "minutes"
            return self

        @property
        def hour(self) -> "Job":
            return self.hours

        @property
        def hours(self) -> "Job":
            self.unit = "hours"
            return self

        @property
        def day(self) -> "Job":
            return self.days

        @property
        def days(self) -> "Job":
            self.unit = "days"
            return self

        def at(self, time_str: str) -> "Job":
            parts = [int(x) for x in time_str.strip().split(":")]
            hh = parts[0] if len(parts) > 0 else 0
            mm = parts[1] if len(parts) > 1 else 0
            ss = parts[2] if len(parts) > 2 else 0
            self.at_time = _dt.time(hh, mm, ss)
            return self

        def tag(self, *tags: Any) -> "Job":
            self.tags.update(tags)
            return self

        def do(self, job_func: Callable[..., Any], *args: Any, **kwargs: Any) -> "Job":
            self.job_func = functools.partial(job_func, *args, **kwargs)
            functools.update_wrapper(self.job_func, job_func)
            self._schedule_next_run()
            if self.scheduler is not None and self not in self.scheduler.jobs:
                self.scheduler.jobs.append(self)
            return self

        @property
        def should_run(self) -> bool:
            return self.next_run is not None and _dt.datetime.now() >= self.next_run

        def run(self) -> Any:
            ret = self.job_func() if self.job_func is not None else None
            self.last_run = _dt.datetime.now()
            self._schedule_next_run()
            return ret

        def _schedule_next_run(self) -> None:
            now = _dt.datetime.now()
            unit = self.unit or "days"
            if unit == "days" and self.at_time is not None:
                nxt = now.replace(
                    hour=self.at_time.hour,
                    minute=self.at_time.minute,
                    second=self.at_time.second,
                    microsecond=0,
                )
                if nxt <= now:
                    nxt += _dt.timedelta(days=max(1, self.interval))
                self.next_run = nxt
            elif unit == "hours" and self.at_time is not None:
                nxt = now.replace(
                    minute=self.at_time.minute,
                    second=self.at_time.second,
                    microsecond=0,
                )
                if nxt <= now:
                    nxt += _dt.timedelta(hours=max(1, self.interval))
                self.next_run = nxt
            else:
                delta_kw = {unit: max(1, self.interval)}
                self.next_run = now + _dt.timedelta(**delta_kw)

    class Scheduler:
        def __init__(self) -> None:
            self.jobs: List[Job] = []

        def every(self, interval: int = 1) -> Job:
            return Job(interval=interval, scheduler=self)

        def run_pending(self) -> None:
            runnable = [j for j in self.jobs if j.should_run]
            for job in sorted(runnable, key=lambda j: j.next_run or _dt.datetime.max):
                res = job.run()
                if res is CancelJob or isinstance(res, CancelJob):
                    if job in self.jobs:
                        self.jobs.remove(job)

        def clear(self, tag: Any = None) -> None:
            if tag is None:
                self.jobs.clear()
            else:
                self.jobs[:] = [j for j in self.jobs if tag not in j.tags]

        def get_jobs(self, tag: Any = None) -> List[Job]:
            if tag is None:
                return list(self.jobs)
            return [j for j in self.jobs if tag in j.tags]

    default_scheduler = Scheduler()

    sched_mod.CancelJob = CancelJob  # type: ignore[attr-defined]
    sched_mod.Job = Job  # type: ignore[attr-defined]
    sched_mod.Scheduler = Scheduler  # type: ignore[attr-defined]
    sched_mod.default_scheduler = default_scheduler  # type: ignore[attr-defined]
    sched_mod.every = default_scheduler.every  # type: ignore[attr-defined]
    sched_mod.run_pending = default_scheduler.run_pending  # type: ignore[attr-defined]
    sched_mod.clear = default_scheduler.clear  # type: ignore[attr-defined]
    sched_mod.get_jobs = default_scheduler.get_jobs  # type: ignore[attr-defined]

    sys.modules["schedule"] = sched_mod


_install_requests_fallback()
_install_schedule_fallback()

_loaded = False


def load_env() -> None:
    """Real env vars (Docker, shell, CLI overrides) win over `.env`. Legacy `USER=`/`PASS=` account keys
    are mapped to MOLLY_USER / MOLLY_PASS so the account can't silently become the OS login name."""
    global _loaded
    if _loaded:
        return
    env_file = PROJECT_ROOT / ".env"
    load_dotenv(env_file, override=False)
    file_vals = dotenv_values(env_file) if env_file.exists() else {}
    for legacy, key in (("USER", "MOLLY_USER"), ("PASS", "MOLLY_PASS")):
        if file_vals.get(legacy) and not os.getenv(key):
            os.environ[key] = str(file_vals[legacy])
    _loaded = True
