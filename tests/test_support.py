"""Test support itself: the adapter's suite may skip without Hermes only when nothing requires it."""

from __future__ import annotations

import importlib
import os
import unittest
from types import ModuleType
from unittest import mock

import support


class RequireHermesTest(unittest.TestCase):
    """setup.sh sets the variable so a run under the wrong interpreter fails instead of skipping."""

    def setUp(self):
        self.addCleanup(lambda: importlib.reload(importlib.import_module("test_adapter")))

    @staticmethod
    def adapter_tests_without_hermes(required: bool) -> ModuleType:
        """test_adapter, re-imported as if `gateway` would not import."""
        with mock.patch.dict(os.environ, {support.REQUIRE_HERMES_VARIABLE: "1" if required else ""}), \
                mock.patch.object(support, "hermes_is_importable", return_value=False):
            return importlib.reload(importlib.import_module("test_adapter"))

    def test_without_hermes_and_not_required_the_adapter_suite_skips(self):
        module = self.adapter_tests_without_hermes(required=False)
        self.assertTrue(getattr(module.AdapterTest, "__unittest_skip__", False))

    def test_required_the_adapter_suite_cannot_skip(self):
        module = self.adapter_tests_without_hermes(required=True)
        self.assertFalse(getattr(module.AdapterTest, "__unittest_skip__", False))


if __name__ == "__main__":
    unittest.main()
