from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4.1-mini", alias="OPENAI_MODEL")
    trilium_url: str = Field(alias="TRILIUM_URL")
    trilium_etapi_token: str = Field(alias="TRILIUM_ETAPI_TOKEN")
    trilium_parent_note_id: str = Field(alias="TRILIUM_PARENT_NOTE_ID")
    youtube_client_secrets: str | None = Field(default=None, alias="YOUTUBE_CLIENT_SECRETS")
    youtube_token_file: str = Field(default=".state/youtube-token.json", alias="YOUTUBE_TOKEN_FILE")
    kokoro_command: str | None = Field(default=None, alias="KOKORO_COMMAND")
    kokoro_voice: str = Field(default="af_heart", alias="KOKORO_VOICE")
    kokoro_lang_code: str = Field(default="a", alias="KOKORO_LANG_CODE")
    kokoro_speed: float = Field(default=1.0, alias="KOKORO_SPEED")
    app_base_url: str = Field(default="http://127.0.0.1:8000", alias="APP_BASE_URL")
    app_host: str = Field(default="0.0.0.0", alias="APP_HOST")
    app_port: int = Field(default=8000, alias="APP_PORT")
    database_url: str = Field(default="sqlite:///./.state/trilium-study.db", alias="DATABASE_URL")
    workspace_dir: str = Field(default=".state/workspace", alias="WORKSPACE_DIR")
    log_dir: str = Field(default=".state/logs", alias="LOG_DIR")
    json_log_file: str = Field(default="app.jsonl", alias="JSON_LOG_FILE")
    text_log_file: str = Field(default="app.log", alias="TEXT_LOG_FILE")
    video_width: int = Field(default=1280, alias="VIDEO_WIDTH")
    video_height: int = Field(default=720, alias="VIDEO_HEIGHT")
    video_background: str = Field(default="#f2e7cc", alias="VIDEO_BACKGROUND")

    @property
    def database_path(self) -> Path:
        if self.database_url.startswith("sqlite:///"):
            return Path(self.database_url.removeprefix("sqlite:///")).resolve()
        raise ValueError("Only sqlite database URLs are supported in v1")

    @property
    def workspace_path(self) -> Path:
        return Path(self.workspace_dir).resolve()

    @property
    def log_path(self) -> Path:
        return Path(self.log_dir).resolve()


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
