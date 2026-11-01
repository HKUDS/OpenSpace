import asyncio
import json
import logging
import multiprocessing as mp
import re
import time
from typing import Dict, List, Optional, Union

from pydantic import BaseModel

from .constants import MAX_RETRIES
from .kwargs import sample_model_kwargs
from .providers import QueryResult
from .providers.model_resolver import resolve_model_backend
from .query import query, query_async

logger = logging.getLogger(__name__)


class LLMClient:
    def __init__(
        self,
        model_names: Union[List[str], str] = "gpt-5.1",
        temperatures: Union[float, List[float]] = 0.75,
        max_tokens: Union[int, List[int]] = 4096,
        reasoning_efforts: Union[str, List[str]] = "disabled",
        model_sample_probs: Optional[List[float]] = None,
        output_model: Optional[BaseModel] = None,
        verbose: bool = True,
        headless_work_dir: Optional[str] = None,
    ):
        self.temperatures = temperatures
        self.max_tokens = max_tokens
        if isinstance(model_names, str):
            model_names = [model_names]
        self.model_names = model_names
        self.reasoning_efforts = reasoning_efforts
        self.model_sample_probs = model_sample_probs
        self.output_model = output_model
        self.structured_output = output_model is not None
        self.verbose = verbose
        self.headless_work_dir = headless_work_dir

    def _attach_headless_work_dir(self, llm_kwargs: Dict) -> Dict:
        model_name = llm_kwargs.get("model_name")
        if not model_name or self.headless_work_dir is None:
            return llm_kwargs
        if resolve_model_backend(model_name).provider != "headless":
            return llm_kwargs
        return {**llm_kwargs, "headless_work_dir": self.headless_work_dir}

    def batch_query(
        self,
        num_samples: int,
        msg: Union[str, List[str]],
        system_msg: Union[str, List[str]],
        msg_history: Union[List[Dict], List[List[Dict]]] = [],
        llm_kwargs: List[Dict] = [],
    ) -> List[QueryResult]:
        """Batch query the LLM with the given message and system message.

        Args:
            msg (str): The message to query the LLM with.
            system_msg (str): The system message to query the LLM with.
        """
        # Repeat msg, system_msg, msg_history num_samples times
        if isinstance(msg, str):
            msg = [msg] * num_samples
        if isinstance(system_msg, str):
            system_msg = [system_msg] * num_samples
        if len(msg_history) == 0:
            msg_history = [[]] * num_samples
        elif isinstance(msg_history[0], dict):
            msg_history = [msg_history] * num_samples

        # multiprocess sample_kwargs_query
        num_processes = min(num_samples, mp.cpu_count())
        with mp.Pool(processes=num_processes) as pool:
            # Submit all tasks asynchronously first
            async_results = []
            for i in range(len(msg)):
                query_kwargs = self._attach_headless_work_dir(llm_kwargs[i])
                async_results.append(
                    pool.apply_async(
                        query_fn,
                        args=(
                            i,
                            msg[i],
                            system_msg[i],
                            msg_history[i],
                            query_kwargs,
                            num_samples,
                            self.output_model,
                            self.verbose,
                        ),
                    )
                )

            # Then collect all results and sort by index
            results = []
            for async_result in async_results:
                try:
                    idx, result = async_result.get()
                    results.append((idx, result))
                except Exception as e:
                    logger.error(f"Error in batch query: {str(e)}")

            # Sort by index and extract just the results
            results.sort(key=lambda x: x[0])
            final_results = [r[1] for r in results if r[1] is not None]

            # Print batch total cost
            if self.verbose:
                total_cost = sum(
                    r.cost
                    for r in final_results
                    if hasattr(r, "cost") and r.cost is not None
                )
                formatted_costs = [
                    f"{r.cost:.4f}"
                    for r in final_results
                    if hasattr(r, "cost") and r.cost is not None
                ]
                logger.info(f"==> SAMPLING: Individual API costs: {formatted_costs}")
                logger.info(f"==> SAMPLING: Total API costs: ${total_cost:.4f}")
            return final_results

    def batch_kwargs_query(
        self,
        num_samples: int,
        msg: Union[str, List[str]],
        system_msg: Union[str, List[str]],
        msg_history: Union[List[Dict], List[List[Dict]]] = [],
        model_sample_probs: Optional[List[float]] = None,
    ) -> List[QueryResult]:
        """Batch query the LLM with the given message and system message.

        Args:
            msg (str): The message to query the LLM with.
            system_msg (str): The system message to query the LLM with.
            model_sample_probs (Optional[List[float]]): Sampling probabilities for each model.
        """
        # Repeat msg, system_msg, msg_history num_samples times
        if isinstance(msg, str):
            msg = [msg] * num_samples
        if isinstance(system_msg, str):
            system_msg = [system_msg] * num_samples
        if len(msg_history) == 0:
            msg_history = [[]] * num_samples
        elif isinstance(msg_history[0], dict):
            msg_history = [msg_history] * num_samples

        # Use provided probs or fall back to stored probs
        posterior = (
            model_sample_probs
            if model_sample_probs is not None
            else self.model_sample_probs
        )

        # multiprocess sample_kwargs_query
        num_processes = min(num_samples, mp.cpu_count())
        with mp.Pool(processes=num_processes) as pool:
            # Submit all tasks asynchronously first
            async_results = []
            if self.verbose:
                lines = [f"==> SAMPLING {num_samples} SAMPLES:"]
                default_probs = [1.0 / len(self.model_names)] * len(self.model_names)
                probs_to_display = posterior if posterior is not None else default_probs
                for name, prob in zip(self.model_names, probs_to_display):
                    lines.append(f"  {name:<30} {prob:>8.4f}")
                logger.info("\n".join(lines))
            for i in range(len(msg)):
                async_results.append(
                    pool.apply_async(
                        sample_kwargs_query_fn,
                        args=(
                            i,
                            msg[i],
                            system_msg[i],
                            msg_history[i],
                            self.model_names,
                            self.temperatures,
                            self.max_tokens,
                            self.reasoning_efforts,
                            posterior,
                            self.output_model,
                            num_samples,
                            self.verbose,
                            self.headless_work_dir,
                        ),
                    )
                )

            # Then collect all results and sort by index
            results = []
            for async_result in async_results:
                try:
                    idx, result = async_result.get()
                    results.append((idx, result))
                except Exception as e:
                    logger.error(f"Error in batch query: {str(e)}")

            # Sort by index and extract just the results
            results.sort(key=lambda x: x[0])
            final_results = [r[1] for r in results if r[1] is not None]

            # Print batch total cost
            if self.verbose:
                total_cost = sum(
                    r.cost
                    for r in final_results
                    if hasattr(r, "cost") and r.cost is not None
                )
                formatted_costs = [
                    f"{r.cost:.4f}"
                    for r in final_results
                    if hasattr(r, "cost") and r.cost is not None
                ]
                logger.info(f"==> SAMPLING: Individual API costs: {formatted_costs}")
                logger.info(f"==> SAMPLING: Total API costs: ${total_cost:.4f}")
            return final_results

    def get_kwargs(self, model_sample_probs: Optional[List[float]] = None):
        """Get model kwargs for sampling.

        Args:
            model_sample_probs (Optional[List[float]]): Sampling probabilities for each model.
        """
        posterior = (
            model_sample_probs
            if model_sample_probs is not None
            else self.model_sample_probs
        )
        if self.verbose:
            lines = ["==> SAMPLING:"]
            default_probs = [1.0 / len(self.model_names)] * len(self.model_names)
            probs_to_display = posterior if posterior is not None else default_probs
            for name, prob in zip(self.model_names, probs_to_display):
                lines.append(f"  {name:<30} {prob:>8.4f}")
            logger.info("\n".join(lines))
        return self._attach_headless_work_dir(
            sample_model_kwargs(
                model_names=self.model_names,
                temperatures=self.temperatures,
                max_tokens=self.max_tokens,
                reasoning_efforts=self.reasoning_efforts,
                model_sample_probs=posterior,
            )
        )

    def query(
        self,
        msg: str,
        system_msg: str,
        msg_history: List[Dict] = [],
        llm_kwargs: Optional[Dict] = None,
        model_sample_probs: Optional[List[float]] = None,
        model_posterior: Optional[List[float]] = None,
    ) -> Optional[QueryResult]:
        """Execute a single query to the LLM.

        Args:
            msg (str): The message to query the LLM with.
            system_msg (str): The system message to query the LLM with.
            msg_history (List[Dict], optional): Message history. Defaults to [].
            llm_kwargs (Dict, optional): Additional LLM parameters.
                Defaults to {}.
            model_sample_probs (Optional[List[float]]): Sampling probabilities for each model.

        Returns:
            QueryResult: The result of the query.
        """
        # Use provided probs or fall back to stored probs
        posterior = (
            model_sample_probs
            if model_sample_probs is not None
            else self.model_sample_probs
        )

        if llm_kwargs is None:
            llm_kwargs = sample_model_kwargs(
                model_names=self.model_names,
                temperatures=self.temperatures,
                max_tokens=self.max_tokens,
                reasoning_efforts=self.reasoning_efforts,
                model_sample_probs=posterior,
            )
            llm_kwargs = self._attach_headless_work_dir(llm_kwargs)
        elif "model_name" not in llm_kwargs:
            # llm_kwargs provided but missing model_name - sample one and merge
            sampled_kwargs = sample_model_kwargs(
                model_names=self.model_names,
                temperatures=self.temperatures,
                max_tokens=self.max_tokens,
                reasoning_efforts=self.reasoning_efforts,
                model_sample_probs=posterior,
            )
            # Merge: provided kwargs override sampled ones, but add model_name
            llm_kwargs = {**sampled_kwargs, **llm_kwargs}
            llm_kwargs = self._attach_headless_work_dir(llm_kwargs)
        else:
            llm_kwargs = self._attach_headless_work_dir(llm_kwargs)
        if self.verbose:
            kwargs_str = [str(v) for v in llm_kwargs.values()]
            logger.info(f"==> QUERYING: {kwargs_str}")

        # Create model_posteriors dict from full posterior (not one-hot)
        model_posteriors = None
        if model_posterior is not None:
            model_posteriors = dict(zip(self.model_names, model_posterior))
            model_posteriors = {k: float(v) for k, v in model_posteriors.items()}

        try_count = 0
        while try_count < MAX_RETRIES:
            try:
                result = query(
                    msg=msg,
                    system_msg=system_msg,
                    msg_history=msg_history,
                    output_model=self.output_model,
                    model_posteriors=model_posteriors,
                    **llm_kwargs,
                )
                if self.verbose and hasattr(result, "cost") and result.cost is not None:
                    logger.info(f"==> QUERY: API cost: ${result.cost:.4f}")
                return result
            except Exception as e:
                model_name = llm_kwargs.get("model_name", "<unknown>")
                logger.error(
                    f"{try_count + 1}/{MAX_RETRIES} Error in query "
                    f"for model={model_name} kwargs={llm_kwargs}: {str(e)}"
                )
                try_count += 1
        return None


