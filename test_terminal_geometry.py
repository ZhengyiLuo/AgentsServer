import unittest

from agent_server import terminal_dimensions


class TerminalGeometryTests(unittest.TestCase):
    def test_compact_xterm_geometry_is_not_silently_enlarged(self) -> None:
        self.assertEqual(terminal_dimensions(20, 5), (20, 5))
        self.assertEqual(terminal_dimensions(2, 1), (2, 1))

    def test_default_and_maximum_geometry_remain_bounded(self) -> None:
        self.assertEqual(terminal_dimensions(), (120, 36))
        self.assertEqual(terminal_dimensions(900, 400), (500, 200))


if __name__ == "__main__":
    unittest.main()
