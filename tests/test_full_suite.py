import unittest
from whattheport.engine.format import bytes_str, duration, percent
from whattheport.engine.prefs import Preferences
from whattheport.engine.scanner_engine import ScanEngine
from whattheport.engine.monitor import ServerMonitor

class TestEngine(unittest.TestCase):
    def test_formatting(self):
        self.assertEqual(bytes_str(1048576), "1 MB")
        self.assertEqual(bytes_str(1073741824), "1.00 GB")
        self.assertEqual(duration(30), "<1m")
        self.assertEqual(duration(120), "2m")
        self.assertEqual(duration(7200), "2h")
        self.assertEqual(duration(90000), "1d")
        self.assertEqual(percent(0.0), "0%")
        self.assertEqual(percent(0.05), "<0.1%")
        self.assertEqual(percent(12.4), "12%")

    def test_pretty_command(self):
        cmd = ScanEngine.pretty_command(["node", "/usr/local/bin/vite", "dev"], "node")
        self.assertEqual(cmd, "vite dev")
        cmd2 = ScanEngine.pretty_command(["python3", "-m", "http.server", "8000"], "python3")
        self.assertEqual(cmd2, "python3 -m http.server 8000")

    def test_scan_and_monitor(self):
        prefs = Preferences()
        monitor = ServerMonitor(prefs)
        monitor.scan()
        self.assertIsInstance(monitor.servers, list)
        tot_mem = monitor.total_server_memory
        self.assertGreaterEqual(tot_mem, 0)

if __name__ == "__main__":
    unittest.main()
