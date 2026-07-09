import unittest
from unittest.mock import Mock, patch

from paper_agent.youcom_search import _format_youcom_search_results, youcom_search


class TestYoucomSearch(unittest.TestCase):
    def test_formatting_uses_web_and_news_results(self):
        payload = {
            "results": {
                "web": [
                    {
                        "title": "PaperAgent README",
                        "url": "https://example.com/readme",
                        "description": "Readme description",
                        "snippets": ["Snippet text"],
                    }
                ],
                "news": [
                    {
                        "title": "PaperAgent release",
                        "url": "https://example.com/news",
                        "description": "News description",
                    }
                ],
            }
        }

        rendered = _format_youcom_search_results(payload, count=5)

        self.assertIn("Web results:", rendered)
        self.assertIn("PaperAgent README", rendered)
        self.assertIn("https://example.com/readme", rendered)
        self.assertIn("Snippet text", rendered)
        self.assertIn("News results:", rendered)
        self.assertIn("PaperAgent release", rendered)

    def test_disabled_when_key_missing(self):
        self.assertEqual(
            youcom_search("paper agent", api_key=None),
            "You.com search is disabled until YDC_API_KEY is configured.",
        )

    @patch("paper_agent.youcom_search.requests.get")
    def test_search_requests_youcom_endpoint(self, mock_get):
        response = Mock()
        response.json.return_value = {"results": {"web": [] , "news": []}}
        response.raise_for_status.return_value = None
        mock_get.return_value = response

        youcom_search("paper agent", count=3, api_key="test-key")

        mock_get.assert_called_once()
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"]["query"], "paper agent")
        self.assertEqual(kwargs["params"]["count"], 3)
        self.assertEqual(kwargs["headers"]["X-API-Key"], "test-key")


if __name__ == "__main__":
    unittest.main()
