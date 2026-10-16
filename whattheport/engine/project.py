"""Project resolver for resolving project manifests, git branches, and worktrees."""
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

@dataclass
class ProjectInfo:
    name: str
    root: Optional[str] = None
    framework: Optional[str] = None
    branch: Optional[str] = None
    is_worktree: bool = False
    worktree_name: Optional[str] = None
    has_vercel: bool = False

@dataclass
class _CachedProject:
    info: ProjectInfo
    git_head_path: Optional[str]
    resolved_at: float

class ProjectResolver:
    def __init__(self):
        self.cache: Dict[str, _CachedProject] = {}
        self.home = str(Path.home())

    def resolve(self, cwd: Optional[str], command: Optional[str]) -> ProjectInfo:
        if not cwd:
            return ProjectInfo(name="Unknown")

        now = time.time()
        if cwd in self.cache and (now - self.cache[cwd].resolved_at) < 15:
            cached = self.cache[cwd]
            info = ProjectInfo(**cached.info.__dict__)
            if cached.git_head_path:
                info.branch = self._branch_from_head(cached.git_head_path)
            return info

        folder_name = os.path.basename(os.path.normpath(cwd)) or "Unknown"
        info = ProjectInfo(name=folder_name)
        git_head_path: Optional[str] = None
        found_manifest = False
        directory = os.path.abspath(cwd)

        while directory != "/" and directory != self.home:
            if not found_manifest:
                manifest_name, manifest_fw = self._read_manifest(directory, command)
                if manifest_name or manifest_fw:
                    info.name = manifest_name or os.path.basename(directory)
                    info.framework = manifest_fw
                    info.root = directory
                    info.has_vercel = os.path.exists(os.path.join(directory, ".vercel", "project.json"))
                    found_manifest = True

            if git_head_path is None:
                git_res = self._git_head(directory)
                if git_res:
                    git_head_path, is_worktree = git_res
                    info.is_worktree = is_worktree
                    if is_worktree:
                        info.worktree_name = os.path.basename(directory)
                    if info.root is None:
                        info.root = directory

            if found_manifest and git_head_path is not None:
                break

            parent = os.path.dirname(directory)
            if parent == directory:
                break
            directory = parent

        if git_head_path:
            info.branch = self._branch_from_head(git_head_path)

        self.cache[cwd] = _CachedProject(
            info=ProjectInfo(**info.__dict__),
            git_head_path=git_head_path,
            resolved_at=now
        )
        return info

    def _read_manifest(self, directory: str, command: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        pkg_json = os.path.join(directory, "package.json")
        if os.path.isfile(pkg_json):
            try:
                with open(pkg_json, "r", encoding="utf-8") as f:
                    data = json.load(f)
                name = data.get("name")
                deps: Dict[str, str] = {}
                deps.update(data.get("dependencies", {}))
                deps.update(data.get("devDependencies", {}))
                fw = self._js_framework(deps, command)
                return name, fw
            except Exception:
                pass

        pyproject = os.path.join(directory, "pyproject.toml")
        if os.path.isfile(pyproject):
            try:
                with open(pyproject, "r", encoding="utf-8") as f:
                    text = f.read()
                name = self._toml_name(text, "project") or self._toml_name(text, "tool.poetry")
                fw = self._python_framework(command, text)
                return name, fw
            except Exception:
                pass

        cargo = os.path.join(directory, "Cargo.toml")
        if os.path.isfile(cargo):
            try:
                with open(cargo, "r", encoding="utf-8") as f:
                    text = f.read()
                name = self._toml_name(text, "package")
                return name, "Rust"
            except Exception:
                pass

        gomod = os.path.join(directory, "go.mod")
        if os.path.isfile(gomod):
            try:
                with open(gomod, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("module "):
                            mod = line.split()[1]
                            name = mod.split("/")[-1]
                            return name, "Go"
            except Exception:
                pass

        gemfile = os.path.join(directory, "Gemfile")
        if os.path.isfile(gemfile):
            is_rails = os.path.isfile(os.path.join(directory, "config", "application.rb"))
            return None, "Rails" if is_rails else "Ruby"

        return None, None

    def _js_framework(self, deps: Dict[str, str], command: Optional[str]) -> Optional[str]:
        cmd = (command or "").lower()
        if "storybook" in cmd:
            v = deps.get("storybook") or next((v for k, v in deps.items() if k.startswith("@storybook/")), None)
            return self._versioned("Storybook", v) if v else "Storybook"

        candidates = [
            ("next", "Next.js"),
            ("nuxt", "Nuxt"),
            ("@remix-run/dev", "Remix"),
            ("astro", "Astro"),
            ("@sveltejs/kit", "SvelteKit"),
            ("expo", "Expo"),
            ("@angular/core", "Angular"),
            ("vite", "Vite"),
            ("react-scripts", "Create React App"),
            ("hono", "Hono"),
            ("express", "Express"),
        ]
        for key, label in candidates:
            if key in deps:
                return self._versioned(label, deps[key])
        return None

    def _python_framework(self, command: Optional[str], text: str) -> str:
        haystack = ((command or "") + text).lower()
        for match, label in [("django", "Django"), ("fastapi", "FastAPI"), ("flask", "Flask"), ("uvicorn", "Uvicorn"), ("gunicorn", "Gunicorn")]:
            if match in haystack:
                return label
        return "Python"

    def _versioned(self, name: str, version: str) -> str:
        # Extract major version
        digits = "".join(c for c in version.split(".")[0] if c.isdigit())
        return f"{name} {digits}" if digits else name

    def _toml_name(self, text: str, section: str) -> Optional[str]:
        in_section = False
        target_section = f"[{section}]"
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("["):
                in_section = (line == target_section)
                continue
            if in_section and line.startswith("name"):
                m = re.search(r'name\s*=\s*["\']([^"\']+)["\']', line)
                if m:
                    return m.group(1)
        return None

    def _git_head(self, directory: str) -> Optional[Tuple[str, bool]]:
        git_path = os.path.join(directory, ".git")
        if os.path.isdir(git_path):
            head_path = os.path.join(git_path, "HEAD")
            if os.path.isfile(head_path):
                return head_path, False
        elif os.path.isfile(git_path):
            try:
                with open(git_path, "r", encoding="utf-8") as f:
                    content = f.read()
                for line in content.splitlines():
                    if line.startswith("gitdir:"):
                        git_dir = line[len("gitdir:"):].strip()
                        if not os.path.isabs(git_dir):
                            git_dir = os.path.join(directory, git_dir)
                        head_path = os.path.join(git_dir, "HEAD")
                        if os.path.isfile(head_path):
                            return head_path, ("/worktrees/" in git_dir)
            except Exception:
                pass
        return None

    def _branch_from_head(self, head_path: str) -> Optional[str]:
        try:
            with open(head_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            prefix = "ref: refs/heads/"
            if content.startswith(prefix):
                return content[len(prefix):]
            if content:
                return content[:7]
        except Exception:
            pass
        return None
