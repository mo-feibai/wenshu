"""Theme mode contracts: system preference by default, explicit choice persisted."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LAYOUT = REPO / "custom/src/layouts/Base.astro"
CSS = REPO / "custom/src/styles/global.css"


class ThemeModeContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = LAYOUT.read_text(encoding="utf-8")
        cls.styles = CSS.read_text(encoding="utf-8")

    def test_matches_system_preference_before_first_paint(self):
        # SSR 保留 data-theme="dark" 作为无 JS 回退；head 内联脚本在首屏前覆盖为系统/用户选择
        self.assertIn("data-theme=\"dark\"", self.source.split("<head>")[0])
        self.assertLess(self.source.index("<head>"), self.source.index("<body>"))
        head = self.source.split("<head>")[1].split("</head>")[0]
        index = head.find("<script")
        self.assertGreaterEqual(index, 0)
        self.assertNotIn("ws-theme", head[:index])
        script = head[index:]
        self.assertIn("matchMedia('(prefers-color-scheme: dark)')", script)
        self.assertIn("localStorage.getItem('ws-theme')", script)
        self.assertIn("root.dataset.theme =", script)
        self.assertIn("dataset.themeMode", script)

    def test_follow_system_listens_to_live_changes(self):
        self.assertIn("systemThemeQuery.addEventListener('change'", self.source)
        self.assertIn("themeMode === 'auto'", self.source)
        self.assertIn("root.dataset.theme = mode === 'auto'", self.source)

    def test_three_state_toggle_and_persistence(self):
        self.assertIn("themeMode === 'auto' ? 'light' : themeMode === 'light' ? 'dark' : 'auto'", self.source)
        self.assertIn("localStorage.setItem('ws-theme', themeMode)", self.source)
        self.assertIn("THEME_LABELS", self.source)
        self.assertIn("THEME_ICONS", self.source)

    def test_both_color_schemes_remain_available(self):
        self.assertIn("[data-theme='dark']", self.styles)
        self.assertIn("[data-theme='light']", self.styles)
        self.assertIn("color-scheme: dark", self.styles)
        self.assertIn("color-scheme: light", self.styles)


if __name__ == "__main__":
    unittest.main()
