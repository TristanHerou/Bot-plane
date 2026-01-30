"""Pydantic models for Plane API and webhook payloads."""

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, computed_field


class GitHubIssueRef(BaseModel):
    """Reference to a GitHub issue extracted from a Plane link."""

    owner: str
    repo: str
    issue_number: int

    @property
    def full_name(self) -> str:
        """Get full repository name (owner/repo)."""
        return f"{self.owner}/{self.repo}"

    @property
    def url(self) -> str:
        """Get the full GitHub issue URL."""
        return f"https://github.com/{self.owner}/{self.repo}/issues/{self.issue_number}"

    @classmethod
    def from_url(cls, url: str) -> "GitHubIssueRef | None":
        """
        Extract GitHub issue reference from URL.

        Supports formats:
        - https://github.com/owner/repo/issues/123
        - http://github.com/owner/repo/issues/123
        - github.com/owner/repo/issues/123
        """
        pattern = r"(?:https?://)?github\.com/([^/]+)/([^/]+)/issues/(\d+)"
        match = re.match(pattern, url)
        if match:
            return cls(
                owner=match.group(1),
                repo=match.group(2),
                issue_number=int(match.group(3)),
            )
        return None


class PlaneLink(BaseModel):
    """A link attached to a Plane work item."""

    id: str
    url: str
    title: str | None = Field(default=None, alias="name")
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}

    @computed_field
    @property
    def github_issue_ref(self) -> GitHubIssueRef | None:
        """Extract GitHub issue reference if this is a GitHub issue link."""
        return GitHubIssueRef.from_url(self.url)

    @property
    def is_github_issue(self) -> bool:
        """Check if this link points to a GitHub issue."""
        return self.github_issue_ref is not None


class PlaneState(BaseModel):
    """Plane work item state."""

    id: str
    name: str
    color: str | None = None
    group: str | None = None  # backlog, unstarted, started, completed, cancelled
    sequence: float | None = None


class PlaneWorkItem(BaseModel):
    """Plane work item (formerly issue)."""

    id: str
    name: str
    description: str | None = None
    description_html: str | None = None
    state: str  # State ID
    state_detail: PlaneState | None = None
    priority: str | None = None
    sequence_id: int | None = None
    project: str  # Project ID
    workspace: str  # Workspace ID
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def state_name(self) -> str | None:
        """Get the state name if state_detail is available."""
        return self.state_detail.name if self.state_detail else None


class PlaneWebhookData(BaseModel):
    """Data payload from Plane webhook."""

    id: str
    name: str | None = None
    state: str | None = None  # State ID
    project: str | None = None
    workspace: str | None = None

    # Old values for comparison (available on updates)
    old_state: str | None = None

    # Additional fields that might be present
    sequence_id: int | None = None
    priority: str | None = None

    model_config = {"extra": "allow"}


class PlaneWebhookEvent(BaseModel):
    """
    Plane webhook event payload.

    Based on Plane webhook documentation.
    Event types: work_item.created, work_item.updated, work_item.deleted
    """

    event: str  # e.g., "work_item.updated"
    action: str  # e.g., "updated", "created", "deleted"
    webhook_id: str
    workspace_id: str
    project_id: str | None = None
    data: PlaneWebhookData
    activity: dict[str, Any] | None = None

    # For identifying bot-triggered updates
    triggered_by: str | None = None

    model_config = {"extra": "allow"}

    @property
    def is_status_change(self) -> bool:
        """Check if this event represents a status change."""
        if self.action != "updated":
            return False
        # Check if state field was modified
        if self.data.old_state and self.data.state:
            return self.data.old_state != self.data.state
        # Fallback: check activity for state changes
        if self.activity:
            return "state" in self.activity
        return False

    @property
    def work_item_id(self) -> str:
        """Get the work item ID from the event data."""
        return self.data.id

    @property
    def new_state_id(self) -> str | None:
        """Get the new state ID if this is a status change."""
        return self.data.state

    @property
    def old_state_id(self) -> str | None:
        """Get the old state ID if available."""
        return self.data.old_state


# Marker for identifying bot-triggered updates
BOT_MARKER = "[sync-bot]"
BOT_ACTIVITY_SOURCE = "plane-github-sync-bot"
