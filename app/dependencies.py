import hmac
import logging
import os
from functools import lru_cache
from pathlib import Path

import yaml
from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client

from app.config import get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------------------


def get_supabase() -> Client:
    s = get_settings()
    return create_client(s.supabase_url, s.supabase_key)


# ---------------------------------------------------------------------------
# Per-Consumer API Key Auth (Phase 5)
# ---------------------------------------------------------------------------


class ConsumerIdentity(BaseModel):
    id: str
    name: str
    scopes: list[str]


@lru_cache
def _load_consumers() -> list[dict]:
    path = Path(__file__).parent.parent / "config" / "api-keys.yaml"
    with open(path) as f:
        return yaml.safe_load(f).get("consumers", [])


def _build_key_index() -> dict[str, dict]:
    """env-key-value -> consumer dict, rebuilt each call so env changes are seen in tests."""
    index: dict[str, dict] = {}
    for c in _load_consumers():
        env_value = os.environ.get(c["key_env"])
        if env_value:
            index[env_value] = c
    return index


def get_consumer(x_api_key: str = Header(..., alias="X-API-Key")) -> ConsumerIdentity:
    index = _build_key_index()
    # hmac.compare_digest requires both args to be ASCII-only if str; encode
    # to bytes so non-ASCII keys (legitimate or malformed input) don't crash.
    provided = x_api_key.encode("utf-8")
    matched: dict | None = None
    for env_value, c in index.items():
        if hmac.compare_digest(env_value.encode("utf-8"), provided):
            matched = c
            break
    if matched is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if not matched.get("active", False):
        raise HTTPException(status_code=403, detail="Consumer not active")
    identity = ConsumerIdentity(id=matched["id"], name=matched["name"], scopes=matched["scopes"])
    logger.info("request_authenticated", extra={"consumer_id": identity.id})
    return identity


def require_scope(scope: str):
    """Return a FastAPI dependency that verifies the consumer carries `scope`.

    Use as `Depends(require_scope("jobs:read"))` on a route. Raises 403 if the
    consumer's `scopes` list does not include the required scope.
    """

    def _check(consumer: ConsumerIdentity = Depends(get_consumer)) -> ConsumerIdentity:
        if scope not in consumer.scopes:
            logger.warning(
                "scope_denied",
                extra={"consumer_id": consumer.id, "required_scope": scope},
            )
            raise HTTPException(
                status_code=403,
                detail=f"Consumer '{consumer.id}' missing required scope: {scope}",
            )
        return consumer

    return _check
