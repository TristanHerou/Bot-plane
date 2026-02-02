"""
Plane ↔ GitHub Sync Bot

A bidirectional synchronization bot that keeps issue/work item statuses
in sync between GitHub and Plane.so.
"""

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.config import Settings, get_settings, setup_logging
from app.services.github_service import GitHubService
from app.services.plane_service import PlaneService
from app.services.sync_service import SyncService
from app.webhooks import github_router, plane_router
from app.webhooks.github import get_github_service as github_get_github_service
from app.webhooks.github import get_sync_service as github_get_sync_service
from app.webhooks.plane import get_sync_service as plane_get_sync_service

logger = logging.getLogger(__name__)

# Global service instances
_github_service: GitHubService | None = None
_plane_service: PlaneService | None = None
_sync_service: SyncService | None = None


def get_github_service() -> GitHubService:
    """Get the GitHub service instance."""
    if _github_service is None:
        raise RuntimeError("GitHub service not initialized")
    return _github_service


def get_plane_service() -> PlaneService:
    """Get the Plane service instance."""
    if _plane_service is None:
        raise RuntimeError("Plane service not initialized")
    return _plane_service


def get_sync_service() -> SyncService:
    """Get the sync service instance."""
    if _sync_service is None:
        raise RuntimeError("Sync service not initialized")
    return _sync_service


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan manager."""
    global _github_service, _plane_service, _sync_service

    settings = get_settings()
    setup_logging(settings)

    logger.info(f"Starting Plane-GitHub Sync Bot v{__version__}")
    logger.info(f"Workspace: {settings.plane_workspace_slug}")
    logger.info(f"Project: {settings.plane_project_id}")

    # Initialize services
    _github_service = GitHubService(settings)
    _plane_service = PlaneService(settings)

    status_mapping = settings.get_status_mapping()
    _sync_service = SyncService(_github_service, _plane_service, status_mapping)

    # Initialize sync service (loads Plane states)

    await _sync_service.initialize()
    logger.info("Sync service initialized successfully")

    yield

    # Cleanup
    logger.info("Shutting down Plane-GitHub Sync Bot")
    _github_service = None
    _plane_service = None
    _sync_service = None


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Plane-GitHub Sync Bot",
        description="Bidirectional status synchronization between Plane.so and GitHub",
        version=__version__,
        lifespan=lifespan,
    )

    # Add CORS middleware (if needed for debugging/testing)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Override dependency injections
    app.dependency_overrides[github_get_sync_service] = get_sync_service
    app.dependency_overrides[github_get_github_service] = get_github_service
    app.dependency_overrides[plane_get_sync_service] = get_sync_service

    # Include routers
    app.include_router(github_router)
    app.include_router(plane_router)

    @app.get("/")
    async def root() -> dict[str, str]:
        """Root endpoint with basic info."""
        return {
            "name": "Plane-GitHub Sync Bot",
            "version": __version__,
            "status": "running",
        }

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Health check endpoint."""
        return {"status": "healthy"}

    return app


# Create the app instance
app = create_app()


def run() -> None:
    """Run the application using uvicorn."""
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.port,
        reload=settings.debug,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    run()
