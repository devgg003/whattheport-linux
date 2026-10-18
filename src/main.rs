mod cli;
mod daemon;
mod notify;
mod tray;
mod view;

const USAGE: &str = "\
wtp — dev servers on this machine

Usage:
  wtp                  List dev servers
  wtp --json           Print the full state as JSON (for scripts and status bars)
  wtp open <port>      Open http://localhost:<port>
  wtp stop <port>      Stop a server and its process tree
  wtp restart <port>   Restart a server with its original command
  wtp daemon [--tray]  Run the background service (top-bar menu, alerts)
  wtp quit             Stop the background service
";

#[tokio::main(flavor = "current_thread")]
async fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let args: Vec<&str> = args.iter().map(String::as_str).collect();
    let port = |s: Option<&&str>| s.and_then(|p| p.trim_start_matches(':').parse::<u16>().ok());

    let result = match args.as_slice() {
        [] | ["ls" | "list"] => cli::list(false).await,
        ["--json" | "-j"] | ["ls" | "list", "--json"] => cli::list(true).await,
        [cmd @ ("open" | "stop" | "restart"), rest @ ..] => match port(rest.first()) {
            Some(p) => cli::act(cmd, p).await,
            None => Err(format!("usage: wtp {cmd} <port>").into()),
        },
        ["daemon"] => daemon::run(false).await,
        ["daemon", "--tray"] => daemon::run(true).await,
        ["quit"] => cli::quit().await,
        ["--version" | "-V"] => {
            println!("wtp {}", env!("CARGO_PKG_VERSION"));
            Ok(())
        }
        ["help" | "--help" | "-h"] => {
            print!("{USAGE}");
            Ok(())
        }
        _ => {
            eprint!("{USAGE}");
            std::process::exit(2);
        }
    };
    if let Err(e) = result {
        eprintln!("wtp: {e}");
        std::process::exit(1);
    }
}
