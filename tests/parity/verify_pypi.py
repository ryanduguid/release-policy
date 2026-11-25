# Copyright 2026 Ryan Duguid
# SPDX-License-Identifier: Apache-2.0

"""Differential parity harness: scripts/verify_pypi.py vs `release-policy verify-pypi`."""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMMIT = "1" * 40
REPOSITORY = "example-owner/demo-pkg"
WORKFLOW = "release.yml"
WHEEL = "demo_pkg-1.2.3-py3-none-any.whl"
SDIST = "demo_pkg-1.2.3.tar.gz"
DIGEST_OID = bytes.fromhex("2b0601040183bf30010d")
MAX_BYTES = 16 * 1024 * 1024

STUB_PKG_MAIN = """import json
import os
import sys
from pathlib import Path

cfg = json.loads(Path(os.environ["FAKE_ATTEST_SCENARIO"]).read_text(encoding="utf-8"))
Path(cfg["log"]).write_text(
    json.dumps(["-m", "pypi_attestations", *sys.argv[1:]]), encoding="utf-8"
)
sys.stderr.write(cfg.get("stderr", ""))
sys.exit(cfg.get("code", 0))
"""


def find_binary(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    env = os.environ.get("RELEASE_POLICY_BIN")
    if env:
        return Path(env).resolve()
    exe = "release-policy.exe" if os.name == "nt" else "release-policy"
    for profile in ("release", "debug"):
        candidate = ROOT / "target" / profile / exe
        if candidate.exists():
            return candidate
    raise FileNotFoundError("pass --binary or set RELEASE_POLICY_BIN")


def find_openssl() -> str | None:
    found = shutil.which("openssl")
    if found:
        return found
    if os.name == "nt":
        git_bin = shutil.which("git")
        if git_bin:
            candidate = Path(git_bin).resolve().parent.parent / "usr" / "bin" / "openssl.exe"
            if candidate.is_file():
                return str(candidate)
    return None


def der(tag: int, contents: bytes) -> bytes:
    n = len(contents)
    if n < 0x80:
        return bytes([tag, n]) + contents
    size = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([tag, 0x80 | len(size)]) + size + contents


def cert(commit: bytes | None = COMMIT.encode(), text_tag: int = 0x0C) -> bytes:
    exts = b""
    if commit is not None:
        exts = der(0x30, der(0x06, DIGEST_OID) + der(0x04, der(text_tag, commit)))
    tbs = der(0x30, der(0x02, b"\x01") + der(0xA3, der(0x30, exts)))
    return der(0x30, tbs + der(0x30, b"") + der(0x03, b"\x00"))


def provenance(
    c: bytes | None = None,
    *,
    kind: str = "GitHub",
    repository: str = REPOSITORY,
    workflow: str = WORKFLOW,
) -> bytes:
    raw_cert = c if c is not None else cert()
    return json.dumps(
        {
            "version": 1,
            "attestation_bundles": [
                {
                    "publisher": {"kind": kind, "repository": repository, "workflow": workflow},
                    "attestations": [
                        {
                            "version": 1,
                            "verification_material": {
                                "certificate": base64.b64encode(raw_cert).decode("ascii")
                            },
                        }
                    ],
                }
            ],
        }
    ).encode("utf-8")


def release_json(files: dict[str, bytes]) -> bytes:
    urls = [
        {"filename": n, "digests": {"sha256": hashlib.sha256(d).hexdigest()}}
        for n, d in files.items()
    ]
    return json.dumps({"info": {"version": "1.2.3"}, "urls": urls}).encode("utf-8")


class MockServer:
    def __init__(self, certfile: Path | None = None, keyfile: Path | None = None) -> None:
        self.routes: dict[str, list[tuple[int, dict[str, str], bytes]]] = {}
        self.requests: list[tuple[str, str]] = []
        parent = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                accept = self.headers.get("Accept", "")
                parent.requests.append((self.path, accept))
                seq = parent.routes.get(self.path)
                if not seq:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status, headers, body = seq.pop(0) if len(seq) > 1 else seq[0]
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except BrokenPipeError:
                    pass

            def log_message(self, *_: object) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        if certfile and keyfile:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile=str(certfile), keyfile=str(keyfile))
            self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.scheme = "https" if certfile else "http"
        self.port = self.server.server_port
        self.base_url = f"{self.scheme}://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def run_py(
    cli_args: list[str],
    cwd: Path,
    base_url: str,
    attempts: int,
    delay_s: float,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    script_dir = str(ROOT / "scripts")
    code = (
        "import sys; "
        f"sys.path.insert(0, {script_dir!r}); "
        "import verify_pypi; "
        f"verify_pypi._PYPI = {base_url!r}; "
        f"verify_pypi._ATTEMPTS = {attempts}; "
        f"verify_pypi._DELAY = {delay_s}; "
        "raise SystemExit(verify_pypi.main(sys.argv[1:]))"
    )
    return subprocess.run(
        [sys.executable, "-c", code, *cli_args],
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )


