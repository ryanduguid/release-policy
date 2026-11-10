// Copyright 2026 Ryan Duguid
// SPDX-License-Identifier: LicenseRef-Duguid-Public-Attribution-1.0

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
    run_with(args, closed_pipe(), broken_stdout, unbuffered)
}

fn run_with(args: &[&str], broken: Stdio, broken_stdout: bool, unbuffered: bool) -> (i32, String) {
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
        command.stdout(broken).stderr(Stdio::piped())
    } else {
        command.stdout(Stdio::piped()).stderr(broken)
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
fn unbuffered_redirected_stdout_keeps_the_complete_output() {
    let directory = support::tempdir();
    let output = support::cli_env(
        directory.path(),
        &[
            "python-release",
            "tag",
            "--tag",
            "example/v1.2.3",
            "--tag-prefix",
            "example",
            "--format",
            "lines",
        ],
        "",
        &[("PYTHONUNBUFFERED", "1")],
    );
    assert_eq!(output.code, 0, "{}", output.stderr);
    assert!(output.stderr.is_empty());
    assert_eq!(
        output.stdout,
        "1.2.3\nv1.2.3\nexample/v1.2.3\nexample-v1.2.3\n"
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

/// A handle opened only for reading: Unix refuses the write as EBADF, and
/// Windows as access denied, which the scripts' C runtime reported as EBADF.
#[test]
fn a_read_only_stdout_is_a_bad_file_descriptor() {
    let dir = support::tempdir();
    let path = dir.path().join("stdout");
    std::fs::write(&path, "").unwrap();
    let read_only = || Stdio::from(std::fs::File::open(&path).unwrap());
    assert_eq!(
        run_with(&["--help"], read_only(), true, false),
        (
            120,
            "Exception ignored while flushing sys.stdout:
OSError: [Errno 9] Bad file descriptor
"
            .into()
        )
    );
    assert_eq!(
        run_with(INPUTS, read_only(), true, true),
        (
            1,
            "release-policy: [Errno 9] Bad file descriptor
"
            .into()
        )
    );
}

/// A closed stdout: CPython's `sys.stdout` was None, so print wrote nothing.
#[cfg(unix)]
#[test]
fn a_closed_stdout_prints_nothing() {
    for unbuffered in [false, true] {
        for args in [&["--help"][..], INPUTS] {
            let dir = support::tempdir();
            let mut command = Command::new("sh");
            command
                .args([
                    "-c",
                    r#"exec "$0" "$@" >&-"#,
                    env!("CARGO_BIN_EXE_release-policy"),
                ])
                .args(args)
                .current_dir(dir.path())
                .env_remove("PYTHONUNBUFFERED");
            if unbuffered {
                command.env("PYTHONUNBUFFERED", "1");
            }
            let output = command.stderr(Stdio::piped()).output().unwrap();
            assert_eq!(
                (
                    output.status.code(),
                    String::from_utf8(output.stderr).unwrap()
                ),
                (Some(0), String::new()),
                "{args:?}, unbuffered {unbuffered}"
            );
        }
    }
}

/// A terminal can stay open yet refuse a nonblocking write while output is stopped.
#[cfg(target_os = "linux")]
#[test]
fn a_terminal_write_failure_keeps_the_buffered_shutdown_status() {
    let output = Command::new("python3")
        .args([
            "-c",
            r#"import fcntl, os, pty, subprocess, sys, termios
binary = sys.argv[1]
for args, broken_stdout in [(['--help'], True), (['--bogus'], False)]:
    master, slave = pty.openpty()
    try:
        fcntl.fcntl(slave, fcntl.F_SETFL, os.O_NONBLOCK)
        termios.tcflow(slave, termios.TCOOFF)
        env = dict(os.environ)
        env.pop('PYTHONUNBUFFERED', None)
        result = subprocess.run([binary, *args],
            stdout=slave if broken_stdout else subprocess.PIPE,
            stderr=subprocess.PIPE if broken_stdout else slave,
            env=env, timeout=10)
        assert result.returncode == 120, (args, result)
        if broken_stdout:
            assert result.stderr == b'Exception ignored while flushing sys.stdout:\nBlockingIOError: [Errno 11] write could not complete without blocking\n', result
        else:
            assert result.stdout == b'', result
    finally:
        os.close(slave)
        os.close(master)
"#,
            env!("CARGO_BIN_EXE_release-policy"),
        ])
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
}

#[cfg(target_os = "linux")]
#[test]
fn a_terminal_receives_every_line_of_buffered_stdout() {
    let output = Command::new("python3")
        .args([
            "-c",
            r#"import errno, os, pty, select, subprocess, sys
master, slave = pty.openpty()
try:
    env = dict(os.environ)
    env.pop('PYTHONUNBUFFERED', None)
    result = subprocess.run([sys.argv[1], 'python-release', 'tag',
        '--tag', 'example/v1.2.3', '--tag-prefix', 'example', '--format', 'lines'],
        stdout=slave, stderr=subprocess.PIPE, env=env, timeout=10)
    assert result.returncode == 0, result
    assert result.stderr == b'', result
    os.close(slave)
    slave = None
    content = b''
    while True:
        assert select.select([master], [], [], 10)[0], 'terminal read timed out'
        try:
            chunk = os.read(master, 4096)
        except OSError as error:
            if error.errno == errno.EIO:
                break
            raise
        if not chunk:
            break
        content += chunk
    assert content.replace(b'\r\n', b'\n') == b'1.2.3\nv1.2.3\nexample/v1.2.3\nexample-v1.2.3\n', content
finally:
    if slave is not None:
        os.close(slave)
    os.close(master)
"#,
            env!("CARGO_BIN_EXE_release-policy"),
        ])
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
}