class AsyncLLMClient:
    def __init__(
        self,
        model_names: Union[List[str], str] = "gpt-5.1",
        temperatures: Union[float, List[float]] = 0.75,
        max_tokens: Union[int, List[int]] = 4096,
        reasoning_efforts: Union[str, List[str]] = "disabled",
        model_sample_probs: Optional[List[float]] = None,
        output_model: Optional[BaseModel] = None,
        verbose: bool = True,
        headless_work_dir: Optional[str] = None,
    ):
        self.temperatures = temperatures
        self.max_tokens = max_tokens
        if isinstance(model_names, str):
            model_names = [model_names]
        self.model_names = model_names
        self.reasoning_efforts = reasoning_efforts
        self.model_sample_probs = model_sample_probs
        self.output_model = output_model
        self.structured_output = output_model is not None
        self.verbose = verbose
        self.headless_work_dir = headless_work_dir

    def _attach_headless_work_dir(self, llm_kwargs: Dict) -> Dict:
        model_name = llm_kwargs.get("model_name")
        if not model_name or self.headless_work_dir is None:
            return llm_kwargs
        if resolve_model_backend(model_name).provider != "headless":
            return llm_kwargs
        return {**llm_kwargs, "headless_work_dir": self.headless_work_dir}

    async def batch_query(
        self,
        num_samples: int,
        msg: Union[str, List[str]],
        system_msg: Union[str, List[str]],
        msg_history: Union[List[Dict], List[List[Dict]]] = [],
        llm_kwargs: List[Dict] = [],
    ) -> List[QueryResult]:
        """Batch query the LLM with the given message and system message asynchronously.

        Args:
            msg (str): The message to query the LLM with.
            system_msg (str): The system message to query the LLM with.
        """
        # Repeat msg, system_msg, msg_history num_samples times
        if isinstance(msg, str):
            msg = [msg] * num_samples
        if isinstance(system_msg, str):
            system_msg = [system_msg] * num_samples
        if len(msg_history) == 0:
            msg_history = [[]] * num_samples
        elif isinstance(msg_history[0], dict):
            msg_history = [msg_history] * num_samples

        # Create async tasks
        tasks = []
        for i in range(len(msg)):
            query_kwargs = self._attach_headless_work_dir(llm_kwargs[i])
            tasks.append(
                self._query_async_with_retry(
                    i,
                    msg[i],
                    system_msg[i],
                    msg_history[i],
                    query_kwargs,
                    num_samples,
                )
            )

        # Execute all tasks concurrently
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results and filter out exceptions
        final_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.info(f"Error in batch query task {i}: {str(result)}")
            elif result is not None and len(result) > 1 and result[1] is not None:
                final_results.append(result[1])

        # Print batch total cost
        if self.verbose:
            total_cost = sum(
                r.cost
                for r in final_results
                if hasattr(r, "cost") and r.cost is not None
            )
            formatted_costs = [
                f"{r.cost:.4f}"
                for r in final_results
                if hasattr(r, "cost") and r.cost is not None
            ]
            logger.info(f"==> SAMPLING: Individual API costs: {formatted_costs}")
            logger.info(f"==> SAMPLING: Total API costs: ${total_cost:.4f}")
        return final_results

    async def batch_kwargs_query(
        self,
        num_samples: int,
        msg: Union[str, List[str]],
        system_msg: Union[str, List[str]],
        msg_history: Union[List[Dict], List[List[Dict]]] = [],
        model_sample_probs: Optional[List[float]] = None,
    ) -> List[QueryResult]:
        """Batch query the LLM with the given message and system message asynchronously.

        Args:
            msg (str): The message to query the LLM with.
            system_msg (str): The system message to query the LLM with.
            model_sample_probs (Optional[List[float]]): Sampling probabilities for each model.
        """
        # Repeat msg, system_msg, msg_history num_samples times
        if isinstance(msg, str):
            msg = [msg] * num_samples
        if isinstance(system_msg, str):
            system_msg = [system_msg] * num_samples
        if len(msg_history) == 0:
            msg_history = [[]] * num_samples
        elif isinstance(msg_history[0], dict):
            msg_history = [msg_history] * num_samples

        # Use provided probs or fall back to stored probs
        posterior = (
            model_sample_probs
            if model_sample_probs is not None
            else self.model_sample_probs
        )

        if self.verbose:
            lines = [f"==> SAMPLING {num_samples} SAMPLES:"]
            default_probs = [1.0 / len(self.model_names)] * len(self.model_names)
            probs_to_display = posterior if posterior is not None else default_probs
            for name, prob in zip(self.model_names, probs_to_display):
                lines.append(f"  {name:<30} {prob:>8.4f}")
            logger.info("\n".join(lines))

        # Create async tasks
        tasks = []
        for i in range(len(msg)):
            tasks.append(
                self._sample_kwargs_query_async_with_retry(
                    i,
                    msg[i],
                    system_msg[i],
                    msg_history[i],
                    posterior,
                    num_samples,
                )
            )

        # Execute all tasks concurrently
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results and filter out exceptions
        final_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.info(f"Error in batch query task {i}: {str(result)}")
            elif result is not None and len(result) > 1 and result[1] is not None:
                final_results.append(result[1])

        # Print batch total cost
        if self.verbose:
            total_cost = sum(
                r.cost
                for r in final_results
                if hasattr(r, "cost") and r.cost is not None
            )
            formatted_costs = [
                f"{r.cost:.4f}"
                for r in final_results
                if hasattr(r, "cost") and r.cost is not None
            ]
            logger.info(f"==> SAMPLING: Individual API costs: {formatted_costs}")
            logger.info(f"==> SAMPLING: Total API costs: ${total_cost:.4f}")
        return final_results

    def get_kwargs(self, model_sample_probs: Optional[List[float]] = None):
        """Get model kwargs for sampling.

        Args:
            model_sample_probs (Optional[List[float]]): Sampling probabilities for each model.
        """
        posterior = (
            model_sample_probs
            if model_sample_probs is not None
            else self.model_sample_probs
        )
        if self.verbose:
            lines = ["==> SAMPLING:"]
            default_probs = [1.0 / len(self.model_names)] * len(self.model_names)
            probs_to_display = posterior if posterior is not None else default_probs
            for name, prob in zip(self.model_names, probs_to_display):
                lines.append(f"  {name:<30} {prob:>8.4f}")
            logger.info("\n".join(lines))
        return self._attach_headless_work_dir(
            sample_model_kwargs(
                model_names=self.model_names,
                temperatures=self.temperatures,
                max_tokens=self.max_tokens,
                reasoning_efforts=self.reasoning_efforts,
                model_sample_probs=posterior,
            )
        )

    async def query(
        self,
        msg: str,
        system_msg: str,
        msg_history: List[Dict] = [],
        llm_kwargs: Optional[Dict] = None,
        model_sample_probs: Optional[List[float]] = None,
        model_posterior: Optional[List[float]] = None,
    ) -> Optional[QueryResult]:
        """Execute a single query to the LLM asynchronously.

        Args:
            msg (str): The message to query the LLM with.
            system_msg (str): The system message to query the LLM with.
            msg_history (List[Dict], optional): Message history. Defaults to [].
            llm_kwargs (Dict, optional): Additional LLM parameters.
                Defaults to {}.
            model_sample_probs (Optional[List[float]]): Sampling probabilities for each model.

        Returns:
            QueryResult: The result of the query.
        """
        # Use provided probs or fall back to stored probs
        posterior = (
            model_sample_probs
            if model_sample_probs is not None
            else self.model_sample_probs
        )

        if llm_kwargs is None:
            llm_kwargs = sample_model_kwargs(
                model_names=self.model_names,
                temperatures=self.temperatures,
                max_tokens=self.max_tokens,
                reasoning_efforts=self.reasoning_efforts,
                model_sample_probs=posterior,
            )
            llm_kwargs = self._attach_headless_work_dir(llm_kwargs)
        elif "model_name" not in llm_kwargs:
            # llm_kwargs provided but missing model_name - sample one and merge
            sampled_kwargs = sample_model_kwargs(
                model_names=self.model_names,
                temperatures=self.temperatures,
                max_tokens=self.max_tokens,
                reasoning_efforts=self.reasoning_efforts,
                model_sample_probs=posterior,
            )
            # Merge: provided kwargs override sampled ones, but add model_name
            llm_kwargs = {**sampled_kwargs, **llm_kwargs}
            llm_kwargs = self._attach_headless_work_dir(llm_kwargs)
        else:
            llm_kwargs = self._attach_headless_work_dir(llm_kwargs)
        if self.verbose:
            kwargs_str = [str(v) for v in llm_kwargs.values()]
            logger.info(f"==> QUERYING: {kwargs_str}")

        # Create model_posteriors dict from full posterior (not one-hot)
        model_posteriors = None
        if model_posterior is not None:
            model_posteriors = dict(zip(self.model_names, model_posterior))
            model_posteriors = {k: float(v) for k, v in model_posteriors.items()}

        try_count = 0
        while try_count < MAX_RETRIES:
            try:
                result = await query_async(
                    msg=msg,
                    system_msg=system_msg,
                    msg_history=msg_history,
                    output_model=self.output_model,
                    model_posteriors=model_posteriors,
                    **llm_kwargs,
                )
                if self.verbose and hasattr(result, "cost") and result.cost is not None:
                    logger.info(f"==> QUERY: API cost: ${result.cost:.4f}")
                return result
            except Exception as e:
                logger.info(f"{try_count + 1}/{MAX_RETRIES} Error in query: {str(e)}")
                try_count += 1
                if try_count < MAX_RETRIES:
                    await asyncio.sleep(1)
        return None

    async def _query_async_with_retry(
        self,
        idx: int,
        msg: str,
        system_msg: str,
        msg_history: List[Dict] = [],
        kwargs: Dict = {},
        total_samples: int = 1,
    ) -> tuple[int, Optional[QueryResult]]:
        kwargs = self._attach_headless_work_dir(kwargs)
        if self.verbose:
            kwargs_str = [str(v) for v in kwargs.values()]
            logger.info(f"==> SAMPLING: {idx + 1}/{total_samples} {kwargs_str}")

        try_count = 0
        while try_count < MAX_RETRIES:
            try:
                result = await query_async(
                    msg=msg,
                    system_msg=system_msg,
                    msg_history=msg_history,
                    output_model=self.output_model,
                    **kwargs,
                )
                return idx, result
            except Exception as e:
                logger.info(f"{try_count + 1}/{MAX_RETRIES} Error in query: {str(e)}")
                try_count += 1
                if try_count == MAX_RETRIES:
                    return idx, None
                await asyncio.sleep(1)  # Add delay between retries
        return idx, None

    async def _sample_kwargs_query_async_with_retry(
        self,
        idx: int,
        msg: str,
        system_msg: str,
        msg_history: List[Dict] = [],
        model_sample_probs: Optional[List[float]] = None,
        total_samples: int = 1,
    ) -> tuple[int, Optional[QueryResult]]:
        kwargs = sample_model_kwargs(
            model_names=self.model_names,
            temperatures=self.temperatures,
            max_tokens=self.max_tokens,
            reasoning_efforts=self.reasoning_efforts,
            model_sample_probs=model_sample_probs,
        )
        kwargs = self._attach_headless_work_dir(kwargs)

        # Create model_posteriors dict from model_names and model_sample_probs
        model_posteriors = None
        if model_sample_probs is not None and isinstance(self.model_names, list):
            model_posteriors = dict(zip(self.model_names, model_sample_probs))
            model_posteriors = {k: float(v) for k, v in model_posteriors.items()}

        if self.verbose:
            kwargs_str = [str(v) for v in kwargs.values()]
            logger.info(f"==> SAMPLING: {idx + 1}/{total_samples} {kwargs_str}")

        try_count = 0
        while try_count < MAX_RETRIES:
            try:
                result = await query_async(
                    msg=msg,
                    system_msg=system_msg,
                    msg_history=msg_history,
                    output_model=self.output_model,
                    model_posteriors=model_posteriors,
                    **kwargs,
                )
                return idx, result
            except Exception as e:
                logger.info(f"{try_count + 1}/{MAX_RETRIES} Error in query: {str(e)}")
                try_count += 1
                if try_count == MAX_RETRIES:
                    return idx, None
                await asyncio.sleep(1)  # Add delay between retries
        return idx, None


