//! A release candidate for nothing: `release-binary.yml` tags, builds, attests
//! and publishes this executable so the real signing and publication path can
//! be checked without spending a real version.

fn version_line() -> String {
    concat!(env!("CARGO_PKG_NAME"), " ", env!("CARGO_PKG_VERSION")).to_owned()
}

fn main() {
    match std::env::args().nth(1).as_deref() {
        Some("--version") => println!("{}", version_line()),
        _ => {
            eprintln!("usage: disposable-binary-canary --version");
            std::process::exit(2);
        }
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn version_line_ends_with_the_package_version() {
        assert!(super::version_line().ends_with(" 0.0.1"));
    }
}
