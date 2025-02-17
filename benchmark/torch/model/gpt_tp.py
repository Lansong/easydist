import math
import os

import torch
import torch.nn as nn
import torch.nn.functional as F

import fairscale.nn.model_parallel.initialize as fs_init
from fairscale.nn.model_parallel.layers import ColumnParallelLinear, RowParallelLinear


class AttentionCore(nn.Module):

    def __init__(self, attention_head_size: int, attention_dropout: float) -> None:
        super().__init__()
        self.attention_head_size = attention_head_size
        self.attention_dropout = nn.Dropout(attention_dropout)

        self.softmax = nn.Softmax(dim=-1)

        # (avoid the const tensor init when forward)
        self.causal_mask = None
        self.where_const = -1e4

    def forward(self, q, k, v):
        x = torch.matmul(q, k.transpose(-1, -2))
        x = x / math.sqrt(self.attention_head_size)

        # (avoid the const tensor init when forward)
        if self.causal_mask is None:
            q_len, k_len = q.size(-2), k.size(-2)
            meta_dev = os.environ.get("METADIST_DEVICE", "cuda")
            self.causal_mask = torch.tril(
                torch.ones((q_len, k_len), dtype=torch.uint8,
                           device=meta_dev)).view(1, 1, q_len, k_len).bool()
        x = torch.where(self.causal_mask, x, self.where_const)
        x = self.softmax(x)
        x = self.attention_dropout(x)

        x = torch.matmul(x, v)
        x = x.transpose(1, 2)
        new_context_layer_shape = x.size()[:-2] + (-1, )
        x = x.reshape(new_context_layer_shape)

        return x


class FeedForward(nn.Module):

    def __init__(self, hidden_size, ratio=4) -> None:
        super().__init__()
        self.hidden_size = hidden_size
        self.ffn_hidden_size = hidden_size * ratio

        self.dense_h_to_4h = ColumnParallelLinear(self.hidden_size,
                                                  self.ffn_hidden_size,
                                                  gather_output=False)
        self.dense_4h_to_h = RowParallelLinear(self.ffn_hidden_size,
                                               self.hidden_size,
                                               input_is_parallel=True)

        self.activation_func = F.gelu

    def forward(self, hidden_states):
        intermediate_parallel = self.dense_h_to_4h(hidden_states)
        intermediate_parallel = self.activation_func(intermediate_parallel)
        output = self.dense_4h_to_h(intermediate_parallel)
        return output


class SelfAttention(nn.Module):

    def __init__(self,
                 dim: int,
                 num_heads: int,
                 attention_dropout: float = 0.,
                 dropout: float = 0.) -> None:
        super().__init__()

        self.attention_head_size = dim // num_heads
        self.query = ColumnParallelLinear(dim, dim, gather_output=False)
        self.key = ColumnParallelLinear(dim, dim, gather_output=False)
        self.value = ColumnParallelLinear(dim, dim, gather_output=False)

        self.dense = RowParallelLinear(dim, dim, input_is_parallel=True)

        self.dropout = nn.Dropout(dropout)

        self.core_attention = AttentionCore(self.attention_head_size, attention_dropout)

    def forward(self, x):

        q = self.query(x)
        k = self.key(x)
        v = self.value(x)

        all_head_size = q.shape[-1]
        num_attention_heads = all_head_size // self.attention_head_size

        new_qkv_shape = q.shape[:-1] + \
            (num_attention_heads, self.attention_head_size)
        q = q.view(new_qkv_shape)
        k = k.view(new_qkv_shape)
        v = v.view(new_qkv_shape)

        q = q.permute((0, 2, 1, 3))
        k = k.permute((0, 2, 1, 3))
        v = v.permute((0, 2, 1, 3))

        x = self.core_attention(q, k, v)

        x = self.dense(x)
        x = self.dropout(x)

        return x


class GPTLayer(nn.Module):

    def __init__(self,
                 dim: int,
                 num_heads: int,
                 mlp_ratio: int = 4,
                 attention_dropout: float = 0.,
                 dropout: float = 0.,
                 dtype: torch.dtype = None):
        super().__init__()
        self.norm1 = nn.LayerNorm(normalized_shape=dim, eps=1e-6, dtype=dtype)
        self.attn = SelfAttention(dim=dim,
                                  num_heads=num_heads,
                                  attention_dropout=attention_dropout,
                                  dropout=dropout)
        self.norm2 = nn.LayerNorm(normalized_shape=dim, eps=1e-6, dtype=dtype)
        self.mlp = FeedForward(hidden_size=dim, ratio=mlp_ratio)

    def forward(self, x):
        x = x + self.attn(self.norm1(x))
        x = x + self.mlp(self.norm2(x))
        return x


class GPT(nn.Module):

    def __init__(self,
                 depth: int,
                 dim: int,
                 num_heads: int,
                 mlp_ratio: int = 4,
                 attention_dropout: float = 0.,
                 dropout: float = 0.,
                 dtype: torch.dtype = None):
        super().__init__()
        self.blocks = nn.ModuleList([
            GPTLayer(
                dim=dim,
                num_heads=num_heads,
                mlp_ratio=mlp_ratio,
                attention_dropout=attention_dropout,
                dropout=dropout,
                dtype=dtype,
            ) for _ in range(depth)
        ])

    def forward(self, x):
        for block in self.blocks:
            x = block(x)
        return x
