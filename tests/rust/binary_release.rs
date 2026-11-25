// Copyright 2026 Ryan Duguid
// SPDX-License-Identifier: Apache-2.0

mod support;

use crc32fast::Hasher;
use flate2::write::GzEncoder;
use flate2::{Compression, GzBuilder};
use release_policy::binary_release::{
    DOCUMENTS, MAX_ARCHIVE_BYTES, MAX_DECOMPRESSED_BYTES, MAX_MEMBERS, TARGETS, accept,
    asset_name, binaries, cargo_package, confined_path, data_names, digests, find_data_files,
    find_documents, handoff_names, linkage, macho_linkage, member_names, needed_libraries,
    package, receive, resolve_path, sbom_lists, smoke_with_timeout, toolchain, verify,
};
use release_policy::common::sha256;
use serde_json::json;
use std::collections::BTreeMap;
use std::fs::File;
#[cfg(unix)]
use std::fs::OpenOptions;
use std::io::Write;
use std::path::Path;
use std::time::Duration;
use support::{FakeGh, cli, cli_env, rule, symlink, tempdir};

const LINUX: &str = "x86_64-unknown-linux-musl";
const LINUX_ARM: &str = "aarch64-unknown-linux-musl";
const MACOS: &str = "x86_64-apple-darwin";
const MACOS_ARM: &str = "aarch64-apple-darwin";
const WIN: &str = "x86_64-pc-windows-msvc";
const WIN_ARM: &str = "aarch64-pc-windows-msvc";

fn write_tar_raw(path: &Path, entries: &[(&str, &[u8], u8, u32, &[u8])]) {
    let mut tar = Vec::new();
    for &(name, data, typeflag, mode, prefix) in entries {
        let mut hdr = [0u8; 512];
        hdr[..name.len()].copy_from_slice(name.as_bytes());
        hdr[100..108].copy_from_slice(format!("{mode:07o}\0").as_bytes());
        hdr[108..116].copy_from_slice(b"0000000\0");
        hdr[116..124].copy_from_slice(b"0000000\0");
        hdr[124..136].copy_from_slice(format!("{:011o}\0", data.len()).as_bytes());
        hdr[136..148].copy_from_slice(b"00000000000\0");
        hdr[148..156].fill(b' ');
        hdr[156] = typeflag;
        hdr[257..263].copy_from_slice(b"ustar\0");
        hdr[263..265].copy_from_slice(b"00");
        hdr[345..345 + prefix.len()].copy_from_slice(prefix);
        let sum: u32 = hdr.iter().map(|&b| u32::from(b)).sum();
        hdr[148..156].copy_from_slice(format!("{sum:06o}\0 ").as_bytes());
        tar.extend_from_slice(&hdr);
        tar.extend_from_slice(data);
        let pad = (512 - (data.len() % 512)) % 512;
        tar.resize(tar.len() + pad, 0);
    }
    tar.resize(tar.len() + 1024, 0);
    let file = File::create(path).unwrap();
    let mut gz = GzBuilder::new().mtime(0).write(file, Compression::default());
    gz.write_all(&tar).unwrap();
    gz.finish().unwrap();
}

