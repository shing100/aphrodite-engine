import random
from array import array
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import torch

from aphrodite.common.sampling_params import SamplingParams, SamplingType
from aphrodite.common.sequence import SequenceData, SequenceGroupMetadata
from aphrodite.common.utils import (PyObjectCache, async_tensor_h2d,
                                    is_pin_memory_available,
                                    make_tensor_with_pad, maybe_expand_dim)
from aphrodite.triton_utils.sample import get_num_triton_sampler_splits

_SAMPLING_EPS = 1e-5
_SEED_0_REPLACEMENT = 3403598558
# Some triton sampler related code is guarded before it is ready.
_USE_TRITON_SAMPLER = False


@dataclass
class SequenceGroupToSample:
    # |---------- N-1 iteration --------|
    # |---------------- N iteration ---------------------|
    # |- tokenA -|......................|-- newTokens ---|
    # |---------- context_len ----------|
    # |-------------------- seq_len ----------------------|
    #                                   |-- query_len ---|
    # Sequence ids for the sequence group in a previous step.
    seq_ids: List[int]
    sampling_params: SamplingParams
    # seq_id -> sequence data.
    seq_data: Dict[int, SequenceData]
    # The length of the sequence (all tokens seen in the past + new token to
    # compute attention) of the sequence group. None if it is in a decode
    # stage.
    seq_len: Optional[int]
    # The length of new query tokens to compute in the current step. None if it
    # is in a decode stage. The length of query_len <= seq_len if chunked
    # prefill is enabled.
    query_len: Optional[int]
    # A random number generator for sampling.
    generator: Optional[torch.Generator]
    # True if the sequence group is in prefill stage. False if it is in a
    # decode stage.
    is_prompt: bool
    # Query token indices from logits. to compute prompt logprob. Empty if
    # prompt logprob is not required.
    prompt_logprob_indices: List[int]
    # Sample token indices from logits. Empty if sampling is not required.
    sample_indices: List[int]

    @property
    def do_sample(self):
        return len(self.sample_indices) > 0

    def __post_init__(self):
        if len(self.prompt_logprob_indices) > 0:
            assert self.sampling_params.prompt_logprobs is not None
        if self.is_prompt:
            assert self.seq_len is not None
            assert self.query_len is not None


def gen_seq_group_to_sample_builder(num_seqs: int):
    return lambda: SequenceGroupToSample(
        seq_ids=[0] * num_seqs,
        sampling_params=None,
        seq_data=None,  # type: ignore
        seq_len=0,
        query_len=0,
        generator=None,
        is_prompt=True,
        prompt_logprob_indices=[],
        sample_indices=[])


class SamplingMetadataCache:
    """Used to cache SamplingMetadata objects between scheduler iterations
    """

    def __init__(self):
        self._seq_group_to_sample_cache: Dict[int, PyObjectCache] = {}

    def get_cached_seq_group_to_sample(self, num_seqs):
        if num_seqs not in self._seq_group_to_sample_cache:
            self._seq_group_to_sample_cache[num_seqs] = PyObjectCache(
                gen_seq_group_to_sample_builder(num_seqs))

        obj = self._seq_group_to_sample_cache[num_seqs].get_object()
        return obj

    def reset(self):
        for cache in self._seq_group_to_sample_cache.values():
            cache.reset()


