"""A fake api.github.com for the App client: installations, tokens and repository lists.

It lists invented installations and repositories, answers each token request with a token it maps
back to its installation, and can fail a page, refuse a token or run a hook while a list is being
fetched. No account is real; tokens are built from parts at run time.
"""

import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2

from burnbird.github.client import API_HOST

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
API = f"https://{API_HOST}"

type Body = dict[str, Any]
type Hook = Callable[[], None]


def installation_item(
    installation_id: int,
    *,
    kind: str = "Organization",
    suspended: bool = False,
    account_id: int | None = None,
) -> Body:
    """One entry of ``GET /app/installations``, with fields Burnbird ignores.

    The account's ID is the installation's minus 1000 unless given.
    """
    account_id = installation_id - 1000 if account_id is None else account_id
    return {
        "id": installation_id,
        "account": {"login": f"example-{account_id}", "id": account_id, "type": kind},
        "repository_selection": "selected",
        "app_id": 1,
        "target_type": kind,
        "permissions": {"actions": "read", "metadata": "read"},
        "events": ["workflow_job"],
        "created_at": "2026-11-13T09:00:00Z",
        "suspended_at": "2026-11-14T08:00:00Z" if suspended else None,
        "suspended_by": None,
    }


def repository_item(repository_id: int, *, private: bool = True) -> Body:
    """One entry of ``GET /installation/repositories``."""
    return {
        "id": repository_id,
        "name": f"example-app-{repository_id}",
        "full_name": f"example-org/example-app-{repository_id}",
        "private": private,
    }


class FakeGithub:
    """``GET /app/installations``, token requests and ``GET /installation/repositories``."""

    def __init__(self) -> None:
        self.installations: list[Body] = []
        self.repositories: dict[int, list[Body]] = {}
        # Each list's items are split into pages of this size, linked with rel="next".
        self.page_size = 100
        # The repository list's total_count, when it should differ from the items it holds.
        self.total_count: int | None = None
        self.failing: set[str] = set()
        self.refused: set[int] = set()
        self.hooks: dict[str, Hook] = {}
        # Installed, so ``GET /app/installations/{id}`` finds them, but missing from the list.
        self.unlisted: set[int] = set()
        self.tokens: dict[str, int] = {}
        self.requests: list[httpx2.Request] = []
        # Answers a request itself when it returns a response.
        self.override: Callable[[httpx2.Request], httpx2.Response | None] = lambda _: None

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if (answer := self.override(request)) is not None:
            return answer
        path = request.url.path
        if request.method == "POST":
            installation_id = int(path.split("/")[3])
            if installation_id in self.refused:
                return httpx2.Response(403, json={"message": "This installation is suspended"})
            token = "gh" + "s_" + secrets.token_urlsafe(30)
            self.tokens[token] = installation_id
            expires = (NOW + timedelta(hours=1)).isoformat()
            return httpx2.Response(201, json={"token": token, "expires_at": expires})
        if path == "/app/installations":
            return self._page(request, "installations", self.installations, wrap=None)
        if path.startswith("/app/installations/"):
            installation_id = int(path.rsplit("/", 1)[1])
            listed = {item.get("id") for item in self.installations}
            if installation_id in listed | self.unlisted:
                return httpx2.Response(200, json=installation_item(installation_id))
            return httpx2.Response(404, json={"message": "Not Found"})
        installation_id = self.tokens[request.headers["Authorization"].removeprefix("Bearer ")]
        name = f"repositories:{installation_id}"
        return self._page(
            request, name, self.repositories.get(installation_id, []), wrap="repositories"
        )

    def _page(
        self, request: httpx2.Request, name: str, items: list[Body], wrap: str | None
    ) -> httpx2.Response:
        page = int(request.url.params.get("page", "1"))
        if f"{name}:{page}" in self.failing:
            return httpx2.Response(500)
        if page == 1 and name in self.hooks:
            self.hooks[name]()
        chunk = items[(page - 1) * self.page_size : page * self.page_size]
        headers = {}
        if page * self.page_size < len(items):
            next_url = f"{API}{request.url.path}?per_page=100&page={page + 1}"
            headers["Link"] = f'<{next_url}>; rel="next"'
        total = len(items) if self.total_count is None else self.total_count
        body: Any = chunk if wrap is None else {"total_count": total, wrap: chunk}
        return httpx2.Response(200, headers=headers, json=body)
