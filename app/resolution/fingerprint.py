from urllib.parse import urlparse


def detect_provider(url: str) -> str:
    """Map a job URL to a description-resolution provider.

    Returns "greenhouse" for Greenhouse-hosted postings (clean JSON API),
    "generic" for any other resolvable http(s) URL (HTML strip), or "" to
    skip (no host / non-http scheme).
    """
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    if parsed.scheme not in ("http", "https"):
        return ""
    host = (parsed.hostname or "").lower()
    if not host:
        return ""
    if "greenhouse.io" in host:
        return "greenhouse"
    return "generic"