#[allow(clippy::too_many_arguments)]
fn build_single_zip(
    path: &Path,
    name: &str,
    data: &[u8],
    ext_attr: u32,
    method: u16,
    flags: u16,
    extra: &[u8],
    corrupt_crc: bool,
    with_zip64_locator: bool,
) {
    let mut hasher = Hasher::new();
    hasher.update(data);
    let crc = hasher.finalize() ^ if corrupt_crc { 1 } else { 0 };
    let payload = if method == 0 {
        data.to_vec()
    } else {
        let mut enc = flate2::write::DeflateEncoder::new(Vec::new(), Compression::default());
        enc.write_all(data).unwrap();
        enc.finish().unwrap()
    };
    let mut buf = Vec::new();
    let name_b = name.as_bytes();
    buf.extend_from_slice(b"PK\x03\x04");
    buf.extend_from_slice(&20u16.to_le_bytes());
    buf.extend_from_slice(&flags.to_le_bytes());
    buf.extend_from_slice(&method.to_le_bytes());
    buf.extend_from_slice(&0u16.to_le_bytes());
    buf.extend_from_slice(&0x0021u16.to_le_bytes());
    buf.extend_from_slice(&crc.to_le_bytes());
    buf.extend_from_slice(&(payload.len() as u32).to_le_bytes());
    buf.extend_from_slice(&(data.len() as u32).to_le_bytes());
    buf.extend_from_slice(&(name_b.len() as u16).to_le_bytes());
    buf.extend_from_slice(&0u16.to_le_bytes());
    buf.extend_from_slice(name_b);
    buf.extend_from_slice(&payload);

    let cd_off = buf.len() as u32;
    let mut cd = Vec::new();
    cd.extend_from_slice(b"PK\x01\x02");
    cd.extend_from_slice(&((3u16 << 8) | 20u16).to_le_bytes());
    cd.extend_from_slice(&20u16.to_le_bytes());
    cd.extend_from_slice(&flags.to_le_bytes());
    cd.extend_from_slice(&method.to_le_bytes());
    cd.extend_from_slice(&0u16.to_le_bytes());
    cd.extend_from_slice(&0x0021u16.to_le_bytes());
    cd.extend_from_slice(&crc.to_le_bytes());
    cd.extend_from_slice(&(payload.len() as u32).to_le_bytes());
    cd.extend_from_slice(&(data.len() as u32).to_le_bytes());
    cd.extend_from_slice(&(name_b.len() as u16).to_le_bytes());
    cd.extend_from_slice(&(extra.len() as u16).to_le_bytes());
    cd.extend_from_slice(&0u16.to_le_bytes());
    cd.extend_from_slice(&0u16.to_le_bytes());
    cd.extend_from_slice(&0u16.to_le_bytes());
    cd.extend_from_slice(&ext_attr.to_le_bytes());
    cd.extend_from_slice(&0u32.to_le_bytes());
    cd.extend_from_slice(name_b);
    cd.extend_from_slice(extra);

    let cd_size = cd.len() as u32;
    buf.extend_from_slice(&cd);
    if with_zip64_locator {
        buf.extend_from_slice(b"PK\x06\x07");
        buf.extend_from_slice(&[0u8; 16]);
    }
    buf.extend_from_slice(b"PK\x05\x06");
    buf.extend_from_slice(&0u16.to_le_bytes());
    buf.extend_from_slice(&0u16.to_le_bytes());
    buf.extend_from_slice(&1u16.to_le_bytes());
    buf.extend_from_slice(&1u16.to_le_bytes());
    buf.extend_from_slice(&cd_size.to_le_bytes());
    buf.extend_from_slice(&cd_off.to_le_bytes());
    buf.extend_from_slice(&0u16.to_le_bytes());
    std::fs::write(path, buf).unwrap();
}

fn macho_binary(cputype: u32, filetype: u32, commands: &[(u32, &[u8])]) -> Vec<u8> {
    let mut cmd_bytes = Vec::new();
    for &(cmd, payload) in commands {
        let len = (8 + payload.len()) as u32;
        cmd_bytes.extend_from_slice(&cmd.to_le_bytes());
        cmd_bytes.extend_from_slice(&len.to_le_bytes());
        cmd_bytes.extend_from_slice(payload);
    }
    let mut out = Vec::new();
    out.extend_from_slice(b"\xcf\xfa\xed\xfe");
    out.extend_from_slice(&cputype.to_le_bytes());
    out.extend_from_slice(&0u32.to_le_bytes());
    out.extend_from_slice(&filetype.to_le_bytes());
    out.extend_from_slice(&(commands.len() as u32).to_le_bytes());
    out.extend_from_slice(&(cmd_bytes.len() as u32).to_le_bytes());
    out.extend_from_slice(&0u32.to_le_bytes());
    out.extend_from_slice(&0u32.to_le_bytes());
    out.extend_from_slice(&cmd_bytes);
    out
}

fn macho_str_cmd(path: &[u8]) -> Vec<u8> {
    let mut p = Vec::new();
    p.extend_from_slice(&24u32.to_le_bytes()); // str offset from command start; 8 + 16 = 24
    p.extend_from_slice(&[0u8; 12]);
    p.extend_from_slice(path);
    p.push(0);
    while p.len() % 8 != 0 {
        p.push(0);
    }
    p
}

fn setup_tree(dir: &Path) {
    let src = dir.join("crate");
    std::fs::create_dir_all(src.join("assets")).unwrap();
    std::fs::write(src.join("LICENSE"), "apache\n").unwrap();
    std::fs::write(src.join("README.md"), "readme\n").unwrap();
    std::fs::write(src.join("NOTICE"), "notice\n").unwrap();
    std::fs::write(src.join("assets/cfg.json"), "{}\n").unwrap();
    std::fs::write(dir.join("LICENSE"), "root-lic\n").unwrap();
    std::fs::write(dir.join("README.md"), "root-readme\n").unwrap();
}

