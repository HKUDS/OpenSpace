"""Boundary-specific public views for research records.

The durable store keeps complete controller state and provider receipts. These
helpers only shape copies made for a model or a public report. In particular,
an experiment result is evidence: a field named ``budget`` or ``input_tokens``
inside that result is not assumed to be controller accounting.
"""

from collections.abc import Mapping


_CONTROLLER_PRIVATE = frozenset({
    'worker_accounting', 'externally_billed_reservation_usd',
    'budget', 'remaining_budget', 'cost_usd', 'costs', 'usage',
    'reservation_usd', 'reserved_usd', 'estimated_cost_usd',
    'spent_usd', 'available_usd', 'limit_usd', 'settled_cost_usd',
    'input_tokens', 'output_tokens', 'cache_tokens', 'total_tokens',
    'model_calls_remaining', 'trace_reads_remaining', 'source_reads_remaining',
    'shared_reads_remaining', 'read_body_bytes_remaining', 'probes_remaining',
    'generation_recoveries_remaining', 'context_bytes_remaining_estimate',
    'remaining_trace_reads', 'remaining_repository_reads', 'remaining_read_bytes',
    'remaining_read_calls', 'context_byte_limit', 'max_calls', 'max_output_tokens',
    'max_proposer_turns', 'max_trace_reads', 'max_repository_reads',
    'max_read_bytes', 'max_read_calls', 'completion_reservation_usd',
    'new_model_calls', 'turns', 'trace_artifacts',
    'usage_totals', 'cost_stop_enabled', 'worker_timeout_seconds',
    'configured_max_output_tokens', 'configured_max_total_output_tokens',
    'configured_provider_timeout_seconds', 'configured_max_inflight_requests',
})


_RECEIPT_PRIVATE = frozenset({
    'worker_accounting', 'externally_billed_reservation_usd',
    'budget', 'cost_usd', 'costs', 'reservation_usd', 'reserved_usd',
    'estimated_cost_usd', 'spent_usd', 'available_usd', 'limit_usd',
    'max_calls', 'max_output_tokens', 'remaining_budget',
})

_EXPERIMENT_WRAPPER_PRIVATE = frozenset({
    'worker_accounting', 'externally_billed_reservation_usd',
    'cost_usd', 'costs', 'reservation_usd', 'reserved_usd',
    'estimated_cost_usd', 'spent_usd', 'available_usd', 'limit_usd',
    'accounting', 'accounting_unknown', 'trace_artifacts',
})

_RECONCILIATION_CODES = frozenset({
    'ACCOUNTING_UNKNOWN', 'DEVELOPMENT_ACCOUNTING_REQUIRED',
    'EXPERIMENT_CALL_RECOVERY_REQUIRED', 'MEASUREMENT_RECOVERY_REQUIRED',
    'ADMISSION_RECOVERY_REQUIRED', 'PORTFOLIO_SESSION_RECOVERY_REQUIRED',
    'RECOVERY_REQUIRED',
})
_CONTENT_CODES = frozenset({
    'CONTENT_LIMIT', 'CONTENT_TOO_LARGE', 'READ_BYTES_EXHAUSTED',
    'READ_CONTENT_TOO_LARGE',
})
_UNAVAILABLE_CODES = frozenset({
    'BUDGET_LIMIT', 'CONTEXT_LIMIT', 'READ_CALLS_EXHAUSTED', 'SOURCE_READS_EXHAUSTED',
    'TRACE_READS_EXHAUSTED', 'PROBES_EXHAUSTED', 'TURN_LIMIT',
})


def _error_code(value):
    if value is None:
        return None
    if not isinstance(value, str):
        value = getattr(value, 'value', value)
    text = str(value).replace('-', '_')
    separated = []
    for index, character in enumerate(text):
        if (character.isupper() and index and text[index - 1].islower()
                and separated[-1] != '_'):
            separated.append('_')
        separated.append(character)
    return ''.join(separated).upper()


def research_error(message, *, code=None):
    """Present a controller failure selected by an explicit type or code.

    Free-form exception text is evidence too. It is returned verbatim unless
    the caller supplies a controller code or passes a known typed exception.
    This prevents messages such as ``Unexpected token`` from being mistaken
    for framework usage accounting.
    """
    if isinstance(message, BaseException):
        selected = (_error_code(code)
                    or _error_code(getattr(message, 'presentation_code', None))
                    or _error_code(type(message).__name__))
        text = str(message)
    else:
        selected = _error_code(code)
        text = str(message)
    if selected in _RECONCILIATION_CODES:
        return 'This operation needs reconciliation before it can continue.'
    if selected in _CONTENT_CODES:
        return 'Further content cannot be returned in this session.'
    if selected in _UNAVAILABLE_CODES:
        return 'This operation is currently unavailable. Use the available tools or finish the current work.'
    return text


def _copy_view(value, omitted, *, controller=False):
    if isinstance(value, Mapping):
        code = value.get('error_code', value.get('code'))
        status = value.get('status')
        if controller and isinstance(status, str) and status in {
            'accounting_unknown', 'budget_limit', 'context_limit', 'turn_limit',
        }:
            code = code or status
        result = {}
        for key, item in value.items():
            if key in omitted:
                continue
            if controller and key == 'status' and isinstance(item, str) and item in {
                'budget_limit', 'context_limit', 'turn_limit',
            }:
                result[key] = 'paused'
            elif controller and key == 'status' and item == 'accounting_unknown':
                result[key] = 'unresolved'
            elif controller and key == 'error' and isinstance(item, (str, BaseException)):
                result[key] = research_error(item, code=code)
            elif controller and key == 'result':
                kind = value.get('kind')
                if kind in {'admit', 'build'}:
                    result[key] = _copy_view(item, omitted, controller=True)
                else:
                    result[key] = experiment_result_view(item)
            else:
                result[key] = _copy_view(item, omitted, controller=controller)
        return result
    if isinstance(value, (tuple, list)):
        return [_copy_view(item, omitted, controller=controller) for item in value]
    return value


def controller_view(value):
    """Copy a controller envelope without its limits, accounting or usage."""
    return _copy_view(value, _CONTROLLER_PRIVATE, controller=True)


def experiment_result_view(value):
    """Copy experimental evidence without its framework accounting wrapper."""
    if not isinstance(value, Mapping):
        if isinstance(value, (tuple, list)):
            return [experiment_result_view(item) for item in value]
        return value
    result = {}
    for key, item in value.items():
        if key in _EXPERIMENT_WRAPPER_PRIVATE:
            continue
        if key == 'model_calls':
            result[key] = receipt_view(item)
        elif key == 'development_measurements' and isinstance(item, (tuple, list)):
            result[key] = [controller_view(measured) for measured in item]
        else:
            # Other mappings are domain observations. Their field names do not
            # acquire controller meaning merely because they are nested here.
            result[key] = _copy_view(item, frozenset())
    return result


def receipt_view(value):
    """Copy a provider receipt without framework price and allowance fields."""
    return _copy_view(value, _RECEIPT_PRIVATE)


# Compatibility for callers that have not yet named their controller boundary.
research_view = controller_view
