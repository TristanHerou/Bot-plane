"""Webhook endpoints for GitHub and Plane."""

from app.webhooks.github import router as github_router
from app.webhooks.plane import router as plane_router

__all__ = ["github_router", "plane_router"]
