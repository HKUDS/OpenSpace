"""Pure models.dev and bundled-pricing normalization."""

from __future__ import annotations

import csv
import json
import math
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .catalog import ModelKind, ModelPrice, PricingCatalog

MILLION = 1_000_000
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_LLM_CSV = _PACKAGE_ROOT / "llm" / "providers" / "pricing.csv"
_EMBEDDING_CSV = _PACKAGE_ROOT / "embed" / "providers" / "pricing.csv"
_OVERLAY_JSON = _PACKAGE_ROOT / "tools" / "pricing" / "models_dev_overlay.json"
_SUPPORTED_PROVIDERS = {
    "amazon-bedrock": "bedrock",
    "anthropic": "anthropic",
    "deepseek": "deepseek",
    "google": "google",
    "openai": "openai",
    "openrouter": "openrouter",
}


def catalog_from_payload(
    payload: Any,
    overlay_path: Path | None = None,
    *,
    include_bundled: bool = True,
) -> PricingCatalog:
    if not isinstance(payload, dict):
        raise ValueError("models.dev payload must be an object")
    bundled = load_bundled_entries() if include_bundled else ()
    entries = {
        (entry.kind, entry.provider, entry.api_model_name): entry for entry in bundled
    }
    discovered = _add_discovered_entries(entries, payload)
    if discovered == 0:
        raise ValueError("models.dev payload contains no supported priced models")
    _apply_overlay(entries, overlay_path or _OVERLAY_JSON)
    return PricingCatalog(tuple(entries.values()))


def load_bundled_entries() -> tuple[ModelPrice, ...]:
    return tuple(_load_llm_csv()) + tuple(_load_embedding_csv())


def _add_discovered_entries(
    entries: dict[tuple[ModelKind, str, str], ModelPrice],
    payload: dict[str, Any],
) -> int:
    discovered = 0
    for source_provider, runtime_provider in _SUPPORTED_PROVIDERS.items():
        provider_payload = payload.get(source_provider)
        if provider_payload is None:
            continue
        if not isinstance(provider_payload, dict):
            raise ValueError(f"Invalid provider payload: {source_provider}")
        models = provider_payload.get("models")
        if not isinstance(models, dict):
            raise ValueError(f"Invalid models collection: {source_provider}")
        for model_id, model in models.items():
            entry = _models_dev_entry(runtime_provider, str(model_id), model)
            if entry is not None:
                entries[(entry.kind, entry.provider, entry.api_model_name)] = entry
                discovered += 1
    return discovered


def _models_dev_entry(provider: str, model_id: str, model: Any) -> ModelPrice | None:
    if not isinstance(model, dict):
        return None
    if provider == "bedrock" and "anthropic" not in model_id:
        return None
    cost = model.get("cost")
    if not isinstance(cost, dict):
        return None
    input_price = _finite_price(cost.get("input"))
    output_price = _finite_price(cost.get("output"))
    kind: ModelKind = "embedding" if _is_embedding_model(model_id, model) else "llm"
    if input_price is None or (kind == "llm" and output_price is None):
        return None
    if kind == "llm" and not _supports_text_output(model):
        return None
    tier = _first_context_tier(cost)
    model_name = f"openrouter/{model_id}" if provider == "openrouter" else model_id
    reasoning = bool(model.get("reasoning", False))
    return ModelPrice(
        model_name=model_name,
        api_model_name=model_id,
        provider=provider,
        kind=kind,
        input_price=input_price / MILLION,
        output_price=None if output_price is None else output_price / MILLION,
        input_price_tier2=_tier_price(tier, "input"),
        output_price_tier2=_tier_price(tier, "output"),
        tier_threshold=None if tier is None else tier["size"],
        is_reasoning=reasoning,
        think_temp_fixed=bool(
            model.get("temperature") is False
            or (reasoning and provider in {"anthropic", "bedrock", "openrouter"})
        ),
    )


def _apply_overlay(
    entries: dict[tuple[ModelKind, str, str], ModelPrice],
    overlay_path: Path,
) -> None:
    overlay = _read_overlay(overlay_path)
    _add_model_aliases(entries, overlay.get("model_aliases", []))
    _apply_llm_overrides(entries, overlay.get("llm_overrides", []))


def _read_overlay(path: Path) -> dict[str, Any]:
    try:
        overlay = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Shinka pricing overlay is invalid") from exc
    if not isinstance(overlay, dict):
        raise ValueError("Shinka pricing overlay must be an object")
    return overlay


