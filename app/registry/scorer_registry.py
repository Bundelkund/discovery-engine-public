class ScorerRegistry:
    _scorers: dict[str, type] = {}

    @classmethod
    def register(cls, scorer_id: str):
        def decorator(klass):
            if scorer_id in cls._scorers:
                raise ValueError(f"Already registered: {scorer_id}")
            cls._scorers[scorer_id] = klass
            return klass

        return decorator

    @classmethod
    def get(cls, scorer_id: str):
        if scorer_id not in cls._scorers:
            raise KeyError(f"Scorer not registered: {scorer_id}")
        return cls._scorers[scorer_id]

    @classmethod
    def get_all(cls) -> dict[str, type]:
        return dict(cls._scorers)

    @classmethod
    def registered_ids(cls) -> list[str]:
        return list(cls._scorers.keys())
