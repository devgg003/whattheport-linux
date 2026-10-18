use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};

use serde::Serialize;

#[derive(Debug, Clone, Default, Serialize)]
pub struct ProjectInfo {
    pub name: String,
    pub root: Option<String>,
    pub framework: Option<String>,
    pub branch: Option<String>,
    pub is_worktree: bool,
    pub worktree_name: Option<String>,
    pub has_vercel: bool,
}

struct Cached {
    info: ProjectInfo,
    head: Option<PathBuf>,
    at: f64,
}

#[derive(Default)]
pub struct ProjectResolver {
    cache: HashMap<String, Cached>,
}

const CACHE_SECS: f64 = 15.0;

impl ProjectResolver {
    pub fn resolve(&mut self, cwd: Option<&str>, command: Option<&str>) -> ProjectInfo {
        let Some(cwd) = cwd else {
            return ProjectInfo { name: "Unknown".into(), ..Default::default() };
        };
        let now = crate::now();
        if let Some(c) = self.cache.get(cwd).filter(|c| now - c.at < CACHE_SECS) {
            let mut info = c.info.clone();
            if let Some(head) = &c.head {
                info.branch = branch_from_head(head);
            }
            return info;
        }

        let home = crate::home_dir();
        let cwd_path = Path::new(cwd);
        let mut info = ProjectInfo {
            name: base_name(cwd_path).unwrap_or_else(|| "Unknown".into()),
            ..Default::default()
        };
        let mut head = None;
        let mut found_manifest = false;
        let mut dir = Some(cwd_path);

        while let Some(d) = dir {
            if d == Path::new("/") || d == home {
                break;
            }
            if !found_manifest {
                let (name, framework) = read_manifest(d, command);
                if name.is_some() || framework.is_some() {
                    info.name = name.or_else(|| base_name(d)).unwrap_or_default();
                    info.framework = framework;
                    info.root = Some(d.to_string_lossy().into_owned());
                    info.has_vercel = d.join(".vercel/project.json").exists();
                    found_manifest = true;
                }
            }
            if head.is_none()
                && let Some((h, is_worktree)) = git_head(d)
            {
                head = Some(h);
                info.is_worktree = is_worktree;
                if is_worktree {
                    info.worktree_name = base_name(d);
                }
                info.root.get_or_insert_with(|| d.to_string_lossy().into_owned());
            }
            if found_manifest && head.is_some() {
                break;
            }
            dir = d.parent();
        }

        info.branch = head.as_deref().and_then(branch_from_head);
        self.cache.insert(cwd.to_string(), Cached { info: info.clone(), head, at: now });
        info
    }
}

fn base_name(p: &Path) -> Option<String> {
    p.file_name().map(|n| n.to_string_lossy().into_owned())
}

fn read_manifest(dir: &Path, command: Option<&str>) -> (Option<String>, Option<String>) {
    if let Ok(text) = fs::read_to_string(dir.join("package.json"))
        && let Ok(json) = serde_json::from_str::<serde_json::Value>(&text)
    {
        let name = json.get("name").and_then(|v| v.as_str()).map(String::from);
        let mut deps: HashMap<String, String> = HashMap::new();
        for key in ["dependencies", "devDependencies"] {
            if let Some(obj) = json.get(key).and_then(|v| v.as_object()) {
                for (k, v) in obj {
                    deps.insert(k.clone(), v.as_str().unwrap_or("").to_string());
                }
            }
        }
        return (name, js_framework(&deps, command));
    }
    if let Ok(text) = fs::read_to_string(dir.join("pyproject.toml")) {
        let name = toml_name(&text, "project").or_else(|| toml_name(&text, "tool.poetry"));
        let haystack = format!("{}{}", command.unwrap_or(""), text).to_lowercase();
        let fw = [("django", "Django"), ("fastapi", "FastAPI"), ("flask", "Flask"), ("uvicorn", "Uvicorn"), ("gunicorn", "Gunicorn")]
            .iter()
            .find(|(k, _)| haystack.contains(k))
            .map_or("Python", |(_, l)| l);
        return (name, Some(fw.into()));
    }
    if let Ok(text) = fs::read_to_string(dir.join("Cargo.toml")) {
        return (toml_name(&text, "package"), Some("Rust".into()));
    }
    if let Ok(text) = fs::read_to_string(dir.join("go.mod")) {
        let name = text
            .lines()
            .find_map(|l| l.strip_prefix("module "))
            .and_then(|m| m.trim().rsplit('/').next())
            .map(String::from);
        return (name, Some("Go".into()));
    }
    if dir.join("Gemfile").is_file() {
        let rails = dir.join("config/application.rb").is_file();
        return (None, Some(if rails { "Rails" } else { "Ruby" }.into()));
    }
    (None, None)
}

