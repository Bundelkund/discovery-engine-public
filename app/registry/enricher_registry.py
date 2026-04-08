class EnricherRegistry:
    _enrichers: dict[str, type] = {}

    @classmethod
    def register(cls, enricher_id: str):
        def decorator(klass):
            if enricher_id in cls._enrichers:
                raise ValueError(f"Already registered: {enricher_id}")
            cls._enrichers[enricher_id] = klass
            return klass

        return decorator

    @classmethod
    def get(cls, enricher_id: str):
        if enricher_id not in cls._enrichers:
            raise KeyError(f"Enricher not registered: {enricher_id}")
        return cls._enrichers[enricher_id]

    @classmethod
    def get_all(cls) -> dict[str, type]:
        return dict(cls._enrichers)

    @classmethod
    def registered_ids(cls) -> list[str]:
        return list(cls._enrichers.keys())
