"""Process control (stop, SIGTERM, SIGKILL, restart) for Linux."""
import os
import shlex
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional
from whattheport.engine.procs import ProcArgs, ProcessInspector, ProcSnapshot
from whattheport.engine.server import Server

class ProcessControl:
    @staticmethod
    def stop(server: Server, force_quit_seconds: float = 3.0, completion: Optional[Callable[[], None]] = None):
        targets = server.process_starts
        now_processes = ProcessInspector.all_processes()

        # Phase 1: send SIGTERM to all processes in the tree that are still the same process
        for pid, started_at in targets.items():
            if ProcessControl._is_same_process(pid, started_at, now_processes):
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except Exception:
                    pass

        # Phase 2: after timeout, SIGKILL any survivors
        def _cleanup_survivors():
            time.sleep(force_quit_seconds)
            later_processes = ProcessInspector.all_processes()
            for pid, started_at in targets.items():
                if ProcessControl._is_same_process(pid, started_at, later_processes):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except Exception:
                        pass
            if completion:
                completion()

        t = threading.Thread(target=_cleanup_survivors, daemon=True)
        t.start()

    @staticmethod
    def restart(server: Server, completion: Optional[Callable[[bool], None]] = None):
        if not server.launch or not server.launch_directory:
            if completion:
                completion(False)
            return

        cmd = ProcessControl.shell_command(server.launch)
        if not cmd:
            if completion:
                completion(False)
            return

        cwd = server.launch_directory
        if not os.path.isdir(cwd):
            if completion:
                completion(False)
            return

        def _do_restart():
            # Stop existing
            ProcessControl.stop(server, force_quit_seconds=2.0)
            # Wait for port to become free (up to 5s)
            for _ in range(20):
                time.sleep(0.25)
                if ProcessControl._is_port_free(server.port):
                    break

            success = ProcessControl._spawn(
                command=cmd,
                environment=server.launch.environment,
                cwd=cwd,
                port=server.port
            )
            if completion:
                completion(success)

        t = threading.Thread(target=_do_restart, daemon=True)
        t.start()

    @staticmethod
    def shell_command(launch: ProcArgs) -> Optional[str]:
        args = [a.strip() for a in launch.arguments if a.strip()]
        if not args:
            return None

        first = args[0]
        shells = {"sh", "bash", "zsh", "dash"}
        exe_base = os.path.basename(first)

        if exe_base in shells and len(args) >= 3 and args[1] == "-c":
            return args[2]

        if len(args) == 1 and " " in first:
            paren_idx = first.find(" (")
            if paren_idx != -1:
                return first[:paren_idx]
            return first

        executable = first if first.startswith("/") else launch.executable_path
        parts = [executable] + args[1:]
        return " ".join(shlex.quote(p) for p in parts)

    @staticmethod
    def _is_same_process(pid: int, started_at: float, current_procs: Dict[int, ProcSnapshot]) -> bool:
        if pid <= 1 or pid == os.getpid():
            return False
        proc = current_procs.get(pid)
        if not proc:
            return False
        return abs(proc.start_time - started_at) < 1.0

    @staticmethod
    def _is_port_free(port: int) -> bool:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.2)
            res = s.connect_ex(("127.0.0.1", port))
            s.close()
            return res != 0  # 0 means connection succeeded -> port is still occupied
        except Exception:
            return True

    @staticmethod
    def _spawn(command: str, environment: Dict[str, str], cwd: str, port: int) -> bool:
        try:
            logs_dir = Path.home() / ".local" / "state" / "whattheport"
            logs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            log_file = logs_dir / f"port-{port}.log"

            env = dict(os.environ)
            env.update(environment)

            with open(log_file, "a", encoding="utf-8") as out:
                subprocess.Popen(
                    ["/bin/bash", "-c", command],
                    cwd=cwd,
                    env=env,
                    stdout=out,
                    stderr=out,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True
                )
            return True
        except Exception:
            return False