#[test]
fn validation_names_documents_and_data_files() {
    assert_eq!(asset_name("my-tool", "1.2.3", LINUX).unwrap(), "my-tool-1.2.3-x86_64-unknown-linux-musl.tar.gz");
    assert_eq!(asset_name("my-tool", "0.0.1", WIN).unwrap(), "my-tool-0.0.1-x86_64-pc-windows-msvc.zip");
    assert!(asset_name("BadStem", "1.0.0", LINUX).is_err());
    assert!(asset_name("my-tool", "01.0.0", LINUX).is_err());
    assert!(asset_name("my-tool", "1.0.0", "riscv64").is_err());

    assert_eq!(binaries("one two_b").unwrap(), ["one", "two_b"]);
    assert!(binaries("").is_err());
    assert!(binaries("dup dup").is_err());
    assert!(binaries("bad/name").is_err());

    assert_eq!(
        data_names("sub/a.txt\n\n  b.json  \n").unwrap(),
        [("a.txt".into(), "sub/a.txt".into()), ("b.json".into(), "b.json".into())]
    );
    assert!(data_names("../evil").is_err());
    assert!(data_names("sub/LICENSE").is_err());
    assert!(data_names("a/same.txt\nb/same.txt").is_err());

    let extra: Vec<&str> = (0..11).map(|_| "x").collect();
    let _ = extra;
    assert_eq!(
        member_names("b a", WIN, &["LICENSE", "README.md"], &["cfg.json"]).unwrap(),
        ["b.exe", "a.exe", "LICENSE", "README.md", "cfg.json"]
    );
    let too_many = ["d1", "d2", "d3", "d4", "d5", "d6", "d7", "d8", "d9", "d10", "d11"];
    assert!(member_names("a b", LINUX, &DOCUMENTS, &too_many).is_err());

    let tmp = tempdir();
    let base = tmp.path();
    setup_tree(base);
    let docs = find_documents(&base.join("crate"), base).unwrap();
    assert_eq!(docs.len(), 3);
    assert_eq!(std::fs::read_to_string(&docs["LICENSE"]).unwrap(), "apache\n");

    let empty = base.join("empty");
    std::fs::create_dir(&empty).unwrap();
    assert_eq!(
        find_documents(&empty, &empty).unwrap_err(),
        "missing release documents: LICENSE, README.md"
    );

    let data = find_data_files(&base.join("crate"), "assets/cfg.json\n").unwrap();
    assert_eq!(data.len(), 1);
    assert!(find_data_files(&base.join("crate"), "missing.txt").is_err());

    let outside = base.join("secret.txt");
    std::fs::write(&outside, "secret").unwrap();
    let link = base.join("crate/link.txt");
    if symlink(&outside, &link) {
        assert!(find_data_files(&base.join("crate"), "link.txt").is_err());
    }
}

#[test]
fn cargo_package_and_toolchain_checks() {
    let tmp = tempdir();
    let root = tmp.path();
    let comp = root.join("comp");
    std::fs::create_dir_all(&comp).unwrap();

    let manifest = comp.join("Cargo.toml");
    std::fs::write(
        &manifest,
        "[package]\nname = \"demo\"\nversion = \"1.2.3\"\npublish = false\n[[bin]]\nname = \"helper\"\n",
    )
    .unwrap();
    assert_eq!(cargo_package(&manifest, "1.2.3", "demo helper").unwrap(), "demo");
    assert!(cargo_package(&manifest, "1.2.4", "demo").is_err());
    assert!(cargo_package(&manifest, "1.2.3", "missing").is_err());

    std::fs::write(&manifest, "[package]\nname = \"demo\"\nversion = \"1.2.3\"\n").unwrap();
    assert!(cargo_package(&manifest, "1.2.3", "demo").unwrap_err().contains("publish = false"));

    std::fs::write(&manifest, "[package]\nname = \"demo\"\nversion.workspace = true\npublish = false\n").unwrap();
    assert!(cargo_package(&manifest, "1.2.3", "demo").unwrap_err().contains("workspace inheritance"));

    std::fs::write(&manifest, "[workspace]\n").unwrap();
    assert!(cargo_package(&manifest, "1.2.3", "demo").unwrap_err().contains("[package]"));

    std::fs::write(&manifest, b"\xff").unwrap();
    assert!(cargo_package(&manifest, "1.2.3", "demo").is_err());

    // Toolchain resolution: root fallback then component override, legacy refused.
    std::fs::write(root.join("rust-toolchain.toml"), "[toolchain]\nchannel = \"1.99.1\"\n").unwrap();
    assert_eq!(toolchain(&comp, root).unwrap(), "1.99.1");

    std::fs::write(comp.join("rust-toolchain.toml"), "[toolchain]\nchannel = \"nightly\"\n").unwrap();
    assert!(toolchain(&comp, root).unwrap_err().contains("MAJOR.MINOR.PATCH"));

    std::fs::write(comp.join("rust-toolchain.toml"), "[toolchain]\nchannel = \"1.98.0\"\n").unwrap();
    assert_eq!(toolchain(&comp, root).unwrap(), "1.98.0");

    std::fs::write(comp.join("rust-toolchain"), "1.99.1\n").unwrap();
    assert!(toolchain(&comp, root).unwrap_err().contains("use rust-toolchain.toml"));

    let outside = tempdir();
    assert!(toolchain(outside.path(), root).unwrap_err().contains("is not inside"));
}

