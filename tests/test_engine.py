import unittest
from whattheport.engine.prefs import Preferences
from whattheport.engine.monitor import ServerMonitor


class TestScannerSmoke(unittest.TestCase):
    def test_live_scan(self):
        prefs = Preferences()
        monitor = ServerMonitor(prefs)
        monitor.scan()
        self.assertIsInstance(monitor.servers, list)


if __name__ == "__main__":
    unittest.main()
