import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from main import app


class SecurityTests(unittest.TestCase):
    def test_missing_scrapingbee_key_returns_configuration_error(self) -> None:
        for path in ("/search?q=widget", "/api/search?q=widget"):
            with self.subTest(path=path), patch("main.SCRAPINGBEE_API_KEY", ""):
                response = TestClient(app).get(path)

                self.assertEqual(response.status_code, 503)
                self.assertIn("Set SCRAPINGBEE_API_KEY", response.text)

    def test_search_escapes_scraped_content(self) -> None:
        scraped_html = """
        <div data-asin="111" data-component-type="s-search-result">
            <h2><a href="/dp/111"><span>Widget</span></a></h2>
            <span class="a-price"><span class="a-offscreen">$39.99</span></span>
            <span class="a-color-price">&lt;img src=x onerror=alert(1)&gt; Save 36%</span>
        </div>
        """

        with (
            patch("main.SCRAPINGBEE_API_KEY", "test-key"),
            patch("main.fetch_amazon_search_page", new_callable=AsyncMock) as fetch_page,
        ):
            fetch_page.return_value = scraped_html
            response = TestClient(app).get("/search?q=widget")

        self.assertEqual(response.status_code, 200)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt; Save 36%", response.text)
        self.assertNotIn("<img src=x onerror=alert(1)>", response.text)

    def test_search_errors_do_not_expose_upstream_request_url(self) -> None:
        secret = "test-api-key"
        request = httpx.Request(
            "GET",
            f"https://app.scrapingbee.com/api/v1?api_key={secret}",
        )
        response = httpx.Response(403, request=request)
        upstream_error = httpx.HTTPStatusError(
            "upstream request failed",
            request=request,
            response=response,
        )

        with (
            patch("main.SCRAPINGBEE_API_KEY", "test-key"),
            patch("main.fetch_amazon_search_page", new_callable=AsyncMock) as fetch_page,
        ):
            fetch_page.side_effect = upstream_error
            client_response = TestClient(app).get("/search?q=widget")

        self.assertEqual(client_response.status_code, 502)
        self.assertNotIn(secret, client_response.text)
        self.assertIn("Search is temporarily unavailable", client_response.text)