def run_rs(
    binary: Path,
    cli_args: list[str],
    cwd: Path,
    base_url: str,
    attempts: int,
    delay_ms: int,
    ca_cert: Path | None,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    extra = ["--base-url", base_url, "--attempts", str(attempts), "--delay-ms", str(delay_ms)]
    if ca_cert is not None:
        extra.extend(["--ca-cert", str(ca_cert)])
    return subprocess.run(
        [str(binary), "verify-pypi", *cli_args, *extra],
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )


def compare_case(
    binary: Path,
    server: MockServer,
    cwd: Path,
    cli_args: list[str],
    routes: dict[str, list[tuple[int, dict[str, str], bytes]]],
    stub_path: Path,
    stub_pkg: Path,
    scenario_file: Path,
    attest_cfg: dict,
    *,
    attempts: int = 3,
    delay_ms: int = 10,
    ca_cert: Path | None = None,
) -> None:
    def prep_env(log_path: Path) -> dict[str, str]:
        scenario_file.write_text(json.dumps({**attest_cfg, "log": str(log_path)}), encoding="utf-8")
        env = dict(os.environ)
        env["PATH"] = f"{stub_path}{os.pathsep}{env.get('PATH', '')}"
        existing_py = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{stub_pkg}{os.pathsep}{existing_py}" if existing_py else str(stub_pkg)
        env["FAKE_ATTEST_SCENARIO"] = str(scenario_file)
        if ca_cert is not None:
            env["SSL_CERT_FILE"] = str(ca_cert)
        return env

    py_log = cwd / "py_attest.json"
    rs_log = cwd / "rs_attest.json"
    for f in (py_log, rs_log):
        if f.exists():
            f.unlink()

    server.routes = {k: list(v) for k, v in routes.items()}
    server.requests.clear()
    py = run_py(cli_args, cwd, server.base_url, attempts, delay_ms / 1000.0, prep_env(py_log))
    py_reqs = list(server.requests)

    server.routes = {k: list(v) for k, v in routes.items()}
    server.requests.clear()
    rs = run_rs(
        binary, cli_args, cwd, server.base_url, attempts, delay_ms, ca_cert, prep_env(rs_log)
    )
    rs_reqs = list(server.requests)

    assert py.returncode == rs.returncode, (py, rs)
    assert py.stdout == rs.stdout, (py.stdout, rs.stdout)
    assert py.stderr == rs.stderr, (py.stderr, rs.stderr)
    assert py_reqs == rs_reqs, (py_reqs, rs_reqs)
    assert py_log.exists() == rs_log.exists()
    if py_log.exists():
        # Both pass: -m pypi_attestations verify pypi --repository ... --provenance-file <tmp> <file>.
        pa = json.loads(py_log.read_text(encoding="utf-8"))
        ra = json.loads(rs_log.read_text(encoding="utf-8"))
        assert pa[:7] == ra[:7] and Path(pa[7]).name == Path(ra[7]).name and pa[8:] == ra[8:], (
            pa,
            ra,
        )


def make_loopback_cert(openssl: str, directory: Path) -> tuple[Path, Path, Path]:
    ca_key = directory / "ca-key.pem"
    ca_cert = directory / "ca.pem"
    cert_p = directory / "cert.pem"
    key_p = directory / "key.pem"
    csr_p = directory / "server.csr"
    ext_p = directory / "ext.cnf"
    ossl_env = {**os.environ, "MSYS_NO_PATHCONV": "1", "MSYS2_ARG_CONV_EXCL": "*"}
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(ca_key),
            "-out",
            str(ca_cert),
            "-days",
            "1",
            "-subj",
            "/CN=Loopback-Test-CA",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "subjectKeyIdentifier=hash",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
        ],
        check=True,
        capture_output=True,
        env=ossl_env,
    )
    subprocess.run(
        [
            openssl,
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key_p),
            "-out",
            str(csr_p),
            "-subj",
            "/CN=127.0.0.1",
        ],
        check=True,
        capture_output=True,
        env=ossl_env,
    )
    ext_p.write_text(
        "basicConstraints=critical,CA:FALSE\n"
        "subjectKeyIdentifier=hash\n"
        "authorityKeyIdentifier=keyid,issuer\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        "subjectAltName=IP:127.0.0.1\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            openssl,
            "x509",
            "-req",
            "-in",
            str(csr_p),
            "-CA",
            str(ca_cert),
            "-CAkey",
            str(ca_key),
            "-CAcreateserial",
            "-out",
            str(cert_p),
            "-days",
            "1",
            "-extfile",
            str(ext_p),
        ],
        check=True,
        capture_output=True,
        env=ossl_env,
    )
    return ca_cert, cert_p, key_p


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", default=None)
    args = parser.parse_args()
    binary = find_binary(args.binary)

    compared = 0
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        stub_pkg = tmp / "stub_pkg"
        pkg_dir = stub_pkg / "pypi_attestations"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text("", encoding="utf-8", newline="\n")
        (pkg_dir / "__main__.py").write_text(STUB_PKG_MAIN, encoding="utf-8", newline="\n")
        if os.name == "nt":
            stub_path = Path(sys.executable).resolve().parent
        else:
            stub_path = tmp / "bin"
            stub_path.mkdir()
            stub_py = stub_path / "python"
            stub_py.write_text(
                f"#!/bin/sh\nexec '{sys.executable}' \"$@\"\n",
                encoding="utf-8",
                newline="\n",
            )
            stub_py.chmod(0o755)
        scenario = tmp / "attest.json"

        whl = tmp / WHEEL
        sdist = tmp / SDIST
        whl.write_bytes(b"wheel bytes")
        sdist.write_bytes(b"sdist bytes")
        built = {WHEEL: whl.read_bytes(), SDIST: sdist.read_bytes()}

        base_args = [
            "--project",
            "demo_pkg",
            "--version",
            "1.2.3",
            "--repository",
            REPOSITORY,
            "--commit",
            COMMIT,
            "--workflow",
            WORKFLOW,
            "--file",
            str(whl),
            "--file",
            str(sdist),
        ]
        rel_path = "/pypi/demo-pkg/1.2.3/json"
        whl_prov = f"/integrity/demo-pkg/1.2.3/{WHEEL}/provenance"
        sdist_prov = f"/integrity/demo-pkg/1.2.3/{SDIST}/provenance"
        ok_routes = {
            rel_path: [(200, {}, release_json(built))],
            whl_prov: [(200, {}, provenance())],
            sdist_prov: [(200, {}, provenance())],
        }

        http_srv = MockServer()
        openssl = find_openssl()
        https_srv: MockServer | None = None
        ca_p: Path | None = None
        if openssl is not None:
            ca_p, cert_p, key_p = make_loopback_cert(openssl, tmp)
            https_srv = MockServer(certfile=cert_p, keyfile=key_p)
        try:
            # 1-2: happy path over HTTP AND over HTTPS (ureq + rustls TLS handshake)
            compare_case(
                binary,
                http_srv,
                tmp,
                base_args,
                ok_routes,
                stub_path,
                stub_pkg,
                scenario,
                {"code": 0},
            )
            compared += 1
            if https_srv is not None:
                compare_case(
                    binary,
                    https_srv,
                    tmp,
                    base_args,
                    ok_routes,
                    stub_path,
                    stub_pkg,
                    scenario,
                    {"code": 0},
                    ca_cert=ca_p,
                )
                compared += 1

            # 3: retry 404 -> partial -> complete
            retry_routes = {
                rel_path: [
                    (404, {}, b""),
                    (200, {}, release_json({WHEEL: built[WHEEL]})),
                    (200, {}, release_json(built)),
                ],
                whl_prov: [(200, {}, provenance())],
                sdist_prov: [(200, {}, provenance())],
            }
            compare_case(
                binary,
                http_srv,
                tmp,
                base_args,
                retry_routes,
                stub_path,
                stub_pkg,
                scenario,
                {"code": 0},
            )
            compared += 1

            # 4: missing file after retries, extra file (no retry), hash mismatch
            extra_whl = {**built, "demo_pkg-1.2.3-py2-none-any.whl": b"x"}
            for routes in (
                {rel_path: [(200, {}, release_json({WHEEL: built[WHEEL]}))]},
                {rel_path: [(200, {}, release_json(extra_whl))]},
                {rel_path: [(200, {}, release_json({WHEEL: built[WHEEL], SDIST: b"bad"}))]},
                {rel_path: [(404, {}, b"")]},
                {rel_path: [(503, {}, b"")]},
                {rel_path: [(302, {"Location": f"{http_srv.base_url}/other"}, b"")]},
                {rel_path: [(200, {}, b"x" * (MAX_BYTES + 1))]},
                {rel_path: [(200, {}, b"{bad")]},
                {
                    rel_path: [(200, {}, release_json(built))],
                    sdist_prov: [(404, {}, b"")],
                    whl_prov: [(200, {}, provenance())],
                },
                {
                    rel_path: [(200, {}, release_json(built))],
                    sdist_prov: [(200, {}, provenance(repository="other/repo"))],
                    whl_prov: [(200, {}, provenance())],
                },
                {
                    rel_path: [(200, {}, release_json(built))],
                    sdist_prov: [(200, {}, provenance(cert(b"2" * 40)))],
                    whl_prov: [(200, {}, provenance())],
                },
                {
                    rel_path: [(200, {}, release_json(built))],
                    sdist_prov: [(200, {}, provenance(cert(None)))],
                    whl_prov: [(200, {}, provenance())],
                },
                {
                    rel_path: [(200, {}, release_json(built))],
                    sdist_prov: [(200, {}, provenance(cert(text_tag=0x13)))],
                    whl_prov: [(200, {}, provenance())],
                },
            ):
                compare_case(
                    binary,
                    http_srv,
                    tmp,
                    base_args,
                    routes,
                    stub_path,
                    stub_pkg,
                    scenario,
                    {"code": 0},
                )
                compared += 1

            # 17-18: pypi-attestations failure (with message and empty stderr)
            attest_cases = (
                {"code": 1, "stderr": "debug\nbad signature\n"},
                {"code": 1, "stderr": ""},
            )
            for attest in attest_cases:
                compare_case(
                    binary,
                    http_srv,
                    tmp,
                    base_args,
                    ok_routes,
                    stub_path,
                    stub_pkg,
                    scenario,
                    attest,
                )
                compared += 1

            # 19-24: pre-request input refusals
            for overrides in (
                ["--project", "../x"],
                ["--version", "1/2"],
                ["--repository", "bad"],
                ["--commit", "short"],
                ["--workflow", "dir/rel.yml"],
                ["--file", str(tmp / "absent.whl")],
            ):
                flag = overrides[0]
                idx = base_args.index(flag)
                mod = list(base_args)
                mod[idx + 1] = overrides[1]
                compare_case(
                    binary,
                    http_srv,
                    tmp,
                    mod,
                    ok_routes,
                    stub_path,
                    stub_pkg,
                    scenario,
                    {"code": 0},
                )
                compared += 1
        finally:
            http_srv.close()
            if https_srv is not None:
                https_srv.close()

    print(f"verify-pypi parity: {compared} inputs compared, 0 unexplained differences")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