fn js_framework(deps: &HashMap<String, String>, command: Option<&str>) -> Option<String> {
    if command.unwrap_or("").to_lowercase().contains("storybook") {
        let v = deps
            .get("storybook")
            .or_else(|| deps.iter().find(|(k, _)| k.starts_with("@storybook/")).map(|(_, v)| v));
        return Some(v.map_or("Storybook".into(), |v| versioned("Storybook", v)));
    }
    const CANDIDATES: &[(&str, &str)] = &[
        ("next", "Next.js"), ("nuxt", "Nuxt"), ("@remix-run/dev", "Remix"), ("astro", "Astro"),
        ("@sveltejs/kit", "SvelteKit"), ("expo", "Expo"), ("@angular/core", "Angular"),
        ("vite", "Vite"), ("react-scripts", "Create React App"), ("hono", "Hono"), ("express", "Express"),
    ];
    CANDIDATES
        .iter()
        .find_map(|(k, label)| deps.get(*k).map(|v| versioned(label, v)))
}

fn versioned(name: &str, version: &str) -> String {
    let major: String = version
        .split('.')
        .next()
        .unwrap_or("")
        .chars()
        .filter(char::is_ascii_digit)
        .collect();
    if major.is_empty() { name.into() } else { format!("{name} {major}") }
}

fn toml_name(text: &str, section: &str) -> Option<String> {
    let header = format!("[{section}]");
    let mut inside = false;
    for line in text.lines().map(str::trim) {
        if line.starts_with('[') {
            inside = line == header;
            continue;
        }
        if inside
            && let Some(rest) = line.strip_prefix("name")
            && let Some(value) = rest.trim_start().strip_prefix('=')
        {
            let v = value.trim().trim_matches(|c| c == '"' || c == '\'');
            if !v.is_empty() {
                return Some(v.into());
            }
        }
    }
    None
}

fn git_head(dir: &Path) -> Option<(PathBuf, bool)> {
    let git = dir.join(".git");
    if git.is_dir() {
        let head = git.join("HEAD");
        return head.is_file().then_some((head, false));
    }
    let content = fs::read_to_string(&git).ok()?;
    let gitdir = content.lines().find_map(|l| l.strip_prefix("gitdir:"))?.trim();
    let gitdir = dir.join(gitdir);
    let head = gitdir.join("HEAD");
    head.is_file()
        .then(|| (head, gitdir.to_string_lossy().contains("/worktrees/")))
}

fn branch_from_head(head: &Path) -> Option<String> {
    let content = fs::read_to_string(head).ok()?;
    let content = content.trim();
    if let Some(b) = content.strip_prefix("ref: refs/heads/") {
        return Some(b.into());
    }
    (!content.is_empty()).then(|| content.chars().take(7).collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn versions_and_toml() {
        assert_eq!(versioned("Next.js", "^15.2.0"), "Next.js 15");
        assert_eq!(versioned("Vite", "latest"), "Vite");
        let toml = "[tool.other]\nname = \"x\"\n[project]\nname = \"billing-api\"\n";
        assert_eq!(toml_name(toml, "project").as_deref(), Some("billing-api"));
    }

    #[test]
    fn resolves_package_json_and_branch() {
        let tmp = std::env::temp_dir().join(format!("wtp-proj-{}", std::process::id()));
        fs::create_dir_all(tmp.join(".git")).unwrap();
        fs::create_dir_all(tmp.join("src")).unwrap();
        fs::write(tmp.join(".git/HEAD"), "ref: refs/heads/feat/auth\n").unwrap();
        fs::write(tmp.join("package.json"), r#"{"name":"web","dependencies":{"next":"15.1.0"}}"#).unwrap();
        let info = ProjectResolver::default().resolve(tmp.join("src").to_str(), None);
        fs::remove_dir_all(&tmp).ok();
        assert_eq!(info.name, "web");
        assert_eq!(info.framework.as_deref(), Some("Next.js 15"));
        assert_eq!(info.branch.as_deref(), Some("feat/auth"));
    }
}
