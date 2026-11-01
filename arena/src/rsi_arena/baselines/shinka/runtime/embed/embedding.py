import logging
from typing import Union, List, Optional, Tuple

import numpy as np
import pandas as pd

from rsi_arena.baselines.shinka.runtime.google_genai import google_genai_auth_mode
from .client import (
    get_async_client_embed,
    get_client_embed,
    resolve_embedding_backend,
)
from .providers.pricing import get_model_price, model_exists

logger = logging.getLogger(__name__)

# Cache for tiktoken encodings
_tiktoken_cache = {}


def _get_google_embeddings_and_cost(
    client,
    model_name: str,
    texts: List[str],
) -> Tuple[List[List[float]], float]:
    """Embed texts with Gemini and bill using Gemini token counts when available."""
    embeddings = []
    total_tokens = 0
    model = (
        model_name if google_genai_auth_mode() == "vertexai" else f"models/{model_name}"
    )
    price_per_token = get_model_price(model_name)

    for text in texts:
        token_count = None
        try:
            token_response = client.models.count_tokens(model=model, contents=text)
            token_count = getattr(token_response, "total_tokens", None)
        except Exception as exc:
            logger.warning(
                "Gemini token counting failed for %s; falling back to whitespace "
                "estimate. Error: %s",
                model_name,
                exc,
            )

        result = client.models.embed_content(model=model, contents=text)
        embeddings.append(result.embeddings[0].values)

        if token_count is None:
            token_count = len(text.split())
        total_tokens += token_count

    return embeddings, total_tokens * price_per_token


def _get_embedding_cost(response, model_name: str) -> float:
    """Compute embedding cost, defaulting to zero for dynamic models without pricing."""
    if not model_exists(model_name):
        logger.warning(
            "Embedding model '%s' has no pricing entry. Defaulting embedding cost to 0.",
            model_name,
        )
        return 0.0
    return response.usage.total_tokens * get_model_price(model_name)


def count_tokens(
    text: Union[str, List[str]],
    model: str = "text-embedding-3-small",
) -> Union[int, List[int]]:
    """
    Count tokens using tiktoken (OpenAI's tokenizer).

    Args:
        text: A string or list of strings to count tokens for.
        model: The model name to determine the encoding. Defaults to
               text-embedding-3-small which uses cl100k_base encoding.

    Returns:
        Token count (int) for a single string, or list of counts for a list.
    """
    try:
        import tiktoken
    except ImportError:
        raise ImportError(
            "tiktoken is required for accurate token counting. "
            "Install it with: pip install tiktoken"
        )

    # Get or create encoding (cached for performance)
    if model not in _tiktoken_cache:
        try:
            # Try to get encoding for specific model
            _tiktoken_cache[model] = tiktoken.encoding_for_model(model)
        except KeyError:
            # Fall back to cl100k_base (used by text-embedding-3-* models)
            _tiktoken_cache[model] = tiktoken.get_encoding("cl100k_base")

    encoding = _tiktoken_cache[model]

    if isinstance(text, str):
        return len(encoding.encode(text))
    else:
        return [len(encoding.encode(t)) for t in text]


def estimate_tokens(text: str) -> int:
    """
    Estimate token count using word count * 1.3 approximation.

    This is a fast heuristic when tiktoken is not available or speed is critical.
    For accurate counts, use count_tokens() instead.
    """
    words = len(text.split())
    return int(words * 1.3)


