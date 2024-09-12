from typing import Any, Dict, List, Optional

import torch

import vllm.envs as envs
from vllm import _custom_ops as ops
from vllm.model_executor.layers.linear import LinearBase, LinearMethodBase
from vllm.model_executor.layers.quantization.base_config import (
    QuantizationConfig)
from vllm.model_executor.parameter import (GroupQuantScaleParameter,
                                           PackedvLLMParameter)


class AWQConfig(QuantizationConfig):
    """Config class for AWQ.

    Reference: https://arxiv.org/abs/2306.00978
    """

    def __init__(
        self,
        weight_bits: int,
        group_size: int,
        zero_point: bool,
    ) -> None:
        self.weight_bits = weight_bits
        self.group_size = group_size
        self.zero_point = zero_point

        if self.weight_bits != 4:
            raise ValueError(
                "Currently, only 4-bit weight quantization is supported for "
                f"AWQ, but got {self.weight_bits} bits.")
        self.pack_factor = 32 // self.weight_bits

    def __repr__(self) -> str:
        return (f"AWQConfig(weight_bits={self.weight_bits}, "
                f"group_size={self.group_size}, "
                f"zero_point={self.zero_point})")

    def get_name(self) -> str:
        return "awq"

    def get_supported_act_dtypes(self) -> List[torch.dtype]:
        return [torch.half]

    @classmethod
    def get_min_capability(cls) -> int:
        # The AWQ kernel only supports Turing or newer GPUs.
        return 75

    @staticmethod
    def get_config_filenames() -> List[str]:
        return [
            "quant_config.json",  # E.g., casperhansen/vicuna-7b-v1.5-awq
            # E.g., abhinavkulkarni/mosaicml-mpt-7b-instruct-w4-g128-awq
            "quantize_config.json",
        ]

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> "AWQConfig":
        weight_bits = cls.get_from_keys(config, ["w_bit", "bits"])
        group_size = cls.get_from_keys(config, ["q_group_size", "group_size"])
        zero_point = cls.get_from_keys(config, ["zero_point"])
        return cls(weight_bits, group_size, zero_point)

    def get_quant_method(self, layer: torch.nn.Module,
                         prefix: str) -> Optional["AWQLinearMethod"]:
        if isinstance(layer, LinearBase):
            return AWQLinearMethod(self)
        return None

    def get_scaled_act_names(self) -> List[str]:
        return ["gelu", "gelu_fast", "gelu_new", "gelu_pytorch_tanh"]


