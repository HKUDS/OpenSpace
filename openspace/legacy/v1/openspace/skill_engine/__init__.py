from importlib import import_module

__all__ = [
    'SkillRegistry',
    'SkillMeta',
    'SKILL_ID_FILENAME',
    'write_skill_id',
    'EvolutionSuggestion',
    'EvolutionType',
    'ExecutionAnalysis',
    'SkillCategory',
    'SkillJudgment',
    'SkillOrigin',
    'SkillLineage',
    'SkillRecord',
    'SkillVisibility',
    'SkillStore',
    'ExecutionAnalyzer',
    'SkillEvolver',
    'EvolutionTrigger',
    'EvolutionContext',
]

_EXPORTS = {
    'SkillRegistry': ('.registry', 'SkillRegistry'),
    'SkillMeta': ('.registry', 'SkillMeta'),
    'SKILL_ID_FILENAME': ('.registry', 'SKILL_ID_FILENAME'),
    'write_skill_id': ('.registry', 'write_skill_id'),
    'EvolutionSuggestion': ('.types', 'EvolutionSuggestion'),
    'EvolutionType': ('.types', 'EvolutionType'),
    'ExecutionAnalysis': ('.types', 'ExecutionAnalysis'),
    'SkillCategory': ('.types', 'SkillCategory'),
    'SkillJudgment': ('.types', 'SkillJudgment'),
    'SkillOrigin': ('.types', 'SkillOrigin'),
    'SkillLineage': ('.types', 'SkillLineage'),
    'SkillRecord': ('.types', 'SkillRecord'),
    'SkillVisibility': ('.types', 'SkillVisibility'),
    'SkillStore': ('.store', 'SkillStore'),
    'ExecutionAnalyzer': ('.analyzer', 'ExecutionAnalyzer'),
    'SkillEvolver': ('.evolver', 'SkillEvolver'),
    'EvolutionTrigger': ('.evolver', 'EvolutionTrigger'),
    'EvolutionContext': ('.evolver', 'EvolutionContext'),
}


def __getattr__(name: str):
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}') from exc
    module = import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
