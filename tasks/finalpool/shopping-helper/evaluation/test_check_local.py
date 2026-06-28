import json
import unittest

from check_local import PRODUCT_TITLE_EXTRACTOR, find_js_content_from_result


class ProductTitleExtractionTests(unittest.TestCase):
    def test_product_title_uses_text_content(self):
        self.assertIn("getElementById('productTitle')", PRODUCT_TITLE_EXTRACTOR)
        self.assertIn('textContent', PRODUCT_TITLE_EXTRACTOR)
        self.assertNotIn('el.value', PRODUCT_TITLE_EXTRACTOR)

    def test_playwright_result_parser_preserves_title(self):
        title = 'TYBOATLE 88" W Black Faux Leather Sofa'
        result = (
            '### Result\n'
            f'{json.dumps(title)}\n\n'
            '### Ran Playwright code\n'
            '```js\nawait page.evaluate(...)\n```'
        )

        self.assertEqual(
            find_js_content_from_result(result),
            title,
        )


if __name__ == '__main__':
    unittest.main()