#[test]
fn package_and_verify_roundtrip_and_traps() {
    let tmp = tempdir();
    let root = tmp.path();
    setup_tree(root);
    let execs = root.join("execs");
    std::fs::create_dir(&execs).unwrap();
    std::fs::write(execs.join("app"), b"elf").unwrap();
    std::fs::write(execs.join("app.exe"), b"pe").unwrap();
    let out = root.join("dist");

    let tgz = package(
        &execs,
        "app",
        LINUX,
        "my-app",
        "1.2.3",
        &root.join("crate"),
        root,
        1_700_000_000,
        &out,
        "assets/cfg.json",
    )
    .unwrap();
    assert!(tgz.exists());
    // Refuses overwrite.
    assert!(
        package(&execs, "app", LINUX, "my-app", "1.2.3", &root.join("crate"), root, 0, &out, "")
            .is_err()
    );

    let extracted = root.join("extracted_tgz");
    let got = verify(&tgz, "app", LINUX, Some(&extracted), "assets/cfg.json").unwrap();
    assert_eq!(got, ["app", "LICENSE", "NOTICE", "README.md", "cfg.json"]);
    assert_eq!(std::fs::read(extracted.join("app")).unwrap(), b"elf");
    assert!(verify(&tgz, "app", LINUX, Some(&extracted), "assets/cfg.json").is_err());

    let zip_path = package(
        &execs,
        "app",
        WIN,
        "my-app",
        "1.2.3",
        &root.join("crate"),
        root,
        100,
        &out,
        "assets/cfg.json",
    )
    .unwrap();
    let ext_zip = root.join("extracted_zip");
    assert_eq!(
        verify(&zip_path, "app", WIN, Some(&ext_zip), "assets/cfg.json").unwrap(),
        ["app.exe", "LICENSE", "NOTICE", "README.md", "cfg.json"]
    );
    assert_eq!(std::fs::read(ext_zip.join("app.exe")).unwrap(), b"pe");

    // Malformed / trapped archives.
    let bad = root.join("bad.zip");
    build_single_zip(&bad, "app.exe", b"x", (0o100_644) << 16, 8, 0, &[], false, true);
    assert!(verify(&bad, "app", WIN, None, "").unwrap_err().contains("ZIP64"));

    build_single_zip(&bad, "app.exe", b"x", (0o100_644) << 16, 8, 0, &[1, 0, 0, 0], false, false);
    assert!(verify(&bad, "app", WIN, None, "").unwrap_err().contains("ZIP64"));

    build_single_zip(&bad, "app.exe", b"x", (0o100_644) << 16, 8, 0, &[], true, false);
    assert!(verify(&bad, "app", WIN, None, "").unwrap_err().contains("Bad CRC-32"));

    build_single_zip(&bad, "app.exe/", b"", (0o040_755) << 16, 0, 0, &[], false, false);
    assert!(verify(&bad, "app", WIN, None, "").unwrap_err().contains("not a regular file"));

    build_single_zip(&bad, "app.exe", b"x", (0o120_777) << 16, 0, 0, &[], false, false);
    assert!(verify(&bad, "app", WIN, None, "").unwrap_err().contains("not a regular file"));

    build_single_zip(&bad, "app.exe", b"x", (0o100_644) << 16, 99, 0, &[], false, false);
    assert!(verify(&bad, "app", WIN, None, "").unwrap_err().contains("unsupported ZIP compression method"));

    build_single_zip(&bad, "app.exe", b"x", (0o100_644) << 16, 8, 1, &[], false, false);
    assert!(verify(&bad, "app", WIN, None, "").unwrap_err().contains("encrypted ZIP"));

    std::fs::write(&bad, b"not a zip").unwrap();
    assert!(verify(&bad, "app", WIN, None, "").unwrap_err().contains("end of central directory"));

    // Tar traps: PAX header, symlink, dir, bad checksum, traversal, non-executable, bomb.
    let bad_tar = root.join("bad.tar.gz");
    write_tar_raw(&bad_tar, &[("PaxHeaders.0/app", b"11 path=x\n", b'x', 0o644, b""), ("app", b"x", b'0', 0o755, b"")]);
    assert!(verify(&bad_tar, "app", LINUX, None, "").unwrap_err().contains("PAX headers"));

    write_tar_raw(&bad_tar, &[("pax_global_header", b"11 comment=a\n", b'g', 0o644, b""), ("app", b"x", b'0', 0o755, b"")]);
    assert!(verify(&bad_tar, "app", LINUX, None, "").unwrap_err().contains("PAX headers"));

    write_tar_raw(&bad_tar, &[("link", b"", b'2', 0o777, b"")]);
    assert!(verify(&bad_tar, "app", LINUX, None, "").unwrap_err().contains("not a regular file"));

    write_tar_raw(&bad_tar, &[("app", b"x", b'0', 0o755, b""), ("app", b"y", b'0', 0o755, b"")]);
    assert!(verify(&bad_tar, "app", LINUX, None, "").unwrap_err().contains("duplicate member"));

    write_tar_raw(&bad_tar, &[("../app", b"x", b'0', 0o755, b"")]);
    assert!(verify(&bad_tar, "app", LINUX, None, "").unwrap_err().contains("unsafe member name"));

    write_tar_raw(&bad_tar, &[("app", b"x", b'0', 0o644, b""), ("LICENSE", b"l", b'0', 0o644, b""), ("README.md", b"r", b'0', 0o644, b"")]);
    assert!(verify(&bad_tar, "app", LINUX, None, "").unwrap_err().contains("not executable"));

    write_tar_raw(&bad_tar, &[("LICENSE", b"l", b'0', 0o644, b""), ("README.md", b"r", b'0', 0o644, b"")]);
    assert!(verify(&bad_tar, "app", LINUX, None, "").unwrap_err().contains("executable app is missing"));

    write_tar_raw(&bad_tar, &[("app", b"x", b'0', 0o755, b""), ("README.md", b"r", b'0', 0o644, b"")]);
    assert!(verify(&bad_tar, "app", LINUX, None, "").unwrap_err().contains("members are"));

    // Corrupt tar checksum
    let mut bad_hdr_tar = vec![0u8; 1536];
    bad_hdr_tar[..3].copy_from_slice(b"app");
    bad_hdr_tar[148..156].copy_from_slice(b"000000\0 ");
    let mut gz = GzEncoder::new(File::create(&bad_tar).unwrap(), Compression::default());
    gz.write_all(&bad_hdr_tar).unwrap();
    gz.finish().unwrap();
    assert!(verify(&bad_tar, "app", LINUX, None, "").unwrap_err().contains("checksum"));

    // Archive decompression bomb (> MAX_DECOMPRESSED_BYTES of compressed zeros)
    let bomb = root.join("bomb.tar.gz");
    {
        let mut gz = GzEncoder::new(File::create(&bomb).unwrap(), Compression::fast());
        let zeros = vec![0u8; 1024 * 1024];
        let chunks = (MAX_DECOMPRESSED_BYTES / zeros.len()) + 2;
        for _ in 0..chunks {
            gz.write_all(&zeros).unwrap();
        }
        gz.finish().unwrap();
    }
    assert!(verify(&bomb, "app", LINUX, None, "").unwrap_err().contains("size limit"));

    // Oversize file check without consuming disk on Unix sparse files
    #[cfg(unix)]
    {
        let huge = root.join("huge.tar.gz");
        let f = OpenOptions::new().write(true).create(true).truncate(true).open(&huge).unwrap();
        f.set_len((MAX_ARCHIVE_BYTES as u64) + 1).unwrap();
        assert!(verify(&huge, "app", LINUX, None, "").unwrap_err().contains("archive size limit"));
    }
}

