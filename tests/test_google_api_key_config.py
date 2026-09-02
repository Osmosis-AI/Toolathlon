import runpy
import unittest
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).parents[1]


class GoogleApiKeyConfigTests(unittest.TestCase):
    def test_mcp_servers_use_distinct_service_keys(self):
        maps = yaml.safe_load(
            (REPO_ROOT / "configs/mcp_servers/google_map.yaml").read_text()
        )
        youtube = yaml.safe_load(
            (REPO_ROOT / "configs/mcp_servers/youtube.yaml").read_text()
        )

        self.assertEqual(
            maps["params"]["env"]["GOOGLE_MAPS_API_KEY"],
            "${token.google_maps_api_key}",
        )
        self.assertEqual(
            youtube["params"]["env"]["YOUTUBE_API_KEY"],
            "${token.youtube_api_key}",
        )

    def test_example_token_config_declares_both_keys(self):
        tokens = runpy.run_path(
            REPO_ROOT / "configs/token_key_session_example.py"
        )["all_token_key_session"]

        self.assertEqual(tokens.google_maps_api_key, "XX")
        self.assertEqual(tokens.youtube_api_key, "XX")

    def test_standalone_setup_populates_all_google_api_keys(self):
        script = (
            REPO_ROOT / "global_preparation/automated_google_setup.sh"
        ).read_text()
        update_script = script.split("# Update Google-related fields", 1)[1].split(
            "# Write back", 1
        )[0]

        for key in (
            "google_cloud_console_api_key",
            "google_maps_api_key",
            "youtube_api_key",
        ):
            self.assertIn(f"'{key}'", update_script)
        self.assertIn('f\'{api_key_field} = "${API_KEY}"\'', update_script)

    def test_eval_server_redacts_distinct_google_api_keys(self):
        source = (REPO_ROOT / "eval_server.py").read_text()
        sensitive_keys = source.split("sensitive_keys = [", 1)[1].split("]", 1)[0]

        self.assertIn("'google_maps_api_key'", sensitive_keys)
        self.assertIn("'youtube_api_key'", sensitive_keys)


if __name__ == "__main__":
    unittest.main()
