"""
Adapted from SakanaAI/ShinkaEvolve (Apache-2.0 License)
Original source: https://github.com/SakanaAI/ShinkaEvolve/blob/main/shinka/llm/embedding.py
"""

import logging
import os
from typing import List, Union

import openai

logger = logging.getLogger(__name__)

M = 1_000_000

OPENAI_EMBEDDING_MODELS = [
    "text-embedding-3-small",
    "text-embedding-3-large",
]

AZURE_EMBEDDING_MODELS = [
    "azure-text-embedding-3-small",
    "azure-text-embedding-3-large",
]

GEMINI_EMBEDDING_MODELS = [
    "gemini-embedding-001",
]

OPENAI_EMBEDDING_COSTS = {
    "text-embedding-3-small": 0.02 / M,
    "text-embedding-3-large": 0.13 / M,
}


class EmbeddingClient:
    def __init__(self, model_name: str = "text-embedding-3-small"):
        """
        Initialize the EmbeddingClient.

        Args:
            model (str): The OpenAI embedding model name to use.
        """
        self.client, self.model = self._get_client_model(model_name)

    def _get_client_model(self, model_name: str) -> tuple[openai.OpenAI, str]:
        if model_name in OPENAI_EMBEDDING_MODELS:
            # Use OPENAI_EMBEDDING_API_KEY if set, otherwise fall back to OPENAI_API_KEY
            # This allows users to use OpenRouter for LLMs while using OpenAI for embeddings
            embedding_api_key = os.getenv("OPENAI_EMBEDDING_API_KEY") or os.getenv("OPENAI_API_KEY")
            client = openai.OpenAI(api_key=embedding_api_key)
            model_to_use = model_name
        elif model_name in AZURE_EMBEDDING_MODELS:
            # get rid of the azure- prefix
            model_to_use = model_name.split("azure-")[-1]
            client = openai.AzureOpenAI(
                api_key=os.getenv("AZURE_OPENAI_API_KEY"),
                api_version=os.getenv("AZURE_API_VERSION"),
                azure_endpoint=os.getenv("AZURE_API_ENDPOINT"),
            )
        elif model_name in GEMINI_EMBEDDING_MODELS:
            gemini_api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
            client = openai.OpenAI(
                api_key=gemini_api_key,
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            )
            model_to_use = model_name
        else:
            raise ValueError(f"Invalid embedding model: {model_name}")

        return client, model_to_use

    def get_embedding(self, code: Union[str, List[str]]) -> Union[List[float], List[List[float]]]:
        """
        Computes the text embedding for a code string.

        Args:
            code (str, list[str]): The code as a string or list
                of strings.

        Returns:
            list: Embedding vector for the code or None if an error
                occurs.
        """
        if isinstance(code, str):
            code = [code]
            single_code = True
        else:
            single_code = False
        try:
            response = self.client.embeddings.create(
                model=self.model, input=code, encoding_format="float"
            )
            # Extract embedding from response
            if single_code:
                return response.data[0].embedding
            else:
                return [d.embedding for d in response.data]
        except Exception as e:
            logger.info(f"Error getting embedding: {e}")
            if single_code:
                return [], 0.0
            else:
                return [[]], 0.0
