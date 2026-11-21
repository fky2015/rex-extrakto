use anyhow::{bail, Result};
use std::path::Path;

fn run() -> Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args == ["--help"] || args == ["-h"] {
        println!("rex-popup: search candidates with fzf; Tab copies, Enter inserts\nUsage: rex-popup STATE.json");
        return Ok(());
    }
    if args == ["--version"] {
        println!("rex-popup {}", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    if args.len() != 1 {
        bail!("expected one state file; use --help for the format");
    }
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?;
    runtime.block_on(rex_extrakto::popup::run(Path::new(&args[0])))
}

fn main() {
    if let Err(error) = run() {
        eprintln!("rex-popup: {error:#}");
        std::process::exit(1);
    }
}
