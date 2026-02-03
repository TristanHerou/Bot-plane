"""Pydantic models for GitHub webhook payloads."""

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class GitHubUser(BaseModel):
    """GitHub user information."""

    login: str
    id: int
    node_id: str
    avatar_url: str | None = None
    type: str = "User"


class GitHubLabel(BaseModel):
    """GitHub issue label."""

    id: int
    node_id: str
    name: str
    color: str
    description: str | None = None
    default: bool = False


class GitHubIssue(BaseModel):
    """GitHub issue information."""

    id: int
    node_id: str
    number: int
    title: str
    body: str | None = None
    state: Literal["open", "closed"]
    state_reason: str | None = None
    locked: bool = False
    user: GitHubUser
    labels: list[GitHubLabel] = Field(default_factory=list)
    assignees: list[GitHubUser] = Field(default_factory=list)
    html_url: str
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None

    def has_label(self, label_name: str) -> bool:
        """Check if issue has a specific label."""
        return any(label.name == label_name for label in self.labels)


class GitHubRepository(BaseModel):
    """GitHub repository information."""

    id: int
    node_id: str
    name: str
    full_name: str
    owner: GitHubUser
    private: bool = False
    html_url: str

    @property
    def owner_name(self) -> str:
        """Get repository owner name."""
        return self.owner.login


class GitHubInstallation(BaseModel):
    """GitHub App installation information."""

    id: int
    node_id: str | None = None


class GitHubSender(BaseModel):
    """GitHub event sender (who triggered the event)."""

    login: str
    id: int
    node_id: str
    type: str = "User"


class GitHubIssueEvent(BaseModel):
    """GitHub issue webhook event payload."""

    action: str
    issue: GitHubIssue
    repository: GitHubRepository
    sender: GitHubSender
    installation: GitHubInstallation | None = None

    @property
    def is_closed(self) -> bool:
        """Check if this is an issue closed event."""
        return self.action == "closed"

    @property
    def is_reopened(self) -> bool:
        """Check if this is an issue reopened event."""
        return self.action == "reopened"

    @property
    def is_sync_relevant(self) -> bool:
        """Check if this event is relevant for status sync."""
        return self.action in ("closed", "reopened")

    @property
    def issue_url(self) -> str:
        """Get the full issue URL."""
        return self.issue.html_url

    @property
    def repo_full_name(self) -> str:
        """Get the full repository name (owner/repo)."""
        return self.repository.full_name


# Constants for bot identification
SYNC_LABEL = "plane-sync"
SYNC_MARKER = "[plane-sync]"

# Regex to match Plane work item references in commit messages: [MAIN-123], [BACK-54], etc.
COMMIT_REF_PATTERN = re.compile(r"\[([A-Za-z0-9]+)-(\d+)\]")


class GitHubCommitAuthor(BaseModel):
    """Commit author in push payload."""

    name: str | None = None
    email: str | None = None
    username: str | None = None


class GitHubPushCommit(BaseModel):
    """Single commit in a push event."""

    id: str
    message: str | None = None
    timestamp: str | None = None
    author: GitHubCommitAuthor | None = None
    url: str | None = None


class GitHubPushEvent(BaseModel):
    """GitHub push webhook event payload."""

    ref: str  # e.g. refs/heads/main
    repository: GitHubRepository
    commits: list[GitHubPushCommit] = Field(default_factory=list)
    head_commit: GitHubPushCommit | None = None
    installation: GitHubInstallation | None = None

    @property
    def branch(self) -> str | None:
        """Branch name if ref is a branch."""
        if self.ref.startswith("refs/heads/"):
            return self.ref.removeprefix("refs/heads/")
        return None
