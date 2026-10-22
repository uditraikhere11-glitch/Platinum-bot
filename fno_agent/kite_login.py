"""
Daily Kite login helper (Kite access tokens expire every morning).

    python -m fno_agent.kite_login [path/to/.env]

1. Open the printed URL, log in, and copy `request_token` from the redirect URL.
2. Paste it here; the access token is written to KITE_TOKEN_FILE.
The running agent picks it up at its 08:00 refresh or on its next token error — no restart needed.
"""

import os
import sys

from dotenv import load_dotenv

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from fno_agent.settings import AGENT_DIR, Settings  # noqa: E402


def main():
    load_dotenv(sys.argv[1] if len(sys.argv) > 1 else os.path.join(AGENT_DIR, ".env"))
    s = Settings.from_env()
    if not (s.kite_api_key and s.kite_api_secret):
        sys.exit("Set KITE_API_KEY and KITE_API_SECRET in the env file.")

    from kiteconnect import KiteConnect

    kite = KiteConnect(api_key=s.kite_api_key)
    print(f"Login here:\n  {kite.login_url()}\n")
    request_token = input("Paste request_token: ").strip()
    session = kite.generate_session(request_token, api_secret=s.kite_api_secret)

    fd = os.open(s.kite_token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(session["access_token"])
    print(f"Access token saved to {s.kite_token_file} for {session.get('user_id')}.")


if __name__ == "__main__":
    main()
