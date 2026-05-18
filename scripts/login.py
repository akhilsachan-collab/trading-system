"""
Upstox OAuth2 login flow.
Spins up a local HTTPS server, opens the browser to the Upstox auth page,
captures the redirect code, exchanges it for an access token, and saves it to .env.
"""

import os
import sys
import ssl
import ipaddress
import datetime
import tempfile
import webbrowser
import threading
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import requests
from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)

API_KEY      = os.getenv("UPSTOX_API_KEY", "")
API_SECRET   = os.getenv("UPSTOX_API_SECRET", "")
REDIRECT_URI = os.getenv("UPSTOX_REDIRECT_URI", "https://127.0.0.1:5000/callback")
BASE_URL     = "https://api.upstox.com/v2"
TIMEOUT_SECS = 120


# ── Self-signed cert ─────────────────────────────────────────────────────────

def generate_cert() -> tuple[str, str]:
    """Write a temporary self-signed cert for 127.0.0.1. Returns (cert_path, key_path)."""
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError:
        print("[ERROR] 'cryptography' package is not installed.")
        print("  -> Run: .venv\\Scripts\\pip install cryptography")
        sys.exit(1)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now  = datetime.datetime.now(datetime.timezone.utc)

    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    tmp = tempfile.mkdtemp()
    cert_path = os.path.join(tmp, "cert.pem")
    key_path  = os.path.join(tmp, "key.pem")

    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))

    return cert_path, key_path


# ── Callback server ──────────────────────────────────────────────────────────

_auth_code:  str | None = None
_auth_error: str | None = None
_got_callback = threading.Event()


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global _auth_code, _auth_error
        params = parse_qs(urlparse(self.path).query)

        if "code" in params:
            _auth_code = params["code"][0]
            body = b"<h2>Login successful! You can close this tab and return to the terminal.</h2>"
            self.send_response(200)
        elif "error" in params:
            desc = params.get("error_description", params.get("error", ["unknown"]))
            _auth_error = desc[0]
            body = b"<h2>Login failed. You can close this tab.</h2>"
            self.send_response(400)
        else:
            body = b"<h2>Unexpected callback. You can close this tab.</h2>"
            self.send_response(400)

        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        _got_callback.set()

    def log_message(self, fmt, *args):
        pass  # silence request logs


# ── OAuth steps ──────────────────────────────────────────────────────────────

def exchange_code(code: str) -> str:
    resp = requests.post(
        f"{BASE_URL}/login/authorization/token",
        data={
            "code":          code,
            "client_id":     API_KEY,
            "client_secret": API_SECRET,
            "redirect_uri":  REDIRECT_URI,
            "grant_type":    "authorization_code",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    if not resp.ok:
        print(f"[ERROR] Token exchange failed: HTTP {resp.status_code} — {resp.text[:300]}")
        sys.exit(1)
    token = resp.json().get("access_token")
    if not token:
        print(f"[ERROR] No access_token in response: {resp.text[:300]}")
        sys.exit(1)
    return token


def save_token(token: str) -> None:
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    updated = [
        f"UPSTOX_ACCESS_TOKEN={token}" if l.startswith("UPSTOX_ACCESS_TOKEN=") else l
        for l in lines
    ]
    ENV_PATH.write_text("\n".join(updated) + "\n", encoding="utf-8")


def verify_token(token: str) -> str:
    resp = requests.get(
        f"{BASE_URL}/user/profile",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=10,
    )
    if not resp.ok:
        print(f"[ERROR] Token verification failed: HTTP {resp.status_code}")
        sys.exit(1)
    return resp.json().get("data", {}).get("user_name", "Unknown")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    if not API_KEY or not API_SECRET:
        print("[ERROR] UPSTOX_API_KEY or UPSTOX_API_SECRET is missing from .env.")
        sys.exit(1)

    auth_url = (
        f"https://api.upstox.com/v2/login/authorization/dialog"
        f"?response_type=code&client_id={API_KEY}&redirect_uri={REDIRECT_URI}"
    )

    # Generate cert and start HTTPS server
    cert_path, key_path = generate_cert()

    try:
        server = HTTPServer(("127.0.0.1", 5000), CallbackHandler)
    except OSError as e:
        print(f"[ERROR] Could not bind to port 5000: {e}")
        print("  -> Make sure nothing else is running on port 5000 and try again.")
        sys.exit(1)

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # Open browser and wait
    print("Opening browser for login...")
    webbrowser.open(auth_url)
    print("Waiting for callback at 127.0.0.1:5000...")
    print("  (Browser may show a security warning — click Advanced > Proceed to continue.)\n")

    if not _got_callback.wait(timeout=TIMEOUT_SECS):
        server.shutdown()
        print("[ERROR] Timed out after 2 minutes with no login detected.")
        print("  -> Complete the Upstox login in the browser before the timer runs out.")
        sys.exit(1)

    server.shutdown()

    if _auth_error:
        print(f"[ERROR] Login was denied: {_auth_error}")
        sys.exit(1)

    # Exchange and save
    print("Got auth code, exchanging for access token...")
    token = exchange_code(_auth_code)

    save_token(token)
    print("Token saved to .env")

    name = verify_token(token)
    sys.stdout.buffer.write(f"✅ Logged in as {name}\n".encode("utf-8"))


if __name__ == "__main__":
    main()
