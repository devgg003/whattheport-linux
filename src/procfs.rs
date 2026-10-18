use std::collections::HashMap;
use std::fs;
use std::sync::OnceLock;

#[derive(Debug, Clone)]
pub struct Proc {
    pub pid: i32,
    pub ppid: i32,
    pub comm: String,
    /// Epoch seconds.
    pub start_time: f64,
    /// utime + stime in clock ticks.
    pub cpu_ticks: u64,
}

#[derive(Debug, Clone, Default)]
pub struct Args {
    pub exe: String,
    pub argv: Vec<String>,
    pub env: HashMap<String, String>,
}

pub struct SystemMemory {
    pub total: u64,
    pub available: u64,
}

fn clk_tck() -> f64 {
    static V: OnceLock<f64> = OnceLock::new();
    *V.get_or_init(|| match unsafe { libc::sysconf(libc::_SC_CLK_TCK) } {
        n if n > 0 => n as f64,
        _ => 100.0,
    })
}

fn page_size() -> u64 {
    static V: OnceLock<u64> = OnceLock::new();
    *V.get_or_init(|| match unsafe { libc::sysconf(libc::_SC_PAGESIZE) } {
        n if n > 0 => n as u64,
        _ => 4096,
    })
}

fn boot_time() -> f64 {
    static V: OnceLock<f64> = OnceLock::new();
    *V.get_or_init(|| {
        fs::read_to_string("/proc/stat")
            .ok()
            .and_then(|s| {
                s.lines()
                    .find_map(|l| l.strip_prefix("btime "))
                    .and_then(|v| v.trim().parse().ok())
            })
            .unwrap_or_else(crate::now)
    })
}

pub fn ticks_to_secs(ticks: u64) -> f64 {
    ticks as f64 / clk_tck()
}

fn parse_stat(pid: i32, content: &str) -> Option<Proc> {
    // comm may contain spaces and parentheses, so split on the last ')'.
    let l = content.find('(')?;
    let r = content.rfind(')')?;
    let comm = content.get(l + 1..r)?.to_string();
    let rest: Vec<&str> = content.get(r + 2..)?.split_ascii_whitespace().collect();
    let ppid = rest.get(1)?.parse().ok()?;
    let utime: u64 = rest.get(11)?.parse().ok()?;
    let stime: u64 = rest.get(12)?.parse().ok()?;
    let start_ticks: u64 = rest.get(19)?.parse().ok()?;
    Some(Proc {
        pid,
        ppid,
        comm,
        start_time: boot_time() + ticks_to_secs(start_ticks),
        cpu_ticks: utime + stime,
    })
}

pub fn pids() -> impl Iterator<Item = i32> {
    fs::read_dir("/proc")
        .into_iter()
        .flatten()
        .flatten()
        .filter_map(|e| e.file_name().to_str()?.parse().ok())
}

pub fn all_processes() -> HashMap<i32, Proc> {
    pids()
        .filter_map(|pid| {
            let s = fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
            parse_stat(pid, &s).map(|p| (pid, p))
        })
        .collect()
}

pub fn process(pid: i32) -> Option<Proc> {
    let s = fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    parse_stat(pid, &s)
}

pub fn rss(pid: i32) -> u64 {
    fs::read_to_string(format!("/proc/{pid}/statm"))
        .ok()
        .and_then(|s| s.split_ascii_whitespace().nth(1)?.parse::<u64>().ok())
        .map(|pages| pages * page_size())
        .unwrap_or(0)
}

pub fn cwd(pid: i32) -> Option<String> {
    fs::read_link(format!("/proc/{pid}/cwd"))
        .ok()
        .map(|p| p.to_string_lossy().into_owned())
}

fn split_nul(data: &[u8]) -> impl Iterator<Item = &[u8]> {
    data.split(|b| *b == 0).filter(|s| !s.is_empty())
}

pub fn cmdline(pid: i32) -> Option<Vec<String>> {
    let data = fs::read(format!("/proc/{pid}/cmdline")).ok()?;
    Some(
        split_nul(&data)
            .map(|s| String::from_utf8_lossy(s).into_owned())
            .collect(),
    )
}

pub fn args(pid: i32) -> Option<Args> {
    let argv = cmdline(pid)?;
    let exe = fs::read_link(format!("/proc/{pid}/exe"))
        .ok()
        .map(|p| p.to_string_lossy().into_owned())
        .or_else(|| argv.first().cloned())
        .unwrap_or_default();
    let env = fs::read(format!("/proc/{pid}/environ"))
        .map(|data| {
            split_nul(&data)
                .filter_map(|kv| {
                    let eq = kv.iter().position(|b| *b == b'=')?;
                    Some((
                        String::from_utf8_lossy(&kv[..eq]).into_owned(),
                        String::from_utf8_lossy(&kv[eq + 1..]).into_owned(),
                    ))
                })
                .collect()
        })
        .unwrap_or_default();
    Some(Args { exe, argv, env })
}

pub fn system_memory() -> SystemMemory {
    let mut m = SystemMemory { total: 0, available: 0 };
    if let Ok(s) = fs::read_to_string("/proc/meminfo") {
        for line in s.lines() {
            let kb = || -> u64 {
                line.split_ascii_whitespace()
                    .nth(1)
                    .and_then(|v| v.parse().ok())
                    .unwrap_or(0)
                    * 1024
            };
            if line.starts_with("MemTotal:") {
                m.total = kb();
            } else if line.starts_with("MemAvailable:") {
                m.available = kb();
            }
        }
    }
    m
}

/// Returns (idle, total) jiffies from the aggregate cpu line.
pub fn system_cpu_times() -> Option<(u64, u64)> {
    let s = fs::read_to_string("/proc/stat").ok()?;
    let nums: Vec<u64> = s
        .lines()
        .next()?
        .split_ascii_whitespace()
        .skip(1)
        .filter_map(|v| v.parse().ok())
        .collect();
    let idle = nums.get(3)? + nums.get(4).copied().unwrap_or(0);
    Some((idle, nums.iter().sum()))
}

pub fn cpu_count() -> f64 {
    std::thread::available_parallelism()
        .map(|n| n.get() as f64)
        .unwrap_or(1.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_comm_with_parens() {
        let line = "42 (weird (name)) S 7 42 42 0 -1 4194560 100 0 0 0 30 12 0 0 20 0 1 0 500 1000 10";
        let p = parse_stat(42, line).unwrap();
        assert_eq!(p.comm, "weird (name)");
        assert_eq!(p.ppid, 7);
        assert_eq!(p.cpu_ticks, 42);
    }

    #[test]
    fn reads_self() {
        let me = std::process::id() as i32;
        assert!(all_processes().contains_key(&me));
        assert!(rss(me) > 0);
        assert!(!args(me).unwrap().argv.is_empty());
    }
}
