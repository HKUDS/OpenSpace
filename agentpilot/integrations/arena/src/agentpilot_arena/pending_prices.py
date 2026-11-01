"""Reconcile prices for already consumed model responses without more inference."""
from pathlib import Path
import time

from agentpilot.persistence import SQLiteUnitOfWorkFactory
from agentpilot_arena.baseline_accounting import BaselineAccounting


def refresh(store, output, *, lookup=None, limit=1):
    with store._connect() as db:
        keys=[r['key'] for r in db.execute("SELECT key FROM external_costs WHERE state='accounting_unknown' AND (label='proposer' OR label LIKE 'research-memory-%') ORDER BY created_at")]
    attempted=0
    for name in ('proposer-calls','policy-calls'):
        database=Path(output)/name/'provider.sqlite3'
        if not database.exists():continue
        unit=SQLiteUnitOfWorkFactory(database)
        for key in keys:
            if attempted>=limit:return
            if store._external_cost(key)['state']=='settled':continue
            with unit() as work:
                receipt=work.usage.get_result(key)
                if receipt is None or not receipt.provider_request_id:continue
                intent=work.usage.get_intent(key)
            saved=store.load_checkpoint('baseline-generation/'+receipt.provider_request_id,{})
            if time.time()<saved.get('retry_at',0):continue
            if receipt.cost_usd is not None:
                store.settle_external(key,receipt.cost_usd)
                continue
            accountant=BaselineAccounting(output,budget=None,model=intent.model,hold_usd=0.,store=store,lookup=lookup)
            call={'ordinal':1,'generation_id':receipt.provider_request_id,
                  'input_tokens':receipt.input_tokens,'output_tokens':receipt.output_tokens}
            cost,_=accountant._generation(call,query=True,cell_id=key)
            attempted+=1
            if cost is not None:store.settle_external(key,cost)
