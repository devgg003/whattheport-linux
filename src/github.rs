//! Optional PR and Vercel preview lookups through the `gh` CLI.

use std::process::{Command, Stdio};

use serde::Serialize;
use serde_json::Value;

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct PullRequest {
    pub number: u64,
    pub title: String,
    pub url: String,
    pub state: String,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Preview {
    pub url: String,
    /// "ready", "building" or "failed"
    pub state: &'static str,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct GitHubInfo {
    pub pull_request: Option<PullRequest>,
    pub preview: Option<Preview>,
}

fn gh(root: &str, args: &[&str]) -> Option<String> {
    let out = Command::new("gh")
        .args(args)
        .current_dir(root)
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .ok()?;
    (out.status.success() && !out.stdout.is_empty()).then(|| String::from_utf8_lossy(&out.stdout).into_owned())
}

pub fn lookup(root: &str, branch: &str, prs: bool, previews: bool) -> GitHubInfo {
    GitHubInfo {
        pull_request: prs.then(|| pull_request(root, branch)).flatten(),
        preview: previews.then(|| preview(root, branch)).flatten(),
    }
}

fn pull_request(root: &str, branch: &str) -> Option<PullRequest> {
    let out = gh(root, &["pr", "list", "--head", branch, "--state", "all", "--limit", "1", "--json", "number,title,url,state"])?;
    let items: Vec<Value> = serde_json::from_str(&out).ok()?;
    let first = items.first()?;
    let state = first["state"].as_str().unwrap_or("").to_lowercase();
    let mut chars = state.chars();
    Some(PullRequest {
        number: first["number"].as_u64()?,
        title: first["title"].as_str().unwrap_or("").into(),
        url: first["url"].as_str()?.into(),
        state: chars.next().map(|c| c.to_uppercase().chain(chars).collect()).unwrap_or_default(),
    })
}

fn preview(root: &str, branch: &str) -> Option<Preview> {
    let commits = gh(root, &["api", &format!("repos/{{owner}}/{{repo}}/commits?sha={branch}&per_page=20"), "--jq", ".[].sha"])?;
    let commits: Vec<&str> = commits.lines().map(str::trim).filter(|s| !s.is_empty()).collect();
    let deployments: Vec<Value> = serde_json::from_str(&gh(root, &["api", "repos/{owner}/{repo}/deployments?per_page=30"])?).ok()?;
    let best = deployments
        .iter()
        .filter(|d| d["environment"].as_str().unwrap_or("").to_lowercase().contains("preview"))
        .filter_map(|d| Some((commits.iter().position(|c| Some(*c) == d["sha"].as_str())?, d)))
        .min_by_key(|(i, _)| *i)?
        .1;
    let id = best["id"].as_u64()?;
    let statuses: Vec<Value> =
        serde_json::from_str(&gh(root, &["api", &format!("repos/{{owner}}/{{repo}}/deployments/{id}/statuses?per_page=1")])?).ok()?;
    let status = statuses.first()?;
    let url = status["environment_url"].as_str().or(status["target_url"].as_str())?;
    let state = match status["state"].as_str().unwrap_or("").to_lowercase().as_str() {
        "success" => "ready",
        "failure" | "error" => "failed",
        _ => "building",
    };
    Some(Preview { url: url.into(), state })
}
