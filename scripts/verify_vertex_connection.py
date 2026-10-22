"""Optional manual Vertex AI smoke test; never runs as part of pytest collection.

Run explicitly with:
    python scripts/verify_vertex_connection.py
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path


def main() -> int:
    from dotenv import load_dotenv

    env_path = Path(os.environ.get("DARVAS_ENV_FILE", ".env")).expanduser().resolve()
    if not env_path.is_file():
        print(f"Environment file not found: {env_path}")
        return 2
    load_dotenv(env_path, override=False)

    credentials_json = os.environ.get("VERTEX_CREDENTIALS_JSON", "").strip()
    project_id = os.environ.get("VERTEX_PROJECT_ID", "").strip()
    location = os.environ.get("VERTEX_LOCATION", "us-central1").strip()
    if not credentials_json or not project_id:
        print("Set VERTEX_CREDENTIALS_JSON and VERTEX_PROJECT_ID in the selected environment file.")
        return 2

    try:
        from google.oauth2.credentials import Credentials
        import vertexai
        from vertexai.generative_models import GenerativeModel
    except ImportError as exc:
        print(f"Install the Vertex AI requirements before running this optional check: {exc}")
        return 2

    try:
        credentials_info = json.loads(credentials_json)
        creds = Credentials.from_authorized_user_info(credentials_info)
    except Exception as exc:
        print(f"Could not parse user credentials JSON: {type(exc).__name__}")
        return 2

    print(f"Initializing Vertex AI project={project_id}, location={location}...")
    vertexai.init(project=project_id, location=location, credentials=creds)
    try:
        response = GenerativeModel("gemini-2.5-flash").generate_content("Reply with the word OK.")
        print(f"Vertex AI smoke test succeeded: {response.text}")
        return 0
    except Exception as exc:
        print(f"Vertex AI smoke test failed ({type(exc).__name__}). Check credentials, project, API enablement and quota.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