class EmbeddingClient:
    def __init__(
        self, model_name: str = "text-embedding-3-small", verbose: bool = False
    ):
        """
        Initialize the EmbeddingClient.

        Args:
            model_name (str): The OpenAI, Azure, or Gemini embedding model name to use.
            verbose (bool): Enable verbose logging.
        """
        self.model_name = model_name
        self.client, self.model = get_client_embed(model_name)
        self.provider = resolve_embedding_backend(model_name).provider
        self.verbose = verbose

    def count_tokens(self, text: Union[str, List[str]]) -> Union[int, List[int]]:
        """
        Count tokens using tiktoken for accurate token counting.

        Args:
            text: A string or list of strings to count tokens for.

        Returns:
            Token count (int) for single string, or list of counts for list.
        """
        return count_tokens(text, self.model_name)

    def get_embedding(
        self, code: Union[str, List[str]]
    ) -> Union[Tuple[List[float], float], Tuple[List[List[float]], float]]:
        """
        Computes the text embedding for a string or list of strings.

        Args:
            code (str, list[str]): The text as a string or list of strings.

        Returns:
            tuple: (embedding_vector(s), cost)
        """
        if isinstance(code, str):
            code = [code]
            single_code = True
        else:
            single_code = False

        # Handle Gemini models
        if self.provider == "google":
            try:
                embeddings, cost = _get_google_embeddings_and_cost(
                    client=self.client,
                    model_name=self.model,
                    texts=code,
                )

                if single_code:
                    return embeddings[0] if embeddings else [], cost
                else:
                    return embeddings, cost
            except Exception as e:
                logger.error(f"Error getting Gemini embedding: {e}")
                if single_code:
                    return [], 0.0
                else:
                    return [[]], 0.0

        # Handle OpenAI and Azure models (same interface)
        try:
            response = self.client.embeddings.create(
                model=self.model, input=code, encoding_format="float"
            )
            cost = _get_embedding_cost(response, self.model_name)
            # Extract embedding from response
            if single_code:
                return response.data[0].embedding, cost
            else:
                return [d.embedding for d in response.data], cost
        except Exception as e:
            logger.info(f"Error getting embedding: {e}")
            if single_code:
                return [], 0.0
            else:
                return [[]], 0.0

    def get_column_embedding(
        self,
        df: pd.DataFrame,
        column_name: Union[str, List[str]],
    ) -> pd.DataFrame:
        """
        Computes the text embedding for a batch of strings in DataFrame columns.

        Args:
            df (pd.DataFrame): A pandas DataFrame with the column to embed.
            column_name (str, list): The name of the columns to embed.

        Returns:
            pd.DataFrame: A pandas DataFrame containing the embedded column.
        """
        if isinstance(column_name, str):
            column_name = [column_name]

        for col in column_name:
            model_name_str = self.model.replace("-", "_")
            new_col_name = f"{col}_embedding_{model_name_str}"
            df[new_col_name] = df[col].apply(
                lambda x: self.get_embedding(x),
            )
        return df

    def get_closest_k_neighbors(
        self,
        new_str_query: str,
        embeddings: list,
        top_k: Union[int, str] = 5,
    ) -> tuple[list, list]:
        """Get k closest neighbors from the embeddings list

        Args:
            new_str_query: The string to get the closest neighbors for.
            embeddings: The list of embeddings to compare against.
            top_k: The number of closest neighbors to return.

        Returns:
            A tuple of the top k indices and the top k similarities.
        """
        # get embedding of the new string
        new_embedding, _ = self.get_embedding(new_str_query)

        if not new_embedding:  # Handle case where embedding fails
            return [], []

        # define cosine similarity
        def cosine_similarity(a, b):
            return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))

        # compute the cosine similarity between the new embed
        similarities = [
            cosine_similarity(new_embedding, embedding) for embedding in embeddings
        ]

        # get the top k neighbors or random rows
        if top_k == "random":
            if len(similarities) < 5:
                top_idx = np.random.choice(
                    len(similarities), size=len(similarities), replace=False
                )
            else:
                top_idx = np.random.choice(len(similarities), size=5, replace=False)
            similarities_subset = [similarities[i] for i in top_idx]
            return top_idx.tolist(), similarities_subset
        elif isinstance(top_k, int):
            top_idx = np.argsort(similarities)[-top_k:]
            similarities_subset = [similarities[i] for i in top_idx]
            return top_idx[::-1].tolist(), similarities_subset[::-1]
        else:
            raise ValueError("top_k must be an int or 'random'")

    def get_dim_reduction(
        self,
        embeddings: list,
        method: str = "pca",
        dims: int = 2,
    ):
        """Performs dimensionality reduction on a list of embeddings using
        various methods.

        Args:
            embeddings: List of embedding vectors
            method: Dimensionality reduction method ('pca', 'umap', or 'tsne')
            dims: Number of dimensions to reduce to

        Returns:
            The transformed embeddings in reduced dimensionality
        """
        if isinstance(embeddings, pd.Series):
            embeddings = embeddings.tolist()

        # Convert list to numpy array if needed
        X = np.array(embeddings) if isinstance(embeddings, list) else embeddings
        # preprocess the embeddings using standard scaler
        from sklearn.preprocessing import StandardScaler

        scaler = StandardScaler()
        X = scaler.fit_transform(X)

        if method.lower() == "pca":
            from sklearn.decomposition import PCA

            model = PCA(n_components=dims)
            return model.fit_transform(X)
        elif method.lower() == "umap":
            from umap import UMAP

            model = UMAP(n_components=dims, random_state=42)
            return model.fit_transform(X)
        elif method.lower() == "tsne":
            from sklearn.manifold import TSNE

            model = TSNE(n_components=dims, random_state=42)
            return model.fit_transform(X)
        else:
            raise ValueError("Method must be one of: 'pca', 'umap', 'tsne'")

    def get_embedding_clusters(
        self,
        embeddings: list,
        num_clusters: int = 4,
        verbose: bool = False,
    ) -> list:
        """
        Performs clustering on a list of embeddings using Gaussian Mixture Model.

        Args:
            embeddings: List of embedding vectors
            num_clusters: Number of clusters to form with GMM.
            verbose: If True, prints detailed cluster information.

        Returns:
            list: Cluster assignments for each embedding.
        """
        from sklearn.mixture import GaussianMixture

        # Perform GMM clustering on the PCA-reduced embeddings
        gmm = GaussianMixture(n_components=num_clusters, random_state=42)
        gmm.fit(embeddings)
        clusters = gmm.predict(embeddings)

        # Optionally display detailed cluster information
        if verbose:
            logger.info(
                f"GMM {num_clusters} Clusters ==> Got {len(embeddings)} "
                f"embeddings with cluster assignments:"
            )
            num_members = pd.Series(clusters).value_counts()
            logger.info(num_members)

        return clusters

    def plot_reduced_embeddings(
        self,
        embeddings: list,
        method: str = "pca",
        num_dims: int = 3,
        title="Embedding",
        cluster_ids: Optional[list] = None,
        cluster_label: str = "Cluster",
        patch_type: Optional[list] = None,
    ):
        transformed = self.get_dim_reduction(embeddings, method, num_dims)

        if num_dims == 2:
            fig, ax = plot_2d_scatter(
                transformed, title, cluster_ids, cluster_label, patch_type
            )
        elif num_dims == 3:
            fig, ax = plot_3d_scatter(
                transformed, title, cluster_ids, cluster_label, patch_type
            )
        else:
            raise ValueError(f"Invalid number of dimensions: {num_dims}")

        return fig, ax


