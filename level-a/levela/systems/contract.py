"""The contract an order system must keep, checked in code (V2 §7 "Detect").

The agent speaks one canonical vocabulary; an adapter maps each canonical
field to a dot path in the order system's JSON ("qty_in_stock" -> "inventory.available").
A response that lacks a field the active adapter expects, or a count that is
not an integer, or a 410, is a contract failure: a fact established here, not
the model's opinion. ContractError carries the one-line failure text (what
the feed shows, what memory embeds) and the raw body (what a repairer reads).
"""
from __future__ import annotations

# Canonical fields, by the response they come from.
HOLD_FIELDS = ("hold_id", "batch", "account_scope", "status")
ACCOUNT_FIELDS = ("account", "name", "phone", "qty_shipped", "qty_in_stock")
CANONICAL = ("batch", "account_scope", "hold_id", "status", "account", "name", "phone", "qty_shipped", "qty_in_stock")
GROUPS = {"holds": HOLD_FIELDS, "accounts": ACCOUNT_FIELDS}
INTEGER_FIELDS = frozenset({"qty_shipped", "qty_in_stock"})

_MISSING = object()


class ContractError(Exception):
    """The order system broke the active adapter's contract.

    failure_text  one line, e.g. "field qty_in_stock missing" or
                  "GET /v1/accounts answered 410 Gone (API v1 was retired)"
    raw_body      the response body as text, verbatim
    status        the HTTP status (None when the body itself was wrong)
    endpoint      "METHOD /path" that failed
    """

    def __init__(self, failure_text: str, *, raw_body: str = "", status: int | None = None,
                 endpoint: str | None = None):
        super().__init__(failure_text)
        self.failure_text, self.raw_body, self.status, self.endpoint = failure_text, raw_body, status, endpoint

    def as_record(self) -> dict:
        """What contain() parties on the adapter and repair reads back."""
        return {"text": self.failure_text, "error_body": self.raw_body[:4000], "status": self.status,
                "endpoint": self.endpoint}


def get_path(doc, path: str):
    """The value at a dot path, or _MISSING."""
    cur = doc
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def set_path(doc: dict, path: str, value) -> None:
    """Write value at a dot path, creating the objects on the way."""
    parts = path.split(".")
    cur = doc
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def expected_fields(mapping: dict) -> dict:
    """{"accounts": [...paths], "holds": [...paths]}: what every row must carry."""
    return {group: [mapping[f] for f in fields] for group, fields in GROUPS.items()}


def _is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def to_canonical(rows, mapping: dict, group: str, *, endpoint: str, raw_body: str = "") -> list[dict]:
    """Order system rows -> canonical rows, or ContractError naming the first field
    (by its order system path) that a row lacks or carries with the wrong type."""
    if not isinstance(rows, list):
        raise ContractError(f"{endpoint} did not return a list", raw_body=raw_body, endpoint=endpoint)
    out = []
    for row in rows:
        canon = {}
        missing = []
        for field in GROUPS[group]:
            value = get_path(row, mapping[field])
            if value is _MISSING:
                missing.append(mapping[field])
                continue
            if field in INTEGER_FIELDS and not _is_count(value):
                raise ContractError(f"field {mapping[field]} is not a count ({value!r})", raw_body=raw_body,
                                    endpoint=endpoint)
            canon[field] = value
        if missing:
            noun = "field" if len(missing) == 1 else "fields"
            raise ContractError(f"{noun} {', '.join(missing)} missing", raw_body=raw_body, endpoint=endpoint)
        out.append(canon)
    return out


def require_field(body, path: str, *, endpoint: str, raw_body: str = ""):
    """One field of a single-object response, or ContractError."""
    value = get_path(body, path)
    if value is _MISSING:
        raise ContractError(f"field {path} missing", raw_body=raw_body, endpoint=endpoint)
    return value
