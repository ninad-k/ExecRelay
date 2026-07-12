import argparse
import contextvars
import json
import logging
import os
import re
import secrets
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

import asyncpg
import bcrypt
import httpx
import jwt
import nats
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from pydantic import BaseModel, EmailStr
from starlette.middleware.base import BaseHTTPMiddleware

SERVICE = "portal-api"
ENV = os.environ.get("ENV", "development").lower()
IS_PROD = ENV in ("prod", "production")
HTTP_ADDR = os.environ.get("HTTP_ADDR", "0.0.0.0:8080")

# --- Required-in-prod config: fail-fast if defaults leak into prod -----------
_DEV_DB = "postgresql://execrelay:execrelay_dev_password@postgres:5432/execrelay"
_DEV_NATS = "nats://execrelay:execrelay_nats_dev@nats:4222"
_DEV_JWT = "changeme-in-production"

DB_DSN = os.environ.get("DATABASE_URL", _DEV_DB)
NATS_URL = os.environ.get("NATS_URL", _DEV_NATS)
JWT_SECRET = os.environ.get("JWT_SECRET", _DEV_JWT)
JWT_ALGO = "HS256"
JWT_TTL_DAYS = 7

_raw_origins = os.environ.get("PORTAL_ALLOWED_ORIGINS", "")
ALLOWED_ORIGINS = [o.strip() for o in _raw_origins.split(",") if o.strip()]

_INSTANCE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
INGRESS_URL = os.environ.get("INGRESS_URL", "http://ingress:8080")
SIGNALS_STREAM = os.environ.get("SIGNALS_STREAM", "SIGNALS")

DEBUG = os.environ.get("DEBUG", "false" if IS_PROD else "true").lower() in (
    "true",
    "1",
    "yes",
    "on",
)

# --- Structured JSON logging -------------------------------------------------
# Every log line is one JSON object. Request middleware injects request_id +
# trace_id via contextvars so every log inside the handler is correlated.
_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=""
)
_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")


class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": SERVICE,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = _request_id.get()
        if rid:
            payload["request_id"] = rid
        tid = _trace_id.get()
        if tid:
            payload["trace_id"] = tid
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for k, v in record.__dict__.items():
            if k in (
                "args",
                "msg",
                "exc_info",
                "exc_text",
                "stack_info",
                "msecs",
                "relativeCreated",
                "thread",
                "threadName",
                "processName",
                "process",
                "levelname",
                "levelno",
                "name",
                "pathname",
                "filename",
                "module",
                "funcName",
                "lineno",
                "created",
                "asctime",
                "message",
            ):
                continue
            payload[k] = v
        return json.dumps(payload, default=str)


logger = logging.getLogger(SERVICE)
log_level = logging.DEBUG if DEBUG else logging.INFO
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_JSONFormatter())
logging.basicConfig(level=log_level, handlers=[_handler], force=True)

# --- Prod config validation: refuse to start with dev defaults ---------------
_config_errors: list[str] = []
if IS_PROD:
    if DB_DSN == _DEV_DB:
        _config_errors.append("DATABASE_URL is required in prod (refusing dev default)")
    if NATS_URL == _DEV_NATS:
        _config_errors.append("NATS_URL is required in prod (refusing dev default)")
    if JWT_SECRET == _DEV_JWT or len(JWT_SECRET) < 32:
        _config_errors.append("JWT_SECRET must be set and >=32 chars in prod")
    if not ALLOWED_ORIGINS or "*" in ALLOWED_ORIGINS:
        _config_errors.append(
            "PORTAL_ALLOWED_ORIGINS must be an explicit comma-separated list in prod "
            "(wildcard '*' is rejected)"
        )

if _config_errors:
    for err in _config_errors:
        logger.error(err, extra={"event": "config_error"})
    sys.stderr.write("\n".join(_config_errors) + "\n")
    raise SystemExit(2)

if not ALLOWED_ORIGINS:
    ALLOWED_ORIGINS = ["*"]
    logger.warning(
        "PORTAL_ALLOWED_ORIGINS unset; defaulting to '*' (dev only)",
        extra={"event": "cors_wildcard_dev"},
    )

if DEBUG:
    logger.info("debug logging enabled")
if not IS_PROD and JWT_SECRET == _DEV_JWT:
    logger.warning(
        "JWT_SECRET using dev default — REQUIRED before prod",
        extra={"event": "weak_jwt_secret"},
    )

# ---------------------------------------------------------------------------
# DB pool lifecycle
# ---------------------------------------------------------------------------

_pool: asyncpg.Pool | None = None
_nc: Any | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    global _pool, _nc
    _pool = await asyncpg.create_pool(
        DB_DSN, min_size=2, max_size=20, command_timeout=10
    )
    logger.info("db pool ready")
    try:
        _nc = await nats.connect(NATS_URL, name="execrelay-portal-api")
        logger.info("nats connected")
    except Exception as exc:
        logger.warning("nats unavailable: %s", exc)
    yield
    await _pool.close()
    if _nc is not None:
        await _nc.drain()


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "db unavailable")
    return _pool


def get_nats():
    if _nc is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "nats unavailable")
    return _nc


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="ExecRelay Portal API", version="1.0.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


class RequestLogMiddleware(BaseHTTPMiddleware):
    """Tags every request with a request_id, propagates trace_id, and logs one
    line per request with timing + status so any failure can be traced."""

    async def dispatch(self, request: Request, call_next):
        rid = (
            request.headers.get("x-request-id")
            or request.headers.get("X-Request-ID")
            or uuid.uuid4().hex
        )
        tid = (
            request.headers.get("x-execrelay-trace-id")
            or request.headers.get("X-ExecRelay-Trace-ID")
            or ""
        )
        rid_token = _request_id.set(rid)
        tid_token = _trace_id.set(tid)
        request.state.request_id = rid
        request.state.trace_id = tid
        start = time.monotonic()
        status_code = 500
        err: str = ""
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["x-request-id"] = rid
            if tid:
                response.headers["x-execrelay-trace-id"] = tid
            return response
        except Exception as exc:
            err = repr(exc)
            logger.exception(
                "request_failed",
                extra={
                    "event": "request_failed",
                    "method": request.method,
                    "path": request.url.path,
                    "err": err,
                },
            )
            return JSONResponse(
                {"error": "internal_error", "request_id": rid},
                status_code=500,
                headers={"x-request-id": rid},
            )
        finally:
            latency_ms = (time.monotonic() - start) * 1000
            client = request.client.host if request.client else ""
            logger.info(
                "request",
                extra={
                    "event": "request",
                    "method": request.method,
                    "path": request.url.path,
                    "status": status_code,
                    "latency_ms": round(latency_ms, 2),
                    "client": client,
                    "ua": request.headers.get("user-agent", "")[:120],
                    "err": err,
                },
            )
            _request_id.reset(rid_token)
            _trace_id.reset(tid_token)