def _add_model_aliases(
    entries: dict[tuple[ModelKind, str, str], ModelPrice], aliases: Any
) -> None:
    if not isinstance(aliases, list):
        return
    provider_map = {**_SUPPORTED_PROVIDERS, "bedrock": "bedrock"}
    for alias in aliases:
        if not isinstance(alias, dict):
            continue
        kind = alias.get("kind")
        target_provider = alias.get("provider")
        target_name = alias.get("model_name")
        source_provider = provider_map.get(str(alias.get("source_provider")))
        source_name = alias.get("source_model_name")
        if kind not in {"llm", "embedding"} or not all(
            isinstance(value, str)
            for value in (target_provider, target_name, source_provider, source_name)
        ):
            continue
        source = entries.get((kind, source_provider, source_name))
        if source is None:
            continue
        aliased = ModelPrice(
            **{
                **asdict(source),
                "model_name": target_name,
                "api_model_name": target_name,
                "provider": target_provider,
            }
        )
        entries[(kind, target_provider, target_name)] = aliased


def _apply_llm_overrides(
    entries: dict[tuple[ModelKind, str, str], ModelPrice], overrides: Any
) -> None:
    if not isinstance(overrides, list):
        return
    for override in overrides:
        if not isinstance(override, dict):
            continue
        provider = override.get("provider")
        model_name = override.get("model_name")
        if not isinstance(provider, str) or not isinstance(model_name, str):
            continue
        key = ("llm", provider, model_name)
        entry = entries.get(key)
        if entry is None:
            continue
        values = asdict(entry)
        for field in ("is_reasoning", "think_temp_fixed", "requires_reasoning"):
            if field in override:
                values[field] = bool(override[field])
        for field in (
            "input_price",
            "output_price",
            "input_price_tier2",
            "output_price_tier2",
        ):
            _apply_price_override(values, override, field)
        threshold = _optional_int(override.get("tier_threshold"))
        if threshold is not None:
            values["tier_threshold"] = threshold
        entries[key] = ModelPrice(**values)


def _apply_price_override(
    values: dict[str, Any], override: dict[str, Any], field: str
) -> None:
    if field not in override:
        return
    price = _finite_price(override[field])
    if price is not None:
        values[field] = price / MILLION


def _is_embedding_model(model_id: str, model: dict[str, Any]) -> bool:
    family = str(model.get("family", ""))
    return "embedding" in model_id.lower() or "embedding" in family.lower()


def _supports_text_output(model: dict[str, Any]) -> bool:
    modalities = model.get("modalities")
    if not isinstance(modalities, dict):
        return True
    output = modalities.get("output")
    return not isinstance(output, list) or "text" in output


def _first_context_tier(cost: dict[str, Any]) -> dict[str, Any] | None:
    tiers = cost.get("tiers")
    if not isinstance(tiers, list):
        return None
    for candidate in tiers:
        if not isinstance(candidate, dict):
            continue
        tier = candidate.get("tier")
        if isinstance(tier, dict) and tier.get("type") == "context":
            size = tier.get("size")
            if (
                isinstance(size, (int, float))
                and not isinstance(size, bool)
                and math.isfinite(size)
                and size > 0
            ):
                return {**candidate, "size": int(size)}
    return None


def _tier_price(tier: dict[str, Any] | None, field: str) -> float | None:
    if tier is None:
        return None
    price = _finite_price(tier.get(field))
    return None if price is None else price / MILLION


def _finite_price(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price) or price < 0:
        return None
    return price


def _load_llm_csv() -> list[ModelPrice]:
    entries: list[ModelPrice] = []
    with _LLM_CSV.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            input_price = _finite_price(row["input_price"])
            output_price = _finite_price(row["output_price"])
            raw_model_name = row["model_name"].strip()
            provider = row["provider"].strip()
            model_name = (
                f"openrouter/{raw_model_name}"
                if provider == "openrouter"
                else raw_model_name
            )
            entries.append(
                ModelPrice(
                    model_name=model_name,
                    api_model_name=raw_model_name,
                    provider=provider,
                    kind="llm",
                    input_price=_per_token(input_price),
                    output_price=_per_token(output_price),
                    input_price_tier2=_csv_price(row.get("input_price_tier2")),
                    output_price_tier2=_csv_price(row.get("output_price_tier2")),
                    tier_threshold=_optional_int(row.get("tier_threshold")),
                    is_reasoning=_truthy(row.get("is_reasoning")),
                    think_temp_fixed=_truthy(row.get("think_temp_fixed")),
                    requires_reasoning=_truthy(row.get("requires_reasoning")),
                )
            )
    return entries


def _load_embedding_csv() -> list[ModelPrice]:
    entries: list[ModelPrice] = []
    with _EMBEDDING_CSV.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            input_price = _finite_price(row["input_price"])
            if input_price is None:
                continue
            model_name = row["model_name"].strip()
            api_model_name = (
                model_name.removeprefix("azure-")
                if row["provider"].strip() == "azure"
                else model_name
            )
            entries.append(
                ModelPrice(
                    model_name=model_name,
                    api_model_name=api_model_name,
                    provider=row["provider"].strip(),
                    kind="embedding",
                    input_price=input_price / MILLION,
                )
            )
    return entries


def _csv_price(value: Any) -> float | None:
    price = _finite_price(value)
    return _per_token(price)


def _per_token(price: float | None) -> float | None:
    return None if price is None else price / MILLION


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _optional_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
