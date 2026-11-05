"""Entry point: blender -b --factory-startup --python run_tests.py"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
suite = unittest.defaultTestLoader.loadTestsFromNames(["test_xr_pointer", "test_ui_quad"])
result = unittest.TextTestRunner(verbosity=2).run(suite)
import os

sys.stdout.flush()
# Skip Blender's shutdown path: it asserts on unrelated teardown and would mask the real result.
os._exit(0 if result.wasSuccessful() else 1)