app.add_middleware(RequestLogMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["x-request-id", "x-execrelay-trace-id"],
)
bearer = HTTPBearer()

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def check_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def make_token(user_id: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(days=JWT_TTL_DAYS)
    return jwt.encode({"sub": user_id, "exp": exp}, JWT_SECRET, algorithm=JWT_ALGO)


def verify_token(token: str) -> str:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        return payload["sub"]
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc))


async def current_user(
    creds: HTTPAuthorizationCredentials = Depends(bearer),
    pool: asyncpg.Pool = Depends(get_pool),
) -> dict[str, Any]:
    user_id = verify_token(creds.credentials)
    row = await pool.fetchrow(
        "SELECT id, email FROM users WHERE id = $1", uuid.UUID(user_id)
    )
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user not found")
    return dict(row)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class RegisterIn(BaseModel):
    email: EmailStr
    password: str
    # ISO 3166-1 alpha-2 country code declared by the registrant. Optional for
    # backward compatibility (empty = not declared), but when supplied it is
    # screened against OFAC-sanctioned jurisdictions
    # (see BLOCKED_REGISTRATION_COUNTRIES).
    country: str = ""


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class LicenseOut(BaseModel):
    id: str
    license_key: str
    active: bool
    created_at: datetime


class CreateLicenseOut(BaseModel):
    id: str
    license_key: str
    hmac_secret: str  # returned once on creation
    active: bool
    created_at: datetime


class PatchActiveIn(BaseModel):
    active: bool


class InstanceOut(BaseModel):
    id: str
    instance_key: str
    platform: str
    active: bool
    created_at: datetime


class CreateInstanceIn(BaseModel):
    instance_key: str
    platform: str  # mt4, mt5, dxtrade


class ConfigExportOut(BaseModel):
    execrelay_licenses: str  # ready to paste as env var value


class SignalOut(BaseModel):
    id: str
    received_at: datetime
    trace_id: str
    ingress_region: str
    command: str
    symbol: str


class FillOut(BaseModel):
    id: str
    created_at: datetime
    trace_id: str
    status: str
    broker_order_id: str | None
    error_code: str | None
    error_message: str | None


class TraceEvent(BaseModel):
    event_type: str
    timestamp: datetime
    detail: dict


class TraceTimeline(BaseModel):
    trace_id: str
    signal: dict | None
    fills: list[dict]
    events: list[dict]


# ---------------------------------------------------------------------------
# Audit log helper
# ---------------------------------------------------------------------------


async def _audit(
    pool: asyncpg.Pool,
    actor_id: Any,
    action: str,
    before: dict | None = None,
    after: dict | None = None,
    reason: str = "",
) -> None:
    try:
        await pool.execute(
            """
            INSERT INTO admin_audit_log
                (actor_user_id, action, reason, before_state, after_state)
            VALUES ($1, $2, $3, $4::jsonb, $5::jsonb)
            """,
            actor_id,
            action,
            reason,
            json.dumps(before) if before else None,
            json.dumps(after) if after else None,
        )
    except Exception as exc:
        logger.warning("audit log failed: %s", exc)


# ---------------------------------------------------------------------------
# Minimal protobuf encoder for Signal message (avoids protobuf library dep)
# ---------------------------------------------------------------------------


def _pb_varint(n: int) -> bytes:
    out = []
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _pb_str_field(field_num: int, s: str) -> bytes:
    enc = s.encode("utf-8")
    tag = _pb_varint((field_num << 3) | 2)
    return tag + _pb_varint(len(enc)) + enc


def _pb_int64_field(field_num: int, n: int) -> bytes:
    return _pb_varint((field_num << 3) | 0) + _pb_varint(n)


def _pb_bytes_field(field_num: int, data: bytes) -> bytes:
    tag = _pb_varint((field_num << 3) | 2)
    return tag + _pb_varint(len(data)) + data


def encode_signal_proto(sig: dict[str, Any]) -> bytes:
    out = b""
    if sig.get("trace_id"):
        out += _pb_str_field(1, sig["trace_id"])
    if sig.get("license_id"):
        out += _pb_str_field(2, sig["license_id"])
    if sig.get("instance_id"):
        out += _pb_str_field(3, sig["instance_id"])
    if sig.get("command"):
        out += _pb_str_field(4, sig["command"])
    if sig.get("raw_command"):
        out += _pb_str_field(5, sig["raw_command"])
    if sig.get("symbol"):
        out += _pb_str_field(6, sig["symbol"])
    if sig.get("ingress_region"):
        out += _pb_str_field(7, sig["ingress_region"])
    if sig.get("received_unix_nano"):
        out += _pb_int64_field(8, sig["received_unix_nano"])
    if sig.get("body_sha256"):
        out += _pb_str_field(9, sig["body_sha256"])
    for param in sig.get("params", []):
        param_bytes = _pb_str_field(1, param.get("key", "")) + _pb_str_field(
            2, param.get("value", "")
        )
        out += _pb_bytes_field(10, param_bytes)
    return out


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    result, shift = 0, 0
    while True:
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7


