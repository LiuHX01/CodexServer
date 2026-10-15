import json
import re
from html.parser import HTMLParser

import httpx


class Assets(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.urls = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and "src" in attrs:
            self.urls.append(attrs["src"])
        if tag == "link" and attrs.get("rel") in {"icon", "stylesheet"}:
            self.urls.append(attrs["href"])


def page_config(text):
    return json.loads(
        re.search(
            r'<script id="docs-config" type="application/json">(.*?)</script>',
            text,
            re.S,
        ).group(1)
    )


async def test_docs_resources_are_served_locally_without_auth(service):
    client, _, _ = service
    response = await client.get("/docs", headers={"Authorization": ""})
    assert response.status_code == 200
    resources = Assets(response.text).urls
    assert len(resources) == 5
    for url in resources:
        assert url.startswith("/docs-assets/")
        resource = await client.get(url, headers={"Authorization": ""})
        assert resource.status_code == 200, url
        assert resource.content
        if ".js?" in url:
            assert "javascript" in resource.headers["content-type"]
            if "swagger-ui-bundle" in url:
                assert "SwaggerUIBundle" in resource.text
        elif ".css?" in url:
            assert "text/css" in resource.headers["content-type"]
        else:
            assert resource.headers["content-type"].startswith("image/svg+xml")
    config = page_config(response.text)
    assert config["schemaUrl"] == "/openapi.json"
    assert config["model"] == "gpt-6.1-sol"
    assert config["effort"] == "medium"
    assert "API 工作台" in response.text
    assert "endpoint-search" in response.text
    assert (await client.get("/docs-assets/config.py")).status_code == 404


async def test_disabling_docs_also_disables_browser_assets(service):
    client, _, manager = service
    manager.settings.enable_docs = False
    for path in (
        "/docs",
        "/openapi.json",
        "/docs-assets/swagger-ui-bundle.js",
        "/docs-assets/console.css",
    ):
        assert (await client.get(path)).status_code == 404


async def test_docs_urls_include_reverse_proxy_root_path(service):
    client, _, _ = service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client._transport.app, root_path="/codex"),
        base_url="http://test",
    ) as prefixed:
        response = await prefixed.get("/codex/docs")
        assert response.status_code == 200
        config = page_config(response.text)
        assert config["schemaUrl"] == "/codex/openapi.json"
        assert config["healthUrl"] == "/codex/healthz"
        resources = Assets(response.text).urls
        for url in resources:
            assert url.startswith("/codex/docs-assets/")
            assert (await prefixed.get(url)).status_code == 200
        assert (await prefixed.get("/codex/openapi.json")).status_code == 200


async def test_docs_config_does_not_allow_script_injection(service):
    client, _, manager = service
    manager.settings.codex_model = '</script><script>alert("injected")</script>'
    page = (await client.get("/docs")).text
    assert "</script><script>alert" not in page
    assert page_config(page)["model"] == manager.settings.codex_model
