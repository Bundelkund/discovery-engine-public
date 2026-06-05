from app.resolution.fingerprint import detect_provider


def test_greenhouse_host():
    assert detect_provider("https://boards.greenhouse.io/acme/jobs/123") == "greenhouse"
    assert detect_provider("https://job-boards.greenhouse.io/acme/jobs/123") == "greenhouse"


def test_generic_host():
    assert detect_provider("https://acme.softgarden.io/job/999") == "generic"
    assert detect_provider("https://careers.example.com/job/1") == "generic"


def test_skip_empty_or_non_http():
    assert detect_provider("") == ""
    assert detect_provider("mailto:hr@example.com") == ""
    assert detect_provider("not a url") == ""