def decode_signal_proto(data: bytes) -> dict[str, Any]:
    sig: dict[str, Any] = {"params": []}
    pos = 0
    string_fields = {
        1: "trace_id",
        2: "license_id",
        3: "instance_id",
        4: "command",
        5: "raw_command",
        6: "symbol",
        7: "ingress_region",
        9: "body_sha256",
    }
    while pos < len(data):
        tag, pos = _varint(data, pos)
        field_num, wire_type = tag >> 3, tag & 0x7
        if wire_type == 0:
            val, pos = _varint(data, pos)
            if field_num == 8:
                sig["received_unix_nano"] = val
        elif wire_type == 2:
            length, pos = _varint(data, pos)
            raw = data[pos : pos + length]
            pos += length
            if field_num in string_fields:
                sig[string_fields[field_num]] = raw.decode("utf-8", errors="replace")
            elif field_num == 10:
                param: dict[str, str] = {}
                p = 0
                while p < len(raw):
                    t, p = _varint(raw, p)
                    fn, wt = t >> 3, t & 0x7
                    if wt == 2:
                        ln, p = _varint(raw, p)
                        v = raw[p : p + ln].decode("utf-8", errors="replace")
                        p += ln
                        if fn == 1:
                            param["key"] = v
                        elif fn == 2:
                            param["value"] = v
                sig["params"].append(param)
        else:
            break
    return sig


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness — process is up. Kept for backward compat with healthcheck.

    Use /readyz for full dependency probing."""
    return {"service": SERVICE, "status": "ok"}


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"service": SERVICE, "status": "ok"}


@app.get("/readyz")
async def readyz() -> JSONResponse:
    """Readiness — DB + NATS reachable. Returns 503 with per-check status
    if any dependency is down so a load balancer can pull this instance."""
    checks: dict[str, dict[str, Any]] = {}
    ok = True

    if _pool is None:
        checks["db"] = {"ok": False, "err": "pool not initialized"}
        ok = False
    else:
        try:
            await _pool.fetchval("SELECT 1")
            checks["db"] = {"ok": True}
        except Exception as exc:
            checks["db"] = {"ok": False, "err": repr(exc)[:200]}
            ok = False

    if _nc is None:
        checks["nats"] = {"ok": False, "err": "not connected"}
        ok = False
    else:
        try:
            connected = bool(getattr(_nc, "is_connected", False))
            checks["nats"] = {"ok": connected}
            if not connected:
                ok = False
        except Exception as exc:
            checks["nats"] = {"ok": False, "err": repr(exc)[:200]}
            ok = False

    return JSONResponse(
        {"service": SERVICE, "ok": ok, "checks": checks},
        status_code=200 if ok else 503,
    )


# OFAC comprehensively-sanctioned jurisdictions, by ISO 3166-1 alpha-2 code.
# This is the application-layer screen at registration time; region-level
# blocks (e.g. Crimea, Donetsk, Luhansk) and IP-based geofencing are handled
# at the network edge, not here, since they have no standalone country code.
BLOCKED_REGISTRATION_COUNTRIES = frozenset({"CU", "IR", "KP", "SY"})


@app.post(
    "/auth/register", response_model=TokenOut, status_code=status.HTTP_201_CREATED
)
@limiter.limit("10/minute")
async def register(
    request: Request, body: RegisterIn, pool: asyncpg.Pool = Depends(get_pool)
) -> TokenOut:
    if len(body.password) < 12:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "password must be at least 12 characters",
        )
    if body.country and body.country.strip().upper() in BLOCKED_REGISTRATION_COUNTRIES:
        raise HTTPException(
            status.HTTP_451_UNAVAILABLE_FOR_LEGAL_REASONS,
            "registration is not available in your jurisdiction",
        )
    existing = await pool.fetchrow("SELECT id FROM users WHERE email = $1", body.email)
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "email already registered")
    user_id = await pool.fetchval(
        "INSERT INTO users (email, password_hash) VALUES ($1, $2) RETURNING id",
        body.email,
        hash_password(body.password),
    )
    await pool.execute(
        "INSERT INTO user_roles (user_id, role_id) "
        "SELECT $1, id FROM roles WHERE name = 'user'",
        user_id,
    )
    return TokenOut(access_token=make_token(str(user_id)))


@app.post("/auth/login", response_model=TokenOut)
@limiter.limit("20/minute")
async def login(
    request: Request, body: LoginIn, pool: asyncpg.Pool = Depends(get_pool)
) -> TokenOut:
    row = await pool.fetchrow(
        "SELECT id, password_hash FROM users WHERE email = $1", body.email
    )
    if row is None or not check_password(body.password, row["password_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    return TokenOut(access_token=make_token(str(row["id"])))


@app.get("/licenses", response_model=list[LicenseOut])
async def list_licenses(
    user: dict = Depends(current_user),
    pool: asyncpg.Pool = Depends(get_pool),
) -> list[LicenseOut]:
    rows = await pool.fetch(
        "SELECT id, license_key, active, created_at FROM licenses "
        "WHERE user_id = $1 ORDER BY created_at DESC",
        user["id"],
    )
    return [
        LicenseOut(
            id=str(r["id"]),
            license_key=r["license_key"],
            active=r["active"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


@app.post(
    "/licenses", response_model=CreateLicenseOut, status_code=status.HTTP_201_CREATED
)
async def create_license(
    user: dict = Depends(current_user),
    pool: asyncpg.Pool = Depends(get_pool),
) -> CreateLicenseOut:
    license_key = str(int(uuid.uuid4().int % 10**11)).zfill(11)
    hmac_secret = secrets.token_hex(16)
    row = await pool.fetchrow(
        "INSERT INTO licenses (user_id, license_key, hmac_secret) "
        "VALUES ($1, $2, $3) RETURNING id, created_at",
        user["id"],
        license_key,
        hmac_secret,
    )
    await _audit(pool, user["id"], "create_license", after={"license_key": license_key})
    return CreateLicenseOut(
        id=str(row["id"]),
        license_key=license_key,
        hmac_secret=hmac_secret,
        active=True,
        created_at=row["created_at"],
    )


@app.patch("/licenses/{license_id}", response_model=LicenseOut)
async def patch_license(
    license_id: str,
    body: PatchActiveIn,
    user: dict = Depends(current_user),
    pool: asyncpg.Pool = Depends(get_pool),
) -> LicenseOut:
    row = await pool.fetchrow(
        "UPDATE licenses SET active = $1 "
        "WHERE id = $2 AND user_id = $3 "
        "RETURNING id, license_key, active, created_at",
        body.active,
        uuid.UUID(license_id),
        user["id"],
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "license not found")
    await _audit(
        pool,
        user["id"],
        "patch_license",
        before={"active": not body.active},
        after={"active": body.active},
        reason=f"license_id={license_id}",
    )
    return LicenseOut(
        id=str(row["id"]),
        license_key=row["license_key"],
        active=row["active"],
        created_at=row["created_at"],
    )


@app.get("/licenses/{license_id}/instances", response_model=list[InstanceOut])
async def list_instances(
    license_id: str,
    user: dict = Depends(current_user),
    pool: asyncpg.Pool = Depends(get_pool),
) -> list[InstanceOut]:
    lic = await _own_license(pool, license_id, user["id"])
    rows = await pool.fetch(
        "SELECT id, instance_key, platform, active, created_at FROM instances "
        "WHERE license_id = $1 ORDER BY created_at DESC",
        lic["id"],
    )
    return [
        InstanceOut(
            id=str(r["id"]),
            instance_key=r["instance_key"],
            platform=r["platform"],
            active=r["active"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


@app.post(
    "/licenses/{license_id}/instances",
    response_model=InstanceOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_instance(
    license_id: str,
    body: CreateInstanceIn,
    user: dict = Depends(current_user),
    pool: asyncpg.Pool = Depends(get_pool),
) -> InstanceOut:
    if body.platform not in ("mt4", "mt5", "dxtrade"):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "platform must be mt4, mt5, or dxtrade",
        )
    if not _INSTANCE_KEY_RE.match(body.instance_key):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "instance_key must contain only letters, digits, _ or -",
        )
    lic = await _own_license(pool, license_id, user["id"])
    try:
        row = await pool.fetchrow(
            "INSERT INTO instances (license_id, instance_key, platform) "
            "VALUES ($1, $2, $3) RETURNING id, instance_key, platform, active, created_at",
            lic["id"],
            body.instance_key,
            body.platform,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "instance_key already exists for this license"
        )
    await _audit(
        pool,
        user["id"],
        "create_instance",
        after={
            "instance_key": body.instance_key,
            "platform": body.platform,
            "license_id": license_id,
        },
    )
    return InstanceOut(
        id=str(row["id"]),
        instance_key=row["instance_key"],
        platform=row["platform"],
        active=row["active"],
        created_at=row["created_at"],
    )


@app.patch("/licenses/{license_id}/instances/{instance_id}", response_model=InstanceOut)
async def patch_instance(
    license_id: str,
    instance_id: str,
    body: PatchActiveIn,
    user: dict = Depends(current_user),
    pool: asyncpg.Pool = Depends(get_pool),
) -> InstanceOut:
    lic = await _own_license(pool, license_id, user["id"])
    row = await pool.fetchrow(
        "UPDATE instances SET active = $1 "
        "WHERE id = $2 AND license_id = $3 "
        "RETURNING id, instance_key, platform, active, created_at",
        body.active,
        uuid.UUID(instance_id),
        lic["id"],
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "instance not found")
    await _audit(
        pool,
        user["id"],
        "patch_instance",
        before={"active": not body.active},
        after={"active": body.active},
        reason=f"instance_id={instance_id}",
    )
    return InstanceOut(
        id=str(row["id"]),
        instance_key=row["instance_key"],
        platform=row["platform"],
        active=row["active"],
        created_at=row["created_at"],
    )


@app.get("/licenses/{license_id}/config", response_model=ConfigExportOut)
async def export_config(
    license_id: str,
    user: dict = Depends(current_user),
    pool: asyncpg.Pool = Depends(get_pool),
) -> ConfigExportOut:
    lic = await _own_license(pool, license_id, user["id"])
    instances = await pool.fetch(
        "SELECT instance_key, platform FROM instances WHERE license_id = $1 AND active = TRUE",
        lic["id"],
    )
    pending = lic["pending_hmac_secret"] or ""
    parts = [
        f"{lic['license_key']}::{lic['hmac_secret']}:{row['instance_key']}:{row['platform']}:{pending}"
        for row in instances
    ]
    return ConfigExportOut(execrelay_licenses=";".join(parts))


@app.get("/licenses/{license_id}/signals", response_model=list[SignalOut])
async def list_signals(
    license_id: str,
    limit: int = 50,
    user: dict = Depends(current_user),
    pool: asyncpg.Pool = Depends(get_pool),
) -> list[SignalOut]:
    lic = await _own_license(pool, license_id, user["id"])
    rows = await pool.fetch(
        "SELECT id, received_at, trace_id, ingress_region, command, symbol "
        "FROM accepted_signals WHERE license_id = $1 "
        "ORDER BY received_at DESC LIMIT $2",
        lic["id"],
        max(1, min(limit, 500)),
    )
    return [
        SignalOut(
            id=str(r["id"]),
            received_at=r["received_at"],
            trace_id=r["trace_id"],
            ingress_region=r["ingress_region"],
            command=r["command"],
            symbol=r["symbol"],
        )
        for r in rows
    ]


@app.get(
    "/licenses/{license_id}/instances/{instance_id}/fills", response_model=list[FillOut]
)
async def list_fills(
    license_id: str,
    instance_id: str,
    limit: int = 50,
    user: dict = Depends(current_user),
    pool: asyncpg.Pool = Depends(get_pool),
) -> list[FillOut]:
    lic = await _own_license(pool, license_id, user["id"])
    inst = await pool.fetchrow(
        "SELECT id FROM instances WHERE id = $1 AND license_id = $2",
        uuid.UUID(instance_id),
        lic["id"],
    )
    if inst is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "instance not found")
    rows = await pool.fetch(
        "SELECT id, created_at, trace_id, status, broker_order_id, "
        "       error_code, error_message "
        "FROM fills WHERE instance_id = $1 "
        "ORDER BY created_at DESC LIMIT $2",
        inst["id"],
        max(1, min(limit, 500)),
    )
    return [
        FillOut(
            id=str(r["id"]),
            created_at=r["created_at"],
            trace_id=r["trace_id"],
            status=r["status"],
            broker_order_id=r["broker_order_id"],
            error_code=r["error_code"],
            error_message=r["error_message"],
        )
        for r in rows
    ]


@app.post("/licenses/{license_id}/rotate-hmac")
async def rotate_hmac(
    license_id: str,
    user: dict = Depends(current_user),
    pool: asyncpg.Pool = Depends(get_pool),
) -> dict:
    lic = await _own_license(pool, license_id, user["id"])
    new_secret = secrets.token_hex(16)
    await pool.execute(
        "UPDATE licenses SET pending_hmac_secret = $1 WHERE id = $2",
        new_secret,
        lic["id"],
    )
    await _audit(
        pool, user["id"], "rotate_hmac_start", reason=f"license_id={license_id}"
    )
    return {
        "pending_hmac_secret": new_secret,
        "message": "Update TradingView to use this secret, then call /confirm-rotation",
    }


@app.post("/licenses/{license_id}/confirm-rotation")
async def confirm_rotation(
    license_id: str,
    user: dict = Depends(current_user),
    pool: asyncpg.Pool = Depends(get_pool),
) -> dict:
    lic = await _own_license(pool, license_id, user["id"])
    if not lic.get("pending_hmac_secret"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "no pending rotation in progress"
        )
    await pool.execute(
        "UPDATE licenses SET hmac_secret = pending_hmac_secret, pending_hmac_secret = NULL WHERE id = $1",
        lic["id"],
    )
    await _audit(
        pool, user["id"], "rotate_hmac_confirm", reason=f"license_id={license_id}"
    )
    return {"message": "HMAC secret rotation complete"}


@app.get("/requests/{request_id}")
async def get_request(
    request_id: str,
    user: dict = Depends(current_user),
    pool: asyncpg.Pool = Depends(get_pool),
) -> dict:
    """Look up a single webhook attempt by request_id. Joins request_log
    with the trace's signal/fills/events so a user can answer
    'what happened to my call?' from a single endpoint.

    Scoped to the authenticated user's licenses by license_key match —
    a user cannot read another user's request history."""
    rows = await pool.fetch(
        """
        SELECT r.id, r.received_at, r.request_id, r.trace_id, r.service,
               r.route, r.method, r.client_ip, r.license_key, r.status_code,
               r.outcome, r.reason_code, r.latency_ms, r.body_sha256,
               r.user_agent, r.detail
        FROM request_log r
        WHERE r.request_id = $1
          AND (
            r.license_key IS NULL
            OR r.license_key IN (
              SELECT license_key FROM licenses WHERE user_id = $2
            )
          )
        ORDER BY r.received_at ASC
        """,
        request_id,
        user["id"],
    )
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "request not found")

    # If we know a trace_id from any of the matching rows, fold in the
    # downstream signal + fills + events so one call gives the full picture.
    trace_id = next((r["trace_id"] for r in rows if r["trace_id"]), "")
    trace: dict[str, Any] = {}
    if trace_id:
        trace = await _trace_payload(pool, trace_id, user["id"])

    return {
        "request_id": request_id,
        "attempts": [
            {
                "received_at": r["received_at"].isoformat(),
                "service": r["service"],
                "route": r["route"],
                "method": r["method"],
                "client_ip": str(r["client_ip"]) if r["client_ip"] else None,
                "license_key": r["license_key"],
                "trace_id": r["trace_id"] or None,
                "status_code": r["status_code"],
                "outcome": r["outcome"],
                "reason_code": r["reason_code"],
                "latency_ms": r["latency_ms"],
                "body_sha256": r["body_sha256"],
                "user_agent": r["user_agent"],
                "detail": r["detail"]
                if isinstance(r["detail"], dict)
                else (json.loads(r["detail"]) if r["detail"] else {}),
            }
            for r in rows
        ],
        "trace": trace,
    }


