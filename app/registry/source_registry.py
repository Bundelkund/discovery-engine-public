class SourceRegistry:
    _adapters: dict[str, type] = {}

    @classmethod
    def register(cls, source_id: str):
        def decorator(klass):
            if source_id in cls._adapters:
                raise ValueError(f"Already registered: {source_id}")
            cls._adapters[source_id] = klass
            return klass

        return decorator

    @classmethod
    def get(cls, source_id: str):
        if source_id not in cls._adapters:
            raise KeyError(f"Source not registered: {source_id}")
        return cls._adapters[source_id]

    @classmethod
    def get_all(cls) -> dict[str, type]:
        return dict(cls._adapters)

    @classmethod
    def registered_ids(cls) -> list[str]:
        return list(cls._adapters.keys())
