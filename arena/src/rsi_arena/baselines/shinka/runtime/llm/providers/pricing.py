"""LLM pricing compatibility API backed by the runtime model catalog."""

from __future__ import annotations

from typing import Optional, Tuple

from rsi_arena.baselines.shinka.runtime.pricing.catalog import ModelPrice, get_catalog


def _entry(model_name: str) -> ModelPrice:
    try:
        return get_catalog().catalog.get_by_name(model_name, kind="llm")
    except KeyError:
        try:
            return get_catalog().catalog.find_by_api_name(model_name, kind="llm")
        except KeyError as exc:
            raise ValueError(f"Model {model_name} not found in pricing data") from exc


def get_model_prices(model_name: str, input_tokens: Optional[int] = None) -> dict:
    input_price, output_price = _entry(model_name).rates(input_tokens)
    if input_price is None or output_price is None:
        raise ValueError(f"Model {model_name} has no pricing data")
    return {"input_price": input_price, "output_price": output_price}


def calculate_cost(
    model_name: str, input_tokens: int, output_tokens: int
) -> Tuple[float, float]:
    prices = get_model_prices(model_name, input_tokens=input_tokens)
    return (
        prices["input_price"] * input_tokens,
        prices["output_price"] * output_tokens,
    )


def model_exists(model_name: str) -> bool:
    try:
        entry = _entry(model_name)
    except ValueError:
        return False
    return entry.input_price is not None and entry.output_price is not None


def is_reasoning_model(model_name: str) -> bool:
    try:
        return _entry(model_name).is_reasoning
    except ValueError:
        return False


def get_provider(model_name: str) -> Optional[str]:
    try:
        return _entry(model_name).provider
    except ValueError:
        return None


def get_all_providers() -> list[str]:
    return get_catalog().catalog.providers(kind="llm")


def get_models_by_provider(provider: str) -> list[str]:
    return get_catalog().catalog.models(provider, kind="llm")


def has_fixed_temperature(model_name: str) -> bool:
    try:
        return _entry(model_name).think_temp_fixed
    except ValueError:
        return False


def requires_reasoning(model_name: str) -> bool:
    try:
        return _entry(model_name).requires_reasoning
    except ValueError:
        return False
