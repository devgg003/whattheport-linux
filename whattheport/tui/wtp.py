"""Interactive terminal UI (wtp) and CLI for WhatThePort."""
import curses
import json
import subprocess
import sys
import time
from whattheport.engine.control import ProcessControl
from whattheport.engine.format import bytes_str, duration, percent, total_str
from whattheport.engine.monitor import ServerMonitor
from whattheport.engine.prefs import Preferences
from whattheport.engine.server import Server, ServerStatus

def run_cli_json():
    prefs = Preferences()
    monitor = ServerMonitor(prefs)
    monitor.scan()
    data = []
    thresh = prefs.threshold_bytes
    for s in monitor.servers:
        data.append({
            "port": s.port,
            "name": s.display_name,
            "pid": s.pid,
            "root_pid": s.root_pid,
            "command": s.command,
            "cwd": s.cwd,
            "cwd_exists": s.cwd_exists,
            "branch": s.project.branch if s.project else None,
            "agent": s.agent.title if s.agent else None,
            "uptime_seconds": s.uptime,
            "memory_bytes": s.memory,
            "cpu_percent": s.cpu,
            "attention": s.status(thresh) == ServerStatus.ATTENTION,
            "leaking": s.is_leaking(),
            "url": s.url
        })
    print(json.dumps(data, indent=2))