class AWQLinearMethod(LinearMethodBase):
    """Linear method for AWQ.

    Args:
        quant_config: The AWQ quantization config.
    """

    def __init__(self, quant_config: AWQConfig):
        self.quant_config = quant_config

    def create_weights(self, layer: torch.nn.Module,
                       input_size_per_partition: int,
                       output_partition_sizes: List[int], input_size: int,
                       output_size: int, params_dtype: torch.dtype,
                       **extra_weight_attrs):
        if input_size_per_partition % self.quant_config.group_size != 0:
            raise ValueError(
                "The input size is not aligned with the quantized "
                "weight shape. This can be caused by too large "
                "tensor parallel size.")

        output_size_per_partition = sum(output_partition_sizes)
        if output_size_per_partition % self.quant_config.pack_factor != 0:
            raise ValueError(
                "The output size is not aligned with the quantized "
                "weight shape. This can be caused by too large "
                "tensor parallel size.")

        weight_loader = extra_weight_attrs.get("weight_loader")
        qweight = PackedvLLMParameter(
            data=torch.empty(
                input_size_per_partition,
                output_size_per_partition // self.quant_config.pack_factor,
                dtype=torch.int32,
            ),
            input_dim=0,
            output_dim=1,
            packed_dim=1,
            packed_factor=self.quant_config.pack_factor,
            weight_loader=weight_loader)

        qzeros = PackedvLLMParameter(
            data=torch.empty(
                input_size_per_partition // self.quant_config.group_size,
                output_size_per_partition // self.quant_config.pack_factor,
                dtype=torch.int32,
            ),
            input_dim=0,
            output_dim=1,
            packed_dim=1,
            packed_factor=self.quant_config.pack_factor,
            weight_loader=weight_loader)

        scales = GroupQuantScaleParameter(data=torch.empty(
            input_size_per_partition // self.quant_config.group_size,
            output_size_per_partition,
            dtype=params_dtype,
        ),
                                          input_dim=0,
                                          output_dim=1,
                                          weight_loader=weight_loader)

        layer.register_parameter("qweight", qweight)
        layer.register_parameter("qzeros", qzeros)
        layer.register_parameter("scales", scales)

    def process_weights_after_loading(self, layer: torch.nn.Module) -> None:
        layer.qweight = torch.nn.Parameter(layer.qweight.data,
                                           requires_grad=False)
        layer.qzeros = torch.nn.Parameter(layer.qzeros.data,
                                          requires_grad=False)
        layer.scales = torch.nn.Parameter(layer.scales.data,
                                          requires_grad=False)

    def apply(self,
              layer: torch.nn.Module,
              x: torch.Tensor,
              bias: Optional[torch.Tensor] = None) -> torch.Tensor:
        qweight = layer.qweight
        scales = layer.scales
        qzeros = layer.qzeros
        pack_factor = self.quant_config.pack_factor
        out_shape = (x.shape[:-1] + (qweight.shape[-1] * pack_factor, ))
        reshaped_x = x.reshape(-1, x.shape[-1])

        # num_tokens >= threshold
        FP16_MATMUL_HEURISTIC_CONDITION = x.shape[:-1].numel() >= 256

        prefer_torch = envs.VLLM_ROCM_PREFER_TORCH
        prefer_triton = envs.VLLM_ROCM_PREFER_TRITON

        if (FP16_MATMUL_HEURISTIC_CONDITION
                or (prefer_torch and not prefer_triton)):
            if prefer_triton:
                out = ops.awq_dequantize(qweight, scales, qzeros, 0, 0, 0)
            else:
                out = torch_awq_dequantize(qweight, scales, qzeros)
            out = torch.matmul(reshaped_x, out)
        else:
            out = ops.awq_gemm(reshaped_x, qweight, scales, qzeros,
                               pack_factor)
        if bias is not None:
            out.add_(bias)
        return out.reshape(out_shape)


def torch_awq_dequantize(qweights: torch.Tensor, scales: torch.Tensor,
                         qzeros: torch.Tensor) -> torch.Tensor:
    reverse_awq_func_desc = torch.tensor([0, 16, 4, 20, 8, 24, 12, 28],
                                         dtype=torch.int32,
                                         device=qweights.device)
    if qzeros is None:
        qzeros = torch.zeros_like(qweights)

    while qweights.dim() < 2:
        qweights = torch.unsqueeze(qweights, 0)
    while qzeros.dim() < 2:
        qzeros = torch.unsqueeze(qzeros, 0)
    while scales.dim() < 2:
        scales = torch.unsqueeze(scales, 0)

    rows = qweights.size(-2)
    group_size_zeros = rows // qzeros.size(-2)
    group_size_scales = rows // scales.size(-2)

    qweights_shape = list(qweights.shape)
    qweights_shape[-1] *= 8
    qzeros_shape = list(qzeros.shape)
    qzeros_shape[-1] *= 8

    qweights = torch.unsqueeze(qweights, -1)
    qzeros = torch.unsqueeze(qzeros, -1)

    unpacked_weights = torch.bitwise_right_shift(qweights,
                                                 reverse_awq_func_desc)
    unpacked_weights = torch.bitwise_and(unpacked_weights, 0xf)
    unpacked_weights = unpacked_weights.to(torch.int8).view(qweights_shape)

    unpacked_zeros = torch.bitwise_right_shift(qzeros, reverse_awq_func_desc)
    unpacked_zeros = torch.bitwise_and(unpacked_zeros, 0xf)
    unpacked_zeros = unpacked_zeros.to(torch.int8).view(qzeros_shape)
    unpacked_zeros = unpacked_zeros.repeat_interleave(group_size_zeros, dim=-2)

    functional_scales = scales.repeat_interleave(group_size_scales, dim=-2)
    return (unpacked_weights - unpacked_zeros) * functional_scales
