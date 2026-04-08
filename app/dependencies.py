from fastapi import Depends, Header, HTTPException
from supabase import create_client, Client

from app.config import get_settings


def get_supabase() -> Client:
    s = get_settings()
    return create_client(s.supabase_url, s.supabase_key)


async def require_api_key(x_api_key: str = Header(...)):
    if x_api_key != get_settings().de_api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")


async def load_profile(
    profile_id: str, supabase: Client = Depends(get_supabase)
):
    result = (
        supabase.table("profiles").select("*").eq("id", profile_id).execute()
    )
    if not result.data:
        raise HTTPException(
            status_code=404, detail=f"Profile {profile_id} not found"
        )
    return result.data[0]
