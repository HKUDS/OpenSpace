"""Embedding pricing compatibility API backed by the runtime model catalog."""

from __future__ import annotations

from typing import Optional

from rsi_arena.baselines.shinka.runtime.pricing.catalog import ModelPrice, get_catalog


def _entry(model_name: str) -> ModelPrice:
    try:
        return get_catalog().catalog.get_by_name(model_name, kind="embedding")
    except KeyError:
        try:
            return get_catalog().catalog.find_by_api_name(model_name, kind="embedding")
        except KeyError as exc:
            raise ValueError(
                f"Embedding model {model_name} not found in pricing data"
            ) from exc


def get_model_price(model_name: str) -> float:
    price = _entry(model_name).input_price
    if price is None:
        raise ValueError(f"Embedding model {model_name} has no pricing data")
    return price


def model_exists(model_name: str) -> bool:
    try:
        entry = _entry(model_name)
    except ValueError:
        return False
    return entry.input_price is not None


def get_provider(model_name: str) -> Optional[str]:
    try:
        return _entry(model_name).provider
    except ValueError:
        return None


def get_all_models() -> list[str]:
    return [
        entry.model_name
        for entry in get_catalog().catalog.entries
        if entry.kind == "embedding"
    ]


def get_all_providers() -> list[str]:
    return get_catalog().catalog.providers(kind="embedding")


def get_models_by_provider(provider: str) -> list[str]:
    return get_catalog().catalog.models(provider, kind="embedding")
