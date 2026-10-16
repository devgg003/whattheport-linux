"""Agent session resolver for linking servers to Claude Code and Codex sessions."""
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

@dataclass
class AgentSession:
    kind: str  # "Claude Code" or "Codex"
    id: str
    title: Optional[str] = None
    transcript_path: Optional[str] = None
    started_at: Optional[float] = None
    directory: Optional[str] = None

    @property
    def short_id(self) -> str:
        return self.id[:8]

    @property
    def resume_command(self) -> str:
        if self.kind == "Claude Code":
            return "claude --resume"
        return "codex resume"

class AgentSessionResolver:
    def __init__(self):
        self.home = Path.home()
        self.claude_cache: Dict[str, tuple] = {}
        self.codex_index: Dict[str, AgentSession] = {}
        self.codex_indexed_at: float = 0

    def resolve(self, environment: Dict[str, str], cwd: Optional[str], link_claude: bool = True, link_codex: bool = True) -> Optional[AgentSession]:
        if link_claude:
            claude_id = environment.get("CLAUDE_CODE_SESSION_ID")
            if claude_id:
                return self._claude_session(claude_id)

        if not link_codex or not cwd or environment.get("CLAUDE_CODE_SESSION_ID"):
            return None

        self._refresh_codex_index_if_needed()
        curr_dir = os.path.abspath(cwd)
        home_str = str(self.home)
        while len(curr_dir) > len(home_str):
            if curr_dir in self.codex_index:
                return self.codex_index[curr_dir]
            parent = os.path.dirname(curr_dir)
            if parent == curr_dir:
                break
            curr_dir = parent

        return None

    def _claude_session(self, session_id: str) -> AgentSession:
        now = time.time()
        if session_id in self.claude_cache:
            cached_session, checked_at = self.claude_cache[session_id]
            if now - checked_at < 30:
                return cached_session

        transcript = self._find_claude_transcript(session_id)
        title: Optional[str] = None
        directory: Optional[str] = None
        started_at: Optional[float] = None

        if transcript and os.path.isfile(transcript):
            started_at = os.path.getmtime(transcript)
            head = self._read_head(transcript, 256 * 1024)
            tail = self._read_tail(transcript, 512 * 1024)
            title = self._last_title(tail) or self._last_title(head) or self._first_prompt(head)
            directory = self._first_value("cwd", head)

        session = AgentSession(
            kind="Claude Code",
            id=session_id,
            title=title,
            transcript_path=transcript,
            started_at=started_at,
            directory=directory
        )
        self.claude_cache[session_id] = (session, now)
        return session

    def _find_claude_transcript(self, session_id: str) -> Optional[str]:
        projects_dir = self.home / ".claude" / "projects"
        if not projects_dir.is_dir():
            return None
        try:
            for folder in projects_dir.iterdir():
                if folder.is_dir():
                    cand = folder / f"{session_id}.jsonl"
                    if cand.is_file():
                        return str(cand)
        except Exception:
            pass
        return None

    def _last_title(self, text: str) -> Optional[str]:
        title = None
        for line in text.splitlines():
            if '"type":"custom-title"' in line or '"type":"ai-title"' in line:
                try:
                    obj = json.loads(line)
                    custom = obj.get("customTitle")
                    if custom:
                        return custom
                    ai = obj.get("aiTitle")
                    if ai:
                        title = ai
                except Exception:
                    continue
        return title

    def _first_prompt(self, text: str) -> Optional[str]:
        for line in text.splitlines():
            if '"type":"user"' in line:
                try:
                    obj = json.loads(line)
                    msg = obj.get("message", {})
                    content = msg.get("content", "").strip()
                    if content and not content.startswith("<"):
                        first_line = content.splitlines()[0]
                        return first_line[:60]
                except Exception:
                    continue
        return None

    def _first_value(self, key: str, text: str) -> Optional[str]:
        for line in text.splitlines():
            if f'"{key}"' in line:
                try:
                    obj = json.loads(line)
                    val = obj.get(key)
                    if isinstance(val, str):
                        return val
                except Exception:
                    continue
        return None

    def _refresh_codex_index_if_needed(self):
        now = time.time()
        if now - self.codex_indexed_at <= 60:
            return
        self.codex_indexed_at = now

        codex_dir = self.home / ".codex"
        titles: Dict[str, str] = {}
        index_file = codex_dir / "session_index.jsonl"
        if index_file.is_file():
            text = self._read_tail(str(index_file), 1024 * 1024)
            for line in text.splitlines():
                try:
                    obj = json.loads(line)
                    if "id" in obj and "thread_name" in obj:
                        titles[obj["id"]] = obj["thread_name"]
                except Exception:
                    continue

        sessions_dir = codex_dir / "sessions"
        if not sessions_dir.is_dir():
            return

        cutoff = now - (3 * 86400)
        new_index: Dict[str, tuple] = {}
        try:
            for root, _, files in os.walk(sessions_dir):
                for f in files:
                    if f.endswith(".jsonl"):
                        full_path = os.path.join(root, f)
                        mtime = os.path.getmtime(full_path)
                        if mtime < cutoff:
                            continue
                        head = self._read_head(full_path, 64 * 1024)
                        lines = head.splitlines()
                        if not lines:
                            continue
                        try:
                            obj = json.loads(lines[0])
                            payload = obj.get("payload", {})
                            sess_id = payload.get("id")
                            cwd = payload.get("cwd")
                            if sess_id and cwd:
                                if cwd in new_index and new_index[cwd][1] > mtime:
                                    continue
                                sess = AgentSession(
                                    kind="Codex",
                                    id=sess_id,
                                    title=titles.get(sess_id),
                                    transcript_path=full_path,
                                    started_at=mtime,
                                    directory=cwd
                                )
                                new_index[cwd] = (sess, mtime)
                        except Exception:
                            continue
        except Exception:
            pass

        self.codex_index = {k: v[0] for k, v in new_index.items()}

    def _read_head(self, path: str, max_bytes: int) -> str:
        try:
            with open(path, "rb") as f:
                return f.read(max_bytes).decode("utf-8", errors="replace")
        except Exception:
            return ""

    def _read_tail(self, path: str, max_bytes: int) -> str:
        try:
            size = os.path.getsize(path)
            offset = max(0, size - max_bytes)
            with open(path, "rb") as f:
                f.seek(offset)
                data = f.read().decode("utf-8", errors="replace")
            if offset > 0 and "\n" in data:
                data = data[data.index("\n") + 1:]
            return data
        except Exception:
            return ""
