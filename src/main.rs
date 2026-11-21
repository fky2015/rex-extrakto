use rex_extrakto::extract::{Extractor, Options};
use std::io::{self, BufWriter, Read, Write};

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let mut options = Options::default();
    let mut format = "json".to_string();
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args == ["--help"] || args == ["-h"] {
        println!(
            "rex-extract: extract terminal text from UTF-8 stdin\n\
Usage: rex-extract [--mode word|line|path|url|quote] [--max-lines N]\n\
                   [--max-items 1..2000] [--min-length N] [--format json|records]\n\
Defaults: mode=word, max-lines=3000, max-items=2000, min-length=3, format=json"
        );
        return Ok(());
    }
    if args == ["--version"] {
        println!("rex-extract {}", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    for pair in args.chunks(2) {
        if pair.len() != 2 {
            return Err("arguments require values".into());
        }
        match pair[0].as_str() {
            "--mode" => options.mode = pair[1].clone(),
            "--format" => format = pair[1].clone(),
            "--max-lines" => options.max_lines = pair[1].parse()?,
            "--max-items" => options.max_items = pair[1].parse()?,
            "--min-length" => options.min_length = pair[1].parse()?,
            _ => return Err("unknown argument".into()),
        }
    }
    options.validate()?;
    if !["json", "records"].contains(&format.as_str()) {
        return Err("format must be json or records".into());
    }
    let mut input = String::new();
    io::stdin().read_to_string(&mut input)?;
    let output = Extractor::default().extract(&input, &options)?;
    let mut stdout = BufWriter::with_capacity(65536, io::stdout().lock());
    if format == "records" {
        for item in output.items {
            stdout.write_all(item.title.as_bytes())?;
            stdout.write_all(b"\0")?;
        }
    } else {
        serde_json::to_writer(&mut stdout, &output)?;
    }
    stdout.flush()?;
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("rex-extract: {error}");
        std::process::exit(1);
    }
}
