"""Process inspection and /proc wrappers for Linux."""
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

@dataclass
class ProcSnapshot:
    pid: int
    ppid: int
    comm: str
    start_time: float  # Epoch seconds

@dataclass
class ProcUsage:
    footprint: int  # RSS / memory bytes
    cpu_nanoseconds: int

@dataclass
class ProcArgs:
    executable_path: str
    arguments: List[str]
    environment: Dict[str, str] = field(default_factory=dict)

# Hertz / clock ticks per second and page size on Linux
_CLK_TCK = os.sysconf(os.sysconf_names.get('SC_CLK_TCK', 'SC_CLK_TCK')) or 100
_PAGE_SIZE = os.sysconf(os.sysconf_names.get('SC_PAGE_SIZE', 'SC_PAGE_SIZE')) or 4096
_BOOT_TIME: Optional[float] = None

def get_boot_time() -> float:
    global _BOOT_TIME
    if _BOOT_TIME is not None:
        return _BOOT_TIME
    try:
        with open("/proc/stat", "r") as f:
            for line in f:
                if line.startswith("btime "):
                    _BOOT_TIME = float(line.split()[1])
                    return _BOOT_TIME
    except Exception:
        pass
    _BOOT_TIME = time.time()
    return _BOOT_TIME

class ProcessInspector:
    """Reads process metadata directly from Linux /proc filesystem."""

    @staticmethod
    def all_processes() -> Dict[int, ProcSnapshot]:
        snapshots: Dict[int, ProcSnapshot] = {}
        boot_time = get_boot_time()

        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            stat_path = os.path.join(entry.path, "stat")
            try:
                with open(stat_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()

                # /proc/[pid]/stat format: pid (comm) state ppid ...
                # comm can contain spaces and parentheses, so find the last ')'
                rparen = content.rfind(")")
                if rparen == -1:
                    continue
                lparen = content.find("(")
                comm = content[lparen + 1:rparen]

                rest = content[rparen + 2:].split()
                # rest[0] is state, rest[1] is ppid
                ppid = int(rest[1])
                # starttime is at index 19 of rest (field 22 in stat manpage)
                # Field 22: starttime in clock ticks after system boot
                starttime_ticks = int(rest[19])
                start_time = boot_time + (starttime_ticks / _CLK_TCK)

                snapshots[pid] = ProcSnapshot(
                    pid=pid,
                    ppid=ppid,
                    comm=comm,
                    start_time=start_time
                )
            except Exception:
                continue

        return snapshots

    @staticmethod
    def usage(pid: int) -> Optional[ProcUsage]:
        try:
            # RSS from statm (pages * _PAGE_SIZE) or status (VmRSS)
            footprint = 0
            statm_path = f"/proc/{pid}/statm"
            try:
                with open(statm_path, "r") as f:
                    parts = f.read().split()
                    # parts[1] is resident set size in pages
                    rss_pages = int(parts[1])
                    footprint = rss_pages * _PAGE_SIZE
            except Exception:
                pass

            # CPU ticks from /proc/[pid]/stat
            # utime is field 14 (rest[11]), stime is field 15 (rest[12])
            stat_path = f"/proc/{pid}/stat"
            with open(stat_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            rparen = content.rfind(")")
            rest = content[rparen + 2:].split()
            utime = int(rest[11])
            stime = int(rest[12])
            total_ticks = utime + stime
            # Convert ticks to nanoseconds
            cpu_nanos = int((total_ticks / _CLK_TCK) * 1_000_000_000)

            return ProcUsage(footprint=footprint, cpu_nanoseconds=cpu_nanos)
        except Exception:
            return None

    @staticmethod
    def current_directory(pid: int) -> Optional[str]:
        try:
            return os.readlink(f"/proc/{pid}/cwd")
        except Exception:
            return None

    @staticmethod
    def arguments(pid: int) -> Optional[ProcArgs]:
        try:
            exe_path = ""
            try:
                exe_path = os.readlink(f"/proc/{pid}/exe")
            except Exception:
                pass

            cmdline_path = f"/proc/{pid}/cmdline"
            with open(cmdline_path, "rb") as f:
                data = f.read()
            args = [a.decode("utf-8", errors="replace") for a in data.split(b"\x00") if a]

            environ_path = f"/proc/{pid}/environ"
            env: Dict[str, str] = {}
            try:
                with open(environ_path, "rb") as f:
                    env_data = f.read()
                for item in env_data.split(b"\x00"):
                    if not item:
                        continue
                    eq = item.find(b"=")
                    if eq != -1:
                        k = item[:eq].decode("utf-8", errors="replace")
                        v = item[eq + 1:].decode("utf-8", errors="replace")
                        env[k] = v
            except Exception:
                pass

            if not exe_path and args:
                exe_path = args[0]

            return ProcArgs(executable_path=exe_path, arguments=args, environment=env)
        except Exception:
            return None

    @staticmethod
    def system_memory() -> Tuple[int, int, int]:
        """Returns (total_bytes, used_bytes, available_bytes)."""
        try:
            total = 0
            avail = 0
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        total = int(line.split()[1]) * 1024
                    elif line.startswith("MemAvailable:"):
                        avail = int(line.split()[1]) * 1024
            used = max(0, total - avail)
            return (total, used, avail)
        except Exception:
            return (0, 0, 0)