class TerminalApp:
    def __init__(self):
        self.prefs = Preferences()
        self.monitor = ServerMonitor(self.prefs)
        self.selected_idx = 0
        self.cleanup_mode = False
        self.cleanup_selected = set()
        self.show_detail = False

    def run(self):
        self.monitor.scan()
        curses.wrapper(self._main)

    def _main(self, stdscr):
        curses.curs_set(0)
        stdscr.nodelay(True)
        curses.use_default_colors()

        # Setup colors if available
        if curses.has_colors():
            curses.init_pair(1, curses.COLOR_CYAN, -1)     # Port
            curses.init_pair(2, curses.COLOR_YELLOW, -1)   # Attention / Amber
            curses.init_pair(3, curses.COLOR_GREEN, -1)    # Normal / Agent
            curses.init_pair(4, curses.COLOR_BLACK, curses.COLOR_WHITE) # Selected row

        last_scan = time.time()

        while True:
            now = time.time()
            if now - last_scan > 2.0:
                self.monitor.scan()
                last_scan = now

            self._draw(stdscr)

            try:
                ch = stdscr.getch()
            except Exception:
                ch = -1

            if ch != -1:
                if ch in (ord('q'), ord('Q'), 27):  # ESC or q
                    if self.show_detail:
                        self.show_detail = False
                    elif self.cleanup_mode:
                        self.cleanup_mode = False
                    else:
                        break
                elif ch in (curses.KEY_UP, ord('k')):
                    if self.selected_idx > 0:
                        self.selected_idx -= 1
                elif ch in (curses.KEY_DOWN, ord('j')):
                    if self.selected_idx < len(self.monitor.servers) - 1:
                        self.selected_idx += 1
                elif ch in (10, 13, curses.KEY_ENTER):  # Enter
                    if not self.cleanup_mode and self.monitor.servers:
                        self.show_detail = not self.show_detail
                elif ch == ord(' '):  # Space
                    if self.cleanup_mode and self.monitor.servers:
                        p = self.monitor.servers[self.selected_idx].port
                        if p in self.cleanup_selected:
                            self.cleanup_selected.remove(p)
                        else:
                            self.cleanup_selected.add(p)
                elif ch in (ord('c'), ord('C')):
                    self.cleanup_mode = not self.cleanup_mode
                    if self.cleanup_mode:
                        cands = self.monitor.cleanup_candidates
                        self.cleanup_selected = {c[0].port for c in cands if not c[1].is_leak}
                    else:
                        self.cleanup_selected.clear()
                elif ch in (ord('x'), ord('X')):  # Stop
                    if self.cleanup_mode and self.cleanup_selected:
                        for s in self.monitor.servers:
                            if s.port in self.cleanup_selected:
                                ProcessControl.stop(s)
                        self.cleanup_mode = False
                        self.cleanup_selected.clear()
                        time.sleep(0.5)
                        self.monitor.scan()
                    elif self.monitor.servers:
                        ProcessControl.stop(self.monitor.servers[self.selected_idx])
                        time.sleep(0.5)
                        self.monitor.scan()
                elif ch in (ord('o'), ord('O')):  # Open in browser
                    if self.monitor.servers:
                        s = self.monitor.servers[self.selected_idx]
                        subprocess.run(["xdg-open", s.url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            time.sleep(0.05)

    def _draw(self, stdscr):
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        if h < 10 or w < 40:
            stdscr.addstr(0, 0, "Terminal window too small.")
            stdscr.refresh()
            return

        if self.monitor.servers:
            self.selected_idx = min(self.selected_idx, len(self.monitor.servers) - 1)
        else:
            self.selected_idx = 0
            self.show_detail = False

        # Header
        tot_mem = self.monitor.total_server_memory
        tot_cpu = self.monitor.total_server_cpu
        sys_tot, sys_used, sys_avail = self.monitor.system_memory

        hdr_line = f" WhatThePort  |  Servers: {total_str(tot_mem)}  CPU: {percent(tot_cpu)}  Free: {bytes_str(sys_avail)}"
        stdscr.addstr(0, 0, hdr_line[:w-1], curses.A_BOLD)
        stdscr.addstr(1, 0, "─" * (w - 1))

        if self.show_detail and self.monitor.servers:
            self._draw_detail(stdscr, h, w)
        else:
            self._draw_list(stdscr, h, w)

        # Footer
        footer_y = h - 1
        stdscr.addstr(footer_y - 1, 0, "─" * (w - 1))
        if self.cleanup_mode:
            f_text = f" [Space] Toggle  [x] Stop selected ({len(self.cleanup_selected)})  [c/q] Cancel"
        else:
            cand_cnt = self.monitor.suggested_cleanup_count
            c_label = f"c: Clean up ({cand_cnt})" if cand_cnt > 0 else "c: Clean up"
            f_text = f" [Enter] Details  [o] Open browser  [x] Stop  [{c_label}]  [q] Quit"
        stdscr.addstr(footer_y, 0, f_text[:w-1], curses.A_DIM)

        stdscr.refresh()

    def _draw_list(self, stdscr, h, w):
        servers = self.monitor.servers
        if not servers:
            stdscr.addstr(3, 2, "No dev servers running.", curses.A_DIM)
            return

        thresh = self.prefs.threshold_bytes
        start_y = 2
        max_rows = h - 4

        for idx, s in enumerate(servers[:max_rows]):
            y = start_y + idx
            is_sel = (idx == self.selected_idx)
            is_attn = s.status(thresh) == ServerStatus.ATTENTION

            prefix = " "
            if self.cleanup_mode:
                prefix = "[x] " if s.port in self.cleanup_selected else "[ ] "
            elif is_sel:
                prefix = "▶ "

            port_s = f":{s.port:<5} "
            name_s = f"{s.display_name:<20} "
            sub_s = ""
            if s.agent:
                sub_s = f"✴ {s.agent.title or s.agent.kind} "
            elif s.project and s.project.branch:
                sub_s = f"{s.project.branch} "
            sub_s += f"up {duration(s.uptime)} "

            mem_s = f"{bytes_str(s.memory):>8}"

            line = f"{prefix}{port_s}{name_s}{sub_s}"
            pad = max(0, w - len(line) - len(mem_s) - 2)
            full_line = f"{line}{' ' * pad}{mem_s}"

            attr = curses.A_NORMAL
            if is_sel:
                attr = curses.color_pair(4) if curses.has_colors() else curses.A_REVERSE
            elif is_attn:
                attr = curses.color_pair(2) | curses.A_BOLD

            stdscr.addstr(y, 0, full_line[:w-1], attr)

    def _draw_detail(self, stdscr, h, w):
        s = self.monitor.servers[self.selected_idx]

        stdscr.addstr(3, 2, f"Port:     :{s.port}", curses.A_BOLD)
        stdscr.addstr(4, 2, f"Project:  {s.display_name}")
        stdscr.addstr(5, 2, f"Uptime:   {duration(s.uptime)}")
        stdscr.addstr(6, 2, f"Memory:   {bytes_str(s.memory)} (Leaking: {'YES' if s.is_leaking() else 'No'})")
        stdscr.addstr(7, 2, f"CPU:      {percent(s.cpu)}")
        if s.agent:
            stdscr.addstr(8, 2, f"Session:  ✴ {s.agent.title or s.agent.id} ({s.agent.kind})")
        if s.project and s.project.branch:
            stdscr.addstr(9, 2, f"Branch:   {s.project.branch}")
        if s.command:
            stdscr.addstr(10, 2, f"Command:  {s.command[:w-12]}")
        if s.cwd:
            stdscr.addstr(11, 2, f"Path:     {s.cwd[:w-12]}")

        stdscr.addstr(13, 2, f"Processes ({len(s.processes)}):", curses.A_BOLD)
        for i, p in enumerate(s.processes[:h-16]):
            stdscr.addstr(14 + i, 4, f"{p.name:<25} {bytes_str(p.memory):>8}  {percent(p.cpu)}")

def main():
    if "--json" in sys.argv:
        run_cli_json()
    else:
        app = TerminalApp()
        app.run()

if __name__ == "__main__":
    main()
