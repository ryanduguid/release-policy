//! A stand-in `gh` for differential and integration tests.
//!
//! `FAKE_GH_SCENARIO` names a JSON file `{"rules": [...], "log": "path"}`.
//! Each call's arguments are normalised (a `release download --dir` target
//! becomes `<dir>`, an `attestation verify` subject becomes its file name),
//! appended to the log as one JSON line and answered by the first rule whose
//! `args` equal them: `stdout`, `stderr` and `code`, and for a download the
//! `files` it writes into the target directory. An unmatched call exits 99.
use serde_json::Value;
use std::io::Write;
use std::path::Path;

fn main() {
    let mut args: Vec<String> = std::env::args().skip(1).collect();
    let mut target = None;
    if args.starts_with(&["release".into(), "download".into()])
        && let Some(i) = args.iter().position(|a| a == "--dir")
        && let Some(dir) = args.get_mut(i + 1)
    {
        target = Some(std::mem::replace(dir, "<dir>".into()));
    }
    if args.starts_with(&["attestation".into(), "verify".into()]) && args.len() > 2 {
        let name = Path::new(&args[2])
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        args[2] = name;
    }
    let scenario: Value = std::env::var("FAKE_GH_SCENARIO")
        .ok()
        .and_then(|p| std::fs::read(p).ok())
        .and_then(|raw| serde_json::from_slice(&raw).ok())
        .unwrap_or(Value::Null);
    if let Some(log) = scenario["log"].as_str() {
        let mut file = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(log)
            .expect("the fake gh log");
        writeln!(file, "{}", serde_json::to_string(&args).unwrap()).unwrap();
    }
    let rules = scenario["rules"].as_array().cloned().unwrap_or_default();
    let found = rules.iter().position(|r| {
        r["args"].as_array().is_some_and(|a| {
            a.iter()
                .map(|v| v.as_str())
                .eq(args.iter().map(|s| Some(s.as_str())))
        })
    });
    let Some(index) = found else {
        eprintln!("fake gh: no rule for {args:?}");
        std::process::exit(99);
    };
    // A rule with a `sequence` answers its nth call with the nth entry, then
    // repeats the last; the counts live beside the log.
    let mut rule = rules[index].clone();
    if let (Some(sequence), Some(log)) = (
        rule["sequence"].as_array().cloned(),
        scenario["log"].as_str(),
    ) {
        let state = format!("{log}.state.json");
        let mut counts: Value = std::fs::read(&state)
            .ok()
            .and_then(|raw| serde_json::from_slice(&raw).ok())
            .unwrap_or_else(|| serde_json::json!({}));
        let count = counts[index.to_string()].as_u64().unwrap_or(0) as usize;
        counts[index.to_string()] = (count + 1).into();
        std::fs::write(&state, serde_json::to_vec(&counts).unwrap()).unwrap();
        rule = sequence[count.min(sequence.len() - 1)].clone();
    }
    let rule = &rule;
    if let (Some(dir), Some(files)) = (&target, rule["files"].as_object()) {
        for (name, content) in files {
            std::fs::write(Path::new(dir).join(name), content.as_str().unwrap_or("")).unwrap();
        }
    }
    std::io::stdout()
        .write_all(rule["stdout"].as_str().unwrap_or("").as_bytes())
        .unwrap();
    std::io::stderr()
        .write_all(rule["stderr"].as_str().unwrap_or("").as_bytes())
        .unwrap();
    std::process::exit(rule["code"].as_i64().unwrap_or(0) as i32);
}