class SamplingMetadata:
    """Metadata for input sequences. Used in sampler.

    The usage is as follow;
    ```
    hidden_states = execute_model(...)
    logits = hidden_states[sampling_metadata.selected_token_indices]
    sample(logits)

    def sample(logits):
        # Use categorized_sample_indices for sampling....
    ```

    Args:
        seq_groups: List of batched sequence groups.
        selected_token_indices: (num_query_tokens_to_logprob). Indices to find
            logits from the initial model output hidden states.
        categorized_sample_indices: SamplingType -> token indices to sample.
            Each token indices is 2D tensor of (num_indices, num_indices) where
            the first item means the sample index within the returned logit
            (before pruning padding), and the second item means the sample
            index after pruning using selected_token_indices.
            For example, if the returned logit is [1, 2, 3], and we select
            [1, 2] for sampling, the pruned logit will be [2, 3]. In this case,
            The first tuple is [1, 2] (sampled index within original logit),
            and the second tuple is [0, 1] (sampled index within pruned logit).
        num_prompts: Number of prompt sequence groups in seq_groups.
        skip_sampler_cpu_output: Indicates if we want to skip the GPU=>CPU 
            serialization of token outputs.
        reuse_sampling_tensors: Indicates if we want to reuse sampling 
            tensors that are part of the sampler forward pass. Currently,
            it is mainly used for multi-step decode.
    """

    def __init__(
        self,
        seq_groups: List[SequenceGroupToSample],
        selected_token_indices: torch.Tensor,
        categorized_sample_indices: Dict[SamplingType, torch.Tensor],
        num_prompts: int,
        skip_sampler_cpu_output: bool = False,
        reuse_sampling_tensors: bool = False,
    ) -> None:
        self.seq_groups = seq_groups
        self.selected_token_indices = selected_token_indices
        self.categorized_sample_indices = categorized_sample_indices
        self.num_prompts = num_prompts
        self.skip_sampler_cpu_output = skip_sampler_cpu_output
        self.reuse_sampling_tensors = reuse_sampling_tensors

    @staticmethod
    def prepare(
        seq_group_metadata_list: List[SequenceGroupMetadata],
        seq_lens: List[int],
        query_lens: Optional[List[int]],
        device: str,
        pin_memory: bool,
        generators: Optional[Dict[str, torch.Generator]] = None,
        cache: Optional[SamplingMetadataCache] = None
    ) -> "SamplingMetadata":
        (
            seq_groups,
            selected_token_indices,
            categorized_sample_indices,
            num_prompts,
        ) = _prepare_seq_groups(seq_group_metadata_list, seq_lens, query_lens,
                                device, generators, cache)
        selected_token_indices = async_tensor_h2d(selected_token_indices,
                                                  dtype=torch.long,
                                                  target_device=device,
                                                  pin_memory=pin_memory)
        categorized_sample_indices = {
            t: maybe_expand_dim(
                async_tensor_h2d(seq_ids,
                                 dtype=torch.int,
                                 target_device=device,
                                 pin_memory=pin_memory), 2, 2)
            for t, seq_ids in categorized_sample_indices.items()
        }

        sampling_metadata = SamplingMetadata(
            seq_groups=seq_groups,
            selected_token_indices=selected_token_indices,
            categorized_sample_indices=categorized_sample_indices,
            num_prompts=num_prompts,
        )
        return sampling_metadata

    def __repr__(self) -> str:
        return (
            "SamplingMetadata("
            f"seq_groups={self.seq_groups}, "
            f"selected_token_indices={self.selected_token_indices}, "
            f"categorized_sample_indices={self.categorized_sample_indices}), ")