#[test]
fn linkage_and_macho_linkage_validation() {
    let linux_ok = "Format: elf64-x86-64\nArch: x86_64\nNeededLibraries [\n]\n";
    assert!(linkage(linux_ok, LINUX).unwrap().is_empty());

    let linux_interp = "Format: elf64-x86-64\nArch: x86_64\nType: PT_INTERP\nNeededLibraries [\n]\n";
    assert!(linkage(linux_interp, LINUX).unwrap_err().contains("not a static"));

    let linux_so = "Format: elf64-x86-64\nArch: x86_64\nNeededLibraries [\n  libc.so.6\n]\n";
    assert!(linkage(linux_so, LINUX).unwrap_err().contains("libc.so.6"));

    assert!(linkage(linux_ok, LINUX_ARM).unwrap_err().contains("not an aarch64"));
    assert!(linkage("Format: a\nFormat: b\n", LINUX).unwrap_err().contains("more than one object"));
    assert!(linkage("Format: elf64-x86-64\nArch: x86_64\n", LINUX).unwrap_err().contains("no NeededLibraries"));
    assert!(needed_libraries("bad").is_err());

    let win_ok = "Format: COFF-x86-64\nArch: x86_64\nNeededLibraries [\n  KERNEL32.dll\n  api-ms-win-core-synch-l1-2-0.dll\n]\n";
    assert_eq!(linkage(win_ok, WIN).unwrap(), ["KERNEL32.dll", "api-ms-win-core-synch-l1-2-0.dll"]);

    let win_rt = "Format: COFF-ARM64\nArch: aarch64\nNeededLibraries [\n  VCRUNTIME140.dll\n]\n";
    assert!(linkage(win_rt, WIN_ARM).unwrap_err().contains("C runtime DLLs"));

    let win_path = "Format: COFF-x86-64\nArch: x86_64\nNeededLibraries [\n  C:\\bad.dll\n]\n";
    assert!(linkage(win_path, WIN).unwrap_err().contains("by path"));

    let win_foreign = "Format: COFF-x86-64\nArch: x86_64\nNeededLibraries [\n  zlib1.dll\n]\n";
    assert!(linkage(win_foreign, WIN).unwrap_err().contains("outside the pinned Windows system set"));

    assert!(linkage(linux_ok, MACOS).unwrap_err().contains("load commands"));
    assert!(macho_linkage(b"", LINUX).unwrap_err().contains("only a macOS"));

    let arm_ok = macho_binary(
        0x0100_000C,
        2,
        &[
            (0xE, &macho_str_cmd(b"/usr/lib/dyld")),
            (0xC, &macho_str_cmd(b"/usr/lib/libSystem.B.dylib")),
            (
                0x8000_0018,
                &macho_str_cmd(b"/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation"),
            ),
        ],
    );
    assert_eq!(
        macho_linkage(&arm_ok, MACOS_ARM).unwrap(),
        [
            "/usr/lib/libSystem.B.dylib",
            "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation"
        ]
    );

    assert!(macho_linkage(&arm_ok, MACOS).unwrap_err().contains("x86_64 Mach-O"));
    assert!(macho_linkage(b"\xca\xfe\xba\xbe", MACOS_ARM).unwrap_err().contains("thin 64-bit"));
    assert!(macho_linkage(b"\xcf\xfa\xed\xfe\x00", MACOS_ARM).unwrap_err().contains("malformed"));

    let bad_type = macho_binary(0x0100_0007, 6, &[]);
    assert!(macho_linkage(&bad_type, MACOS).unwrap_err().contains("file type"));

    let rpath = macho_binary(0x0100_000C, 2, &[(0x8000_001C, &macho_str_cmd(b"@executable_path"))]);
    assert!(macho_linkage(&rpath, MACOS_ARM).unwrap_err().contains("run-path"));

    let dyld_env = macho_binary(0x0100_000C, 2, &[(0x27, &[])]);
    assert!(macho_linkage(&dyld_env, MACOS_ARM).unwrap_err().contains("environment variables"));

    let bad_dylinker = macho_binary(0x0100_000C, 2, &[(0xE, &macho_str_cmd(b"/tmp/dyld"))]);
    assert!(macho_linkage(&bad_dylinker, MACOS_ARM).unwrap_err().contains("dynamic linker"));

    let foreign_mac = macho_binary(0x0100_000C, 2, &[(0xC, &macho_str_cmd(b"/opt/homebrew/lib/libz.dylib"))]);
    assert!(macho_linkage(&foreign_mac, MACOS_ARM).unwrap_err().contains("macOS does not ship"));

    let mismatched_fw = macho_binary(
        0x0100_000C,
        2,
        &[(0xC, &macho_str_cmd(b"/System/Library/Frameworks/Foo.framework/Versions/A/Bar"))],
    );
    assert!(macho_linkage(&mismatched_fw, MACOS_ARM).unwrap_err().contains("macOS does not ship"));
}

