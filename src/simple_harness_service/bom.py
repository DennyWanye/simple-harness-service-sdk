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
            if requirement.partition("@")[0].strip().lower()
            == str(item["distribution"]).lower()
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
        _validate_direct_url(component, item, direct_url)
        result[component] = distribution.version
    return result


def _validate_direct_url(
    component: str, item: Mapping[str, object], direct_url: str
) -> None:
    try:
        provenance = json.loads(direct_url)
    except json.JSONDecodeError as error:
        raise ServiceError(
            ServiceErrorCode.INTERNAL, f"{component} provenance malformed"
        ) from error
    if not isinstance(provenance, dict) or provenance.get("url") != item["url"]:
        raise ServiceError(ServiceErrorCode.INTERNAL, f"{component} URL/SHA drift")
    archive = provenance.get("archive_info")
    if not isinstance(archive, dict):
        raise ServiceError(ServiceErrorCode.INTERNAL, f"{component} SHA provenance missing")
    hashes = archive.get("hashes")
    if not isinstance(hashes, dict) or set(hashes) != {"sha256"}:
        raise ServiceError(ServiceErrorCode.INTERNAL, f"{component} SHA provenance missing")
    digest = hashes["sha256"]
    if not isinstance(digest, str) or digest != item["sha256"]:
        raise ServiceError(ServiceErrorCode.INTERNAL, f"{component} URL/SHA drift")
    legacy_hash = archive.get("hash")
    if legacy_hash is not None and legacy_hash != f"sha256={digest}":
        raise ServiceError(ServiceErrorCode.INTERNAL, f"{component} SHA provenance ambiguous")


def _item(bom: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = bom.get(key)
    if not isinstance(value, Mapping):
        raise ServiceError(ServiceErrorCode.INTERNAL, "invalid compatibility BOM")
    return value


__all__ = ("load_bom", "validate_installed_bom", "validate_metadata_requirements")