async def _trace_payload(
    pool: asyncpg.Pool, trace_id: str, user_id: Any
) -> dict[str, Any]:
    """Shared trace lookup used by /traces/{id} and /requests/{id}.
    Returns signal + fills + events scoped to the user's licenses."""
    sig = await pool.fetchrow(
        """
        SELECT s.id, s.received_at, s.trace_id, s.command, s.symbol,
               s.ingress_region, s.payload
        FROM accepted_signals s
        JOIN licenses l ON l.id = s.license_id
        WHERE s.trace_id = $1 AND l.user_id = $2
        LIMIT 1
        """,
        trace_id,
        user_id,
    )
    fills = await pool.fetch(
        "SELECT id, created_at, status, broker_order_id, error_code, error_message "
        "FROM fills WHERE trace_id = $1 ORDER BY created_at",
        trace_id,
    )
    events = await pool.fetch(
        "SELECT event_type, severity, payload, created_at "
        "FROM system_events WHERE trace_id = $1 ORDER BY created_at",
        trace_id,
    )
    return {
        "trace_id": trace_id,
        "signal": {
            "id": str(sig["id"]),
            "received_at": sig["received_at"].isoformat(),
            "command": sig["command"],
            "symbol": sig["symbol"],
            "ingress_region": sig["ingress_region"],
            "payload": json.loads(sig["payload"]),
        }
        if sig
        else None,
        "fills": [
            {
                "id": str(f["id"]),
                "created_at": f["created_at"].isoformat(),
                "status": f["status"],
                "broker_order_id": f["broker_order_id"],
                "error_code": f["error_code"],
                "error_message": f["error_message"],
            }
            for f in fills
        ],
        "events": [
            {
                "event_type": e["event_type"],
                "severity": e["severity"],
                "payload": json.loads(e["payload"]),
                "created_at": e["created_at"].isoformat(),
            }
            for e in events
        ],
    }


