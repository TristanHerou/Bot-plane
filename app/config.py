"""Configuration management using Pydantic Settings."""

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class StatusMapping:
    """Manages status mappings between GitHub and Plane."""

    def __init__(self, mapping_data: dict[str, Any]) -> None:
        self.github_to_plane: dict[str, str] = mapping_data.get("github_to_plane", {})
        self.plane_to_github: dict[str, str] = mapping_data.get("plane_to_github", {})
        self.plane_state_ids: dict[str, str] = mapping_data.get("plane_state_ids", {})

    def get_plane_status_for_github_action(self, github_action: str) -> str | None:
        """Get the Plane status name for a GitHub action (closed/reopened)."""
        return self.github_to_plane.get(github_action)

    def get_plane_state_id(self, status_name: str) -> str | None:
        """Get the Plane state ID for a status name."""
        return self.plane_state_ids.get(status_name)

    def get_github_state_for_plane_status(self, plane_status: str) -> str | None:
        """Get the GitHub state (open/closed) for a Plane status name."""
        return self.plane_to_github.get(plane_status)

    def is_plane_status_closed(self, plane_status: str) -> bool:
        """Check if a Plane status should map to GitHub closed state."""
        return self.get_github_state_for_plane_status(plane_status) == "closed"

    @classmethod
    def from_file(cls, file_path: Path) -> "StatusMapping":
        """Load status mapping from a YAML file."""
        with open(file_path) as f:
            data = yaml.safe_load(f)
        return cls(data or {})

    @classmethod
    def default(cls) -> "StatusMapping":
        """Create default status mapping."""
        return cls(
            {
                "github_to_plane": {
                    "closed": "Done",
                    "reopened": "Backlog",
                },
                "plane_to_github": {
                    "Done": "closed",
                    "Cancelled": "closed",
                    "Backlog": "open",
                    "Todo": "open",
                    "In Progress": "open",
                },
                "plane_state_ids": {},
            }
        )


class Settings(BaseSettings):
    """Application settings with validation."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Plane Configuration
    plane_api_key: str = Field(..., description="Plane API key (format: plane_api_xxxxx)")
    plane_workspace_slug: str = Field(..., description="Plane workspace slug")
    plane_project_id: str = Field(..., description="Plane project UUID")
    plane_base_url: str = Field(
        default="https://api.plane.so", description="Plane API base URL"
    )

    # GitHub App Configuration
    github_app_id: int = Field(..., description="GitHub App ID")
    github_app_private_key_path: Path | None = Field(
        default=None, description="Path to GitHub App private key PEM file"
    )
    github_app_private_key_base64: str | None = Field(
        default=None, description="GitHub App private key as base64-encoded string"
    )
    github_webhook_secret: str = Field(..., description="GitHub webhook secret for verification")

    # Plane Webhook Configuration
    plane_webhook_secret: str | None = Field(
        default=None, description="Plane webhook secret for verification (strongly recommended)"
    )

    # Status Mapping
    status_mapping_file: Path | None = Field(
        default=None, description="Path to status mapping YAML file"
    )

    # Bot Configuration
    port: int = Field(default=8000, description="Port to run the bot on")
    log_level: str = Field(default="INFO", description="Logging level")
    debug: bool = Field(default=False, description="Enable debug mode")
    webhook_base_url: str | None = Field(
        default=None, description="Base URL where this bot is hosted"
    )

    # Rate Limiting
    plane_rate_limit: int = Field(
        default=60, description="Plane API rate limit (requests per minute)"
    )

    @field_validator("plane_api_key")
    @classmethod
    def validate_plane_api_key(cls, v: str) -> str:
        if not v.startswith("plane_api_"):
            raise ValueError("Plane API key must start with 'plane_api_'")
        return v

    @field_validator("plane_webhook_secret")
    @classmethod
    def validate_plane_webhook_secret(cls, v: str | None) -> str | None:
        """Strip whitespace (trailing newline in .env is common)."""
        return v.strip() if v and isinstance(v, str) else v

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        v_upper = v.upper()
        if v_upper not in valid_levels:
            raise ValueError(f"Log level must be one of: {valid_levels}")
        return v_upper

    @model_validator(mode="after")
    def validate_github_key_source(self) -> "Settings":
        """Ensure at least one GitHub private key source is provided."""
        if not self.github_app_private_key_path and not self.github_app_private_key_base64:
            raise ValueError(
                "Either GITHUB_APP_PRIVATE_KEY_PATH or GITHUB_APP_PRIVATE_KEY_BASE64 must be set"
            )
        return self

    def get_github_private_key(self) -> bytes:
        """Get the GitHub App private key from configured source."""
        import base64

        if self.github_app_private_key_base64:
            return base64.b64decode(self.github_app_private_key_base64)
        if self.github_app_private_key_path:
            return self.github_app_private_key_path.read_bytes()
        raise ValueError("No GitHub private key source configured")

    def get_status_mapping(self) -> StatusMapping:
        """Load and return the status mapping."""
        if self.status_mapping_file and self.status_mapping_file.exists():
            return StatusMapping.from_file(self.status_mapping_file)
        return StatusMapping.default()


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


def setup_logging(settings: Settings) -> None:
    """Configure logging based on settings."""
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format=log_format,
    )

    # Reduce noise from httpx
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