class AsyncEmbeddingClient:
    """Async version of EmbeddingClient for non-blocking API calls."""

    def __init__(
        self, model_name: str = "text-embedding-3-small", verbose: bool = False
    ):
        """
        Initialize the AsyncEmbeddingClient.

        Args:
            model_name (str): The OpenAI, Azure, or Gemini embedding model name to use.
            verbose (bool): Enable verbose logging.
        """
        self.model_name = model_name
        self.async_client, self.model = get_async_client_embed(model_name)
        self.provider = resolve_embedding_backend(model_name).provider
        self.verbose = verbose

    async def embed_async(
        self, code: Union[str, List[str]]
    ) -> Union[Tuple[List[float], float], Tuple[List[List[float]], float]]:
        """
        Asynchronously compute text embeddings.

        Args:
            code (str, list[str]): The code as a string or list of strings.

        Returns:
            tuple: (embedding_vector(s), cost)
        """
        if isinstance(code, str):
            code = [code]
            single_code = True
        else:
            single_code = False

        # Handle Gemini models (no async API yet, use thread pool)
        if self.provider == "google":
            import asyncio

            try:
                embeddings, cost = await asyncio.to_thread(
                    _get_google_embeddings_and_cost,
                    client=self.async_client,
                    model_name=self.model,
                    texts=code,
                )
                if single_code:
                    return embeddings[0] if embeddings else [], cost
                else:
                    return embeddings, cost
            except Exception as e:
                logger.error(f"Error getting Gemini embedding: {e}")
                if single_code:
                    return [], 0.0
                else:
                    return [[]], 0.0

        # Handle OpenAI and Azure models (true async)
        try:
            response = await self.async_client.embeddings.create(
                model=self.model, input=code, encoding_format="float"
            )
            cost = _get_embedding_cost(response, self.model_name)
            if single_code:
                return response.data[0].embedding, cost
            else:
                return [d.embedding for d in response.data], cost
        except Exception as e:
            logger.error(f"Error getting embedding: {e}")
            if single_code:
                return [], 0.0
            else:
                return [[]], 0.0

    def get_embedding(
        self, code: Union[str, List[str]]
    ) -> Union[Tuple[List[float], float], Tuple[List[List[float]], float]]:
        """
        Synchronous wrapper for compatibility.
        Note: This defeats the purpose of async - use embed_async() instead.
        """
        import asyncio

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're already in an async context, can't use run_until_complete
                raise RuntimeError(
                    "Cannot call get_embedding from within async context. Use embed_async() instead."
                )
            return loop.run_until_complete(self.embed_async(code))
        except RuntimeError:
            # Create new event loop if needed
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(self.embed_async(code))
            finally:
                loop.close()


