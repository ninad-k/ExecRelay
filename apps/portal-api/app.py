from __future__ import annotations

import argparse
import json
import logging
import os
import re
import secrets
import sys
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
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from pydantic import BaseModel, EmailStr

SERVICE = "portal-api"
HTTP_ADDR = os.environ.get("HTTP_ADDR", "0.0.0.0:8080")
DB_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://execrelay:execrelay_dev_password@postgres:5432/execrelay",
)
JWT_SECRET = os.environ.get("JWT_SECRET", "changeme-in-production")
JWT_ALGO = "HS256"
JWT_TTL_DAYS = 7
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get("PORTAL_ALLOWED_ORIGINS", "*").split(",")
    if o.strip()
]
_INSTANCE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
NATS_URL = os.environ.get("NATS_URL", "nats://execrelay:execrelay_nats_dev@nats:4222")
INGRESS_URL = os.environ.get("INGRESS_URL", "http://ingress:8080")
SIGNALS_STREAM = os.environ.get("SIGNALS_STREAM", "SIGNALS")

logger = logging.getLogger(SERVICE)
DEBUG = os.environ.get("DEBUG", "true").lower() in ("true", "1", "yes", "on")
log_level = logging.DEBUG if DEBUG else logging.INFO
logging.basicConfig(
    level=log_level,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stdout,
)

if DEBUG:
    logger.info("Debug logging enabled")

if JWT_SECRET == "changeme-in-production":
    logger.warning(
        "JWT_SECRET is using the default value — set it before any non-local deployment"
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
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
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
    return {"service": SERVICE, "status": "ok"}


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


@app.get("/traces/{trace_id}")
async def get_trace(
    trace_id: str,
    user: dict = Depends(current_user),
    pool: asyncpg.Pool = Depends(get_pool),
) -> dict:
    sig = await pool.fetchrow(
        """
        SELECT s.id, s.received_at, s.trace_id, s.command, s.symbol,
               s.ingress_region, s.payload, l.user_id
        FROM accepted_signals s
        JOIN licenses l ON l.id = s.license_id
        WHERE s.trace_id = $1 AND l.user_id = $2
        LIMIT 1
        """,
        trace_id,
        user["id"],
    )

    fills = await pool.fetch(
        "SELECT id, created_at, status, broker_order_id, error_code, error_message, payload "
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