#[test]
fn receive_digests_sbom_accept_and_cli() {
    let tmp = tempdir();
    let root = tmp.path();
    setup_tree(root);
    let execs = root.join("execs");
    std::fs::create_dir(&execs).unwrap();
    std::fs::write(execs.join("app"), b"elf").unwrap();
    std::fs::write(execs.join("app.exe"), b"pe").unwrap();

    // Create archives for all 6 targets + source archives + SBOMs to test receive/digests/accept.
    let admit_dist = root.join("admit_dist");
    let pub_dist = root.join("pub_dist");
    std::fs::create_dir_all(&admit_dist).unwrap();
    std::fs::create_dir_all(&pub_dist).unwrap();

    let stem = "my-app";
    let version = "1.2.3";
    let names = handoff_names(stem, version).unwrap();
    assert_eq!(names.len(), 2 + TARGETS.len() * 2 + 1);

    let mut digest_args = Vec::new();
    let incoming_all = root.join("incoming_all");
    std::fs::create_dir(&incoming_all).unwrap();

    // Build target archives cleanly and test receive on Linux target.
    let build_out = root.join("built");
    let tgz = package(
        &execs,
        "app",
        LINUX,
        stem,
        version,
        &root.join("crate"),
        root,
        1_700_000_000,
        &build_out,
        "assets/cfg.json",
    )
    .unwrap();
    let tgz_bytes = std::fs::read(&tgz).unwrap();
    let tgz_name = tgz.file_name().unwrap().to_str().unwrap().to_owned();
    let inc_one = root.join("inc_one");
    std::fs::create_dir(&inc_one).unwrap();
    std::fs::write(inc_one.join(&tgz_name), &tgz_bytes).unwrap();
    std::fs::write(
        inc_one.join(format!("{tgz_name}.sha256")),
        format!("{}  {tgz_name}\n", sha256(&tgz_bytes)),
    )
    .unwrap();

    let admitted = receive(
        &inc_one,
        stem,
        version,
        "app",
        LINUX,
        &pub_dist,
        &root.join("recv_ext"),
        &root.join("crate"),
        root,
        "assets/cfg.json",
    )
    .unwrap();
    assert!(admitted.exists());

    // Failure unlinks failed admission, leaves already-existing files untouched.
    assert!(
        receive(
            &inc_one,
            stem,
            version,
            "app",
            LINUX,
            &pub_dist,
            &root.join("recv_ext2"),
            &root.join("crate"),
            root,
            "assets/cfg.json",
        )
        .is_err()
    );
    assert!(admitted.exists());

    let bad_dist = root.join("bad_dist");
    std::fs::write(
        inc_one.join(format!("{tgz_name}.sha256")),
        format!("{}  {tgz_name}\n", "0".repeat(64)),
    )
    .unwrap();
    assert!(
        receive(
            &inc_one,
            stem,
            version,
            "app",
            LINUX,
            &bad_dist,
            &root.join("recv_ext3"),
            &root.join("crate"),
            root,
            "assets/cfg.json",
        )
        .unwrap_err()
        .contains("recorded digest")
    );
    assert!(!bad_dist.join(&tgz_name).exists());

    // Populate all handoff files in admit_dist, incoming_all, and pub_dist for digests/accept.
    let mut map: BTreeMap<String, String> = BTreeMap::new();
    for (role, fname) in &names {
        let data = if role.ends_with("-sbom") {
            json!({"packages": [{"name": "demo"}]}).to_string().into_bytes()
        } else {
            format!("payload:{role}").into_bytes()
        };
        std::fs::write(admit_dist.join(fname), &data).unwrap();
        std::fs::write(incoming_all.join(fname), &data).unwrap();
        if !role.ends_with("-sbom") {
            std::fs::write(pub_dist.join(fname), &data).unwrap();
        }
        map.insert(role.clone(), sha256(&data));
    }

    let computed = digests(&admit_dist, stem, version).unwrap();
    assert_eq!(computed.len(), names.len());
    for line in &computed {
        digest_args.push(line.clone());
    }
    let sbom_path = admit_dist.join(&names[8].1);
    assert!(sbom_lists(&sbom_path, "demo").unwrap());
    assert!(!sbom_lists(&sbom_path, "other").unwrap());

    let kept = accept(&incoming_all, &pub_dist, stem, version, &digest_args).unwrap();
    assert_eq!(kept.len(), names.len());
    assert!(pub_dist.join(&names.last().unwrap().1).exists());

    // Accept refuses duplicate/tampered inputs.
    assert!(accept(&incoming_all, &pub_dist, stem, version, &[]).is_err());
    let mut dup = digest_args.clone();
    dup.push(digest_args[0].clone());
    assert!(accept(&incoming_all, &pub_dist, stem, version, &dup).unwrap_err().contains("repeated"));

    // CLI integration + confined_path + smoke via FakeGh.
    let assets_out = cli(root, &["binary-release", "assets", "--stem", stem, "--version", version], "");
    assert_eq!(assets_out.code, 0);
    assert_eq!(assets_out.stdout.lines().count(), names.len());

    let bad_epoch = cli(
        root,
        &[
            "binary-release",
            "package",
            "--binary",
            "app",
            "--target",
            LINUX,
            "--stem",
            stem,
            "--version",
            version,
            "--epoch",
            "-1",
            "--executables",
            execs.to_str().unwrap(),
            "--source",
            root.join("crate").to_str().unwrap(),
            "--root",
            root.to_str().unwrap(),
            "--output-dir",
            build_out.to_str().unwrap(),
        ],
        "",
    );
    assert_eq!(bad_epoch.code, 1);
    assert_eq!(bad_epoch.stderr, "binary_release: epoch is not a Unix time: '-1'\n");

    let sbom_ok = cli(
        root,
        &[
            "binary_release",
            "sbom-lists",
            "--package",
            "demo",
            "--sbom",
            sbom_path.to_str().unwrap(),
        ],
        "",
    );
    assert_eq!(sbom_ok.code, 0);
    assert_eq!(
        sbom_ok.stdout,
        format!("{} lists demo\n", sbom_path.file_name().unwrap().to_str().unwrap())
    );

    let fake = FakeGh::program(
        "smoke-app",
        json!([rule(&["--version"], "smoke-app 1.2.3\nextra\n", "", 0)]),
    );
    let smoke_exe = fake.bin().join(format!("smoke-app{}", std::env::consts::EXE_SUFFIX));
    let mut env_pairs = fake.env();
    env_pairs.push(("RUNNER_TEMP".into(), fake.bin().parent().unwrap().to_string_lossy().into_owned()));
    let env_refs: Vec<(&str, &str)> = env_pairs.iter().map(|(k, v)| (k.as_str(), v.as_str())).collect();
    let smoke_out = cli_env(
        root,
        &[
            "binary-release",
            "smoke",
            "--version",
            "1.2.3",
            "--executable",
            smoke_exe.to_str().unwrap(),
        ],
        "",
        &env_refs,
    );
    assert_eq!(smoke_out.code, 0, "stderr: {}", smoke_out.stderr);
    assert_eq!(smoke_out.stdout, "smoke-app 1.2.3\n");

    // Bad version from smoke
    fake.set_rules(json!([rule(&["--version"], "smoke-app 9.9.9\n", "", 0)]));
    let smoke_bad = cli_env(
        root,
        &[
            "binary-release",
            "smoke",
            "--version",
            "1.2.3",
            "--executable",
            smoke_exe.to_str().unwrap(),
        ],
        "",
        &env_refs,
    );
    assert_eq!(smoke_bad.code, 1);
    assert!(smoke_bad.stderr.contains("expected a first line ending in ' 1.2.3'"));

    // Outside trusted base refused by confined_path
    let outside_base = if cfg!(windows) { r"C:\outside\nowhere" } else { "/etc/passwd" };
    assert!(
        confined_path(outside_base)
            .unwrap_err()
            .contains("outside the working, workspace and temporary directories")
    );
    let _ = resolve_path(root);
    let _ = MAX_MEMBERS;
    let _ = smoke_with_timeout(&smoke_exe, "1.2.3", Duration::from_secs(5));
}