@app.get("/traces/{trace_id}")
async def get_trace(
    trace_id: str,
    user: dict = Depends(current_user),
    pool: asyncpg.Pool = Depends(get_pool),
) -> dict:
    payload = await _trace_payload(pool, trace_id, user["id"])
    # Include the originating request_log rows for the trace so the user
    # can pivot to /requests/{request_id} from any trace lookup.
    req_rows = await pool.fetch(
        """
        SELECT received_at, request_id, service, route, status_code,
               outcome, reason_code, latency_ms
        FROM request_log
        WHERE trace_id = $1
          AND (
            license_key IS NULL
            OR license_key IN (
              SELECT license_key FROM licenses WHERE user_id = $2
            )
          )
        ORDER BY received_at
        """,
        trace_id,
        user["id"],
    )
    payload["requests"] = [
        {
            "received_at": r["received_at"].isoformat(),
            "request_id": r["request_id"],
            "service": r["service"],
            "route": r["route"],
            "status_code": r["status_code"],
            "outcome": r["outcome"],
            "reason_code": r["reason_code"],
            "latency_ms": r["latency_ms"],
        }
        for r in req_rows
    ]
    return payload


import time as _time


@app.post("/signals/{signal_id}/replay", status_code=status.HTTP_202_ACCEPTED)
async def replay_signal(
    signal_id: str,
    user: dict = Depends(current_user),
    pool: asyncpg.Pool = Depends(get_pool),
) -> dict:
    try:
        sid = uuid.UUID(signal_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "signal not found")

    row = await pool.fetchrow(
        """
        SELECT s.id, s.trace_id, s.raw_payload,
               s.command, s.symbol, s.ingress_region, s.payload,
               l.license_key, l.user_id,
               i.instance_key, i.platform
        FROM accepted_signals s
        JOIN licenses l ON l.id = s.license_id
        LEFT JOIN instances i ON i.id = s.instance_id
        WHERE s.id = $1 AND l.user_id = $2
        LIMIT 1
        """,
        sid,
        user["id"],
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "signal not found")

    if row["raw_payload"] is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "signal predates replay support (no raw_payload stored)",
        )

    nc = get_nats()
    js = nc.jetstream()

    original = decode_signal_proto(bytes(row["raw_payload"]))
    new_trace_id = original.get("trace_id", "") + "-r"
    original["trace_id"] = new_trace_id
    original["received_unix_nano"] = _time.time_ns()
    new_payload = encode_signal_proto(original)

    platform = row["platform"] or "mt5"
    subject = f"signals.{platform}.{row['license_key']}.{row['instance_key']}"

    await js.publish(subject, new_payload)

    await _audit(
        pool,
        user["id"],
        "replay_signal",
        reason=f"original_trace_id={row['trace_id']} new_trace_id={new_trace_id}",
    )

    return {"trace_id": new_trace_id, "subject": subject}


