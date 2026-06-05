import pytest

from app.models.job import NormalizedJob
from app.resolution.description_resolver import DescriptionResolver

GH_HTML = "<p>" + ("Greenhouse SAFe Scrum full posting text. " * 10) + "</p>"
GENERIC_HTML = (
    "<html><body><h1>Role</h1><p>"
    + ("Full job description with many keywords. " * 10)
    + "</p></body></html>"
)


class FakeResp:
    def __init__(self, *, text="", data=None, url=""):
        self.text = text
        self._data = data or {}
        self.url = url

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class FakeClient:
    """Routes greenhouse-api calls to JSON, everything else to HTML."""

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None):
        if "boards-api.greenhouse.io" in url:
            return FakeResp(data={"content": GH_HTML}, url=url)
        return FakeResp(text=GENERIC_HTML, url=url)


def _job(url, desc=""):
    return NormalizedJob(
        title="X", url=url, source="adzuna", external_id="e", description=desc
    )


@pytest.mark.asyncio
async def test_generic_fills_thin_description(monkeypatch):
    import app.resolution.description_resolver as mod

    monkeypatch.setattr(mod.httpx, "AsyncClient", FakeClient)
    job = _job("https://acme.softgarden.io/job/1")
    resolver = DescriptionResolver({"min_description_chars": 200})
    filled = await resolver.resolve_batch([job])
    assert filled == 1
    assert "Full job description" in job.description


@pytest.mark.asyncio
async def test_greenhouse_json_path(monkeypatch):
    import app.resolution.description_resolver as mod

    monkeypatch.setattr(mod.httpx, "AsyncClient", FakeClient)
    job = _job("https://boards.greenhouse.io/acme/jobs/123")
    resolver = DescriptionResolver({"min_description_chars": 200})
    filled = await resolver.resolve_batch([job])
    assert filled == 1
    assert "Greenhouse SAFe Scrum" in job.description


@pytest.mark.asyncio
async def test_long_description_skipped_by_gate(monkeypatch):
    import app.resolution.description_resolver as mod

    monkeypatch.setattr(mod.httpx, "AsyncClient", FakeClient)
    long_desc = "already full " * 50  # > 200 chars
    job = _job("https://acme.softgarden.io/job/1", desc=long_desc)
    resolver = DescriptionResolver({"min_description_chars": 200})
    filled = await resolver.resolve_batch([job])
    assert filled == 0
    assert job.description == long_desc


@pytest.mark.asyncio
async def test_max_resolve_cap(monkeypatch):
    import app.resolution.description_resolver as mod

    monkeypatch.setattr(mod.httpx, "AsyncClient", FakeClient)
    jobs = [
        _job("https://acme.softgarden.io/job/1"),
        _job("https://acme.softgarden.io/job/2"),
    ]
    resolver = DescriptionResolver({"min_description_chars": 200, "max_resolve": 1})
    filled = await resolver.resolve_batch(jobs)
    assert filled == 1
    # First target filled, second left untouched by the cap.
    assert "Full job description" in jobs[0].description
    assert jobs[1].description == ""


@pytest.mark.asyncio
async def test_fetch_failure_leaves_job_unchanged(monkeypatch):
    import app.resolution.description_resolver as mod

    class BoomClient(FakeClient):
        async def get(self, url, params=None):
            raise RuntimeError("network down")

    monkeypatch.setattr(mod.httpx, "AsyncClient", BoomClient)
    job = _job("https://acme.softgarden.io/job/1")
    resolver = DescriptionResolver({"min_description_chars": 200})
    filled = await resolver.resolve_batch([job])
    assert filled == 0
    assert job.description == ""


@pytest.mark.asyncio
async def test_no_url_skipped(monkeypatch):
    import app.resolution.description_resolver as mod

    monkeypatch.setattr(mod.httpx, "AsyncClient", FakeClient)
    job = _job("")
    resolver = DescriptionResolver({"min_description_chars": 200})
    filled = await resolver.resolve_batch([job])
    assert filled == 0
