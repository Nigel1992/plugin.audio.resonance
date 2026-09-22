"""Exercise the service range parser without requiring Kodi's interpreter."""
import ast
import pathlib
import re
import unittest

source = pathlib.Path(__file__).parents[1] / "service.py"
tree = ast.parse(source.read_text())
function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "byte_range")
namespace = {"re": re}
exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
byte_range = namespace["byte_range"]


class RangeTests(unittest.TestCase):
    def test_full_open_ended_suffix_and_clamped_ranges(self):
        for header, expected in ((None, (0, 99)), ("bytes=10-", (10, 99)), ("bytes=-10", (90, 99)), ("bytes=10-999", (10, 99)), ("bytes=0-0", (0, 0))):
            with self.subTest(header=header):
                self.assertEqual(byte_range(header, 100), expected)

    def test_invalid_and_unsatisfiable_ranges(self):
        for header in ("bytes=100-", "bytes=9-2", "bytes=-0", "bytes=-", "bytes=1-2,4-5", "items=0-1"):
            with self.subTest(header=header), self.assertRaises(ValueError):
                byte_range(header, 100)
