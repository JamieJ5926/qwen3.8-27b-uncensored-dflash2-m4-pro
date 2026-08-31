"""Append-only artifact files. CompactObservation stores pointers, not payloads."""

from __future__ import annotations

from pathlib import Path


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def write(self, run_id: str, name: str, payload: str) -> str:
        directory = self.root / run_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        if path.exists():
            return self.ref(run_id, name)
        path.write_text(payload, encoding="utf-8")
        return self.ref(run_id, name)

    def read(self, artifact_ref: str) -> str:
        path = self.resolve(artifact_ref)
        return path.read_text(encoding="utf-8")

    def ref(self, run_id: str, name: str) -> str:
        return f"artifact:{run_id}/{name}"

    def resolve(self, artifact_ref: str) -> Path:
        if not artifact_ref.startswith("artifact:"):
            raise ValueError(f"invalid artifact_ref: {artifact_ref}")
        relative = artifact_ref[len("artifact:") :]
        path = (self.root / relative).resolve()
        root = self.root.resolve()
        if root not in path.parents and path != root:
            raise ValueError(f"artifact_ref escapes store: {artifact_ref}")
        return path