@app.post("/licenses/{license_id}/signals/correlate")
async def correlate_signals(
    license_id: str,
    body: dict[str, Any],
    user: dict = Depends(current_user),
    pool: asyncpg.Pool = Depends(get_pool),
) -> dict:
    lic = await _own_license(pool, license_id, user["id"])
    signal_ids = body.get("signal_ids", [])

    if not signal_ids or len(signal_ids) < 2:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "at least 2 signals required")

    signals = await pool.fetch(
        "SELECT id, symbol, command, entry_price FROM accepted_signals "
        "WHERE id = ANY($1) AND license_id = $2",
        signal_ids,
        lic["id"],
    )

    if not signals:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "signals not found")

    symbol_groups = {}
    for sig in signals:
        sym = sig["symbol"]
        if sym not in symbol_groups:
            symbol_groups[sym] = []
        symbol_groups[sym].append(sig)

    correlations = {}
    for sym_a, sym_b in [
        (list(symbol_groups.keys())[i], list(symbol_groups.keys())[j])
        for i in range(len(symbol_groups))
        for j in range(i + 1, len(symbol_groups))
    ]:
        corr = await pool.fetchval(
            "SELECT correlation_coefficient FROM symbol_correlations "
            "WHERE license_id = $1 AND symbol_a = $2 AND symbol_b = $3",
            lic["id"],
            sym_a,
            sym_b,
        )
        if corr is not None:
            correlations[f"{sym_a}-{sym_b}"] = corr

    conflicts = []
    for sym, sigs in symbol_groups.items():
        commands = [s["command"] for s in sigs]
        if "buy" in commands and "sell" in commands:
            conflicts.append(f"{sym}: conflicting buy/sell signals")

    return {
        "signals": [
            {"id": s["id"], "symbol": s["symbol"], "command": s["command"]}
            for s in signals
        ],
        "correlation_matrix": correlations,
        "symbol_groups": {k: len(v) for k, v in symbol_groups.items()},
        "conflicts": conflicts,
    }


@app.post("/licenses/{license_id}/signal-groups", status_code=status.HTTP_201_CREATED)
async def create_signal_group(
    license_id: str,
    body: dict[str, Any],
    user: dict = Depends(current_user),
    pool: asyncpg.Pool = Depends(get_pool),
) -> dict:
    lic = await _own_license(pool, license_id, user["id"])
    signal_ids = body.get("signal_ids", [])
    group_name = body.get("group_name", "")

    if not signal_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "signal_ids required")

    group_id = await pool.fetchval(
        "INSERT INTO signal_groups (license_id, group_name, metadata) "
        "VALUES ($1, $2, $3) RETURNING id",
        lic["id"],
        group_name,
        body.get("metadata", {}),
    )

    for sig_id in signal_ids:
        await pool.execute(
            "INSERT INTO signal_group_members (group_id, signal_id, membership_reason) "
            "VALUES ($1, $2, $3)",
            group_id,
            sig_id,
            body.get("reason", "user_grouped"),
        )

    return {"group_id": group_id, "signal_count": len(signal_ids)}


@app.get("/licenses/{license_id}/portfolio-exposure")
async def portfolio_exposure(
    license_id: str,
    user: dict = Depends(current_user),
    pool: asyncpg.Pool = Depends(get_pool),
) -> dict:
    lic = await _own_license(pool, license_id, user["id"])

    accounts = await pool.fetch(
        "SELECT DISTINCT account_id FROM account_positions WHERE license_id = $1",
        lic["id"],
    )

    result = {"license_id": str(lic["id"]), "accounts": []}

    for account_row in accounts:
        account_id = account_row["account_id"]

        positions = await pool.fetch(
            "SELECT symbol, position_size, entry_price, current_price FROM account_positions "
            "WHERE license_id = $1 AND account_id = $2",
            lic["id"],
            account_id,
        )

        limit = await pool.fetchrow(
            "SELECT max_notional_usd, max_position_size_pct, max_loss_pct FROM portfolio_exposure_limits "
            "WHERE license_id = $1 AND account_id = $2",
            lic["id"],
            account_id,
        )

        notional = sum(
            float(p["position_size"])
            * float(p["current_price"] or p["entry_price"] or 1.0)
            for p in positions
        )
        limit_usd = float(limit["max_notional_usd"] or 200000) if limit else 200000

        result["accounts"].append(
            {
                "account_id": account_id,
                "notional_usd": notional,
                "limit_usd": limit_usd,
                "utilization_pct": round(notional / limit_usd * 100, 2),
                "positions": [
                    {"symbol": p["symbol"], "size": float(p["position_size"])}
                    for p in positions
                ],
            }
        )

    return result


@app.get("/licenses/{license_id}/risk-metrics")
async def risk_metrics(
    license_id: str,
    user: dict = Depends(current_user),
    pool: asyncpg.Pool = Depends(get_pool),
) -> dict:
    lic = await _own_license(pool, license_id, user["id"])

    accounts = await pool.fetch(
        "SELECT DISTINCT account_id FROM account_drawdowns WHERE license_id = $1",
        lic["id"],
    )

    result = {
        "license_id": str(lic["id"]),
        "accounts": [],
        "total_exposure": 0,
        "total_limit": 0,
        "breaches": [],
    }

    for account_row in accounts:
        account_id = account_row["account_id"]

        drawdown = await pool.fetchrow(
            "SELECT peak_equity, current_equity, drawdown_pct FROM account_drawdowns "
            "WHERE license_id = $1 AND account_id = $2",
            lic["id"],
            account_id,
        )

        positions = await pool.fetch(
            "SELECT symbol, position_size, current_price FROM account_positions "
            "WHERE license_id = $1 AND account_id = $2 AND position_size != 0",
            lic["id"],
            account_id,
        )

        notional = sum(
            float(p["position_size"]) * float(p["current_price"] or 0)
            for p in positions
        )

        limit = await pool.fetchrow(
            "SELECT max_notional_usd FROM portfolio_exposure_limits "
            "WHERE license_id = $1 AND account_id = $2",
            lic["id"],
            account_id,
        )

        limit_usd = float(limit["max_notional_usd"]) if limit else 0
        result["total_exposure"] += notional
        result["total_limit"] += limit_usd

        largest_position = max(
            [
                (
                    p["symbol"],
                    p["position_size"],
                    float(p["position_size"]) * float(p["current_price"] or 0),
                )
                for p in positions
            ],
            key=lambda x: abs(x[2]),
            default=None,
        )

        result["accounts"].append(
            {
                "account_id": account_id,
                "notional_exposure": notional,
                "exposure_limit": limit_usd,
                "exposure_ratio": round(notional / limit_usd * 100, 2)
                if limit_usd
                else 0,
                "peak_equity": float(drawdown["peak_equity"]) if drawdown else 0,
                "current_equity": float(drawdown["current_equity"]) if drawdown else 0,
                "drawdown_pct": float(drawdown["drawdown_pct"]) if drawdown else 0,
                "largest_position": {
                    "symbol": largest_position[0],
                    "size": float(largest_position[1]),
                    "value": float(largest_position[2]),
                }
                if largest_position
                else None,
            }
        )

    breaches = await pool.fetch(
        "SELECT account_id, breach_type, current_value, limit_value, created_at "
        "FROM risk_breach_log WHERE license_id = $1 ORDER BY created_at DESC LIMIT 20",
        lic["id"],
    )

    result["breaches"] = [
        {
            "account_id": b["account_id"],
            "breach_type": b["breach_type"],
            "current_value": float(b["current_value"]),
            "limit_value": float(b["limit_value"]),
            "created_at": b["created_at"].isoformat(),
        }
        for b in breaches
    ]

    return result


