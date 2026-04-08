from fastapi import Header, HTTPException
from supabase import create_client, Client

from app.config import get_settings


def get_supabase() -> Client:
    s = get_settings()
    return create_client(s.supabase_url, s.supabase_key)


async def require_api_key(x_api_key: str = Header(...)):
    if x_api_key != get_settings().de_api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")