def plot_2d_scatter(
    transformed: np.ndarray,
    title: str = "Embedding",
    cluster_ids: Optional[list] = None,
    cluster_label: str = "Cluster",
    patch_type: Optional[list] = None,
):
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap
    from matplotlib.lines import Line2D

    # Create figure and 2D axes with adjusted size and spacing
    fig, ax = plt.subplots(figsize=(10, 7))

    # Prepare cluster IDs and colormap
    if cluster_ids is not None:
        original_unique_ids, cluster_ids_for_coloring = np.unique(
            cluster_ids, return_inverse=True
        )
        num_distinct_colors = len(original_unique_ids)
    else:
        cluster_ids_for_coloring = np.zeros(transformed.shape[0])
        original_unique_ids = [0]
        num_distinct_colors = 1

    # Create discrete colormap
    base_colors = [
        "green",
        "red",
        "blue",
        "yellow",
        "purple",
        "orange",
        "brown",
        "pink",
        "gray",
        "cyan",
    ]
    if num_distinct_colors > 0:
        multiplier = (num_distinct_colors - 1) // len(base_colors) + 1
        extended_colors = base_colors * multiplier
        colors_for_cmap = extended_colors[:num_distinct_colors]
    else:
        colors_for_cmap = ["blue"]

    cmap = ListedColormap(colors_for_cmap)

    marker_shapes = ["o", "s", "^", "P", "X", "D", "v", "<", ">"]

    if patch_type is not None:
        patch_type_array = np.array(patch_type)
        unique_patches = np.unique(patch_type_array)

        for i, patch_val in enumerate(unique_patches):
            patch_mask = patch_type_array == patch_val
            current_marker = marker_shapes[i % len(marker_shapes)]

            c_val_scatter = None
            cmap_val_scatter = None
            if cluster_ids is not None:
                c_val_scatter = cluster_ids_for_coloring[patch_mask]
                cmap_val_scatter = cmap

            label_text = str(patch_val)

            scatter_args = {
                "marker": current_marker,
                "alpha": 0.6,
                "s": 100,
                "label": label_text,
            }
            if c_val_scatter is not None:
                scatter_args["c"] = c_val_scatter
                scatter_args["cmap"] = cmap_val_scatter

            ax.scatter(
                transformed[patch_mask, 0],
                transformed[patch_mask, 1],
                **scatter_args,
            )
    else:
        c_val_scatter_else = None
        if cluster_ids is not None:
            c_val_scatter_else = cluster_ids_for_coloring

        scatter_args_else = {"marker": "o", "alpha": 0.6, "s": 100}
        if c_val_scatter_else is not None:
            scatter_args_else["c"] = c_val_scatter_else
            scatter_args_else["cmap"] = cmap

        ax.scatter(
            transformed[:, 0],
            transformed[:, 1],
            **scatter_args_else,
        )

    # Add labels and title with adjusted padding
    ax.set_xlabel("1st Latent Dim.", fontsize=20)
    ax.set_ylabel("2nd Latent Dim.", fontsize=20)
    ax.set_title(title, fontsize=30)

    # no spines for right and top
    ax.spines["right"].set_visible(False)
    ax.spines["top"].set_visible(False)

    # Add colorbar with discrete levels
    if cluster_ids is not None:
        try:
            ax.scatter(
                transformed[:, 0],
                transformed[:, 1],
                c=cluster_ids_for_coloring,
                cmap=cmap,
                s=0,
                alpha=0,
            )
        except Exception:
            pass

    if patch_type is not None:
        legend_handles = []
        unique_patches_for_legend = np.unique(np.array(patch_type))
        for i, patch_val in enumerate(unique_patches_for_legend):
            legend_handles.append(
                Line2D(
                    [0],
                    [0],
                    marker=marker_shapes[i % len(marker_shapes)],
                    color="black",
                    label=str(patch_val),
                    linestyle="None",
                    markersize=10,
                )
            )
        if legend_handles:
            ax.legend(handles=legend_handles, title="Patch Types", loc="best")

    fig.tight_layout()

    return fig, ax