@app.post("/api/backtest", status_code=status.HTTP_200_OK)
async def backtest(
    body: dict[str, Any],
    pool: asyncpg.Pool = Depends(get_pool),
) -> dict:
    license_id = body.get("license_id")
    date_start = body.get("date_start")
    date_end = body.get("date_end")

    if not license_id or not date_start or not date_end:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "license_id, date_start, date_end required"
        )

    async with httpx.AsyncClient(timeout=300.0) as client:
        try:
            resp = await client.post(
                "http://backtester:8080/backtest",
                json={
                    "license_id": license_id,
                    "date_start": date_start,
                    "date_end": date_end,
                },
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"backtester service error: {str(exc)[:200]}",
            )


@app.post("/licenses/{license_id}/test-signal", status_code=status.HTTP_202_ACCEPTED)
async def test_signal(
    license_id: str,
    user: dict = Depends(current_user),
    pool: asyncpg.Pool = Depends(get_pool),
) -> dict:
    import hashlib
    import hmac as _hmac
    import time as _time2

    lic = await _own_license(pool, license_id, user["id"])
    inst = await pool.fetchrow(
        "SELECT instance_key, platform FROM instances "
        "WHERE license_id = $1 AND active = TRUE LIMIT 1",
        lic["id"],
    )
    if inst is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "no active instances — add one first"
        )

    body = f"{lic['license_key']}:buy:{inst['instance_key']}:symbol=EURUSD".encode()
    ts = str(int(_time2.time()))

    sig = _hmac.new(lic["hmac_secret"].encode(), body, hashlib.sha256).hexdigest()

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{INGRESS_URL}/webhook",
            content=body,
            headers={
                "Content-Type": "text/plain",
                "X-ExecRelay-Timestamp": ts,
                "X-ExecRelay-Signature": f"sha256={sig}",
            },
        )

    if resp.status_code not in (200, 202):
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"ingress returned {resp.status_code}: {resp.text[:200]}",
        )

    trace_id = resp.json().get("trace_id", "")
    await _audit(
        pool,
        user["id"],
        "test_signal",
        after={"trace_id": trace_id, "license_id": license_id},
    )
    return {"trace_id": trace_id, "status": "accepted"}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _own_license(pool: asyncpg.Pool, license_id: str, user_id: Any) -> Any:
    try:
        lid = uuid.UUID(license_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "license not found")
    row = await pool.fetchrow(
        "SELECT id, license_key, hmac_secret, pending_hmac_secret, active FROM licenses "
        "WHERE id = $1 AND user_id = $2",
        lid,
        user_id,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "license not found")
    return row


# ---------------------------------------------------------------------------
# Trade journal export
# ---------------------------------------------------------------------------
#
# Exports fills (executed trades) for the authenticated user as CSV or JSON
# over a date range. Streams the rows so a large export doesn't materialize the
# whole result set in memory. Always scoped to the requesting user's licenses
# via the JOIN on licenses.user_id — never returns another user's data.
#
# GET /journal/export?from=2025-01-01&to=2025-12-31&format=csv
# GET /journal/export?from=2025-01-01&to=2025-12-31&format=json
#
# from/to are ISO dates (date-only); the range is inclusive on `from` and
# exclusive on `to`. Default `to` is today; default `from` is 30 days ago.

from fastapi.responses import StreamingResponse  # noqa: E402

JOURNAL_MAX_ROWS = 200_000


@app.get("/journal/export")
async def journal_export(
    from_: str | None = None,
    to: str | None = None,
    format: str = "csv",
    user: dict = Depends(current_user),
    pool: asyncpg.Pool = Depends(get_pool),
) -> StreamingResponse:
    # FastAPI query alias to accept ?from= (Python keyword)
    from_value = from_

    fmt = format.lower().strip()
    if fmt not in ("csv", "json"):
        raise HTTPException(status_code=400, detail="format must be csv or json")

    now = _time.time()
    try:
        end_ts = (
            _datetime.fromisoformat(to).replace(tzinfo=_timezone.utc)
            if to
            else _datetime.fromtimestamp(now, _timezone.utc)
        )
        start_ts = (
            _datetime.fromisoformat(from_value).replace(tzinfo=_timezone.utc)
            if from_value
            else end_ts - _timedelta(days=30)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid date: {exc}") from exc

    if start_ts >= end_ts:
        raise HTTPException(status_code=400, detail="from must be before to")

    # Bound the result set so a runaway export can't OOM the server.
    sql = (
        "SELECT f.id::text AS fill_id, f.trace_id, f.broker_order_id, "
        "       f.status, f.error_code, f.error_message, f.payload, "
        "       f.created_at, l.license_key, i.instance_key "
        "  FROM fills f "
        "  JOIN licenses l ON l.id = f.license_id "
        "  LEFT JOIN instances i ON i.id = f.instance_id "
        " WHERE l.user_id = $1 AND f.created_at >= $2 AND f.created_at < $3 "
        " ORDER BY f.created_at ASC "
        " LIMIT $4"
    )

    if fmt == "csv":
        return StreamingResponse(
            _stream_csv(pool, sql, user["id"], start_ts, end_ts),
            media_type="text/csv",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="execrelay-journal-'
                    f'{start_ts.date()}-{end_ts.date()}.csv"'
                ),
            },
        )

    return StreamingResponse(
        _stream_json(pool, sql, user["id"], start_ts, end_ts),
        media_type="application/json",
    )


async def _stream_csv(
    pool: asyncpg.Pool,
    sql: str,
    user_id: str,
    start_ts: "_datetime",
    end_ts: "_datetime",
) -> "_AsyncIterator[str]":
    yield (
        "fill_id,trace_id,broker_order_id,status,error_code,"
        "error_message,license_key,instance_key,created_at,payload_json\n"
    )
    async with pool.acquire() as conn:
        async with conn.transaction():
            async for row in conn.cursor(
                sql, user_id, start_ts, end_ts, JOURNAL_MAX_ROWS
            ):
                yield _csv_row(row)


