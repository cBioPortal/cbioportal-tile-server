"""Trusted, study-qualified WSI resource bindings for authenticated requests."""

from __future__ import annotations

import bisect
import hashlib
import json
import re
from pathlib import Path
from threading import Lock


class ResourceIndexUnavailable(RuntimeError):
    """Raised when authenticated resource bindings are not configured safely."""


class ResourceIndex:
    def __init__(self, path: str):
        self.path = Path(path) if path else None
        self._lock = Lock()
        self._signature: tuple[int, int] | None = None
        self._revision: str | None = None
        self._studies: dict[str, dict[str, frozenset[str]]] | None = None
        self._slide_bindings: dict[str, dict[str, dict[str, str | None]]] = {}
        self._sorted_resources: dict[str, dict[str, tuple[tuple[str, str], ...]]] = {}

    def _load(self) -> dict[str, dict[str, frozenset[str]]]:
        if self.path is None:
            raise ResourceIndexUnavailable("WSI_RESOURCE_INDEX_FILE is not configured")
        try:
            raw_bytes = self.path.read_bytes()
            stat = self.path.stat()
            raw = json.loads(raw_bytes)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResourceIndexUnavailable("trusted WSI resource index is unavailable") from exc

        if (
            not isinstance(raw, dict)
            or raw.get("version") != 2
            or not isinstance(raw.get("studies"), dict)
        ):
            raise ResourceIndexUnavailable("trusted WSI resource index has an invalid version")

        studies: dict[str, dict[str, frozenset[str]]] = {}
        slide_bindings: dict[str, dict[str, dict[str, str | None]]] = {}
        sorted_resources: dict[str, dict[str, tuple[tuple[str, str], ...]]] = {}
        for study_id, resources in raw["studies"].items():
            if not isinstance(study_id, str) or not isinstance(resources, dict):
                raise ResourceIndexUnavailable("trusted WSI resource index has invalid study data")
            if any(
                not isinstance(resources.get(resource_type) or [], list)
                for resource_type in ("patients", "samples")
            ):
                raise ResourceIndexUnavailable("trusted WSI resource index has invalid resource data")
            raw_slides = resources.get("slides")
            if not isinstance(raw_slides, dict):
                raise ResourceIndexUnavailable("trusted WSI resource index has invalid slide bindings")
            normalized_slides: dict[str, dict[str, str | None]] = {}
            for image_id, binding in raw_slides.items():
                if (
                    not isinstance(image_id, str)
                    or not isinstance(binding, dict)
                    or not isinstance(binding.get("patient_id"), str)
                    or not binding["patient_id"]
                    or (
                        binding.get("source_path") is not None
                        and not isinstance(binding.get("source_path"), str)
                    )
                ):
                    raise ResourceIndexUnavailable("trusted WSI resource index has invalid slide binding")
                normalized_slides[image_id] = {
                    "patient_id": binding["patient_id"],
                    "source_path": binding.get("source_path"),
                }

            studies[study_id] = {
                "patients": frozenset(str(value) for value in resources.get("patients") or []),
                "samples": frozenset(str(value) for value in resources.get("samples") or []),
                "slides": frozenset(normalized_slides),
            }
            slide_bindings[study_id] = normalized_slides
            sorted_resources[study_id] = {
                resource_type: tuple(
                    sorted(
                        (resource_id.casefold(), resource_id)
                        for resource_id in studies[study_id][resource_type]
                    )
                )
                for resource_type in ("patients", "samples", "slides")
            }

        self._signature = (stat.st_mtime_ns, stat.st_size)
        self._revision = hashlib.sha256(raw_bytes).hexdigest()[:16]
        self._studies = studies
        self._slide_bindings = slide_bindings
        self._sorted_resources = sorted_resources
        return studies

    def _studies_for_request(self) -> dict[str, dict[str, frozenset[str]]]:
        with self._lock:
            if self.path is None:
                return self._load()
            try:
                stat = self.path.stat()
            except OSError:
                return self._load()
            signature = (stat.st_mtime_ns, stat.st_size)
            if self._studies is None or self._signature != signature:
                return self._load()
            return self._studies

    def revision(self) -> str:
        """Return the current trusted-index revision after validating its file."""
        self._studies_for_request()
        if self._revision is None:
            raise ResourceIndexUnavailable("trusted WSI resource index is unavailable")
        return self._revision

    def contains(self, study_id: str, resource_type: str, resource_id: str) -> bool:
        if resource_type not in {"patients", "samples", "slides"}:
            return False
        studies = self._studies_for_request()
        return str(resource_id) in studies.get(study_id, {}).get(resource_type, frozenset())

    def slide_binding(self, study_id: str, image_id: str) -> dict[str, str | None] | None:
        """Return the exact study-qualified slide binding."""
        self._studies_for_request()
        binding = self._slide_bindings.get(study_id, {}).get(str(image_id))
        return dict(binding) if binding is not None else None

    def suggestions(self, study_id: str, query: str) -> list[dict[str, str]]:
        """Build safe autocomplete suggestions from one study's trusted bindings."""
        self._studies_for_request()
        if re.match(r"^P-\d.*-T", query, re.IGNORECASE):
            resource_type, item_type = "samples", "sample"
        elif re.match(r"^P-", query, re.IGNORECASE):
            resource_type, item_type = "patients", "patient"
        elif re.match(r"^\d", query):
            resource_type, item_type = "slides", "slide"
        else:
            return []
        prefix = query.casefold()
        resources = self._sorted_resources.get(study_id, {}).get(resource_type, ())
        start = bisect.bisect_left(resources, (prefix, ""))
        return [
            {"type": item_type, "id": resource_id, "label": resource_id, "sublabel": ""}
            for folded_id, resource_id in resources[start:]
            if folded_id.startswith(prefix)
        ][:8]


_instances: dict[str, ResourceIndex] = {}
_instances_lock = Lock()


def get_resource_index(path: str) -> ResourceIndex:
    with _instances_lock:
        if path not in _instances:
            _instances[path] = ResourceIndex(path)
        return _instances[path]
