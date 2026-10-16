"""Server data models and status representations matching WhatThePort."""
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from whattheport.engine.agents import AgentSession
from whattheport.engine.project import ProjectInfo
from whattheport.engine.procs import ProcArgs

@dataclass
class Sample:
    time: float
    memory: int  # bytes
    cpu: float  # percent

@dataclass
class ServerProcess:
    pid: int
    name: str
    depth: int
    memory: int
    cpu: float

class ServerStatus:
    NORMAL = "normal"
    ATTENTION = "attention"

@dataclass
class Server:
    port: int
    pid: int
    root_pid: int
    process_name: str
    addresses: List[str]
    cwd: Optional[str]
    cwd_exists: bool
    command: Optional[str]
    launch: Optional[ProcArgs]
    launch_directory: Optional[str]
    started_at: float
    project: ProjectInfo
    agent: Optional[AgentSession]
    processes: List[ServerProcess]
    process_starts: Dict[int, float]
    memory: int
    cpu: float
    connections: int
    history: List[Sample] = field(default_factory=list)
    last_active: float = field(default_factory=time.time)
    is_protected: bool = False

    @property
    def url(self) -> str:
        return f"http://localhost:{self.port}"

    @property
    def display_name(self) -> str:
        if self.project and self.project.name and self.project.name != "Unknown":
            return self.project.name
        return self.command or self.process_name

    @property
    def uptime(self) -> float:
        return max(0.0, time.time() - self.started_at)

    @property
    def idle_for(self) -> float:
        return max(0.0, time.time() - self.last_active)

    @property
    def memory_growth(self) -> int:
        if not self.history or len(self.history) < 2:
            return 0
        first_mem = self.history[0].memory
        return max(0, self.memory - first_mem)

    def is_leaking(self) -> bool:
        # > 500MB growth over history window (up to 10 min)
        return self.memory_growth > 500 * 1024 * 1024

    def status(self, alert_threshold: int) -> str:
        if self.memory >= alert_threshold or self.is_leaking():
            return ServerStatus.ATTENTION
        return ServerStatus.NORMAL

@dataclass
class CleanUpReason:
    kind: str  # "worktreeDeleted", "idle", "longRunning", "leaking"
    detail: Optional[str] = None

    @property
    def is_leak(self) -> bool:
        return self.kind == "leaking"

    @property
    def label(self) -> str:
        if self.kind == "worktreeDeleted":
            return "Worktree deleted"
        elif self.kind == "idle":
            return f"Idle {self.detail}"
        elif self.kind == "longRunning":
            return f"Running {self.detail}"
        elif self.kind == "leaking":
            return f"+{self.detail} in 10m"
        return ""
