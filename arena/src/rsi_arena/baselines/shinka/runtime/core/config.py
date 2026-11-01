from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from rsi_arena.baselines.shinka.runtime.llm import BanditBase
from rsi_arena.baselines.shinka.runtime.defaults import (
    DEFAULT_TASK_SYS_MSG,
    default_llm_dynamic_selection_kwargs,
    default_llm_kwargs,
    default_llm_models,
    default_patch_type_probs,
    default_patch_types,
    default_prompt_patch_type_probs,
    default_prompt_patch_types,
)

FOLDER_PREFIX = "gen"


@dataclass
class EvolutionConfig:
    task_sys_msg: Optional[str] = DEFAULT_TASK_SYS_MSG
    patch_types: List[str] = field(default_factory=default_patch_types)
    patch_type_probs: List[float] = field(default_factory=default_patch_type_probs)
    num_generations: int = 50
    max_patch_resamples: int = 3
    max_patch_attempts: int = 1
    job_type: str = "local"
    language: str = "python"
    llm_models: List[str] = field(default_factory=default_llm_models)
    llm_dynamic_selection: Optional[Union[str, BanditBase]] = "ucb"
    llm_dynamic_selection_kwargs: dict = field(
        default_factory=default_llm_dynamic_selection_kwargs
    )
    llm_kwargs: dict = field(default_factory=default_llm_kwargs)
    meta_rec_interval: Optional[int] = 10
    meta_llm_models: Optional[List[str]] = None
    meta_llm_kwargs: dict = field(default_factory=lambda: {})
    meta_max_recommendations: int = 5
    sample_single_meta_rec: bool = True
    embedding_model: Optional[str] = "text-embedding-3-small"
    init_program_path: Optional[str] = "initial.py"
    results_dir: Optional[str] = None

    # Optional W&B logging is additive to the existing database/WebUI logging.
    enable_wandb_logging: bool = False
    wandb_project: Optional[str] = "shinka-evolve"
    wandb_entity: Optional[str] = None
    wandb_group: Optional[str] = None
    wandb_name: Optional[str] = None
    wandb_mode: Optional[str] = None
    wandb_tags: List[str] = field(default_factory=list)
    wandb_notes: Optional[str] = None
    wandb_dir: Optional[str] = None
    wandb_run_id: Optional[str] = None
    wandb_resume: str = "allow"
    wandb_config: Dict[str, Any] = field(default_factory=dict)

    max_novelty_attempts: int = 3
    code_embed_sim_threshold: float = 0.99
    novelty_llm_models: Optional[List[str]] = None
    novelty_llm_kwargs: dict = field(default_factory=lambda: {})
    use_text_feedback: bool = False
    max_api_costs: Optional[float] = None
    inspiration_sort_order: str = "ascending"
    enable_controlled_oversubscription: bool = False
    proposal_target_mode: str = "adaptive"
    proposal_target_min_samples: int = 5
    proposal_target_ratio_cap: float = 2.0
    proposal_buffer_max: int = 2
    proposal_target_hard_cap: Optional[int] = None
    proposal_target_ewma_alpha: float = 0.3

    # Meta-prompt evolution settings.
    evolve_prompts: bool = False
    prompt_patch_types: List[str] = field(default_factory=default_prompt_patch_types)
    prompt_patch_type_probs: List[float] = field(
        default_factory=default_prompt_patch_type_probs
    )
    prompt_evolution_interval: Optional[int] = None
    prompt_archive_size: int = 10
    prompt_llm_models: Optional[List[str]] = None
    prompt_llm_kwargs: dict = field(default_factory=lambda: {})
    prompt_ucb_exploration_constant: float = 1.0
    prompt_epsilon: float = 0.1
    prompt_evo_top_k_programs: int = 3
    prompt_percentile_recompute_interval: int = 20
