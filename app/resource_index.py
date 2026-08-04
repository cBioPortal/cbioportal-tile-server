"""Trusted study-to-resource bindings for authenticated WSI requests.

The index is produced by ``tools/load_clickhouse_hierarchy.py`` from the same
normalized publication that cBioPortal activates. It is deliberately not
derived from a request's ``studyId`` query parameter.
"""

from __future__ import annotations

import json
from pathlib import Path
from threading import Lock


class ResourceIndexUnavailable(RuntimeError):
    """Raised when authenticated resource bindings are not configured safely."""


class ResourceIndex:
    def __init__(self, path: str):
        self.path = Path(path) if path else None
        self._lock = Lock()
        self._mtime_ns: int | None = None
        self._studies: dict[str, dict[str, frozenset[str]]] | None = None

    def _load(self) -> dict[str, dict[str, frozenset[str]]]:
        if self.path is None:
            raise ResourceIndexUnavailable("WSI_RESOURCE_INDEX_FILE is not configured")
        try:
            stat = self.path.stat()
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResourceIndexUnavailable("trusted WSI resource index is unavailable") from exc

        if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("studies"), dict):
            raise ResourceIndexUnavailable("trusted WSI resource index has an invalid version")

        studies: dict[str, dict[str, frozenset[str]]] = {}
        owners: dict[tuple[str, str], str] = {}
        for study_id, resources in raw["studies"].items():
            if not isinstance(study_id, str) or not isinstance(resources, dict):
                raise ResourceIndexUnavailable("trusted WSI resource index has invalid study data")
            if any(
                resources.get(resource_type) is not None
                and not isinstance(resources.get(resource_type), list)
                for resource_type in ("patients", "samples", "slides")
            ):
                raise ResourceIndexUnavailable("trusted WSI resource index has invalid resource data")
            studies[study_id] = {}
            for resource_type in ("patients", "samples", "slides"):
                values = resources.get(resource_type) or []
                normalized_values = frozenset(str(value) for value in values)
                for resource_id in normalized_values:
                    owner_key = (resource_type, resource_id)
                    owner = owners.get(owner_key)
                    if owner is not None and owner != study_id:
                        raise ResourceIndexUnavailable(
                            "trusted WSI resource index has an ambiguous resource"
                        )
                    owners[owner_key] = study_id
                studies[study_id][resource_type] = normalized_values

        self._mtime_ns = stat.st_mtime_ns
        self._studies = studies
        return studies

    def _studies_for_request(self) -> dict[str, dict[str, frozenset[str]]]:
        with self._lock:
            if self.path is None:
                return self._load()
            try:
                mtime_ns = self.path.stat().st_mtime_ns
            except OSError:
                return self._load()
            if self._studies is None or self._mtime_ns != mtime_ns:
                return self._load()
            return self._studies

    def contains(self, study_id: str, resource_type: str, resource_id: str) -> bool:
        if resource_type not in {"patients", "samples", "slides"}:
            return False
        studies = self._studies_for_request()
        return resource_id in studies.get(study_id, {}).get(resource_type, frozenset())

    def filter_search(self, study_id: str, results: list[dict]) -> list[dict]:
        """Retain only search results bound to the token's study."""
        filtered: list[dict] = []
        for result in results:
            if not isinstance(result, dict):
                continue
            result_type = str(result.get("type") or "").lower()
            resource_type = {
                "patient": "patients",
                "sample": "samples",
                "slide": "slides",
            }.get(result_type)
            if resource_type and result.get("id") is not None and self.contains(
                study_id, resource_type, str(result["id"])
            ):
                filtered.append(result)
        return filtered


_instances: dict[str, ResourceIndex] = {}
_instances_lock = Lock()


def get_resource_index(path: str) -> ResourceIndex:
    with _instances_lock:
        if path not in _instances:
            _instances[path] = ResourceIndex(path)
        return _instances[path]
