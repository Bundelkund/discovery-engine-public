from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.dependencies import get_supabase, require_api_key
from app.repositories.profiles import ProfileRepository

profiles_router = APIRouter(prefix="/profiles", tags=["profiles"])


class CreateProfileRequest(BaseModel):
    user_id: str
    name: str
    archetypes: dict[str, float] = {}
    keywords_positive: list[str] = []
    keywords_negative: list[str] = []
    seniority_boost: list[str] = [
        "Senior", "Lead", "Head", "Principal",
    ]
    seniority_penalty: list[str] = [
        "Junior", "Intern", "Trainee", "Werkstudent",
    ]
    target_roles: list[str] = []
    cv_text: str = ""


class UpdateProfileRequest(BaseModel):
    name: Optional[str] = None
    archetypes: Optional[dict[str, float]] = None
    keywords_positive: Optional[list[str]] = None
    keywords_negative: Optional[list[str]] = None
    target_roles: Optional[list[str]] = None
    cv_text: Optional[str] = None


@profiles_router.post("", dependencies=[Depends(require_api_key)])
async def create_profile(
    request: CreateProfileRequest, supabase=Depends(get_supabase)
):
    repo = ProfileRepository(supabase)
    data = request.model_dump()
    result = await repo.create(data)
    return result


@profiles_router.get("", dependencies=[Depends(require_api_key)])
async def list_profiles(supabase=Depends(get_supabase)):
    repo = ProfileRepository(supabase)
    return await repo.list_all()


@profiles_router.get("/{profile_id}", dependencies=[Depends(require_api_key)])
async def get_profile(profile_id: str, supabase=Depends(get_supabase)):
    repo = ProfileRepository(supabase)
    result = await repo.get(profile_id)
    if not result:
        raise HTTPException(status_code=404, detail="Profile not found")
    return result


@profiles_router.put("/{profile_id}", dependencies=[Depends(require_api_key)])
async def update_profile(
    profile_id: str,
    request: UpdateProfileRequest,
    supabase=Depends(get_supabase),
):
    repo = ProfileRepository(supabase)
    data = {k: v for k, v in request.model_dump().items() if v is not None}
    result = await repo.update(profile_id, data)
    if not result:
        raise HTTPException(status_code=404, detail="Profile not found")
    return result


@profiles_router.delete("/{profile_id}", dependencies=[Depends(require_api_key)])
async def delete_profile(profile_id: str, supabase=Depends(get_supabase)):
    repo = ProfileRepository(supabase)
    success = await repo.delete(profile_id)
    if not success:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"status": "deleted"}
