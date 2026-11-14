"""ElevenLabs post-call webhook signatures (HMAC-SHA256).

Header `ElevenLabs-Signature: t=<unix seconds>,v0=<hex>`, where hex is
HMAC-SHA256(secret, f"{t}.{raw_body}"). Requests older than 30 minutes are
rejected. Verify against the raw body bytes, before any JSON parsing. The
simulator signs its post-call payloads with the same function, so simulated
and real webhooks share one code path.

The header comes from anyone on the internet, and Starlette decodes header
bytes as latin-1, so it may hold any character. Parsing accepts only what a
real signature can contain (`t` a short run of ASCII digits, `v0` ASCII hex)
and everything else is "not signed": a malformed header is a 401, never an
exception (non-ASCII text makes hmac.compare_digest raise TypeError,
superscript digits pass str.isdigit() and then make int() raise, and int()
refuses a run of digits longer than 4300).
"""
from __future__ import annotations

import hashlib
import hmac
import re
import time

HEADER = "ElevenLabs-Signature"
TOLERANCE_S = 30 * 60

# Unix seconds have 10 digits until the year 2286; 15 leaves room and keeps int() cheap.
_TIMESTAMP = re.compile(r"[0-9]{1,15}")
_HEX = re.compile(r"[0-9a-fA-F]{1,128}")


def _digest(secret: str, t: str, raw: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), f"{t}.".encode("utf-8") + raw, hashlib.sha256).hexdigest()


def sign(raw: bytes, secret: str, t: int | None = None) -> str:
    t = str(int(time.time()) if t is None else int(t))
    return f"t={t},v0={_digest(secret, t, raw)}"


def parse(header: str | None) -> tuple[str, str] | None:
    """(t, v0) from a signature header, or None when it is missing or cannot
    be a signature. Uses fullmatch on ASCII-only patterns, so a non-ASCII
    character anywhere in t or v0 is refused here, not by a crash later."""
    if not header:
        return None
    parts = {}
    for piece in header.split(","):
        k, _, v = piece.strip().partition("=")
        if k:
            parts[k] = v
    t, sig = parts.get("t") or "", parts.get("v0") or ""
    if not _TIMESTAMP.fullmatch(t) or not _HEX.fullmatch(sig):
        return None
    return t, sig


def well_formed(header: str | None) -> bool:
    """Whether the header could carry a signature at all. The webhook checks
    this before it reads the body, so an unsigned request costs no memory."""
    return parse(header) is not None


def verify(raw: bytes, header: str | None, secret: str, *, now: float | None = None,
           tolerance_s: int = TOLERANCE_S) -> bool:
    parsed = parse(header)
    if parsed is None or not secret:
        return False
    t, sig = parsed
    now = time.time() if now is None else now
    if int(t) < now - tolerance_s:
        return False
    return hmac.compare_digest(sig.lower().encode("ascii"), _digest(secret, t, raw).encode("ascii"))
