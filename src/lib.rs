pub mod agents;
pub mod alert;
pub mod control;
pub mod format;
pub mod github;
pub mod model;
pub mod prefs;
pub mod procfs;
pub mod project;
pub mod rules;
pub mod scan;
pub mod sockets;
pub mod tree;

pub fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

pub fn home_dir() -> std::path::PathBuf {
    std::env::var_os("HOME")
        .map(Into::into)
        .unwrap_or_else(|| "/".into())
}