def plot_3d_scatter(
    transformed: np.ndarray,
    title: str = "Embedding",
    cluster_ids: Optional[list] = None,
    cluster_label: str = "Cluster",
    patch_type: Optional[list] = None,
):
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from matplotlib.colors import ListedColormap

    # Create figure and 3D axes with adjusted size and spacing
    fig = plt.figure(figsize=(8, 6))
    ax = fig.add_subplot(111, projection="3d", computed_zorder=False)

    # Prepare cluster IDs and colormap
    if cluster_ids is not None:
        original_unique_ids, cluster_ids_for_coloring = np.unique(
            cluster_ids, return_inverse=True
        )
        num_distinct_colors = len(original_unique_ids)
    else:
        cluster_ids_for_coloring = np.zeros(transformed.shape[0])
        original_unique_ids = [0]
        num_distinct_colors = 1

    # Create discrete colormap
    base_colors = [
        "green",
        "red",
        "blue",
        "yellow",
        "purple",
        "orange",
        "brown",
        "pink",
        "gray",
        "cyan",
    ]
    if num_distinct_colors > 0:
        multiplier = (num_distinct_colors - 1) // len(base_colors) + 1
        extended_colors = base_colors * multiplier
        colors_for_cmap = extended_colors[:num_distinct_colors]
    else:
        colors_for_cmap = ["blue"]

    cmap = ListedColormap(colors_for_cmap)

    marker_shapes = ["o", "s", "^", "P", "X", "D", "v", "<", ">"]

    if patch_type is not None:
        patch_type_array = np.array(patch_type)
        unique_patches = np.unique(patch_type_array)

        for i, patch_val in enumerate(unique_patches):
            patch_mask = patch_type_array == patch_val
            current_marker = marker_shapes[i % len(marker_shapes)]

            c_val_scatter = None
            cmap_val_scatter = None
            if cluster_ids is not None:
                c_val_scatter = cluster_ids_for_coloring[patch_mask]
                cmap_val_scatter = cmap

            label_text = str(patch_val)

            scatter_args = {
                "marker": current_marker,
                "alpha": 0.6,
                "s": 20,
                "label": label_text,
            }
            if c_val_scatter is not None:
                scatter_args["c"] = c_val_scatter
                scatter_args["cmap"] = cmap_val_scatter

            ax.scatter(
                transformed[patch_mask, 0],
                transformed[patch_mask, 1],
                transformed[patch_mask, 2],
                **scatter_args,
            )
    else:
        c_val_scatter_else = None
        if cluster_ids is not None:
            c_val_scatter_else = cluster_ids_for_coloring

        scatter_args_else = {
            "marker": "o",
            "alpha": 0.6,
            "s": 20,
        }
        if c_val_scatter_else is not None:
            scatter_args_else["c"] = c_val_scatter_else
            scatter_args_else["cmap"] = cmap

        ax.scatter(
            transformed[:, 0],
            transformed[:, 1],
            transformed[:, 2],
            **scatter_args_else,
        )

    # Add labels and title with adjusted padding
    ax.set_xlabel("1st Latent Dim.", labelpad=-15, fontsize=8)
    ax.set_ylabel("2nd Latent Dim.", labelpad=-15, fontsize=8)
    ax.set_zlabel("3rd Latent Dim.", labelpad=-17, rotation=90, fontsize=8)
    ax.set_title(title, y=0.95)

    # Add colorbar with discrete levels
    if cluster_ids is not None:
        try:
            ax.scatter(
                transformed[:, 0],
                transformed[:, 1],
                transformed[:, 2],
                c=cluster_ids_for_coloring,
                cmap=cmap,
                s=0,
                alpha=0,
            )
        except Exception:
            pass

    if patch_type is not None:
        legend_handles_3d = []
        unique_patches_for_legend_3d = np.unique(np.array(patch_type))
        for i, patch_val in enumerate(unique_patches_for_legend_3d):
            legend_handles_3d.append(
                Line2D(
                    [0],
                    [0],
                    marker=marker_shapes[i % len(marker_shapes)],
                    color="black",
                    label=str(patch_val),
                    linestyle="None",
                    markersize=10,
                )
            )
        if legend_handles_3d:
            ax.legend(
                handles=legend_handles_3d,
                title="Patch Types",
                loc="best",
                bbox_to_anchor=(0.9, 0.5),
            )

    # Adjust the view angle for better visualization
    ax.view_init(elev=20, azim=45)

    # Adjust layout with specific spacing
    plt.subplots_adjust(left=0.05, right=0.9, top=0.9, bottom=0.05)
    fig.tight_layout()
    return fig, ax