async def _stream_json(
    pool: asyncpg.Pool,
    sql: str,
    user_id: str,
    start_ts: "_datetime",
    end_ts: "_datetime",
) -> "_AsyncIterator[str]":
    yield '{"fills":['
    first = True
    async with pool.acquire() as conn:
        async with conn.transaction():
            async for row in conn.cursor(
                sql, user_id, start_ts, end_ts, JOURNAL_MAX_ROWS
            ):
                if not first:
                    yield ","
                first = False
                yield _json_row(row)
    yield "]}"


def _csv_row(row: "asyncpg.Record") -> str:
    fields = [
        row["fill_id"],
        row["trace_id"] or "",
        row["broker_order_id"] or "",
        row["status"],
        row["error_code"] or "",
        row["error_message"] or "",
        row["license_key"],
        row["instance_key"] or "",
        row["created_at"].isoformat(),
        _json.dumps(row["payload"]) if row["payload"] is not None else "",
    ]
    return ",".join(_csv_escape(str(f)) for f in fields) + "\n"


def _json_row(row: "asyncpg.Record") -> str:
    return _json.dumps(
        {
            "fill_id": row["fill_id"],
            "trace_id": row["trace_id"],
            "broker_order_id": row["broker_order_id"],
            "status": row["status"],
            "error_code": row["error_code"],
            "error_message": row["error_message"],
            "license_key": row["license_key"],
            "instance_key": row["instance_key"],
            "created_at": row["created_at"].isoformat(),
            "payload": row["payload"],
        }
    )


def _csv_escape(s: str) -> str:
    if any(c in s for c in (",", '"', "\n", "\r")):
        return '"' + s.replace('"', '""') + '"'
    return s


@app.get("/user/export")
async def export_user_data(
    user: dict = Depends(current_user),
    pool: asyncpg.Pool = Depends(get_pool),
) -> dict:
    user_id = user["id"]

    # Fetch user profile details
    user_row = await pool.fetchrow(
        "SELECT id, email, created_at FROM users WHERE id = $1", user_id
    )
    profile = {
        "id": str(user_row["id"]),
        "email": user_row["email"],
        "created_at": user_row["created_at"].isoformat()
        if user_row["created_at"]
        else None,
    }

    # Fetch roles
    role_rows = await pool.fetch(
        "SELECT r.name FROM user_roles ur JOIN roles r ON r.id = ur.role_id WHERE ur.user_id = $1",
        user_id,
    )
    roles = [r["name"] for r in role_rows]

    # Fetch licenses
    license_rows = await pool.fetch(
        "SELECT id, license_key, active, created_at FROM licenses WHERE user_id = $1",
        user_id,
    )
    licenses = [
        {
            "id": str(r["id"]),
            "license_key": r["license_key"],
            "active": r["active"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in license_rows
    ]

    # Fetch instances
    instance_rows = await pool.fetch(
        """
        SELECT i.id, i.license_id, i.instance_key, i.platform, i.active, i.created_at
        FROM instances i
        JOIN licenses l ON l.id = i.license_id
        WHERE l.user_id = $1
        """,
        user_id,
    )
    instances = [
        {
            "id": str(r["id"]),
            "license_id": str(r["license_id"]),
            "instance_key": r["instance_key"],
            "platform": r["platform"],
            "active": r["active"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in instance_rows
    ]

    # Fetch audit logs where user is actor
    audit_rows = await pool.fetch(
        "SELECT id, action, reason, before_state, after_state, created_at FROM admin_audit_log WHERE actor_user_id = $1",
        user_id,
    )
    audit_logs = []
    for r in audit_rows:
        before = r["before_state"]
        if isinstance(before, str):
            try:
                before = json.loads(before)
            except Exception:
                pass
        after = r["after_state"]
        if isinstance(after, str):
            try:
                after = json.loads(after)
            except Exception:
                pass
        audit_logs.append(
            {
                "id": str(r["id"]),
                "action": r["action"],
                "reason": r["reason"],
                "before_state": before,
                "after_state": after,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
        )

    # Fetch report subscriptions
    sub_rows = await pool.fetch(
        "SELECT id, report_type, schedule, active, created_at FROM report_subscriptions WHERE user_id = $1",
        user_id,
    )
    subscriptions = [
        {
            "id": str(r["id"]),
            "report_type": r["report_type"],
            "schedule": r["schedule"],
            "active": r["active"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in sub_rows
    ]

    # Fetch portfolio exposure limits
    limit_rows = await pool.fetch(
        """
        SELECT id, account_id, max_notional_usd, max_position_size_pct, max_loss_pct, created_at
        FROM portfolio_exposure_limits
        WHERE license_id IN (SELECT id FROM licenses WHERE user_id = $1)
        """,
        user_id,
    )
    limits = [
        {
            "id": str(r["id"]),
            "account_id": r["account_id"],
            "max_notional_usd": float(r["max_notional_usd"])
            if r["max_notional_usd"] is not None
            else None,
            "max_position_size_pct": float(r["max_position_size_pct"])
            if r["max_position_size_pct"] is not None
            else None,
            "max_loss_pct": float(r["max_loss_pct"])
            if r["max_loss_pct"] is not None
            else None,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in limit_rows
    ]

    return {
        "profile": profile,
        "roles": roles,
        "licenses": licenses,
        "instances": instances,
        "audit_logs": audit_logs,
        "report_subscriptions": subscriptions,
        "portfolio_exposure_limits": limits,
    }


# Local imports to avoid disturbing the top of the file. Aliased with leading
# underscores so they're not part of the module's public surface.
import json as _json  # noqa: E402
import time as _time  # noqa: E402
from datetime import datetime as _datetime  # noqa: E402
from datetime import timedelta as _timedelta  # noqa: E402
from datetime import timezone as _timezone  # noqa: E402
from typing import AsyncIterator as _AsyncIterator  # noqa: E402, F401


# ---------------------------------------------------------------------------
# Entry point (uvicorn)
# ---------------------------------------------------------------------------


def main() -> None:
    import urllib.request

    parser = argparse.ArgumentParser()
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()

    if args.healthcheck:
        host_part = (
            "127.0.0.1"
            if HTTP_ADDR.startswith("0.0.0.0:")
            else HTTP_ADDR.rsplit(":", 1)[0]
        )
        port_part = HTTP_ADDR.rsplit(":", 1)[1]
        with urllib.request.urlopen(
            f"http://{host_part}:{port_part}/health", timeout=1.5
        ) as r:
            if r.status != 200:
                raise SystemExit(1)
        return

    import uvicorn

    host, port = HTTP_ADDR.rsplit(":", 1)
    uvicorn.run(app, host=host, port=int(port), log_level="info")


if __name__ == "__main__":
    main()
