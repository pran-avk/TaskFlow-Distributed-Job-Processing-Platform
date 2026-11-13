def test_root_redirects_to_dashboard(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/ui/"


def test_dashboard_assets_served_with_security_headers(client):
    page = client.get("/ui/")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert '<script src="app.js"' in page.text
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-content-type-options"] == "nosniff"

    for asset, content_type in (("app.js", "javascript"), ("styles.css", "text/css")):
        response = client.get(f"/ui/{asset}")
        assert response.status_code == 200
        assert content_type in response.headers["content-type"]


def test_api_responses_do_not_get_ui_headers(client):
    assert "content-security-policy" not in client.get("/api/v1/health/live").headers


def test_job_type_schemas_require_auth(client):
    assert client.get("/api/v1/jobs/types/schemas").status_code == 401


def test_job_type_schemas_describe_payloads(client, user):
    schemas = {s["job_type"]: s for s in client.get("/api/v1/jobs/types/schemas", headers=user["headers"]).json()}
    report = schemas["generate_report"]
    assert report["timeout_seconds"] == 120
    props = report["payload_schema"]["properties"]
    assert props["format"]["enum"] == ["csv", "json"]
    assert report["payload_schema"]["required"] == ["report_id"]
    operations = schemas["process_file"]["payload_schema"]["properties"]["operations"]
    assert operations["type"] == "array" and "checksum" in operations["items"]["enum"]
    # Regression: the form pre-ticks defaults, so they must be present in the schema.
    assert operations["default"] == ["line_count", "word_count", "checksum"]
