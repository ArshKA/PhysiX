# SPDX-FileCopyrightText: Copyright (c) 2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The model definition for 3D layers

Adapted from: https://github.com/lucidrains/magvit2-pytorch/blob/9f49074179c912736e617d61b32be367eb5f993a/
magvit2_pytorch/magvit2_pytorch.py#L889

[MIT License Copyright (c) 2023 Phil Wang]
https://github.com/lucidrains/magvit2-pytorch/blob/9f49074179c912736e617d61b32be367eb5f993a/LICENSE
"""
import math
from typing import Tuple, Union

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.checkpoint import checkpoint

from cosmos1.models.autoregressive.tokenizer.patching import Patcher3D, UnPatcher3D
from cosmos1.models.autoregressive.tokenizer.universal_patcher import PaddedPatcher3D
from cosmos1.models.autoregressive.tokenizer.utils import (
    CausalNormalize,
    batch2space,
    batch2time,
    cast_tuple,
    is_odd,
    nonlinearity,
    replication_pad,
    space2batch,
    time2batch,
)
from cosmos1.utils import log


class CausalConv3d(nn.Module):
    def __init__(
        self,
        chan_in: int = 1,
        chan_out: int = 1,
        kernel_size: Union[int, Tuple[int, int, int]] = 3,
        pad_mode: str = "constant",
        **kwargs,
    ):
        super().__init__()
        kernel_size = cast_tuple(kernel_size, 3)

        time_kernel_size, height_kernel_size, width_kernel_size = kernel_size

        assert is_odd(height_kernel_size) and is_odd(width_kernel_size)

        dilation = kwargs.pop("dilation", 1)
        stride = kwargs.pop("stride", 1)
        time_stride = kwargs.pop("time_stride", 1)
        time_dilation = kwargs.pop("time_dilation", 1)
        padding = kwargs.pop("padding", 1)

        self.pad_mode = pad_mode
        time_pad = time_dilation * (time_kernel_size - 1) + (1 - time_stride)
        self.time_pad = time_pad

        self.spatial_pad = (padding, padding, padding, padding)

        stride = (time_stride, stride, stride)
        dilation = (time_dilation, dilation, dilation)
        self.conv3d = nn.Conv3d(chan_in, chan_out, kernel_size, stride=stride, dilation=dilation, **kwargs)

    def _replication_pad(self, x: torch.Tensor) -> torch.Tensor:
        x_prev = x[:, :, :1, ...].repeat(1, 1, self.time_pad, 1, 1)
        x = torch.cat([x_prev, x], dim=2)
        padding = self.spatial_pad + (0, 0)
        return F.pad(x, padding, mode=self.pad_mode, value=0.0)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self._replication_pad(x)
        return self.conv3d(x)


class CausalHybridUpsample3d(nn.Module):
    def __init__(self, in_channels: int, spatial_up: bool = True, temporal_up: bool = True, **ignore_kwargs) -> None:
        super().__init__()
        self.conv1 = (
            CausalConv3d(in_channels, in_channels, kernel_size=(3, 1, 1), stride=1, time_stride=1, padding=0)
            if temporal_up
            else nn.Identity()
        )
        self.conv2 = (
            CausalConv3d(in_channels, in_channels, kernel_size=(1, 3, 3), stride=1, time_stride=1, padding=1)
            if spatial_up
            else nn.Identity()
        )
        self.conv3 = (
            CausalConv3d(in_channels, in_channels, kernel_size=1, stride=1, time_stride=1, padding=0)
            if spatial_up or temporal_up
            else nn.Identity()
        )
        self.spatial_up = spatial_up
        self.temporal_up = temporal_up

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if not self.spatial_up and not self.temporal_up:
            return x

        # hybrid upsample temporally.
        if self.temporal_up:
            time_factor = 1.0 + 1.0 * (x.shape[2] > 1)
            if isinstance(time_factor, torch.Tensor):
                time_factor = time_factor.item()
            x = x.repeat_interleave(int(time_factor), dim=2)
            x = x[..., int(time_factor - 1) :, :, :]
            x = self.conv1(x) + x

        # hybrid upsample spatially.
        if self.spatial_up:
            x = x.repeat_interleave(2, dim=3).repeat_interleave(2, dim=4)
            x = self.conv2(x) + x

        # final 1x1x1 conv.
        x = self.conv3(x)
        return x


class CausalHybridDownsample3d(nn.Module):
    def __init__(
        self, in_channels: int, spatial_down: bool = True, temporal_down: bool = True, **ignore_kwargs
    ) -> None:
        super().__init__()
        self.conv1 = (
            CausalConv3d(in_channels, in_channels, kernel_size=(1, 3, 3), stride=2, time_stride=1, padding=0)
            if spatial_down
            else nn.Identity()
        )
        self.conv2 = (
            CausalConv3d(in_channels, in_channels, kernel_size=(3, 1, 1), stride=1, time_stride=2, padding=0)
            if temporal_down
            else nn.Identity()
        )
        self.conv3 = (
            CausalConv3d(in_channels, in_channels, kernel_size=1, stride=1, time_stride=1, padding=0)
            if spatial_down or temporal_down
            else nn.Identity()
        )
        self.spatial_down = spatial_down
        self.temporal_down = temporal_down

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if not self.spatial_down and not self.temporal_down:
            return x

        # hybrid downsample spatially.
        if self.spatial_down:
            pad = (0, 1, 0, 1, 0, 0)
            x = F.pad(x, pad, mode="constant", value=0)
            x1 = self.conv1(x)
            x2 = F.avg_pool3d(x, kernel_size=(1, 2, 2), stride=(1, 2, 2))
            x = x1 + x2

        # hybrid downsample temporally.
        if self.temporal_down:
            x = replication_pad(x)
            x1 = self.conv2(x)
            x2 = F.avg_pool3d(x, kernel_size=(2, 1, 1), stride=(2, 1, 1))
            x = x1 + x2

        # final 1x1x1 conv.
        x = self.conv3(x)
        return x

class FiLMLayer(nn.Module):
    def __init__(self, embedding_dim, output_dim):
        super().__init__()
        self.embedding_dim = embedding_dim
        self.output_dim = output_dim

        self.film_layer = nn.Sequential(
            nn.Linear(embedding_dim, embedding_dim),
            nn.SiLU(),
            nn.Linear(embedding_dim, output_dim * 2),
        )
        
        # zero initialization for the last layer
        nn.init.zeros_(self.film_layer[-1].weight)
        nn.init.zeros_(self.film_layer[-1].bias)
        
    def forward(self, x, cond):
        # Apply the FiLM layer to the input tensor
        film_params = self.film_layer(cond)
        gamma, beta = film_params.chunk(2, dim=-1)
        
        # Reshape gamma and beta to match the input tensor shape
        gamma = gamma.view(-1, self.output_dim, 1, 1, 1)
        beta = beta.view(-1, self.output_dim, 1, 1, 1)
        
        # Apply FiLM modulation
        x = x * (gamma + 1) + beta
        return x

class CausalResnetBlockFactorized3d(nn.Module):
    def __init__(self, *, in_channels: int, out_channels: int = None, dropout: float, num_groups: int, film=False, film_embedding=None) -> None:
        super().__init__()
        self.in_channels = in_channels
        out_channels = in_channels if out_channels is None else out_channels

        self.norm1 = CausalNormalize(in_channels, num_groups=1)
        self.conv1 = nn.Sequential(
            CausalConv3d(in_channels, out_channels, kernel_size=(1, 3, 3), stride=1, padding=1),
            CausalConv3d(out_channels, out_channels, kernel_size=(3, 1, 1), stride=1, padding=0),
        )
        self.norm2 = CausalNormalize(out_channels, num_groups=num_groups)
        self.dropout = torch.nn.Dropout(dropout)
        self.conv2 = nn.Sequential(
            CausalConv3d(out_channels, out_channels, kernel_size=(1, 3, 3), stride=1, padding=1),
            CausalConv3d(out_channels, out_channels, kernel_size=(3, 1, 1), stride=1, padding=0),
        )
        self.nin_shortcut = (
            CausalConv3d(in_channels, out_channels, kernel_size=1, stride=1, padding=0)
            if in_channels != out_channels
            else nn.Identity()
        )
        
        self.film = film
        if self.film:
            self.film_layer_1 = FiLMLayer(film_embedding, in_channels)
            self.film_layer_2 = FiLMLayer(film_embedding, out_channels)

    def forward(self, x: torch.Tensor, cond_embedding=None) -> torch.Tensor:
        h = x
        h = self.norm1(h)
        # if self.film:
        #     h = self.film_layer_1(h, cond_embedding)
        h = nonlinearity(h)
        h = self.conv1(h)

        h = self.norm2(h)
        # if self.film:
        #     h = self.film_layer_2(h, cond_embedding)
        h = nonlinearity(h)
        h = self.dropout(h)
        h = self.conv2(h)
        x = self.nin_shortcut(x)

        return x + h


class CausalAttnBlock(nn.Module):
    def __init__(self, in_channels: int, num_groups: int) -> None:
        super().__init__()

        self.norm = CausalNormalize(in_channels, num_groups=num_groups)
        self.q = CausalConv3d(in_channels, in_channels, kernel_size=1, stride=1, padding=0)
        self.k = CausalConv3d(in_channels, in_channels, kernel_size=1, stride=1, padding=0)
        self.v = CausalConv3d(in_channels, in_channels, kernel_size=1, stride=1, padding=0)
        self.proj_out = CausalConv3d(in_channels, in_channels, kernel_size=1, stride=1, padding=0)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h_ = x
        h_ = self.norm(h_)
        q = self.q(h_)
        k = self.k(h_)
        v = self.v(h_)

        # compute attention
        q, batch_size = time2batch(q)
        k, batch_size = time2batch(k)
        v, batch_size = time2batch(v)

        b, c, h, w = q.shape
        q = q.reshape(b, c, h * w)
        q = q.permute(0, 2, 1)
        k = k.reshape(b, c, h * w)
        w_ = torch.bmm(q, k)
        w_ = w_ * (int(c) ** (-0.5))
        w_ = F.softmax(w_, dim=2)

        # attend to values
        v = v.reshape(b, c, h * w)
        w_ = w_.permute(0, 2, 1)
        h_ = torch.bmm(v, w_)
        h_ = h_.reshape(b, c, h, w)

        h_ = batch2time(h_, batch_size)
        h_ = self.proj_out(h_)
        return x + h_


class CausalTemporalAttnBlock(nn.Module):
    def __init__(self, in_channels: int, num_groups: int) -> None:
        super().__init__()

        self.norm = CausalNormalize(in_channels, num_groups=num_groups)
        self.q = CausalConv3d(in_channels, in_channels, kernel_size=1, stride=1, padding=0)
        self.k = CausalConv3d(in_channels, in_channels, kernel_size=1, stride=1, padding=0)
        self.v = CausalConv3d(in_channels, in_channels, kernel_size=1, stride=1, padding=0)
        self.proj_out = CausalConv3d(in_channels, in_channels, kernel_size=1, stride=1, padding=0)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h_ = x
        h_ = self.norm(h_)
        q = self.q(h_)
        k = self.k(h_)
        v = self.v(h_)

        # compute attention
        q, batch_size, height = space2batch(q)
        k, _, _ = space2batch(k)
        v, _, _ = space2batch(v)

        bhw, c, t = q.shape
        q = q.permute(0, 2, 1)  # (bhw, t, c)
        k = k.permute(0, 2, 1)  # (bhw, t, c)
        v = v.permute(0, 2, 1)  # (bhw, t, c)

        w_ = torch.bmm(q, k.permute(0, 2, 1))  # (bhw, t, t)
        w_ = w_ * (int(c) ** (-0.5))

        # Apply causal mask
        mask = torch.tril(torch.ones_like(w_))
        w_ = w_.masked_fill(mask == 0, float("-inf"))
        w_ = F.softmax(w_, dim=2)

        # attend to values
        h_ = torch.bmm(w_, v)  # (bhw, t, c)
        h_ = h_.permute(0, 2, 1).reshape(bhw, c, t)  # (bhw, c, t)

        h_ = batch2space(h_, batch_size, height)
        h_ = self.proj_out(h_)
        return x + h_


class EncoderFactorized(nn.Module):
    def __init__(
        self,
        in_channels: int,
        channels: int,
        channels_mult: list[int],
        num_res_blocks: int,
        attn_resolutions: list[int],
        dropout: float,
        resolution: int,
        z_channels: int,
        spatial_compression: int,
        temporal_compression: int,
        modified_in_channels: int = None,
        **ignore_kwargs,
    ) -> None:
        super().__init__()
        self.num_resolutions = len(channels_mult)
        self.num_res_blocks = num_res_blocks

        # Patcher.
        patch_size = ignore_kwargs.get("patch_size", 1)
        if ignore_kwargs.get("padded_patcher", False):
            assert ignore_kwargs.get("variables", None) is not None, "variables must be provided for padded patcher"
            self.patcher3d = PaddedPatcher3D(
                ignore_kwargs["variables"],
                patch_size,
                ignore_kwargs.get("patch_method", "haar"),
                ignore_kwargs["max_img_size"],
                ignore_kwargs["learnable_padding"],
            )
        else:
            self.patcher3d = Patcher3D(patch_size, ignore_kwargs.get("patch_method", "rearrange"))
        in_channels = (modified_in_channels if modified_in_channels is not None else in_channels) * patch_size * patch_size * patch_size
        # in_channels = modified_in_channels if modified_in_channels else (in_channels * patch_size * patch_size * patch_size)

        # calculate the number of downsample operations
        self.num_spatial_downs = int(math.log2(spatial_compression)) - int(math.log2(patch_size))
        assert (
            self.num_spatial_downs <= self.num_resolutions
        ), f"Spatially downsample {self.num_resolutions} times at most"

        self.num_temporal_downs = int(math.log2(temporal_compression)) - int(math.log2(patch_size))
        assert (
            self.num_temporal_downs <= self.num_resolutions
        ), f"Temporally downsample {self.num_resolutions} times at most"

        # downsampling
        self.conv_in = nn.Sequential(
            CausalConv3d(in_channels, channels, kernel_size=(1, 3, 3), stride=1, padding=1),
            CausalConv3d(channels, channels, kernel_size=(3, 1, 1), stride=1, padding=0),
        )
        
        if ignore_kwargs.get("film", False):
            n_datasets = ignore_kwargs.get("n_datasets")
            film_embedding = ignore_kwargs.get("film_embedding", 512)
            self.dataset_embedding = nn.Embedding(n_datasets, film_embedding)
            resnet_kwargs = {"film": ignore_kwargs.get("film", False), "film_embedding": film_embedding}
            self.film = True
        else:
            resnet_kwargs = {"film": False, "film_embedding": None}
            self.film = False

        curr_res = resolution // patch_size
        in_ch_mult = (1,) + tuple(channels_mult)
        self.in_ch_mult = in_ch_mult
        self.down = nn.ModuleList()
        for i_level in range(self.num_resolutions):
            block = nn.ModuleList()
            attn = nn.ModuleList()
            block_in = channels * in_ch_mult[i_level]
            block_out = channels * channels_mult[i_level]
            for _ in range(self.num_res_blocks):
                block.append(
                    CausalResnetBlockFactorized3d(
                        in_channels=block_in, out_channels=block_out, dropout=dropout, num_groups=1, **resnet_kwargs
                    )
                )
                block_in = block_out
                if curr_res in attn_resolutions:
                    attn.append(
                        nn.Sequential(
                            CausalAttnBlock(block_in, num_groups=1), CausalTemporalAttnBlock(block_in, num_groups=1)
                        )
                    )
            down = nn.Module()
            down.block = block
            down.attn = attn
            if i_level != self.num_resolutions - 1:
                spatial_down = i_level < self.num_spatial_downs
                temporal_down = i_level < self.num_temporal_downs
                down.downsample = CausalHybridDownsample3d(
                    block_in, spatial_down=spatial_down, temporal_down=temporal_down
                )
                curr_res = curr_res // 2
            self.down.append(down)

        # middle
        self.mid = nn.Module()
        self.mid.block_1 = CausalResnetBlockFactorized3d(
            in_channels=block_in, out_channels=block_in, dropout=dropout, num_groups=1, **resnet_kwargs
        )
        self.mid.attn_1 = nn.Sequential(
            CausalAttnBlock(block_in, num_groups=1), CausalTemporalAttnBlock(block_in, num_groups=1)
        )
        self.mid.block_2 = CausalResnetBlockFactorized3d(
            in_channels=block_in, out_channels=block_in, dropout=dropout, num_groups=1, **resnet_kwargs
        )

        # end
        self.norm_out = CausalNormalize(block_in, num_groups=1)
        self.conv_out = nn.Sequential(
            CausalConv3d(block_in, z_channels, kernel_size=(1, 3, 3), stride=1, padding=1),
            CausalConv3d(z_channels, z_channels, kernel_size=(3, 1, 1), stride=1, padding=0),
        )

    def forward(self, x: torch.Tensor, skip_patcher=False, variables=None, dataset_id=None, checkpointing=False) -> torch.Tensor:
        # Helper function to conditionally apply checkpoint
        def maybe_checkpoint(module, *args):
            if checkpointing:
                return checkpoint(module, *args, use_reentrant=False)
            else:
                return module(*args)
        
        if not skip_patcher:
            if isinstance(self.patcher3d, PaddedPatcher3D):
                x = self.patcher3d(x, variables)
            else:
                x = self.patcher3d(x)

        # downsampling
        h = self.conv_in(x)
        
        if dataset_id is not None and self.film:
            dataset_embedding = self.dataset_embedding(dataset_id)
        
        for i_level in range(self.num_resolutions):
            for i_block in range(self.num_res_blocks):
                if dataset_id is not None and self.film:
                    h = maybe_checkpoint(self.down[i_level].block[i_block], h, dataset_embedding)
                else:
                    h = maybe_checkpoint(self.down[i_level].block[i_block], h)
                
                if len(self.down[i_level].attn) > 0:
                    h = maybe_checkpoint(self.down[i_level].attn[i_block], h)
            
            if i_level != self.num_resolutions - 1:
                h = self.down[i_level].downsample(h)

        # middle
        if dataset_id is not None and self.film:
            h = maybe_checkpoint(self.mid.block_1, h, dataset_embedding)
        else:
            h = maybe_checkpoint(self.mid.block_1, h)
        
        h = maybe_checkpoint(self.mid.attn_1, h)
        
        if dataset_id is not None and self.film:
            h = maybe_checkpoint(self.mid.block_2, h, dataset_embedding)
        else:
            h = maybe_checkpoint(self.mid.block_2, h)

        # end
        h = self.norm_out(h)
        h = nonlinearity(h)
        h = self.conv_out(h)
        return h

class EncoderFactorizedDownOnly(nn.Module):
    def __init__(
        self,
        in_channels: int,
        channels: int,
        channels_mult: list[int],
        num_res_blocks: int,
        attn_resolutions: list[int],
        dropout: float,
        resolution: int,
        z_channels: int,
        spatial_compression: int,
        temporal_compression: int,
        modified_in_channels: int = None,
        **ignore_kwargs,
    ) -> None:
        super().__init__()
        self.num_resolutions = len(channels_mult)
        self.num_res_blocks = num_res_blocks

        # Patcher.
        patch_size = ignore_kwargs.get("patch_size", 1)
        if ignore_kwargs.get("padded_patcher", False):
            assert ignore_kwargs.get("variables", None) is not None, "variables must be provided for padded patcher"
            self.patcher3d = PaddedPatcher3D(
                ignore_kwargs["variables"],
                patch_size,
                ignore_kwargs.get("patch_method", "haar"),
                ignore_kwargs["max_img_size"],
                ignore_kwargs["learnable_padding"],
            )
        else:
            self.patcher3d = Patcher3D(patch_size, ignore_kwargs.get("patch_method", "rearrange"))
        in_channels = (modified_in_channels if modified_in_channels is not None else in_channels) * patch_size * patch_size * patch_size
        # in_channels = modified_in_channels if modified_in_channels else (in_channels * patch_size * patch_size * patch_size)

        # calculate the number of downsample operations
        self.num_spatial_downs = int(math.log2(spatial_compression)) - int(math.log2(patch_size))
        assert (
            self.num_spatial_downs <= self.num_resolutions
        ), f"Spatially downsample {self.num_resolutions} times at most"

        self.num_temporal_downs = int(math.log2(temporal_compression)) - int(math.log2(patch_size))
        assert (
            self.num_temporal_downs <= self.num_resolutions
        ), f"Temporally downsample {self.num_resolutions} times at most"

        # downsampling
        self.conv_in = nn.Sequential(
            CausalConv3d(in_channels, channels, kernel_size=(1, 3, 3), stride=1, padding=1),
            CausalConv3d(channels, channels, kernel_size=(3, 1, 1), stride=1, padding=0),
        )

    def forward(self, x: torch.Tensor, skip_patcher=False, variables=None) -> torch.Tensor:
        if not skip_patcher:
            if isinstance(self.patcher3d, PaddedPatcher3D):
                x = self.patcher3d(x, variables)
            else:
                x = self.patcher3d(x)

        # downsampling
        h = self.conv_in(x)

        return h

class EncoderFactorizedMidOnly(nn.Module):
    def __init__(
        self,
        in_channels: int,
        channels: int,
        channels_mult: list[int],
        num_res_blocks: int,
        attn_resolutions: list[int],
        dropout: float,
        resolution: int,
        z_channels: int,
        spatial_compression: int,
        temporal_compression: int,
        modified_in_channels: int = None,
        **ignore_kwargs,
    ) -> None:
        super().__init__()
        self.num_resolutions = len(channels_mult)
        self.num_res_blocks = num_res_blocks
        patch_size = ignore_kwargs.get("patch_size", 1)

        # calculate the number of downsample operations
        self.num_spatial_downs = int(math.log2(spatial_compression)) - int(math.log2(patch_size))
        assert (
            self.num_spatial_downs <= self.num_resolutions
        ), f"Spatially downsample {self.num_resolutions} times at most"

        self.num_temporal_downs = int(math.log2(temporal_compression)) - int(math.log2(patch_size))
        assert (
            self.num_temporal_downs <= self.num_resolutions
        ), f"Temporally downsample {self.num_resolutions} times at most"

        curr_res = resolution // patch_size
        in_ch_mult = (1,) + tuple(channels_mult)
        self.in_ch_mult = in_ch_mult
        self.down = nn.ModuleList()
        for i_level in range(self.num_resolutions):
            block = nn.ModuleList()
            attn = nn.ModuleList()
            block_in = channels * in_ch_mult[i_level]
            block_out = channels * channels_mult[i_level]
            for _ in range(self.num_res_blocks):
                block.append(
                    CausalResnetBlockFactorized3d(
                        in_channels=block_in, out_channels=block_out, dropout=dropout, num_groups=1
                    )
                )
                block_in = block_out
                if curr_res in attn_resolutions:
                    attn.append(
                        nn.Sequential(
                            CausalAttnBlock(block_in, num_groups=1), CausalTemporalAttnBlock(block_in, num_groups=1)
                        )
                    )
            down = nn.Module()
            down.block = block
            down.attn = attn
            if i_level != self.num_resolutions - 1:
                spatial_down = i_level < self.num_spatial_downs
                temporal_down = i_level < self.num_temporal_downs
                down.downsample = CausalHybridDownsample3d(
                    block_in, spatial_down=spatial_down, temporal_down=temporal_down
                )
                curr_res = curr_res // 2
            self.down.append(down)

        # middle
        self.mid = nn.Module()
        self.mid.block_1 = CausalResnetBlockFactorized3d(
            in_channels=block_in, out_channels=block_in, dropout=dropout, num_groups=1
        )
        self.mid.attn_1 = nn.Sequential(
            CausalAttnBlock(block_in, num_groups=1), CausalTemporalAttnBlock(block_in, num_groups=1)
        )
        self.mid.block_2 = CausalResnetBlockFactorized3d(
            in_channels=block_in, out_channels=block_in, dropout=dropout, num_groups=1
        )

        # end
        self.norm_out = CausalNormalize(block_in, num_groups=1)
        self.conv_out = nn.Sequential(
            CausalConv3d(block_in, z_channels, kernel_size=(1, 3, 3), stride=1, padding=1),
            CausalConv3d(z_channels, z_channels, kernel_size=(3, 1, 1), stride=1, padding=0),
        )

    def forward(self, h) -> torch.Tensor:
        for i_level in range(self.num_resolutions):
            for i_block in range(self.num_res_blocks):
                h = self.down[i_level].block[i_block](h)
                if len(self.down[i_level].attn) > 0:
                    h = self.down[i_level].attn[i_block](h)
            if i_level != self.num_resolutions - 1:
                h = self.down[i_level].downsample(h)
        
        # middle
        h = self.mid.block_1(h)
        h = self.mid.attn_1(h)
        h = self.mid.block_2(h)

        # end
        h = self.norm_out(h)
        h = nonlinearity(h)
        h = self.conv_out(h)
        return h

class DecoderFactorized(nn.Module):
    def __init__(
        self,
        out_channels: int,
        channels: int,
        channels_mult: list[int],
        num_res_blocks: int,
        attn_resolutions: list[int],
        dropout: float,
        resolution: int,
        z_channels: int,
        spatial_compression: int,
        temporal_compression: int,
        modified_out_channels: int = None,
        **ignore_kwargs,
    ):
        super().__init__()
        self.num_resolutions = len(channels_mult)
        self.num_res_blocks = num_res_blocks

        # UnPatcher.
        patch_size = ignore_kwargs.get("patch_size", 1)
        self.unpatcher3d = UnPatcher3D(patch_size, ignore_kwargs.get("patch_method", "rearrange"))
        out_ch = out_channels * patch_size * patch_size * patch_size
        final_out_ch = out_ch if modified_out_channels is None else (modified_out_channels * patch_size * patch_size * patch_size)

        # calculate the number of upsample operations
        self.num_spatial_ups = int(math.log2(spatial_compression)) - int(math.log2(patch_size))
        assert self.num_spatial_ups <= self.num_resolutions, f"Spatially upsample {self.num_resolutions} times at most"
        self.num_temporal_ups = int(math.log2(temporal_compression)) - int(math.log2(patch_size))
        assert (
            self.num_temporal_ups <= self.num_resolutions
        ), f"Temporally upsample {self.num_resolutions} times at most"

        block_in = channels * channels_mult[self.num_resolutions - 1]
        curr_res = (resolution // patch_size) // 2 ** (self.num_resolutions - 1)
        self.z_shape = (1, z_channels, curr_res, curr_res)
        log.debug("Working with z of shape {} = {} dimensions.".format(self.z_shape, np.prod(self.z_shape)))

        # z to block_in
        self.conv_in = nn.Sequential(
            CausalConv3d(z_channels, block_in, kernel_size=(1, 3, 3), stride=1, padding=1),
            CausalConv3d(block_in, block_in, kernel_size=(3, 1, 1), stride=1, padding=0),
        )

        # middle
        self.mid = nn.Module()
        self.mid.block_1 = CausalResnetBlockFactorized3d(
            in_channels=block_in, out_channels=block_in, dropout=dropout, num_groups=1
        )
        self.mid.attn_1 = nn.Sequential(
            CausalAttnBlock(block_in, num_groups=1), CausalTemporalAttnBlock(block_in, num_groups=1)
        )
        self.mid.block_2 = CausalResnetBlockFactorized3d(
            in_channels=block_in, out_channels=block_in, dropout=dropout, num_groups=1
        )

        legacy_mode = ignore_kwargs.get("legacy_mode", False)
        # upsampling
        self.up = nn.ModuleList()
        for i_level in reversed(range(self.num_resolutions)):
            block = nn.ModuleList()
            attn = nn.ModuleList()
            block_out = channels * channels_mult[i_level]
            for _ in range(self.num_res_blocks + 1):
                block.append(
                    CausalResnetBlockFactorized3d(
                        in_channels=block_in, out_channels=block_out, dropout=dropout, num_groups=1
                    )
                )
                block_in = block_out
                if curr_res in attn_resolutions:
                    attn.append(
                        nn.Sequential(
                            CausalAttnBlock(block_in, num_groups=1), CausalTemporalAttnBlock(block_in, num_groups=1)
                        )
                    )
            up = nn.Module()
            up.block = block
            up.attn = attn
            if i_level != 0:
                # The layer index for temporal/spatial downsampling performed in the encoder should correspond
                # to the layer index, inreverse order, where upsampling is performed in the decoder.
                # If you've a pre-trained model, you can simply finetune.
                # For example:
                #   Input tensor = (1, 3, 17, 32, 32)
                #   Patch size = 4 for 3D wavelet transform
                #   Compression rate = (8x16x16)
                #
                # We expect successive downsampling in the encoder and upsampling in the decoder to be mirrored.
                # ENCODER: `(...,5,8,8) -> (...,3,4,4) -> (...,3,2,2)`
                # DECODER: `(...,3,2,2) -> (...,3,4,4) -> (...,5,8,8)`
                #
                # if legacy_mode is True, the temporal upsampling is not perfectly mirrored.
                # ENCODER: `(...,5,8,8) -> (...,3,4,4) -> (...,3,2,2)`
                # DECODER: `(...,3,2,2) -> (...,5,4,4) -> (...,5,8,8)`
                #
                # Most of the CV and DV tokenizers were trained before 09/01/2024 with upsampling that's not mirrored.
                # Going forward, new CV/DV tokenizers will adopt `legacy_mode=False`, i.e. use mirrored upsampling.
                i_level_reverse = self.num_resolutions - i_level - 1
                if legacy_mode:
                    temporal_up = i_level_reverse < self.num_temporal_ups
                else:
                    temporal_up = 0 < i_level_reverse < self.num_temporal_ups + 1
                spatial_up = temporal_up or (
                    i_level_reverse < self.num_spatial_ups and self.num_spatial_ups > self.num_temporal_ups
                )
                up.upsample = CausalHybridUpsample3d(block_in, spatial_up=spatial_up, temporal_up=temporal_up)
                curr_res = curr_res * 2
            self.up.insert(0, up)  # prepend to get consistent order

        # end
        self.norm_out = CausalNormalize(block_in, num_groups=1)
        self.conv_out = nn.Sequential(
            CausalConv3d(block_in, out_ch, kernel_size=(1, 3, 3), stride=1, padding=1),
            CausalConv3d(out_ch, final_out_ch, kernel_size=(3, 1, 1), stride=1, padding=0),
        )

    def forward(self, z, checkpointing=False):
        # Helper function to conditionally apply checkpoint
        def maybe_checkpoint(module, *args):
            if checkpointing:
                return checkpoint(module, *args, use_reentrant=False)
            else:
                return module(*args)
        
        h = self.conv_in(z)

        # middle block.
        h = maybe_checkpoint(self.mid.block_1, h)
        h = maybe_checkpoint(self.mid.attn_1, h)
        h = maybe_checkpoint(self.mid.block_2, h)

        # decoder blocks.
        for i_level in reversed(range(self.num_resolutions)):
            for i_block in range(self.num_res_blocks + 1):
                h = maybe_checkpoint(self.up[i_level].block[i_block], h)
                if len(self.up[i_level].attn) > 0:
                    h = maybe_checkpoint(self.up[i_level].attn[i_block], h)
            if i_level != 0:
                h = self.up[i_level].upsample(h)

        h = self.norm_out(h)
        h = nonlinearity(h)
        h = self.conv_out(h)
        h = self.unpatcher3d(h)
        return h
