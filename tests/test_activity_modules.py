"""Discover tests belonging to optional modules, without retaining deleted ones."""
from pathlib import Path
import unittest


def load_tests(loader, tests, pattern):
    root = Path(__file__).resolve().parents[1]
    suite = unittest.TestSuite()
    for folder in sorted((root/'activities').glob('*/tests')):
        suite.addTests(loader.discover(str(folder), pattern='test_*.py', top_level_dir=str(root)))
    return suite