def _prepare_seq_groups(
    seq_group_metadata_list: List[SequenceGroupMetadata],
    seq_lens: List[int],
    query_lens: Optional[List[int]],
    device: str,
    generators: Optional[Dict[str, torch.Generator]] = None,
    cache: Optional[SamplingMetadataCache] = None,
) -> Tuple[List[SequenceGroupToSample], List[int], Dict[
        SamplingType, List[Tuple[int, int]]], int]:
    """Prepare sequence groups and indices for sampling.

    Args:
        seq_group_metadata_list: A list of sequence group to batch.
        seq_lens: A list of sequence lens per sequence group.
            Index of prompt len should match with seq_group_metadata_list.
        query_lens: A list of query lengths. Prompt lens include the length
            of entire prompt tokens, and it could be shorter.
        device: A device to use for random number generators,
            `SequenceGroupToSample.generator`.
        generators: A store of per-request random number generators used
            for seeded requests.

    Returns:
        seq_groups: A list of sequence group to sample.
        selected_token_indices: See the definition from `SamplingMetadata`.
        categorized_sample_indices: See the definition from `SamplingMetadata`.
        num_prompts: Total number of prompts from `seq_group_metadata_list`.
    """
    # Batched sequence groups for the current model forward stsep.
    seq_groups: List[SequenceGroupToSample] = []
    # A list of token indices to sample/compute logprob. It is used to
    # prune the outcome logits from the model for the performance.
    selected_token_indices: List[int] = []
    # Used for selected_token_indices.
    model_output_idx = 0

    # Sampling type -> (
    # indices to sample/prompt logprob within pruned output logits,
    # indices to sample within pruned logits)
    categorized_sample_indices: Dict[SamplingType, List[Tuple[int, int]]] = {
        t: []
        for t in SamplingType
    }
    # Index of logits to compute logprob. Logits include both prompt logprob
    # and sample logprob indices.
    logit_idx = 0
    # Index to sample from a sample tensor. It is used by triton sample kernel.
    # See `_sample_with_triton_kernel` for more details.
    sample_idx = 0
    # Total number of prompts from given sequence groups.
    num_prompts = 0

    for i, seq_group_metadata in enumerate(seq_group_metadata_list):
        seq_ids = seq_group_metadata.seq_data.keys()

        if cache is not None:
            sample_obj = cache.get_cached_seq_group_to_sample(len(seq_ids))

            for j, seq_id in enumerate(seq_ids):
                sample_obj.seq_ids[j] = seq_id

            sample_obj.prompt_logprob_indices.clear()
            sample_obj.sample_indices.clear()
        sampling_params = seq_group_metadata.sampling_params
        is_prompt = seq_group_metadata.is_prompt
        generator: Optional[torch.Generator] = None
        # If the current seq group is in decode stage, it is None.
        seq_len: Optional[int] = None
        query_len: Optional[int] = None
        prompt_logprob_indices: List[int] = \
            sample_obj.prompt_logprob_indices if cache is not None else []
        sample_indices: List[int] = \
            sample_obj.sample_indices if cache is not None else []
        do_sample = seq_group_metadata.do_sample

        if seq_group_metadata.is_prompt:
            if sampling_params.seed is not None:
                generator = torch.Generator(device=device).manual_seed(
                    sampling_params.seed)
                if generators is not None:
                    generators[seq_group_metadata.request_id] = generator

            num_prompts += 1
            num_prefill_sample = len(seq_ids)
            assert num_prefill_sample == 1
            assert query_lens is not None and seq_lens is not None
            query_len, seq_len = query_lens[i], seq_lens[i]
            # If we need sampling, exclude num_prefill_sample tokens from
            # prompt logprob.
            prompt_logprob_len = (query_len - num_prefill_sample
                                  if do_sample else query_len)
            sample_len = num_prefill_sample if do_sample else 0
        else:
            # Decode
            prompt_logprob_len = 0
            sample_len = len(seq_ids) if do_sample else 0

            if sampling_params.seed is not None and generators is not None:
                generator = generators.get(seq_group_metadata.request_id)

        # Update indices to select from the model output.
        """
        This blocks computes selected_token_indices which is used in the
        following way.

        hidden_states = model(...)
        logits = hidden_states[selected_token_indices]
        """

        if sampling_params.prompt_logprobs is not None:
            selected_token_indices.extend(
                range(model_output_idx, model_output_idx + prompt_logprob_len))
        model_output_idx += prompt_logprob_len
        if do_sample:
            selected_token_indices.extend(
                range(model_output_idx, model_output_idx + sample_len))
        model_output_idx += sample_len

        # We now find indices for logprob computation and sampling.
        """
        This block computes categorized_sample_indices which is used in the
        following way.

        hidden_states = model(...)
        logits = hidden_states[selected_token_indices]
        def sample(logits):
           # Use categorized_sample_indices for sampling.
           # prompt_logprob_indices to find prompt logprob indices.
           # sample_indices to find sample indices.
        """

        if sampling_params.prompt_logprobs is not None:
            prompt_logprob_indices.extend(
                range(logit_idx, logit_idx + prompt_logprob_len))
            logit_idx += prompt_logprob_len
        if do_sample:
            sample_indices.extend(range(logit_idx, logit_idx + sample_len))
            categorized_sample_indices[sampling_params.sampling_type].extend(
                list(
                    zip(range(logit_idx, logit_idx + sample_len),
                        range(sample_idx, sample_idx + sample_len))))
            logit_idx += sample_len
            sample_idx += sample_len

        if cache is not None:
            sample_obj.sampling_params = sampling_params
            sample_obj.seq_data = seq_group_metadata.seq_data
            sample_obj.seq_len = seq_len
            sample_obj.query_len = query_len
            sample_obj.generator = generator
            sample_obj.is_prompt = is_prompt
        else:
            sample_obj = SequenceGroupToSample(
                seq_ids=list(seq_ids),
                sampling_params=sampling_params,
                seq_data=seq_group_metadata.seq_data,
                seq_len=seq_len,
                query_len=query_len,
                generator=generator,
                is_prompt=is_prompt,
                prompt_logprob_indices=list(prompt_logprob_indices),
                sample_indices=list(sample_indices))

        seq_groups.append(sample_obj)

    if cache is not None:
        cache.reset()
    return (seq_groups, selected_token_indices, categorized_sample_indices,
            num_prompts)


