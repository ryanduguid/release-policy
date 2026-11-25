# Copyright 2026 Ryan Duguid
# SPDX-License-Identifier: Apache-2.0

"""Differential parity harness: scripts/binary_release.py vs `release-policy binary-release`."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "binary_release.py"
LINUX = "x86_64-unknown-linux-musl"
MACOS_ARM = "aarch64-apple-darwin"
WIN = "x86_64-pc-windows-msvc"


def find_binary(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    env = os.environ.get("RELEASE_POLICY_BIN")
    if env:
        return Path(env).resolve()
    for profile in ("release", "debug"):
        candidate = ROOT / "target" / profile / "release-policy"
        if candidate.exists():
            return candidate
    raise FileNotFoundError("pass --binary or set RELEASE_POLICY_BIN")


def run_both(
    binary: Path, cwd: Path, args: list[str], env: dict[str, str] | None = None
) -> tuple[subprocess.CompletedProcess[str], subprocess.CompletedProcess[str]]:
    run_env = env if env is not None else dict(os.environ)
    py = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        env=run_env,
        text=True,
        capture_output=True,
    )
    rs = subprocess.run(
        [str(binary), "binary-release", *args],
        cwd=cwd,
        env=run_env,
        text=True,
        capture_output=True,
    )
    return py, rs


def assert_parity(
    binary: Path, cwd: Path, args: list[str], env: dict[str, str] | None = None
) -> None:
    py, rs = run_both(binary, cwd, args, env=env)
    assert py.returncode == rs.returncode, (
        f"{args}: rc py={py.returncode} rs={rs.returncode}\n"
        f"py={py.stderr}\nrs={rs.stderr}"
    )
    assert py.stdout == rs.stdout, f"{args}: stdout mismatch\npy={py.stdout!r}\nrs={rs.stdout!r}"
    if py.returncode != 0:
        assert rs.stderr.startswith("binary_release: ") or py.returncode == 2
        if py.returncode == 1:
            assert py.stderr == rs.stderr, (
                f"{args}: stderr mismatch\npy={py.stderr!r}\nrs={rs.stderr!r}"
            )


def macho_file(cputype: int, commands: list[tuple[int, bytes]]) -> bytes:
    body = b"".join(
        struct.pack("<II", cmd, 8 + len(payload)) + payload for cmd, payload in commands
    )
    header = struct.pack("<8I", 0xFEEDFACF, cputype, 0, 2, len(commands), len(body), 0, 0)
    return header + body


def macho_str(text: bytes) -> bytes:
    raw = text + b"\0"
    pad = (-len(raw)) % 8
    return struct.pack("<I", 24) + (b"\0" * 12) + raw + (b"\0" * pad)


def write_tar_gz(path: Path, entries: list[tuple[str, bytes, bytes, int]]) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        for name, data, kind, mode in entries:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.size = len(data)
            info.mode = mode
            info.mtime = 0
            tf.addfile(info, io.BytesIO(data))
    with gzip.GzipFile(path, mode="wb", mtime=0) as gz:
        gz.write(buf.getvalue())


def archive_signature(path: Path, is_zip: bool) -> list[tuple[str, bytes, int]]:
    if is_zip:
        with zipfile.ZipFile(path) as zf:
            return sorted(
                (i.filename, zf.read(i), (i.external_attr >> 16) & 0o7777)
                for i in zf.infolist()
            )
    with tarfile.open(path, "r:gz") as tf:
        return sorted(
            (m.name, tf.extractfile(m).read(), m.mode & 0o7777) for m in tf.getmembers()
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", default=None)
    args = parser.parse_args()
    binary = find_binary(args.binary)

    compared = 0
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        ws = tmp / "ws"
        ws.mkdir()
        src = ws / "comp"
        (src / "sub").mkdir(parents=True)
        (ws / "LICENSE").write_text("root lic\n", encoding="utf-8")
        (ws / "README.md").write_text("root readme\n", encoding="utf-8")
        (src / "LICENSE").write_text("comp lic\n", encoding="utf-8")
        (src / "README.md").write_text("comp readme\n", encoding="utf-8")
        (src / "NOTICE").write_text("notice\n", encoding="utf-8")
        (src / "sub" / "data.txt").write_text("data\n", encoding="utf-8")
        manifest = src / "Cargo.toml"
        manifest.write_text(
            '[package]\nname = "demo"\nversion = "1.2.3"\npublish = false\n',
            encoding="utf-8",
        )
        (ws / "rust-toolchain.toml").write_text(
            '[toolchain]\nchannel = "1.99.1"\n', encoding="utf-8"
        )
        execs = ws / "execs"
        execs.mkdir()
        (execs / "demo").write_bytes(b"#!/bin/sh\necho demo 1.2.3\n")
        (execs / "demo").chmod(0o755)
        (execs / "demo.exe").write_bytes(b"MZ")

        env = dict(os.environ)
        env["GITHUB_WORKSPACE"] = str(ws)

        bad_manifest = ws / "bad_Cargo.toml"
        bad_manifest.write_text('[package]\nname = "demo"\nversion = "1.2.3"\n', encoding="utf-8")
        ws_manifest = ws / "ws_Cargo.toml"
        ws_manifest.write_text(
            '[package]\nname = "demo"\nversion.workspace = true\n', encoding="utf-8"
        )
        for cmd in (
            ["cargo-package", "--version", "1.2.3", "--binary", "demo", "--manifest", str(manifest)],
            ["cargo-package", "--version", "1.2.4", "--binary", "demo", "--manifest", str(manifest)],
            ["cargo-package", "--version", "1.2.3", "--binary", "other", "--manifest", str(manifest)],
            [
                "cargo-package",
                "--version",
                "1.2.3",
                "--binary",
                "demo",
                "--manifest",
                str(bad_manifest),
            ],
            [
                "cargo-package",
                "--version",
                "1.2.3",
                "--binary",
                "demo",
                "--manifest",
                str(ws_manifest),
            ],
        ):
            assert_parity(binary, ws, cmd, env=env)
            compared += 1

        assert_parity(binary, ws, ["toolchain", "--source", str(src), "--root", str(ws)], env=env)
        compared += 1
        assert_parity(
            binary, ws, ["toolchain", "--source", str(ws.parent), "--root", str(ws)], env=env
        )
        compared += 1
        (src / "rust-toolchain").write_text("1.99.1\n", encoding="utf-8")
        assert_parity(binary, ws, ["toolchain", "--source", str(src), "--root", str(ws)], env=env)
        (src / "rust-toolchain").unlink()
        compared += 1

        assert_parity(binary, ws, ["assets", "--stem", "demo-app", "--version", "1.2.3"], env=env)
        compared += 1

        for triple, is_zip in ((LINUX, False), (WIN, True)):
            py_out = ws / f"py_out_{triple}"
            rs_out = ws / f"rs_out_{triple}"
            pkg_base = [
                "package",
                "--binary",
                "demo",
                "--target",
                triple,
                "--stem",
                "demo-app",
                "--version",
                "1.2.3",
                "--epoch",
                "1700000000",
                "--executables",
                str(execs),
                "--source",
                str(src),
                "--root",
                str(ws),
                "--data-files",
                "sub/data.txt",
            ]
            py_p = subprocess.run(
                [sys.executable, str(SCRIPT), *pkg_base, "--output-dir", str(py_out)],
                cwd=ws,
                env=env,
                text=True,
                capture_output=True,
            )
            rs_p = subprocess.run(
                [str(binary), "binary-release", *pkg_base, "--output-dir", str(rs_out)],
                cwd=ws,
                env=env,
                text=True,
                capture_output=True,
            )
            assert py_p.returncode == rs_p.returncode == 0, (py_p, rs_p)
            py_arc = Path(py_p.stdout.strip())
            rs_arc = Path(rs_p.stdout.strip())
            assert py_arc.name == rs_arc.name
            assert archive_signature(py_arc, is_zip) == archive_signature(rs_arc, is_zip)
            compared += 1

            for arc in (py_arc, rs_arc):
                assert_parity(
                    binary,
                    ws,
                    [
                        "verify",
                        "--binary",
                        "demo",
                        "--target",
                        triple,
                        "--archive",
                        str(arc),
                        "--data-files",
                        "sub/data.txt",
                    ],
                    env=env,
                )
                compared += 1

        assert_parity(
            binary,
            ws,
            [
                "package",
                "--binary",
                "demo",
                "--target",
                LINUX,
                "--stem",
                "demo-app",
                "--version",
                "1.2.3",
                "--epoch",
                "-5",
                "--executables",
                str(execs),
                "--source",
                str(src),
                "--root",
                str(ws),
                "--output-dir",
                str(ws / "bad_out"),
            ],
            env=env,
        )
        compared += 1

        pax_tar = ws / "pax.tar.gz"
        b = io.BytesIO()
        with tarfile.open(fileobj=b, mode="w", format=tarfile.PAX_FORMAT) as tf:
            info = tarfile.TarInfo("demo")
            info.size = 1
            info.mode = 0o755
            info.pax_headers = {"comment": "hi"}
            tf.addfile(info, io.BytesIO(b"x"))
        pax_tar.write_bytes(gzip.compress(b.getvalue()))
        for trap_tar in (pax_tar,):
            assert_parity(
                binary,
                ws,
                ["verify", "--binary", "demo", "--target", LINUX, "--archive", str(trap_tar)],
                env=env,
            )
            compared += 1

        trav_tar = ws / "trav.tar.gz"
        write_tar_gz(trav_tar, [("../demo", b"x", tarfile.REGTYPE, 0o755)])
        sym_tar = ws / "sym.tar.gz"
        write_tar_gz(sym_tar, [("demo", b"", tarfile.SYMTYPE, 0o777)])
        for trap_tar in (trav_tar, sym_tar):
            assert_parity(
                binary,
                ws,
                ["verify", "--binary", "demo", "--target", LINUX, "--archive", str(trap_tar)],
                env=env,
            )
            compared += 1

        zip64 = ws / "z64.zip"
        with zipfile.ZipFile(zip64, "w") as zf:
            zi = zipfile.ZipInfo("demo.exe")
            zi.file_size = 1
            zf.writestr(zi, b"x")
        raw_z = zip64.read_bytes()
        idx = raw_z.rfind(b"PK\x05\x06")
        zip64.write_bytes(raw_z[:idx] + b"PK\x06\x07" + (b"\0" * 16) + raw_z[idx:])

        trav_zip = ws / "trav.zip"
        with zipfile.ZipFile(trav_zip, "w") as zf:
            zf.writestr("../demo.exe", b"x")
        for trap_zip in (zip64, trav_zip):
            assert_parity(
                binary,
                ws,
                ["verify", "--binary", "demo", "--target", WIN, "--archive", str(trap_zip)],
                env=env,
            )
            compared += 1

        for ver in ("1.2.3", "9.9.9"):
            assert_parity(
                binary,
                ws,
                ["smoke", "--version", ver, "--executable", str(execs / "demo")],
                env=env,
            )
            compared += 1

        for label, txt, target in (
            ("musl_ok", "Format: elf64-x86-64\nArch: x86_64\nNeededLibraries [\n]\n", LINUX),
            (
                "musl_dyn",
                "Format: elf64-x86-64\nArch: x86_64\nNeededLibraries [\n  libc.so.6\n]\n",
                LINUX,
            ),
            (
                "win_ok",
                "Format: COFF-x86-64\nArch: x86_64\n"
                "NeededLibraries [\n  KERNEL32.dll\n  api-ms-win-crt-math-l1-1-0.dll\n]\n",
                WIN,
            ),
            (
                "win_crt",
                "Format: COFF-x86-64\nArch: x86_64\nNeededLibraries [\n  VCRUNTIME140.dll\n]\n",
                WIN,
            ),
            (
                "win_foreign",
                "Format: COFF-x86-64\nArch: x86_64\nNeededLibraries [\n  evil.dll\n]\n",
                WIN,
            ),
        ):
            ro = ws / f"{label}.txt"
            ro.write_text(txt, encoding="utf-8")
            assert_parity(
                binary,
                ws,
                ["linkage", "--target", target, "--readobj-output", str(ro)],
                env=env,
            )
            compared += 1

        macho_ok = ws / "mach_ok"
        macho_ok.write_bytes(
            macho_file(
                0x0100000C,
                [
                    (0xE, macho_str(b"/usr/lib/dyld")),
                    (0xC, macho_str(b"/usr/lib/libSystem.B.dylib")),
                ],
            )
        )
        macho_bad = ws / "mach_rpath"
        macho_bad.write_bytes(macho_file(0x0100000C, [(0x8000001C, macho_str(b"@rpath/x"))]))
        for mp in (macho_ok, macho_bad):
            assert_parity(
                binary,
                ws,
                ["macho-linkage", "--target", MACOS_ARM, "--executable", str(mp)],
                env=env,
            )
            compared += 1

        sbom = ws / "demo.spdx.json"
        sbom.write_text(json.dumps({"packages": [{"name": "demo"}]}), encoding="utf-8")
        for pkg in ("demo", "other"):
            assert_parity(
                binary,
                ws,
                ["sbom-lists", "--package", pkg, "--sbom", str(sbom)],
                env=env,
            )
            compared += 1

        arc_src = ws / f"py_out_{LINUX}" / f"demo-app-1.2.3-{LINUX}.tar.gz"
        inc = ws / "inc"
        inc.mkdir()
        shutil.copy2(arc_src, inc / arc_src.name)
        digest = hashlib.sha256(arc_src.read_bytes()).hexdigest()
        (inc / f"{arc_src.name}.sha256").write_text(
            f"{digest}  {arc_src.name}\n", encoding="utf-8"
        )
        py_dist = ws / "py_dist"
        rs_dist = ws / "rs_dist"
        recv_common = [
            "receive",
            "--stem",
            "demo-app",
            "--version",
            "1.2.3",
            "--binary",
            "demo",
            "--target",
            LINUX,
            "--incoming",
            str(inc),
            "--source",
            str(src),
            "--root",
            str(ws),
            "--data-files",
            "sub/data.txt",
        ]
        py_r = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                *recv_common,
                "--dist",
                str(py_dist),
                "--extract-dir",
                str(ws / "py_ext"),
            ],
            cwd=ws,
            env=env,
            text=True,
            capture_output=True,
        )
        rs_r = subprocess.run(
            [
                str(binary),
                "binary-release",
                *recv_common,
                "--dist",
                str(rs_dist),
                "--extract-dir",
                str(ws / "rs_ext"),
            ],
            cwd=ws,
            env=env,
            text=True,
            capture_output=True,
        )
        assert py_r.returncode == rs_r.returncode == 0, (py_r, rs_r)
        assert Path(py_r.stdout.strip()).name == Path(rs_r.stdout.strip()).name
        compared += 1

        admit = ws / "admit"
        admit.mkdir()
        pub_py = ws / "pub_py"
        pub_rs = ws / "pub_rs"
        pub_py.mkdir()
        pub_rs.mkdir()
        inc_acc = ws / "inc_acc"
        inc_acc.mkdir()
        assets_list = subprocess.run(
            [sys.executable, str(SCRIPT), "assets", "--stem", "demo-app", "--version", "1.2.3"],
            cwd=ws,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.splitlines()
        for fname in assets_list:
            data = (
                json.dumps({"packages": [{"name": "demo"}]}).encode("utf-8")
                if fname.endswith(".spdx.json")
                else f"bytes:{fname}".encode("utf-8")
            )
            (admit / fname).write_bytes(data)
            (inc_acc / fname).write_bytes(data)
            if not fname.endswith(".spdx.json"):
                (pub_py / fname).write_bytes(data)
                (pub_rs / fname).write_bytes(data)
        d_cmd = ["digests", "--stem", "demo-app", "--version", "1.2.3", "--dist", str(admit)]
        assert_parity(binary, ws, d_cmd, env=env)
        compared += 1
        d_lines = subprocess.run(
            [str(binary), "binary-release", *d_cmd],
            cwd=ws,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.splitlines()
        acc_args = ["accept", "--stem", "demo-app", "--version", "1.2.3", "--incoming", str(inc_acc)]
        for dl in d_lines:
            acc_args.extend(["--digest", dl])
        py_a = subprocess.run(
            [sys.executable, str(SCRIPT), *acc_args, "--dist", str(pub_py)],
            cwd=ws,
            env=env,
            text=True,
            capture_output=True,
        )
        rs_a = subprocess.run(
            [str(binary), "binary-release", *acc_args, "--dist", str(pub_rs)],
            cwd=ws,
            env=env,
            text=True,
            capture_output=True,
        )
        assert py_a.returncode == rs_a.returncode == 0
        assert py_a.stdout == rs_a.stdout
        compared += 1

        link = ws / "sym_cargo.toml"
        link.symlink_to(manifest)
        assert_parity(
            binary,
            ws,
            ["cargo-package", "--version", "1.2.3", "--binary", "demo", "--manifest", str(link)],
            env=env,
        )
        compared += 1
        assert_parity(
            binary,
            ws,
            ["sbom-lists", "--package", "demo", "--sbom", "/etc/hosts"],
            env=env,
        )
        compared += 1

    print(f"binary-release parity: {compared} inputs compared, 0 unexplained differences")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
