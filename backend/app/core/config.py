from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    app_name: str = "Strategy Monitor"
    app_env: str = "development"
    database_url: str = f"sqlite:///{ROOT_DIR / 'data' / 'app.db'}"
    session_cookie_name: str = "strategy_monitor_session"
    session_days: int = 14
    registration_enabled: bool = True
    keep_scan_artifacts: bool = False
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    scanner_project_path: str = str(ROOT_DIR.parent / "a-share-pattern-scan-tool")
    scanner_board: str = "main_board"
    scanner_timeout_seconds: int = 1800

    model_config = SettingsConfigDict(env_file=ROOT_DIR / ".env", env_file_encoding="utf-8")

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


settings = Settings()
