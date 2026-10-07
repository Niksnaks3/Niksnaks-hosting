"""The title-bar layout rule that gives the main window its minimize button.

Run with:  python -m unittest
"""

import unittest

from niksnaks_hosting.shared.utils.decoration_layout import with_minimize


class WithMinimizeTests(unittest.TestCase):
    def test_layouts(self):
        cases = {
            "appmenu:close": "appmenu:minimize,close",  # GNOME's default
            "close:maximize": "close,minimize:maximize",  # elementary
            "icon:minimize,maximize,close": "icon:minimize,maximize,close",  # KDE, unchanged
            "menu:minimize,maximize,close": "menu:minimize,maximize,close",  # GTK's default (Windows)
            "appmenu:maximize,close": "appmenu:minimize,maximize,close",
            "menu:close": "menu:minimize,close",
            "close": "close,minimize",
            ":close": ":minimize,close",
            "close:": "close,minimize:",
            "": "",  # no title buttons at all: left alone
        }
        for given, expected in cases.items():
            with self.subTest(layout=given):
                self.assertEqual(with_minimize(given), expected)
