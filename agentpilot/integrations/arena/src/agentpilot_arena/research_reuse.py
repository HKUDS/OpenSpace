"""Reuse an immutable implementation while keeping the ordinary B/policy chain."""
from __future__ import annotations

from dataclasses import replace
import hashlib
from agentpilot_arena.runtime.research_build_prompt import PortfolioBuildHarness


class BoundedDirectionFactory:
    """Bound the strategy description, not the actual worker instruction."""
    def __init__(self, factory):self.factory=factory
    def __getattr__(self,name):return getattr(self.factory,name)
    def create(self, *, instruction, **kwargs):
        if len(instruction.strip()) <= 4800:
            return self.factory.create(instruction=instruction,**kwargs)
        digest=hashlib.sha256(instruction.encode()).hexdigest()
        brief=instruction[:4100]+'\n[Full instruction retained; sha256:'+digest+']\n'+instruction[-500:]
        action=self.factory.create(instruction=brief,**kwargs)
        return replace(action,d2_trace={**action.d2_trace,'full_implementation_instruction':instruction})


class FullInstructionHarness(PortfolioBuildHarness):
    @staticmethod
    def _request_payload(action, diagnostic, rework_budget):
        payload=dict(PortfolioBuildHarness._request_payload(action,diagnostic,rework_budget))
        full=action.d2_trace.get('full_implementation_instruction')
        if full is not None:
            payload['proposal']={**payload['proposal'],'implementation_direction':full}
        return payload


def configure_worker(worker, *, refusal_ledger=None):
    worker.action_factory=BoundedDirectionFactory(worker.action_factory)
    return worker


class ReusableBuildBackend:
    def __init__(self, backend, store):
        self.backend,self.store=backend,store

    def __getattr__(self,name):
        return getattr(self.backend,name)

    def prepare(self,kind,arguments):
        if kind!='build' or 'reuse_artifact' not in arguments:
            return self.backend.prepare(kind,arguments)
        if set(arguments)!={'reuse_artifact'}:
            raise ValueError('Reuse takes only an existing candidate artifact')
        artifact=self.store.artifact(arguments['reuse_artifact'])
        if not artifact.base_id:
            raise ValueError('Choose a built candidate, not the initial program')
        base=self.store.artifact(self.backend.default_base_id)
        if artifact.commit_sha==base.commit_sha:
            raise ValueError('Candidate and current baseline contain the same program')
        instruction=artifact.metadata.get('instruction') or 'Evaluate the retained implementation.'
        prepared=dict(self.backend.prepare('build',{'instruction':instruction,'base_artifact':base.id}))
        prepared['arguments']={**prepared['arguments'],'reuse_artifact':artifact.to_dict()}
        prepared['reservation_usd']=0.
        prepared['label']='Evaluate existing '+artifact.id
        return prepared

    def execute(self,ticket):
        saved=ticket.arguments.get('reuse_artifact') if ticket.kind=='build' else None
        if not saved:
            return self.backend.execute(ticket)
        actual=self.store.artifact(saved['id'])
        if actual.to_dict()!=saved:
            raise ValueError('Reused artifact identity changed')
        base=ticket.arguments['base']
        return {'cost_usd':0.,'summary':'Existing implementation reused; no coding worker was called.',
            'artifact':{'commit_sha':actual.commit_sha,'config':actual.config,
                'base_id':base['artifact_id'],'metadata':{**actual.metadata,
                    'build_ticket':ticket.id,'reused_from':actual.id}}}

    def recover(self,ticket):
        if ticket.kind=='build' and ticket.arguments.get('reuse_artifact'):
            return self.execute(ticket)
        return self.backend.recover(ticket)
