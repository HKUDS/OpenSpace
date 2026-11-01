"""Operator exclusions scoped to the model where a rate limit was observed.

The standalone Harbor relay carries the same data; boundary tests check parity.
"""

DISABLED_OPENROUTER_PROVIDERS = ('deepinfra', 'parasail')
MODEL_DISABLED_OPENROUTER_PROVIDERS = {'qwen/qwen3.5-9b': ('together', 'darkbloom/fp4'),
 'qwen/qwen3.8-27b': ('dekallm',),
 'z-ai/glm-5.3-flash': ('baseten/fp8',
                        'cloudflare',
                        'coreweave/fp8',
                        'crusoe/fp4',
                        'digitalocean',
                        'fireworks',
                        'io-net/fp8',
                        'makora',
                        'modal/fp8',
                        'morph/fp8',
                        'nextbit/fp8',
                        'novita/fp8',
                        'phala/fp8',
                        'reka/fp8',
                        'streamlake/fp8',
                        'together',
                        'venice')}


def ignored_providers(model=None):
    model = model.removeprefix('openrouter/') if isinstance(model, str) else model
    return DISABLED_OPENROUTER_PROVIDERS + MODEL_DISABLED_OPENROUTER_PROVIDERS.get(model, ())


def provider_policy():
    return {'excluded_providers': list(DISABLED_OPENROUTER_PROVIDERS),
            'model_excluded_providers': {model: list(routes) for model, routes in MODEL_DISABLED_OPENROUTER_PROVIDERS.items()}}
