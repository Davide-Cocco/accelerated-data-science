#!/usr/bin/env python
# -*- coding: utf-8 -*--

# Copyright (c) 2023, 2024 Oracle and/or its affiliates.
# Licensed under the Universal Permissive License v 1.0 as shown at https://oss.oracle.com/licenses/upl/

import logging
from typing import Any, Dict, List, Optional

from langchain.callbacks.manager import CallbackManagerForLLMRun

from ads.llm.langchain.plugins.base import BaseLLM, GenerativeAiClientModel
from ads.llm.langchain.plugins.contant import Task

logger = logging.getLogger(__name__)


class GenerativeAI(GenerativeAiClientModel, BaseLLM):
    """GenerativeAI Service.

    To use, you should have the ``oci`` python package installed.

    Example
    -------

    .. code-block:: python

        from ads.llm import GenerativeAI

        gen_ai = GenerativeAI(compartment_id="ocid1.compartment.oc1..<ocid>")

    """

    task: str = "text_generation"
    """Task can be either text_generation or text_summarization."""

    model: Optional[str] = "cohere.command"
    """Model name to use."""

    frequency_penalty: float = None
    """Penalizes repeated tokens according to frequency. Between 0 and 1."""

    presence_penalty: float = None
    """Penalizes repeated tokens. Between 0 and 1."""

    truncate: Optional[str] = None
    """Specify how the client handles inputs longer than the maximum token."""

    length: str = "AUTO"
    """Indicates the approximate length of the summary. """

    format: str = "PARAGRAPH"
    """Indicates the style in which the summary will be delivered - in a free form paragraph or in bullet points."""

    extractiveness: str = "AUTO"
    """Controls how close to the original text the summary is. High extractiveness summaries will lean towards reusing sentences verbatim, while low extractiveness summaries will tend to paraphrase more."""

    additional_command: str = ""
    """A free-form instruction for modifying how the summaries get generated. """

    @property
    def _identifying_params(self) -> Dict[str, Any]:
        """Get the identifying parameters."""
        return {
            **{
                "model": self.model,
                "task": self.task,
                "client_kwargs": self.client_kwargs,
                "endpoint_kwargs": self.endpoint_kwargs,
            },
            **self._default_params,
        }

    @property
    def _llm_type(self) -> str:
        """Return type of llm."""
        return "GenerativeAI"

    @property
    def _default_params(self) -> Dict[str, Any]:
        """Get the default parameters for calling OCIGenerativeAI API."""
        # This property is used by _identifying_params(), which then used for serialization
        # All parameters returning here should be JSON serializable.

        return (
            {
                "compartment_id": self.compartment_id,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "top_k": self.k,
                "top_p": self.p,
                "frequency_penalty": self.frequency_penalty,
                "presence_penalty": self.presence_penalty,
                "truncate": self.truncate,
            }
            if self.task == Task.TEXT_GENERATION
            else {
                "compartment_id": self.compartment_id,
                "temperature": self.temperature,
                "length": self.length,
                "format": self.format,
                "extractiveness": self.extractiveness,
                "additional_command": self.additional_command,
            }
        )

    def _invocation_params(self, stop: Optional[List[str]], **kwargs: Any) -> dict:
        params = self._default_params
        if self.task == Task.TEXT_SUMMARIZATION:
            return {**params}

        if self.stop is not None and stop is not None:
            raise ValueError("`stop` found in both the input and default params.")
        elif self.stop is not None:
            params["stop_sequences"] = self.stop
        else:
            params["stop_sequences"] = stop
        return {**params, **kwargs}

    def _call(
        self,
        prompt: str,
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ):
        """Call out to GenerativeAI's generate endpoint.

        Parameters
        ----------
        prompt (str):
            The prompt to pass into the model.
        stop (List[str], Optional):
            List of stop words to use when generating.

        Returns
        -------
        The string generated by the model.

        Example
        -------

            .. code-block:: python

                response = gen_ai("Tell me a joke.")
        """

        params = self._invocation_params(stop, **kwargs)
        self._print_request(prompt, params)

        try:
            completion = self.completion_with_retry(prompt=prompt, **params)
        except Exception:
            logger.error(
                "Error occur when invoking oci service api."
                "DEBUG INTO: task=%s, params=%s, prompt=%s",
                self.task,
                params,
                prompt,
            )
            raise

        return completion

    def _text_generation(self, request_class, serving_mode, **kwargs):
        from oci.generative_ai_inference.models import (
            GenerateTextDetails,
            GenerateTextResult,
        )

        compartment_id = kwargs.pop("compartment_id")
        inference_request = request_class(**kwargs)
        response = self.client.generate_text(
            GenerateTextDetails(
                compartment_id=compartment_id,
                serving_mode=serving_mode,
                inference_request=inference_request,
            ),
            **self.endpoint_kwargs,
        ).data
        response: GenerateTextResult
        return response.inference_response

    def _cohere_completion(self, serving_mode, **kwargs) -> str:
        from oci.generative_ai_inference.models import (
            CohereLlmInferenceRequest,
            CohereLlmInferenceResponse,
        )

        response = self._text_generation(
            CohereLlmInferenceRequest, serving_mode, **kwargs
        )
        response: CohereLlmInferenceResponse
        if kwargs.get("num_generations", 1) == 1:
            completion = response.generated_texts[0].text
        else:
            completion = [result.text for result in response.generated_texts]
        self._print_response(completion, response)
        return completion

    def _llama_completion(self, serving_mode, **kwargs) -> str:
        from oci.generative_ai_inference.models import (
            LlamaLlmInferenceRequest,
            LlamaLlmInferenceResponse,
        )

        # truncate and stop_sequence are not supported.
        kwargs.pop("truncate", None)
        kwargs.pop("stop_sequences", None)
        # top_k must be >1 or -1
        if "top_k" in kwargs and kwargs["top_k"] == 0:
            kwargs.pop("top_k")

        # top_p must be 1 when temperature is 0
        if kwargs.get("temperature") == 0:
            kwargs["top_p"] = 1

        response = self._text_generation(
            LlamaLlmInferenceRequest, serving_mode, **kwargs
        )
        response: LlamaLlmInferenceResponse
        if kwargs.get("num_generations", 1) == 1:
            completion = response.choices[0].text
        else:
            completion = [result.text for result in response.choices]
        self._print_response(completion, response)
        return completion

    def _cohere_summarize(self, serving_mode, **kwargs) -> str:
        from oci.generative_ai_inference.models import SummarizeTextDetails

        kwargs["input"] = kwargs.pop("prompt")

        response = self.client.summarize_text(
            SummarizeTextDetails(serving_mode=serving_mode, **kwargs),
            **self.endpoint_kwargs,
        )
        return response.data.summary

    def completion_with_retry(self, **kwargs: Any) -> Any:
        from oci.generative_ai_inference.models import OnDemandServingMode

        serving_mode = OnDemandServingMode(model_id=self.model)

        if self.task == Task.TEXT_SUMMARIZATION:
            return self._cohere_summarize(serving_mode, **kwargs)
        elif self.model.startswith("cohere"):
            return self._cohere_completion(serving_mode, **kwargs)
        elif self.model.startswith("meta.llama"):
            return self._llama_completion(serving_mode, **kwargs)
        raise ValueError(f"Model {self.model} is not supported.")

    def batch_completion(
        self,
        prompt: str,
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        num_generations: int = 1,
        **kwargs: Any,
    ) -> List[str]:
        """Generates multiple completion for the given prompt.

        Parameters
        ----------
        prompt (str):
            The prompt to pass into the model.
        stop: (List[str], optional):
            Optional list of stop words to use when generating. Defaults to None.
        num_generations (int, optional):
            Number of completions aims to get. Defaults to 1.

        Raises
        ------
        NotImplementedError
            Raise when invoking batch_completion under summarization task.

        Returns
        -------
        List[str]
            List of multiple completions.

        Example
        -------

            .. code-block:: python

                responses = gen_ai.batch_completion("Tell me a joke.", num_generations=5)

        """
        if self.task == Task.TEXT_SUMMARIZATION:
            raise NotImplementedError(
                f"task={Task.TEXT_SUMMARIZATION} does not support batch_completion. "
            )

        return self._call(
            prompt=prompt,
            stop=stop,
            run_manager=run_manager,
            num_generations=num_generations,
            **kwargs,
        )