"""Machine-readable compatibility closure validation."""

from __future__ import annotations

import importlib.metadata as metadata
import importlib.resources as resources
import json
from collections.abc import Mapping
from typing import Any

from .contracts import ServiceError, ServiceErrorCode


def load_bom() -> dict[str, Any]:
    value = json.loads(
        resources.files("simple_harness_service")
        .joinpath("compatibility-bom.json")
        .read_text(encoding="utf-8")
    )
    if not isinstance(value, dict) or value.get("schema") != "simple-harness-service-bom-v1":
        raise ServiceError(ServiceErrorCode.INTERNAL, "invalid compatibility BOM")
    return value


def validate_metadata_requirements(requires_dist: list[str]) -> None:
    bom = load_bom()
    for component in ("harness", "memory"):
        item = _item(bom, component)
        expected_url = f"{item['url']}#sha256={item['sha256']}"
        matches = [
            requirement
            for requirement in requires_dist
            if requirement.lower().startswith(str(item["distribution"]).lower())
        ]
        if len(matches) != 1 or expected_url not in matches[0].replace(" ", ""):
            raise ServiceError(ServiceErrorCode.INTERNAL, f"{component} metadata pin drift")
        if component == "memory" and "extra == 'memory'" not in matches[0]:
            raise ServiceError(ServiceErrorCode.INTERNAL, "memory extra marker drift")


def validate_installed_bom(*, include_memory: bool = False) -> dict[str, str]:
    bom = load_bom()
    service = _item(bom, "service")
    service_dist = metadata.distribution(str(service["distribution"]))
    if service_dist.version != service["version"]:
        raise ServiceError(ServiceErrorCode.INTERNAL, "service version drift")
    validate_metadata_requirements(service_dist.metadata.get_all("Requires-Dist") or [])
    result = {"service": service_dist.version}
    for component in ("harness", "memory"):
        if component == "memory" and not include_memory:
            continue
        item = _item(bom, component)
        distribution = metadata.distribution(str(item["distribution"]))
        if distribution.version != item["version"]:
            raise ServiceError(ServiceErrorCode.INTERNAL, f"{component} version drift")
        direct_url = distribution.read_text("direct_url.json")
        if direct_url is None:
            raise ServiceError(ServiceErrorCode.INTERNAL, f"{component} source provenance missing")
        provenance = json.loads(direct_url)
        archive = provenance.get("archive_info", {})
        hashes = archive.get("hashes", {})
        digest = hashes.get("sha256") or str(archive.get("hash", "")).removeprefix("sha256=")
        if provenance.get("url") != item["url"]:
            raise ServiceError(ServiceErrorCode.INTERNAL, f"{component} URL/SHA drift")
        # Some compliant installers retain the source URL but omit archive_info after
        # verifying the hash from Requires-Dist. The exact hash remains mandatory in
        # the service METADATA; when an installer retains it, it must also agree.
        if digest and digest != item["sha256"]:
            raise ServiceError(ServiceErrorCode.INTERNAL, f"{component} URL/SHA drift")
        result[component] = distribution.version
    return result


def _item(bom: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = bom.get(key)
    if not isinstance(value, Mapping):
        raise ServiceError(ServiceErrorCode.INTERNAL, "invalid compatibility BOM")
    return value


__all__ = ("load_bom", "validate_installed_bom", "validate_metadata_requirements")
