from __future__ import annotations

import argparse
from pathlib import Path

from app.config import get_settings


YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"


def parse_args() -> argparse.Namespace:
    settings = get_settings()
    return argparse.ArgumentParser(
        description="Generate a persisted OAuth token file for YouTube uploads."
    ).parse_args()


def resolve_paths() -> tuple[Path, Path]:
    settings = get_settings()
    if not settings.youtube_client_secrets:
        raise SystemExit("YOUTUBE_CLIENT_SECRETS is not set in .env")
    client_secrets = Path(settings.youtube_client_secrets).expanduser().resolve()
    token_file = Path(settings.youtube_token_file).expanduser().resolve()
    if not client_secrets.exists():
        raise SystemExit(f"Client secrets file not found: {client_secrets}")
    return client_secrets, token_file


def main() -> None:
    parse_args()
    client_secrets, token_file = resolve_paths()

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("Missing YouTube auth dependencies. Run: uv sync --extra youtube") from exc

    flow = InstalledAppFlow.from_client_secrets_file(
        str(client_secrets),
        scopes=[YOUTUBE_UPLOAD_SCOPE],
    )
    credentials = flow.run_local_server(
        port=0,
        open_browser=False,
        authorization_prompt_message="Open this URL in your browser:\n{url}\n",
        success_message="Authentication complete. You can close this tab.",
    )

    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(credentials.to_json(), encoding="utf-8")

    print(f"Wrote YouTube token to {token_file}")
    print("You can now use the persisted refresh token for upload requests.")


if __name__ == "__main__":
    main()