def query_fn(
    idx: int,
    msg: str,
    system_msg: str,
    msg_history: List[Dict] = [],
    kwargs: Dict = {},
    total_samples: int = 1,
    output_model: Optional[BaseModel] = None,
    verbose: bool = False,
) -> tuple[int, Optional[QueryResult]]:
    if verbose:
        kwargs_str = [str(v) for v in kwargs.values()]
        logger.info(f"==> SAMPLING: {idx + 1}/{total_samples} {kwargs_str}")
    try_count = 0
    while try_count < MAX_RETRIES:
        try:
            result = query(
                msg=msg,
                system_msg=system_msg,
                msg_history=msg_history,
                output_model=output_model,
                **kwargs,
            )
            return idx, result
        except Exception as e:
            logger.error(f"{try_count + 1}/{MAX_RETRIES} Error in query: {str(e)}")
            try_count += 1
            if try_count == MAX_RETRIES:
                # Return None result after max retries
                return idx, None
            # Add a small delay between retries
            time.sleep(1)
    return idx, None


def sample_kwargs_query_fn(
    idx: int,
    msg: str,
    system_msg: str,
    msg_history: List[Dict] = [],
    model_names: Union[List[str], str] = "gpt-4o-2024-05-13",
    temperatures: Union[float, List[float]] = 0.75,
    max_tokens: Union[int, List[int]] = 4096,
    reasoning_efforts: Union[str, List[str]] = "high",
    model_sample_probs: Optional[List[float]] = None,
    output_model: Optional[BaseModel] = None,
    total_samples: int = 1,
    verbose: bool = False,
    headless_work_dir: Optional[str] = None,
) -> tuple[int, Optional[QueryResult]]:
    kwargs = sample_model_kwargs(
        model_names=model_names,
        temperatures=temperatures,
        max_tokens=max_tokens,
        reasoning_efforts=reasoning_efforts,
        model_sample_probs=model_sample_probs,
    )
    model_name = kwargs.get("model_name")
    if (
        model_name
        and headless_work_dir is not None
        and resolve_model_backend(model_name).provider == "headless"
    ):
        kwargs = {**kwargs, "headless_work_dir": headless_work_dir}

    # Create model_posteriors dict from model_names and model_sample_probs
    model_posteriors = None
    if model_sample_probs is not None and isinstance(model_names, list):
        model_posteriors = dict(zip(model_names, model_sample_probs))
        model_posteriors = {k: float(v) for k, v in model_posteriors.items()}
    if verbose:
        kwargs_str = [str(v) for v in kwargs.values()]
        logger.info(f"==> SAMPLING: {idx + 1}/{total_samples} {kwargs_str}")
    try_count = 0
    while try_count < MAX_RETRIES:
        try:
            result = query(
                msg=msg,
                system_msg=system_msg,
                msg_history=msg_history,
                output_model=output_model,
                model_posteriors=model_posteriors,
                **kwargs,
            )
            return idx, result
        except Exception as e:
            logger.error(f"{try_count + 1}/{MAX_RETRIES} Error in query: {str(e)}")
            try_count += 1
            if try_count == MAX_RETRIES:
                # Return None result after max retries
                return idx, None
            # Add a small delay between retries
            time.sleep(1)
    return idx, None


def extract_between(
    content: str,
    start: str = "<json>",
    end: str = "</json>",
    return_dict: bool = True,
    fallback: bool = False,
) -> Optional[Union[str, dict]]:
    """Extract text from between start and end tags.

    Args:
        content (str): The input string containing CUDA code

    Returns:
        str: The extracted text, or None if no text is found
    """
    match = re.search(f"{start}\\s*(.*?)\\s*{end}", content, re.DOTALL)
    if match:
        matched_str = match.group(1).strip()
        if return_dict:
            return json.loads(matched_str)
        else:
            return matched_str

    # Extracts any block between ``` and ```
    if fallback:
        match = re.search("```\\s*(.*?)\\s*```", content, re.DOTALL)
        if match:
            matched_str = match.group(1).strip()
            if return_dict:
                return json.loads(matched_str)
            else:
                return matched_str
    return "none"
