"""Receipt-first portfolio turns: replay settled output, never reissue an unknown call."""
import re
from agentpilot.app.provider_calls import ProviderCallContext
from agentpilot.providers import CompletionRequest
from agentpilot.providers.base import AccountingStatus, ProviderCallRefused, ProviderCompletionUnusable, ProviderUsage


class PortfolioSessionRecoveryRequired(RuntimeError):
    """An interrupted external dispatch needs reconciliation, not a new call."""


def refusal_blocker(detail):
    """Classify a transport refusal without publishing its raw provider payload."""
    if re.search(r'(?:transport returned|Error code:|HTTP)\s*402\b', str(detail), re.I):
        return {'code': 'provider_payment_required', 'http_status': 402,
                'retryable': False,
                'message': 'Provider credits cannot cover this request. Automatic retries are stopped. Add credits, then explicitly resume the experiment.'}
    return None


def request_data(session, pending):
    if 'request_ref' in pending:
        value = session.store.load_document(pending['request_ref'])
    else:
        value = pending.get('request')
    if not isinstance(value, dict) or 'tools' not in value:
        raise PortfolioSessionRecoveryRequired('Frozen request is unavailable for this pending call')
    return value


def persist_receipt(session, call_id, text, usage, *, error=None, finish_reason=None, blocker=None):
    if usage is None:
        raise PortfolioSessionRecoveryRequired('Completion has no usage receipt')
    status = getattr(usage, 'accounting_status', AccountingStatus.PROVIDER_REPORTED)
    value = {'call_id': call_id, 'text': text,
             'request_ref': session.state['pending'].get('request_ref'),
             'usage': {'input_tokens': usage.input_tokens, 'output_tokens': usage.output_tokens,
                       'cost_usd': usage.cost_usd, 'provider_request_id': usage.provider_request_id,
                       'accounting_status': status.value if isinstance(status, AccountingStatus) else status},
             'error': error, 'finish_reason': finish_reason}
    if blocker is not None:
        value['blocker'] = blocker
    session.store.put_document('model-receipt/' + call_id, value)
    return value


def _obtain(session, call_id, operation):
    try:
        result = operation()
    except ProviderCallRefused as error:
        return persist_receipt(session, call_id, None, ProviderUsage(0, 0, 0., AccountingStatus.PROVIDER_REPORTED),
                               error='ProviderCallRefused', blocker=refusal_blocker(error))
    except ProviderCompletionUnusable as error:
        return persist_receipt(session, call_id, error.settled_text, error.usage,
                               error=type(error).__name__, finish_reason=error.finish_reason)
    if result is None:
        raise PortfolioSessionRecoveryRequired('No recorded completion for this dispatch; reconcile its durable receipt before resuming')
    return persist_receipt(session, call_id, result.text, result.usage, finish_reason=getattr(result, 'raw_finish_reason', None))


def dispatch(session, payload):
    pending = session.state['pending']
    call_id = pending['call_id']
    request = CompletionRequest(
        model=payload['model'], prompt='', system='', messages=tuple(payload['messages']),
        tools=tuple(payload['tools']), tool_choice='auto',
        max_output_tokens=payload['max_output_tokens'], temperature=payload['temperature'],
        reasoning_effort=payload['reasoning_effort'], disable_reasoning=payload['disable_reasoning'],
        metadata=payload['metadata'])
    context = ProviderCallContext(call_id=call_id, run_id=session.request.state.run_id,
                                  batch_id=session.request.state.batch_id, purpose='portfolio-proposer')
    try:
        return _obtain(session, call_id, lambda: session.engine.completion.complete(request, context=context))
    except BaseException as error:
        session.store.mark_external_unknown(call_id, error=type(error).__name__)
        raise


def recover(session):
    call_id = session.state['pending']['call_id']
    saved = session.store.load_document('model-receipt/' + call_id)
    if saved is not None:
        return saved
    lookup = getattr(session.engine.completion, 'recover', None)
    if not callable(lookup):
        raise PortfolioSessionRecoveryRequired('No recorded completion for this dispatch; reconcile its durable receipt before resuming')
    return _obtain(session, call_id, lambda: lookup(call_id))
