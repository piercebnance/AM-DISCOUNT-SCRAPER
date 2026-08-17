import unittest

from main import parse_amazon_discounted_products


class AmazonParserTests(unittest.TestCase):
    def test_prefers_results_with_explicit_savings_over_list_price_only(self) -> None:
        html = """
        <html><body>
            <div data-asin="111" data-component-type="s-search-result">
                <h2><a href="/dp/111"><span>Save 36% Widget</span></a></h2>
                <span class="a-price"><span class="a-offscreen">$39.99</span></span>
                <span class="a-text-price"><span class="a-offscreen">$63.99</span></span>
                <span class="a-color-price">List Price: $63.99</span>
                <span class="a-color-price">Save 36%</span>
            </div>
            <div data-asin="222" data-component-type="s-search-result">
                <h2><a href="/dp/222"><span>List Price Only Widget</span></a></h2>
                <span class="a-price"><span class="a-offscreen">$50.00</span></span>
                <span class="a-text-price"><span class="a-offscreen">$60.00</span></span>
                <span class="a-color-price">List Price: $60.00</span>
            </div>
        </body></html>
        """

        results = parse_amazon_discounted_products(html, "widget", max_items=10)

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["title"], "Save 36% Widget")
        self.assertEqual(results[1]["title"], "List Price Only Widget")
        self.assertTrue(results[0]["discounted"])
        self.assertFalse(results[1]["discounted"])
        self.assertIn("save 36%", results[0]["content"].lower())
        self.assertNotIn("search term", results[0]["content"].lower())
        self.assertNotIn("search term", results[1]["content"].lower())
        self.assertEqual(results[0]["discount_price"], "$39.99")
        self.assertEqual(results[0]["original_price"], "$63.99")

    def test_extracts_product_image_url(self) -> None:
        html = """
        <html><body>
            <div data-asin="444" data-component-type="s-search-result">
                <h2><a href="/dp/444"><span>Widget With Image</span></a></h2>
                <span class="a-price"><span class="a-offscreen">$19.99</span></span>
                <span class="a-text-price"><span class="a-offscreen">$29.99</span></span>
                <span class="a-color-price">Save 20%</span>
                <img src="https://m.media-amazon.com/images/I/71abc.jpg" alt="Widget product" />
            </div>
        </body></html>
        """

        results = parse_amazon_discounted_products(html, "widget", max_items=10)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["image_url"], "https://m.media-amazon.com/images/I/71abc.jpg")

    def test_ignores_non_discount_percentage_text(self) -> None:
        html = """
        <html><body>
            <div data-asin="333" data-component-type="s-search-result">
                <h2><a href="/dp/333"><span>Recycled T-Shirt</span></a></h2>
                <span class="a-price"><span class="a-offscreen">$24.99</span></span>
                <span class="a-text-price"><span class="a-offscreen">$29.99</span></span>
                <span class="a-size-base.a-color-secondary">Contains at least 50% recycled material</span>
            </div>
        </body></html>
        """

        results = parse_amazon_discounted_products(html, "t-shirt", max_items=10)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Recycled T-Shirt")
        self.assertFalse(results[0]["discounted"])
        self.assertNotIn("recycled material", results[0]["content"].lower())


if __name__ == "__main__":
    unittest.main()
