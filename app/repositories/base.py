from supabase import Client


class BaseRepository:
    def __init__(self, client: Client):
        self.client = client
