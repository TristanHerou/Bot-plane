"""GitHub API service using GitHub App authentication."""

import logging
import time
from typing import Any

import httpx
import jwt

from app.config import Settings
from app.models.github import SYNC_LABEL

logger = logging.getLogger(__name__)


class GitHubAuthError(Exception):
    """Raised when GitHub authentication fails."""

    pass


class GitHubAPIError(Exception):
    """Raised when GitHub API call fails."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubService:
    """Service for interacting with GitHub API using GitHub App authentication."""

    GITHUB_API_BASE = "https://api.github.com"
    JWT_EXPIRATION_SECONDS = 600  # 10 minutes (max allowed)
    TOKEN_BUFFER_SECONDS = 60  # Refresh token 1 minute before expiry

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._private_key: bytes | None = None
        self._installation_tokens: dict[int, tuple[str, float]] = {}  # {installation_id: (token, expiry)}

    @property
    def private_key(self) -> bytes:
        """Get the GitHub App private key."""
        if self._private_key is None:
            self._private_key = self.settings.get_github_private_key()
        return self._private_key

    def _generate_jwt(self) -> str:
        """
        Generate a JWT for GitHub App authentication.

        The JWT is used to authenticate as the GitHub App itself
        (not as an installation).
        """
        now = int(time.time())
        payload = {
            "iat": now - 60,  # Issued 60 seconds ago to account for clock drift
            "exp": now + self.JWT_EXPIRATION_SECONDS,
            "iss": self.settings.github_app_id,
        }
        return jwt.encode(payload, self.private_key, algorithm="RS256")

    async def _get_installation_token(
        self, installation_id: int, client: httpx.AsyncClient
    ) -> str:
        """
        Get an installation access token for a specific installation.

        Tokens are cached and reused until they're about to expire.
        """
        now = time.time()

        # Check if we have a valid cached token
        if installation_id in self._installation_tokens:
            token, expiry = self._installation_tokens[installation_id]
            if now < expiry - self.TOKEN_BUFFER_SECONDS:
                return token

        # Generate new installation token
        app_jwt = self._generate_jwt()
        response = await client.post(
            f"{self.GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

        if response.status_code != 201:
            logger.error(
                f"Failed to get installation token: {response.status_code} - {response.text}"
            )
            raise GitHubAuthError(
                f"Failed to get installation token: {response.status_code}"
            )

        data = response.json()
        token = data["token"]
        # Parse expiry time (ISO format) and cache
        # Token expires in 1 hour by default
        expiry = now + 3600
        self._installation_tokens[installation_id] = (token, expiry)

        logger.debug(f"Generated new installation token for installation {installation_id}")
        return token

    async def _get_installation_id_for_repo(
        self, owner: str, repo: str, client: httpx.AsyncClient
    ) -> int:
        """Get the installation ID for a specific repository."""
        app_jwt = self._generate_jwt()
        response = await client.get(
            f"{self.GITHUB_API_BASE}/repos/{owner}/{repo}/installation",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

        if response.status_code != 200:
            logger.error(
                f"Failed to get installation for repo {owner}/{repo}: "
                f"{response.status_code} - {response.text}"
            )
            raise GitHubAuthError(
                f"GitHub App not installed on {owner}/{repo}"
            )

        return response.json()["id"]

    async def _make_request(
        self,
        method: str,
        endpoint: str,
        installation_id: int,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make an authenticated request to the GitHub API."""
        async with httpx.AsyncClient() as client:
            token = await self._get_installation_token(installation_id, client)

            response = await client.request(
                method,
                f"{self.GITHUB_API_BASE}{endpoint}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json=json_data,
            )

            if response.status_code >= 400:
                logger.error(
                    f"GitHub API error: {method} {endpoint} - "
                    f"{response.status_code} - {response.text}"
                )
                raise GitHubAPIError(
                    f"GitHub API error: {response.status_code}",
                    status_code=response.status_code,
                )

            if response.status_code == 204:
                return {}

            return response.json()

    async def update_issue_state(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        state: str,
        installation_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Update the state of a GitHub issue.

        Args:
            owner: Repository owner
            repo: Repository name
            issue_number: Issue number
            state: New state ('open' or 'closed')
            installation_id: Optional installation ID (will be fetched if not provided)

        Returns:
            Updated issue data
        """
        if state not in ("open", "closed"):
            raise ValueError(f"Invalid issue state: {state}")

        async with httpx.AsyncClient() as client:
            if installation_id is None:
                installation_id = await self._get_installation_id_for_repo(
                    owner, repo, client
                )

            token = await self._get_installation_token(installation_id, client)

            response = await client.patch(
                f"{self.GITHUB_API_BASE}/repos/{owner}/{repo}/issues/{issue_number}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json={"state": state},
            )

            if response.status_code != 200:
                logger.error(
                    f"Failed to update issue state: {response.status_code} - {response.text}"
                )
                raise GitHubAPIError(
                    f"Failed to update issue {owner}/{repo}#{issue_number}",
                    status_code=response.status_code,
                )

            logger.info(
                f"Updated GitHub issue {owner}/{repo}#{issue_number} state to '{state}'"
            )
            return response.json()

    async def add_label(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        label: str = SYNC_LABEL,
        installation_id: int | None = None,
    ) -> None:
        """
        Add a label to a GitHub issue.

        Used to mark issues that have been synced by the bot.
        """
        async with httpx.AsyncClient() as client:
            if installation_id is None:
                installation_id = await self._get_installation_id_for_repo(
                    owner, repo, client
                )

            token = await self._get_installation_token(installation_id, client)

            # First, ensure the label exists
            await self._ensure_label_exists(owner, repo, label, token, client)

            # Add label to issue
            response = await client.post(
                f"{self.GITHUB_API_BASE}/repos/{owner}/{repo}/issues/{issue_number}/labels",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json={"labels": [label]},
            )

            if response.status_code not in (200, 201):
                logger.warning(
                    f"Failed to add label '{label}' to {owner}/{repo}#{issue_number}: "
                    f"{response.status_code}"
                )

    async def _ensure_label_exists(
        self,
        owner: str,
        repo: str,
        label: str,
        token: str,
        client: httpx.AsyncClient,
    ) -> None:
        """Ensure a label exists in the repository, creating it if needed."""
        # Check if label exists
        response = await client.get(
            f"{self.GITHUB_API_BASE}/repos/{owner}/{repo}/labels/{label}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

        if response.status_code == 200:
            return  # Label already exists

        # Create label
        response = await client.post(
            f"{self.GITHUB_API_BASE}/repos/{owner}/{repo}/labels",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={
                "name": label,
                "color": "6B7280",  # Gray color
                "description": "Synced with Plane.so",
            },
        )

        if response.status_code == 201:
            logger.info(f"Created label '{label}' in {owner}/{repo}")
        elif response.status_code == 422:
            # Label might already exist (race condition)
            pass
        else:
            logger.warning(
                f"Failed to create label '{label}': {response.status_code}"
            )

    async def get_issue(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        installation_id: int | None = None,
    ) -> dict[str, Any]:
        """Get a GitHub issue by number."""
        async with httpx.AsyncClient() as client:
            if installation_id is None:
                installation_id = await self._get_installation_id_for_repo(
                    owner, repo, client
                )

            return await self._make_request(
                "GET",
                f"/repos/{owner}/{repo}/issues/{issue_number}",
                installation_id,
            )

    def verify_webhook_signature(
        self, payload: bytes, signature: str
    ) -> bool:
        """
        Verify the GitHub webhook signature.

        Args:
            payload: Raw request body
            signature: X-Hub-Signature-256 header value

        Returns:
            True if signature is valid
        """
        import hashlib
        import hmac

        if not signature.startswith("sha256="):
            return False

        expected = hmac.new(
            self.settings.github_webhook_secret.encode(),
            payload,
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(f"sha256={expected}", signature)