@dataclass
class SamplingTensors:
    """Tensors for sampling."""

    temperatures: torch.Tensor
    top_ps: torch.Tensor
    top_ks: torch.Tensor
    top_as: torch.Tensor
    min_ps: torch.Tensor
    presence_penalties: torch.Tensor
    frequency_penalties: torch.Tensor
    repetition_penalties: torch.Tensor
    tfss: torch.Tensor
    eta_cutoffs: torch.Tensor
    epsilon_cutoffs: torch.Tensor
    typical_ps: torch.Tensor
    smoothing_factors: torch.Tensor
    smoothing_curves: torch.Tensor
    sampling_seeds: torch.Tensor
    sample_indices: torch.Tensor
    extra_seeds: Optional[torch.Tensor]
    prompt_tokens: torch.Tensor
    output_tokens: torch.Tensor

    @classmethod
    def from_sampling_metadata(
        cls,
        sampling_metadata: "SamplingMetadata",
        vocab_size: int,
        device: torch.device,
        dtype: torch.dtype,
        *,
        extra_seeds_to_generate: int = 0,
        extra_entropy: Optional[Tuple[int, ...]] = None
    ) -> Tuple["SamplingTensors", bool, bool, bool, bool, bool, bool, bool,
               bool, bool]:
        """
        extra_seeds_to_generate: extra seeds to generate using the
            user-defined seed for each sequence.
        extra_entropy: extra entropy to use when generating seeds.
        """
        prompt_tokens: List[array] = []
        output_tokens: List[array] = []
        top_ks: List[int] = []
        temperatures: List[float] = []
        top_ps: List[float] = []
        top_as: List[float] = []
        min_ps: List[float] = []
        presence_penalties: List[float] = []
        frequency_penalties: List[float] = []
        repetition_penalties: List[float] = []
        tfss: List[float] = []
        eta_cutoffs: List[float] = []
        epsilon_cutoffs: List[float] = []
        typical_ps: List[float] = []
        smoothing_factors: List[float] = []
        smoothing_curves: List[float] = []
        sampling_seeds: List[int] = []
        sample_indices: List[int] = []
        do_penalties = False
        do_top_p_top_k = False
        do_top_as = False
        do_min_p = False
        do_tfss = False
        do_eta_cutoffs = False
        do_epsilon_cutoffs = False
        do_typical_ps = False
        do_quadratic = False

        if _USE_TRITON_SAMPLER:
            prompt_best_of: List[int] = []

            # We need one base seed per Triton slice.
            seeds_to_generate = (extra_seeds_to_generate +
                                 get_num_triton_sampler_splits(vocab_size))

        assert sampling_metadata.seq_groups is not None
        for seq_group in sampling_metadata.seq_groups:
            seq_ids = seq_group.seq_ids
            sampling_params = seq_group.sampling_params
            temperature = sampling_params.temperature
            p = sampling_params.presence_penalty
            f = sampling_params.frequency_penalty
            r = sampling_params.repetition_penalty
            top_p = sampling_params.top_p
            top_a = sampling_params.top_a
            min_p = sampling_params.min_p
            tfs = sampling_params.tfs
            eta_cutoff = sampling_params.eta_cutoff
            epsilon_cutoff = sampling_params.epsilon_cutoff
            typical_p = sampling_params.typical_p
            smoothing_factor = sampling_params.smoothing_factor
            smoothing_curve = sampling_params.smoothing_curve

            # k should not be greater than the vocab size.
            top_k = min(sampling_params.top_k, vocab_size)
            top_k = vocab_size if top_k == -1 else top_k
            if temperature < _SAMPLING_EPS:
                # NOTE: Zero temperature means deterministic sampling
                # (i.e., greedy sampling or beam search).
                # Set the temperature to 1 to avoid division by zero.
                temperature = 1.0
            if not do_top_p_top_k and (top_p < 1.0 - _SAMPLING_EPS
                                       or top_k != vocab_size):
                do_top_p_top_k = True
            if do_top_as is False and top_a > 0.0:
                do_top_as = True
            if not do_min_p and min_p > _SAMPLING_EPS:
                do_min_p = True
            if not do_penalties and (abs(p) >= _SAMPLING_EPS
                                     or abs(f) >= _SAMPLING_EPS
                                     or abs(r - 1.0) >= _SAMPLING_EPS):
                do_penalties = True
            if do_tfss is False and tfs < 1.0 - _SAMPLING_EPS:
                do_tfss = True
            if do_eta_cutoffs is False and eta_cutoff > _SAMPLING_EPS:
                do_eta_cutoffs = True
            if do_epsilon_cutoffs is False and epsilon_cutoff > _SAMPLING_EPS:
                do_epsilon_cutoffs = True
            if do_typical_ps is False and typical_p < 1.0 - _SAMPLING_EPS:
                do_typical_ps = True
            if do_quadratic is False and (smoothing_factor > _SAMPLING_EPS
                                          or smoothing_curve > 1.0):
                do_quadratic = True

            is_prompt = seq_group.is_prompt
            if (is_prompt and sampling_params.prompt_logprobs is not None):
                # For tokens in the prompt that we only need to get
                # their logprobs
                query_len = seq_group.query_len
                assert query_len is not None
                prefill_len = len(seq_group.prompt_logprob_indices)
                temperatures += [temperature] * prefill_len
                top_ps += [top_p] * prefill_len
                top_ks += [top_k] * prefill_len
                top_as += [top_a] * prefill_len
                min_ps += [min_p] * prefill_len
                presence_penalties += [0] * prefill_len
                frequency_penalties += [0] * prefill_len
                repetition_penalties += [1] * prefill_len
                tfss += [1] * prefill_len
                eta_cutoffs += [0] * prefill_len
                epsilon_cutoffs += [0] * prefill_len
                typical_ps += [1] * prefill_len
                smoothing_factors += [smoothing_factor] * prefill_len
                smoothing_curves += [smoothing_curve] * prefill_len

            if seq_group.do_sample:
                sample_lens = len(seq_group.sample_indices)
                assert sample_lens == len(seq_ids)
                temperatures += [temperature] * len(seq_ids)
                top_ps += [top_p] * len(seq_ids)
                top_ks += [top_k] * len(seq_ids)
                top_as += [top_a] * len(seq_ids)
                min_ps += [min_p] * len(seq_ids)
                presence_penalties += [p] * len(seq_ids)
                frequency_penalties += [f] * len(seq_ids)
                repetition_penalties += [r] * len(seq_ids)
                tfss += [tfs] * len(seq_ids)
                eta_cutoffs += [eta_cutoff] * len(seq_ids)
                epsilon_cutoffs += [epsilon_cutoff] * len(seq_ids)
                typical_ps += [typical_p] * len(seq_ids)
                smoothing_factors += [smoothing_factor] * len(seq_ids)
                smoothing_curves += [smoothing_curve] * len(seq_ids)

            if _USE_TRITON_SAMPLER:
                if is_prompt:
                    prompt_best_of.append(sampling_params.best_of)
                    query_len = seq_group.query_len
                    assert query_len is not None

                seed = sampling_params.seed
                is_greedy = sampling_params.sampling_type == SamplingType.GREEDY

                for seq_id in seq_ids:
                    seq_data = seq_group.seq_data[seq_id]
                    extra_entropy = extra_entropy or ()
                    seq_seeds = cls._get_sequence_seeds(
                        seed,
                        seq_data.get_len(),
                        *extra_entropy,
                        seq_id,
                        seeds_to_generate=seeds_to_generate,
                        is_greedy=is_greedy)
                    sampling_seeds.append(seq_seeds)
                sample_indices.extend(seq_group.sample_indices)

        if do_penalties:
            for seq_group in sampling_metadata.seq_groups:
                seq_ids = seq_group.seq_ids
                if (seq_group.is_prompt
                        and sampling_params.prompt_logprobs is not None):
                    prefill_len = len(seq_group.prompt_logprob_indices)
                    prompt_tokens.extend(
                        array('l') for _ in range(prefill_len))
                    output_tokens.extend(
                        array('l') for _ in range(prefill_len))
                if seq_group.do_sample:
                    for seq_id in seq_ids:
                        seq_data = seq_group.seq_data[seq_id]
                        prompt_tokens.append(seq_data.prompt_token_ids_array)
                        output_tokens.append(seq_data.output_token_ids_array)

        sampling_tensors = SamplingTensors.from_lists(
            temperatures, top_ps, top_ks, top_as, min_ps, presence_penalties,
            frequency_penalties, repetition_penalties, tfss, eta_cutoffs,
            epsilon_cutoffs, typical_ps, smoothing_factors, smoothing_curves,
            sampling_seeds, sample_indices, prompt_tokens, output_tokens,
            vocab_size, extra_seeds_to_generate, device, dtype)
        return (sampling_tensors, do_penalties, do_top_p_top_k, do_top_as,
                do_min_p, do_tfss, do_eta_cutoffs, do_epsilon_cutoffs,
                do_typical_ps, do_quadratic)

    @classmethod
    def from_lists(cls, temperatures: List[float], top_ps: List[float],
                   top_ks: List[int], top_as: List[float], min_ps: List[float],
                   presence_penalties: List[float],
                   frequency_penalties: List[float],
                   repetition_penalties: List[float], tfss: List[float],
                   eta_cutoffs: List[float], epsilon_cutoffs: List[float],
                   typical_ps: List[float], smoothing_factors: List[float],
                   smoothing_curves: List[float], sampling_seeds: List[int],
                   sample_indices: List[int], prompt_tokens: List[array],
                   output_tokens: List[array], vocab_size: int,
                   extra_seeds_to_generate: int, device: torch.device,
                   dtype: torch.dtype) -> "SamplingTensors":
        # Note that the performance will be very bad without
        # pinned memory.
        pin_memory = is_pin_memory_available()
        do_penalties = prompt_tokens or output_tokens

        if do_penalties:
            prompt_t = make_tensor_with_pad(
                prompt_tokens,
                vocab_size,
                device="cpu",
                dtype=torch.int64,
                pin_memory=pin_memory,
            )
            output_t = make_tensor_with_pad(
                output_tokens,
                vocab_size,
                device="cpu",
                dtype=torch.int64,
                pin_memory=pin_memory,
            )
        else:
            empty_tensor = torch.empty(0, device=device, dtype=torch.long)
            prompt_t = empty_tensor
            output_t = empty_tensor

        temperatures_t = torch.tensor(
            temperatures,
            device="cpu",
            dtype=dtype,
            pin_memory=pin_memory,
        )
        top_ps_t = torch.tensor(
            top_ps,
            device="cpu",
            dtype=dtype,
            pin_memory=pin_memory,
        )
        top_as_t = torch.tensor(top_as,
                                device="cpu",
                                dtype=dtype,
                                pin_memory=pin_memory)
        min_ps_t = torch.tensor(
            min_ps,
            device="cpu",
            dtype=dtype,
            pin_memory=pin_memory,
        )
        presence_penalties_t = torch.tensor(
            presence_penalties,
            device="cpu",
            dtype=dtype,
            pin_memory=pin_memory,
        )
        frequency_penalties_t = torch.tensor(
            frequency_penalties,
            device="cpu",
            dtype=dtype,
            pin_memory=pin_memory,
        )
        repetition_penalties_t = torch.tensor(
            repetition_penalties,
            device="cpu",
            dtype=dtype,
            pin_memory=pin_memory,
        )
        top_ks_t = torch.tensor(
            top_ks,
            device="cpu",
            dtype=torch.int,
            pin_memory=pin_memory,
        )
        tfss_t = torch.tensor(tfss,
                              device="cpu",
                              dtype=dtype,
                              pin_memory=pin_memory)
        eta_cutoffs_t = torch.tensor(eta_cutoffs,
                                     device="cpu",
                                     dtype=dtype,
                                     pin_memory=pin_memory)
        epsilon_cutoffs_t = torch.tensor(epsilon_cutoffs,
                                         device="cpu",
                                         dtype=dtype,
                                         pin_memory=pin_memory)
        typical_ps_t = torch.tensor(typical_ps,
                                    device="cpu",
                                    dtype=dtype,
                                    pin_memory=pin_memory)
        smoothing_factors_t = torch.tensor(smoothing_factors,
                                           device="cpu",
                                           dtype=dtype,
                                           pin_memory=pin_memory)
        smoothing_curves_t = torch.tensor(smoothing_curves,
                                          device="cpu",
                                          dtype=dtype,
                                          pin_memory=pin_memory)
        sample_indices_t = torch.tensor(
            sample_indices,
            device="cpu",
            dtype=torch.long,
            pin_memory=pin_memory,
        )
        # need to transpose and make contiguous to
        # copy the tensor correctly.
        # [batch_size, n_seeds] -> [n_seeds, batch_size]
        sampling_seeds_t = torch.tensor(
            sampling_seeds,
            device="cpu",
            dtype=torch.long,
            pin_memory=pin_memory,
        ).t().contiguous()

        # Because the memory is pinned, we can do non-blocking
        # transfer to device.

        # How many seeds the sample operation itself will need.
        num_base_seeds = sampling_seeds_t.shape[0] - extra_seeds_to_generate
        sampling_seeds_gpu = sampling_seeds_t.to(device=device,
                                                 non_blocking=True)
        extra_seeds_gpu = sampling_seeds_gpu[num_base_seeds:]
        if not extra_seeds_gpu.numel():
            extra_seeds_gpu = None
        sampling_seeds_gpu = sampling_seeds_gpu[:num_base_seeds]

        return cls(
            temperatures=temperatures_t.to(device=device, non_blocking=True),
            top_ps=top_ps_t.to(device=device, non_blocking=True),
            top_ks=top_ks_t.to(device=device, non_blocking=True),
            top_as=top_as_t.to(device=device, non_blocking=True),
            min_ps=min_ps_t.to(device=device, non_blocking=True),
            presence_penalties=presence_penalties_t.to(device=device,
                                                       non_blocking=True),
            frequency_penalties=frequency_penalties_t.to(device=device,
                                                         non_blocking=True),
            repetition_penalties=repetition_penalties_t.to(device=device,
                                                           non_blocking=True),
            tfss=tfss_t.to(device=device, non_blocking=True),
            eta_cutoffs=eta_cutoffs_t.to(device=device, non_blocking=True),
            epsilon_cutoffs=epsilon_cutoffs_t.to(device=device,
                                                 non_blocking=True),
            smoothing_factors=smoothing_factors_t.to(device=device,
                                                     non_blocking=True),
            smoothing_curves=smoothing_curves_t.to(device=device,
                                                   non_blocking=True),
            typical_ps=typical_ps_t.to(device=device, non_blocking=True),
            prompt_tokens=prompt_t.to(device=device, non_blocking=True),
            output_tokens=output_t.to(device=device, non_blocking=True),
            sampling_seeds=sampling_seeds_gpu,
            sample_indices=sample_indices_t.to(device=device,
                                               non_blocking=True),
            extra_seeds=extra_seeds_gpu,
        )

    @staticmethod
    def _get_sequence_seeds(
        seed: int,
        *extra_entropy: int,
        seeds_to_generate: int,
        is_greedy: bool,
    ):
        """Get `seeds_to_generate` child seeds from `seed` and extra entropy."""
        if not is_greedy:
            if seed is None:
                randint_fn = random.randint
            else:
                generator = random.Random(str((seed, ) + extra_entropy))
                randint_fn = generator.randint
            lo, hi = torch.iinfo(torch.long).min, torch.iinfo(torch.long).max
            # If the user/random sets seed = 0 but request should
            # have sampling, we need to change it to something
            # else. We use a constant in that case.
            # This way we don't need to create and load a bool
            # matrix in the sampling kernel, which reduces CPU
            # overhead and latency.
            seq_seeds = [
                randint_fn(lo, hi) or _SEED_0_REPLACEMENT
                for _ in range(seeds_to_generate)
            ]
        else:
            # For the kernel, seed == 0 means greedy decoding.
            seq_seeds = [0] * seeds_to_generate
        return seq_seeds
