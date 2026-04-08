import logging

from app.repositories.base import BaseRepository

logger = logging.getLogger(__name__)


class ProfileRepository(BaseRepository):
    TABLE = "profiles"

    async def get(self, profile_id: str) -> dict | None:
        result = (
            self.client.table(self.TABLE)
            .select("*")
            .eq("id", profile_id)
            .execute()
        )
        return result.data[0] if result.data else None

    async def create(self, data: dict) -> dict:
        # Map model field to DB columns
        if "keywords_positive" in data:
            data["keywords_positive_tech"] = data.pop("keywords_positive")
        data.pop("keywords_negative", None) if not data.get("keywords_negative") else None
        result = self.client.table(self.TABLE).insert(data).execute()
        return result.data[0] if result.data else {}

    async def update(self, profile_id: str, data: dict) -> dict:
        result = (
            self.client.table(self.TABLE)
            .update(data)
            .eq("id", profile_id)
            .execute()
        )
        return result.data[0] if result.data else {}

    async def delete(self, profile_id: str) -> bool:
        result = (
            self.client.table(self.TABLE)
            .delete()
            .eq("id", profile_id)
            .execute()
        )
        return bool(result.data)

    async def list_all(self) -> list[dict]:
        result = (
            self.client.table(self.TABLE)
            .select("id,name,archetypes,target_roles")
            .execute()
        )
        return result.data or []
