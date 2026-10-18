const MB: f64 = 1024.0 * 1024.0;

/// Per-port accent colours (dark theme variants).
pub const PORT_COLORS: &[&str] = &["#6EC7ED", "#B599F0", "#EB9CD4", "#7D9CF2", "#7DDBE0", "#D9A3F2", "#ABC2E0"];
pub const AMBER: &str = "#FFB224";

pub fn port_color(index: usize) -> &'static str {
    PORT_COLORS[index % PORT_COLORS.len()]
}

/// "640 MB" / "2.18 GB"
pub fn bytes(v: u64) -> String {
    let mb = v as f64 / MB;
    if mb >= 1024.0 { format!("{:.2} GB", mb / 1024.0) } else { format!("{mb:.0} MB") }
}

/// Coarser variant for totals: "3.4 GB", "7 GB", "512 MB".
pub fn total(v: u64) -> String {
    let mb = v as f64 / MB;
    if mb >= 1024.0 {
        let s = format!("{:.1}", mb / 1024.0);
        format!("{} GB", s.strip_suffix(".0").unwrap_or(&s))
    } else {
        format!("{mb:.0} MB")
    }
}

pub fn duration(secs: f64) -> String {
    let minutes = (secs / 60.0) as u64;
    match minutes {
        0 => "<1m".into(),
        1..60 => format!("{minutes}m"),
        _ if minutes < 24 * 60 => {
            let (h, m) = (minutes / 60, minutes % 60);
            if m == 0 { format!("{h}h") } else { format!("{h}h {m}m") }
        }
        _ => format!("{}d", minutes / 60 / 24),
    }
}

pub fn percent(v: f64) -> String {
    if v <= 0.0 {
        "0%".into()
    } else if v < 0.1 {
        "<0.1%".into()
    } else if v < 10.0 {
        format!("{v:.1}%")
    } else {
        format!("{v:.0}%")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn formats() {
        assert_eq!(bytes(640 * 1024 * 1024), "640 MB");
        assert_eq!(bytes(2_340_000_000), "2.18 GB");
        assert_eq!(total(7 * 1024 * 1024 * 1024), "7 GB");
        assert_eq!(duration(30.0), "<1m");
        assert_eq!(duration(42.0 * 60.0), "42m");
        assert_eq!(duration(135.0 * 60.0), "2h 15m");
        assert_eq!(duration(3.0 * 86400.0), "3d");
        assert_eq!(percent(6.24), "6.2%");
        assert_eq!(percent(0.05), "<0.1%");
    }
}
