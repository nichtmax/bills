import unittest

from bills.addons import REGISTRY
from bills.addons.zai import ZaiAddon
from bills.config import Config, DEFAULT_CRON
from bills.web import _known_addons


class ZaiAddonTests(unittest.TestCase):
    def test_zai_addon_is_registered(self) -> None:
        self.assertIn("zai", REGISTRY)
        self.assertEqual(REGISTRY["zai"].provider, "Z.ai")

    def test_zai_has_default_schedule(self) -> None:
        self.assertIn("zai", DEFAULT_CRON)

    def test_api_key_headers_are_built(self) -> None:
        addon = ZaiAddon.__new__(ZaiAddon)
        headers = addon._api_headers("test-key")
        self.assertEqual(headers["Authorization"], "Bearer test-key")
        self.assertEqual(headers["X-API-Key"], "test-key")

    def test_zai_is_in_default_enabled_addons(self) -> None:
        self.assertIn("zai", Config().enabled_addons())

    def test_zai_is_in_known_addons_for_dashboard(self) -> None:
        self.assertIn("zai", _known_addons(Config()))


if __name__ == "__main__":
    unittest.main()
