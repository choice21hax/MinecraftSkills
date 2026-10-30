"""Provider adapters. Every adapter exposes search / versions / version with normalized models."""

from __future__ import annotations

from typing import Protocol

from mcat.models import ModVersion, ProjectHit


class Provider(Protocol):
    name: str

    def search(
        self,
        query: str,
        *,
        minecraft: str | None = ...,
        loader: str | None = ...,
        project_type: str = ...,
        limit: int = ...,
    ) -> list[ProjectHit]: ...

    def versions(self, project: str, *, minecraft: str | None = ..., loader: str | None = ...) -> list[ModVersion]: ...
