"""基于标准库 http.server 的演练裁决 HTTP 服务。"""

from __future__ import annotations

import json
import os
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .statemachine import (
    Conflict,
    DrillStore,
    NotFound,
    STATE_VERDICT,
)

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
_CODE_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,48}$")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _require_int(body, field, minimum=None) -> int:
    if field not in body:
        raise ApiError(HTTPStatus.BAD_REQUEST, "MISSING_FIELD",
                       f"field {field!r} is required")
    value = body[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_FIELD",
                       f"field {field!r} must be an integer")
    if minimum is not None and value < minimum:
        raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_FIELD",
                       f"field {field!r} must be >= {minimum}")
    return value


def _require_number(body, field) -> float:
    if field not in body:
        raise ApiError(HTTPStatus.BAD_REQUEST, "MISSING_FIELD",
                       f"field {field!r} is required")
    value = body[field]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_FIELD",
                       f"field {field!r} must be a number")
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")):
        raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_FIELD",
                       f"field {field!r} must be a finite number")
    return value


def _require_str(body, field, pattern: re.Pattern, max_len: int) -> str:
    if field not in body:
        raise ApiError(HTTPStatus.BAD_REQUEST, "MISSING_FIELD",
                       f"field {field!r} is required")
    value = body[field]
    if not isinstance(value, str):
        raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_FIELD",
                       f"field {field!r} must be a string")
    if len(value) > max_len or not pattern.match(value):
        raise ApiError(
            HTTPStatus.BAD_REQUEST, "INVALID_FIELD",
            f"field {field!r} must match {pattern.pattern} "
            f"(max {max_len} chars)")
    return value


class Handler(BaseHTTPRequestHandler):
    server_version = "BeamProtect/1.0"

    def log_message(self, fmt, *args):  # 精简访问日志
        self.server.log_line(f"{self.address_string()} {fmt % args}")

    @property
    def store(self) -> DrillStore:
        return self.server.store  # type: ignore[attr-defined]

    # ---------------------------------------------------------- HTTP 工具

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ApiError(HTTPStatus.BAD_REQUEST, "EMPTY_BODY",
                           "request body must be a JSON object")
        if length > 65536:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                           "BODY_TOO_LARGE", "request body too large")
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_JSON",
                           "request body must be valid JSON")
        if not isinstance(body, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_BODY",
                           "request body must be a JSON object")
        return body

    def _write_json(self, status: int, payload) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status, code, message, extra=None):
        payload = {"error": {"code": code, "message": message}}
        if extra:
            payload["error"].update(extra)
        self._write_json(status, payload)

    def _serve_index(self):
        index = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "static", "index.html")
        try:
            with open(index, "rb") as fh:
                data = fh.read()
        except OSError:
            self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND",
                        "console page missing")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # --------------------------------------------------------------- 路由

    def do_GET(self):
        try:
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path in ("/", "/index.html"):
                self._serve_index()
                return
            if path == "/health":
                self._write_json(HTTPStatus.OK, {"status": "ok"})
                return
            if path == "/api/drills":
                self._write_json(HTTPStatus.OK,
                                 {"drills": self.store.list_drills()})
                return
            m = re.fullmatch(r"/api/drills/([A-Za-z0-9_.\-]+)", path)
            if m:
                self._write_json(HTTPStatus.OK,
                                 self.store.drill_status(m.group(1)))
                return
            self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "no such route")
        except NotFound as exc:
            self._error(HTTPStatus.NOT_FOUND, "DRILL_NOT_FOUND", str(exc))
        except Exception:
            self.server.log_error("GET %s failed", self.path)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "INTERNAL",
                        "internal error")

    def do_POST(self):
        try:
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/api/drills":
                self._create_drill()
                return
            m = re.fullmatch(
                r"/api/drills/([A-Za-z0-9_.\-]+)/procedures", path)
            if m:
                self._register_procedure(m.group(1))
                return
            m = re.fullmatch(
                r"/api/drills/([A-Za-z0-9_.\-]+)/observations", path)
            if m:
                self._submit_observation(m.group(1))
                return
            self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "no such route")
        except ApiError as exc:
            self._error(exc.status, exc.code, exc.message)
        except NotFound as exc:
            self._error(HTTPStatus.NOT_FOUND, "DRILL_NOT_FOUND", str(exc))
        except Conflict as exc:
            # 所有幂等/不可变冲突：明确 409 拒绝，附稳定拒因码
            self._error(HTTPStatus.CONFLICT, exc.kind, exc.reason,
                        {"delivery_id": exc.delivery_id, "seq": exc.seq})
        except Exception:
            self.server.log_error("POST %s failed", self.path)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "INTERNAL",
                        "internal error")

    # --------------------------------------------------------------- 动作

    def _create_drill(self):
        body = self._read_json()
        drill_id = _require_str(body, "drill_id", _ID_RE, 64)
        name = body.get("name", drill_id)
        if not isinstance(name, str) or not 1 <= len(name) <= 128:
            raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_FIELD",
                           "field 'name' must be a 1..128 char string")
        base_code = _require_str(body, "base_procedure_code", _CODE_RE, 48)
        base_threshold = _require_number(body, "base_threshold")
        status = self.store.create_drill(
            drill_id, name, base_code, base_threshold)
        self._write_json(HTTPStatus.CREATED, status)

    def _register_procedure(self, drill_id):
        body = self._read_json()
        effective_from = _require_int(body, "effective_from", minimum=1)
        code = _require_str(body, "code", _CODE_RE, 48)
        threshold = _require_number(body, "threshold")
        proc = self.store.register_procedure(
            drill_id, effective_from, code, threshold)
        self._write_json(HTTPStatus.CREATED, proc)

    def _submit_observation(self, drill_id):
        body = self._read_json()
        delivery_id = _require_str(body, "delivery_id", _ID_RE, 64)
        seq = _require_int(body, "seq", minimum=0)
        reading = _require_number(body, "reading")
        outcome = self.store.submit_observation(
            drill_id, delivery_id, seq, reading)
        payload = {
            "state": outcome.state,
            "duplicate": outcome.duplicate,
            "water_level": outcome.water_level,
            "verdict": outcome.verdict,
            "drained": outcome.drained,
        }
        self._write_json(
            HTTPStatus.OK if outcome.state == STATE_VERDICT
            else HTTPStatus.ACCEPTED, payload)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, store: DrillStore):
        super().__init__(addr, Handler)
        self.store = store
        self._log_lock = threading.Lock()

    def log_line(self, line: str):
        with self._log_lock:
            print(f"[http] {line}", flush=True)

    def log_error(self, fmt, *args):
        with self._log_lock:
            print(f"[http-error] {fmt % args}", flush=True)
