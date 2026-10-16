"""Configuration and user preferences for WhatThePort Linux."""
import json
from pathlib import Path
from typing import Set, List, Dict, Any

CONFIG_DIR = Path.home() / ".config" / "whattheport"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_ALLOWLIST: Set[str] = {
    "node", "npm", "npx", "pnpm", "yarn", "bun", "bunx", "deno", "turbo", "nx",
    "python", "python3", "Python", "uv", "poetry", "pipenv", "uvicorn", "gunicorn", "flask", "django",
    "ruby", "bundle", "rails", "puma", "unicorn",
    "php", "php-fpm",
    "java", "gradle", "mvn",
    "go", "air",
    "cargo", "rustc",
    "dotnet",
    "beam.smp", "elixir", "mix",
    "nginx", "httpd", "apache", "caddy",
    "postgres", "mysql", "mariadbd", "redis-server", "mongod",
    "docker-proxy",
}

DEFAULT_PROTECTED: List[str] = ["postgres", "redis-server", "mongod", "mysqld", "mariadbd", "mysql"]

DEFAULT_CONFIG: Dict[str, Any] = {
    "min_port": 3000,
    "max_port": 65535,
    "scan_interval": 2.0,
    "threshold_gb": 2.0,
    "leak_warnings": True,
    "snooze_minutes": 60,
    "cleanup_mode": "ask",  # "off", "ask", "automatic"
    "cleanup_notify": True,
    "cleanup_deleted_worktree": True,
    "cleanup_idle_hours": 4,
    "cleanup_running_days": 3,
    "force_quit_seconds": 3.0,
    "protected_processes": DEFAULT_PROTECTED,
    "allowlist": sorted(list(DEFAULT_ALLOWLIST)),
    "link_claude": True,
    "link_codex": True,
    "show_branches": True,
    "vercel_previews": False,
    "github_pull_requests": False,
}

class Preferences:
    def __init__(self):
        self.data: Dict[str, Any] = dict(DEFAULT_CONFIG)
        self.load()

    def load(self):
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                    self.data.update(saved)
            except Exception:
                pass

    def save(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
        except Exception:
            pass

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any):
        self.data[key] = value
        self.save()

    @property
    def min_port(self) -> int:
        return int(self.data.get("min_port", 3000))

    @property
    def max_port(self) -> int:
        return int(self.data.get("max_port", 65535))

    @property
    def scan_interval(self) -> float:
        return float(self.data.get("scan_interval", 2.0))

    @property
    def threshold_bytes(self) -> int:
        gb = float(self.data.get("threshold_gb", 2.0))
        return int(gb * 1024 * 1024 * 1024)

    @property
    def allowlist(self) -> Set[str]:
        return set(self.data.get("allowlist", DEFAULT_ALLOWLIST))

    @property
    def protected(self) -> Set[str]:
        return set(self.data.get("protected_processes", DEFAULT_PROTECTED))

    @property
    def cleanup_mode(self) -> str:
        return self.data.get("cleanup_mode", "ask")

    @property
    def idle_threshold_seconds(self) -> float:
        hours = max(int(self.data.get("cleanup_idle_hours", 4)), 1)
        return float(hours * 3600)

    @property
    def running_threshold_seconds(self) -> float:
        days = max(int(self.data.get("cleanup_running_days", 3)), 1)
        return float(days * 86400)
