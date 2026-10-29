"""Handles: the canonical form, the allow-list file, and Zack's phone number on it."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from support import ZACK_EMAIL, ZACK_PHONE, plugin_module, write_allow_list

handles = plugin_module("handles")


class CanonicalHandleTest(unittest.TestCase):
    def test_phone_numbers_in_any_written_form_reach_one_form(self):
        for written in ("+1 (555) 010-0199", "(555) 010-0199", "555.010.0199", "15550100199", "+15550100199"):
            with self.subTest(written=written):
                self.assertEqual(handles.canonical_handle(written), ZACK_PHONE)

    def test_a_number_outside_the_us_keeps_its_country_code(self):
        self.assertEqual(handles.canonical_handle("+44 20 7946 0958"), "+442079460958")

    def test_an_email_address_is_lower_cased(self):
        self.assertEqual(handles.canonical_handle("Zack@Example.com"), ZACK_EMAIL)

    def test_anything_else_is_refused_rather_than_guessed(self):
        for written in ("Zack Kuptsow", "Zack", "tel:+15550100199", "", "12345", "+0123456789",
                        "-zack@example.com", "zack@", "@example.com"):
            with self.subTest(written=written), self.assertRaises(handles.InvalidHandleError):
                handles.canonical_handle(written)


class ReadAllowedHandlesTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())

    def test_reads_canonical_handles_skipping_comments_blanks_and_duplicates(self):
        path = write_allow_list(self.directory, "# Zack", "", "(555) 010-0199", ZACK_EMAIL.upper(), ZACK_PHONE)
        self.assertEqual(handles.read_allowed_handles(path), (ZACK_PHONE, ZACK_EMAIL))

    def test_a_missing_file_is_an_error(self):
        with self.assertRaises(handles.AllowListError):
            handles.read_allowed_handles(self.directory / "absent.txt")

    def test_a_file_without_a_handle_is_an_error(self):
        with self.assertRaises(handles.AllowListError):
            handles.read_allowed_handles(write_allow_list(self.directory, "# nobody yet", ""))

    def test_a_bad_line_is_named_by_number_and_never_quoted(self):
        path = write_allow_list(self.directory, ZACK_PHONE, "Partner Name")
        with self.assertRaises(handles.AllowListError) as raised:
            handles.read_allowed_handles(path)
        self.assertIn("line 2", str(raised.exception))
        self.assertNotIn("Partner", str(raised.exception))


class FirstPhoneNumberTest(unittest.TestCase):
    def test_the_phone_number_wins_over_an_email_listed_first(self):
        self.assertEqual(handles.first_phone_number((ZACK_EMAIL, ZACK_PHONE)), ZACK_PHONE)

    def test_no_phone_number_is_an_error(self):
        with self.assertRaises(handles.AllowListError):
            handles.first_phone_number((ZACK_EMAIL,))


class TextingProfileHomeTest(unittest.TestCase):
    """The scripts read the texting profile's files whatever HERMES_HOME says (ADR 0034)."""

    user_home = Path("/Users/someone")
    root = Path("/Users/someone/.hermes")
    texting_home = root / "profiles/imessage"

    def test_unset_is_the_default_roots_texting_profile(self):
        self.assertEqual(handles.texting_profile_home(None, self.user_home), self.texting_home)

    def test_empty_reads_as_unset(self):
        self.assertEqual(handles.texting_profile_home("", self.user_home), self.texting_home)

    def test_the_root_gives_its_texting_profile(self):
        texting_home = Path("/srv/hermes/profiles/imessage")
        self.assertEqual(handles.texting_profile_home("/srv/hermes", self.user_home), texting_home)

    def test_another_profiles_home_gives_the_texting_profile_beside_it(self):
        coder_home = str(self.root / "profiles/coder")
        self.assertEqual(handles.texting_profile_home(coder_home, self.user_home), self.texting_home)

    def test_the_texting_profiles_own_home_is_kept(self):
        own_home = str(self.texting_home)
        self.assertEqual(handles.texting_profile_home(own_home, self.user_home), self.texting_home)


if __name__ == "__main__":
    unittest.main()
