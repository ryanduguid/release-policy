//! A standard stream that cannot be written fails as it did for the scripts,
//! measured against them on a closed pipe: stdout still pending at exit is
//! exit status 120, a print that fails at once is an uncaught OSError (exit 1),
//! a diagnostic that cannot be written loses the original code, and argparse
//! ignored its own write errors.
mod support;

use std::process::{Command, Stdio};

/// `str(OSError)` for a write to a pipe with no reader, and its exception line.
const CLOSED_PIPE: (&str, &str) = if cfg!(windows) {
    (
        "[Errno 22] Invalid argument",
        "OSError: [Errno 22] Invalid argument",
    )
} else {
    (
        "[Errno 32] Broken pipe",
        "BrokenPipeError: [Errno 32] Broken pipe",
    )
};

const INPUTS: &[&str] = &[
    "python-release",
    "validate-inputs",
    "--source-directory",
    ".",
    "--tag-prefix",
    "v",
];

fn closed_pipe() -> Stdio {
    let (reader, writer) = std::io::pipe().unwrap();
    drop(reader);
    writer.into()
}

/// Exit code and the text on the stream that still works.
fn run(args: &[&str], broken_stdout: bool, unbuffered: bool) -> (i32, String) {
    let dir = support::tempdir();
    let mut command = Command::new(env!("CARGO_BIN_EXE_release-policy"));
    command
        .args(args)
        .current_dir(dir.path())
        .env_remove("PYTHONUNBUFFERED");
    if unbuffered {
        command.env("PYTHONUNBUFFERED", "1");
    }
    let output = if broken_stdout {
        command.stdout(closed_pipe()).stderr(Stdio::piped())
    } else {
        command.stdout(Stdio::piped()).stderr(closed_pipe())
    }
    .output()
    .unwrap();
    let other = if broken_stdout {
        output.stderr
    } else {
        output.stdout
    };
    (
        output.status.code().unwrap(),
        String::from_utf8(other).unwrap().replace("\r\n", "\n"),
    )
}

#[test]
fn pending_stdout_fails_at_exit_with_status_120() {
    let flush = format!(
        "Exception ignored while flushing sys.stdout:\n{}\n",
        CLOSED_PIPE.1
    );
    assert_eq!(run(&["--help"], true, false), (120, flush.clone()));
    assert_eq!(run(INPUTS, true, false), (120, flush));
}

#[test]
fn unbuffered_stdout_fails_at_the_print() {
    assert_eq!(run(&["--help"], true, true), (0, String::new()));
    assert_eq!(
        run(INPUTS, true, true),
        (1, format!("release-policy: {}\n", CLOSED_PIPE.0))
    );
}

#[test]
fn an_unwritable_diagnostic_loses_the_original_code() {
    let bad_tag = [
        "python-release",
        "tag",
        "--tag",
        "nope",
        "--tag-prefix",
        "v",
    ];
    assert_eq!(run(&bad_tag, false, false), (120, String::new()));
    assert_eq!(run(&bad_tag, false, true), (1, String::new()));
    assert_eq!(run(&["pin-families"], false, false), (120, String::new()));
    assert_eq!(run(&["pin-families"], false, true), (1, String::new()));
    // argparse ignored the failed usage message and kept its exit status 2.
    assert_eq!(run(&["--bogus"], false, false), (120, String::new()));
    assert_eq!(run(&["--bogus"], false, true), (2, String::new()));
}
