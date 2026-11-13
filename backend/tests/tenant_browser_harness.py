from __future__ import annotations

import datetime
import ipaddress
import os
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from io import BytesIO
from pathlib import Path

import httpx
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[2]


def _certificate(folder: Path) -> tuple[Path, Path]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=2))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            ]),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )
    key_path = folder / "key.pem"
    cert_path = folder / "cert.pem"
    key_path.write_bytes(private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def run_tenant_browser_smoke(
    *,
    email: str,
    password: str,
    tenant_app_factory: Callable[[str], FastAPI],
    simulate_groq_timeout: bool = False,
    review_supplier_batch: bool = False,
    portfolio_sample: bool = False,
    large_review_fixture: dict[str, object] | None = None,
) -> str:
    if os.environ.get("KP_DESK_BROWSER_E2E") != "1":
        raise RuntimeError("Browser smoke is strictly opt-in.")
    assets = ROOT / "frontend" / "dist"
    index = assets / "index.html"
    if not index.is_file():
        raise RuntimeError("Run npm run build in frontend first.")

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"https://127.0.0.1:{port}"
    app = tenant_app_factory(url)
    app.mount("/assets", StaticFiles(directory=str(assets / "assets")), name="e2e-assets")
    if large_review_fixture is not None:
        # Only this ephemeral loopback TLS server can serve the fixture.
        test_assets = ROOT / "frontend" / "dist-e2e"
        test_index = test_assets / "e2e" / "large-review" / "index.html"
        if not test_index.is_file():
            raise RuntimeError("Build the isolated A91 React fixture first.")

        @app.get("/__e2e/fixture", include_in_schema=False)
        def isolated_large_review_fixture() -> JSONResponse:
            return JSONResponse(large_review_fixture, headers={
                "Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow",
            })

        app.mount("/__e2e", StaticFiles(
            directory=str(test_assets), html=True,
        ), name="isolated-a91-react-assets")

    @app.get("/{path:path}", include_in_schema=False)
    def browser_index(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(404)
        resource = assets / path
        if path and resource.is_file() and resource.resolve().is_relative_to(assets.resolve()):
            return FileResponse(resource)
        nested = resource / "index.html"
        if path and nested.is_file() and nested.resolve().is_relative_to(assets.resolve()):
            return FileResponse(nested, media_type="text/html")
        if path.startswith("app/"):
            return FileResponse(assets / "app" / "index.html", media_type="text/html")
        if path.startswith("auth/"):
            return FileResponse(assets / "auth" / "login" / "index.html", media_type="text/html")
        if not path:
            return FileResponse(index, media_type="text/html")
        return FileResponse(assets / "404.html", media_type="text/html", status_code=404)

    with tempfile.TemporaryDirectory(prefix="kpdesk-https-e2e-") as directory:
        cert_path, key_path = _certificate(Path(directory))
        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=port,
            ssl_certfile=str(cert_path), ssl_keyfile=str(key_path),
            log_level="warning", access_log=False,
            timeout_keep_alive=2, timeout_graceful_shutdown=4,
        ))
        runner = threading.Thread(target=server.run, name="kpdesk-https-e2e", daemon=True)
        runner.start()
        try:
            for _ in range(80):
                if server.started:
                    break
                if not runner.is_alive():
                    raise RuntimeError("Isolated HTTPS tenant server exited.")
                time.sleep(0.1)
            if not server.started:
                raise RuntimeError("Isolated HTTPS tenant server did not start.")
            with httpx.Client(verify=False, timeout=5) as probe:
                landing = probe.get(url + "/auth/login")
                if landing.status_code != 200 or '<div id="root"></div>' not in landing.text:
                    raise AssertionError(
                        f"HTTPS frontend landing invalid: {landing.status_code} "
                        + landing.text[:400]
                    )
                asset = next(
                    (part.split('"')[0] for part in landing.text.split('src="/assets/')[1:]),
                    None,
                )
                if asset is not None:
                    response = probe.get(url + "/assets/" + asset)
                    if response.status_code != 200:
                        raise AssertionError(
                            f"HTTPS frontend JavaScript unavailable: {response.status_code}"
                        )
            env = {
                **os.environ,
                "KP_DESK_E2E_URL": url,
                "KP_DESK_E2E_EMAIL": email,
                "KP_DESK_E2E_PASSWORD": password,
                "KP_DESK_E2E_EXPECT_LIVE_AUDIT": "1",
                "KP_DESK_E2E_BATCH_ONLY": "1" if review_supplier_batch else "0",
                "KP_DESK_E2E_PORTFOLIO_SAMPLE": "1" if portfolio_sample else "0",
                "KP_DESK_E2E_PORTFOLIO_PDF": (
                    str(Path(directory) / "portfolio.pdf") if portfolio_sample else ""
                ),
                "KP_DESK_E2E_LARGE_REVIEW": "1" if large_review_fixture is not None else "0",
                "KP_DESK_E2E_SIMULATE_GROQ_TIMEOUT": (
                    "1" if simulate_groq_timeout else "0"
                ),
            }
            script = ROOT / "frontend" / "scripts" / "tenant-browser-smoke.mjs"
            try:
                result = subprocess.run(
                    ["node", str(script)], cwd=ROOT / "frontend", env=env,
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=70, check=False,
                )
            except subprocess.TimeoutExpired as error:
                output = error.stdout or b""
                stderr = error.stderr or b""
                raise AssertionError(
                    "HTTPS Chrome smoke timed out.\n"
                    + output.decode("utf-8", "replace")[-4000:]
                    + stderr.decode("utf-8", "replace")[-4000:]
                ) from error
            if result.returncode:
                raise AssertionError(
                    f"HTTPS browser smoke failed (exit {result.returncode}):\n"
                    f"{result.stdout[-4500:]}\n{result.stderr[-4500:]}"
                )
            if portfolio_sample:
                artifact = Path(directory) / "portfolio.pdf"
                if not artifact.is_file() or not 100 < artifact.stat().st_size < 500_000:
                    raise AssertionError("Authenticated browser did not produce portfolio PDF.")
                text_pdf = "\n".join(
                    page.extract_text() or ""
                    for page in PdfReader(BytesIO(artifact.read_bytes())).pages
                )
                if not all(item in text_pdf for item in (
                    "PAPER-A4-80", "399.00", "798.00", "Не утверждено",
                )):
                    raise AssertionError("Portfolio PDF failed exact content verification.")
            return result.stdout
        finally:
            server.should_exit = True
            runner.join(timeout=8)
            if runner.is_alive():
                # Uvicorn may be draining a Windows Proactor TLS transport.
                # Bound shutdown and allow the server's supported forced-exit
                # path; never silently leave its daemon thread behind.
                server.force_exit = True
                runner.join(timeout=8)
            if runner.is_alive():
                raise RuntimeError(
                    "HTTPS test server did not shut down after forced exit."
                )
