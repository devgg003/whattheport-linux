//! User preferences, stored in ~/.config/whattheport/config.json (same file as
//! the Python version, so existing settings carry over).

use std::collections::{BTreeMap, HashSet};
use std::fs;
use std::path::PathBuf;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::rules;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum CleanupMode {
    Off,
    Ask,
    Automatic,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum TrayMode {
    /// Tray icon everywhere except GNOME, where the extension draws the menu.
    Auto,
    On,
    Off,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Prefs {
    pub min_port: u16,
    pub max_port: u16,
    pub scan_interval: f64,
    pub threshold_gb: f64,
    pub leak_warnings: bool,
    pub snooze_minutes: u64,
    pub cleanup_mode: CleanupMode,
    pub cleanup_notify: bool,
    pub cleanup_deleted_worktree: bool,
    pub cleanup_idle_hours: u64,
    pub cleanup_running_days: u64,
    pub force_quit_seconds: f64,
    pub protected_processes: Vec<String>,
    pub allowlist: Vec<String>,
    pub link_claude: bool,
    pub link_codex: bool,
    pub show_branches: bool,
    pub vercel_previews: bool,
    pub github_pull_requests: bool,
    pub tray: TrayMode,
    /// Keys we don't know about are kept so older/newer versions can share the file.
    #[serde(flatten)]
    pub extra: BTreeMap<String, Value>,
}

impl Default for Prefs {
    fn default() -> Self {
        Self {
            min_port: 3000,
            max_port: 65535,
            scan_interval: 2.0,
            threshold_gb: 2.0,
            leak_warnings: true,
            snooze_minutes: 60,
            cleanup_mode: CleanupMode::Ask,
            cleanup_notify: true,
            cleanup_deleted_worktree: true,
            cleanup_idle_hours: 4,
            cleanup_running_days: 3,
            force_quit_seconds: 3.0,
            protected_processes: Vec::new(),
            allowlist: Vec::new(),
            link_claude: true,
            link_codex: true,
            show_branches: true,
            vercel_previews: false,
            github_pull_requests: false,
            tray: TrayMode::Auto,
            extra: BTreeMap::new(),
        }
    }
}

pub fn config_path() -> PathBuf {
    std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| crate::home_dir().join(".config"))
        .join("whattheport/config.json")
}

impl Prefs {
    pub fn load() -> Self {
        fs::read_to_string(config_path())
            .ok()
            .and_then(|s| serde_json::from_str(&s).ok())
            .unwrap_or_default()
    }

    pub fn save(&self) -> std::io::Result<()> {
        let path = config_path();
        if let Some(dir) = path.parent() {
            fs::create_dir_all(dir)?;
        }
        let tmp = path.with_extension("json.tmp");
        fs::write(&tmp, serde_json::to_string_pretty(self)?)?;
        fs::rename(tmp, path)
    }

    /// Apply a single `key = json value` change, as sent by the menu.
    pub fn set(&mut self, key: &str, value: Value) -> Result<(), String> {
        let mut obj = serde_json::to_value(&*self).map_err(|e| e.to_string())?;
        obj.as_object_mut().ok_or("bad prefs")?.insert(key.into(), value);
        *self = serde_json::from_value(obj).map_err(|e| format!("{key}: {e}"))?;
        Ok(())
    }

    pub fn threshold_bytes(&self) -> u64 {
        (self.threshold_gb.max(0.1) * 1024.0 * 1024.0 * 1024.0) as u64
    }

    pub fn idle_threshold_secs(&self) -> f64 {
        self.cleanup_idle_hours.max(1) as f64 * 3600.0
    }

    pub fn running_threshold_secs(&self) -> f64 {
        self.cleanup_running_days.max(1) as f64 * 86400.0
    }

    pub fn allowlist_set(&self) -> HashSet<String> {
        rules::DEFAULT_ALLOWLIST
            .iter()
            .map(|s| s.to_string())
            .chain(self.allowlist.iter().cloned())
            .collect()
    }

    pub fn protected_set(&self) -> HashSet<String> {
        rules::DEFAULT_PROTECTED
            .iter()
            .map(|s| s.to_string())
            .chain(self.protected_processes.iter().cloned())
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_python_config_and_keeps_unknown_keys() {
        let json = r#"{"threshold_gb": 4, "cleanup_mode": "automatic", "future_key": [1]}"#;
        let p: Prefs = serde_json::from_str(json).unwrap();
        assert_eq!(p.threshold_gb, 4.0);
        assert_eq!(p.cleanup_mode, CleanupMode::Automatic);
        assert_eq!(p.min_port, 3000);
        assert!(serde_json::to_string(&p).unwrap().contains("future_key"));
    }

    #[test]
    fn set_validates_types() {
        let mut p = Prefs::default();
        p.set("leak_warnings", Value::Bool(false)).unwrap();
        assert!(!p.leak_warnings);
        assert!(p.set("threshold_gb", Value::String("big".into())).is_err());
        assert_eq!(p.threshold_gb, 2.0);
    }
}
