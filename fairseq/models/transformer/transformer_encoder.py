# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import math
from typing import Dict, List, Optional

import torch
import torch.nn as nn
from fairseq import utils
from fairseq.distributed import fsdp_wrap
from fairseq.models import FairseqEncoder
from fairseq.modules import (
    FairseqDropout,
    LayerDropModuleList,
    LayerNorm,
    PositionalEmbedding,
    SinusoidalPositionalEmbedding,
)
from fairseq.modules import transformer_layer
from fairseq.modules.checkpoint_activations import checkpoint_wrapper
from fairseq.modules.quant_noise import quant_noise as apply_quant_noise_
from torch import Tensor
from fairseq.models.transformer import (
    TransformerConfig,
)
import fairseq


# rewrite name for backward compatibility in `make_generation_fast_`
def module_name_fordropout(module_name: str) -> str:
    if module_name == "TransformerEncoderBase":
        return "TransformerEncoder"
    else:
        return module_name


# takes in a raw positional embedding, zero out any emb vector that doesn't corresponds to a character
def get_pos_emb(raw_pos_emb, type_vector, batch, src_len):
    for i in range(batch):
        for j in range(src_len):
            if type_vector[i, j] != 2:
                raw_pos_emb[i, j, :] = torch.zeros_like(raw_pos_emb[i, j, :])
    return raw_pos_emb


# get the backward positional embedding (zero vectors unchanged, reverse others)
def reverse_pos_emb(forward_pos_emb, batch, src_len):
    emd_dim = len(forward_pos_emb[0, 0, :])
    # print("emd dim", emd_dim)
    backward_pos_emb = torch.zeros([batch, src_len, emd_dim], dtype=torch.float, device=torch.device(
        'cuda' if torch.cuda.is_available() else 'cpu'))
    for i in range(batch):
        stack = []
        for j in range(src_len):
            emb = forward_pos_emb[i, j, :]
            if len(torch.nonzero(emb)) >= 1:  # if the emb vector is not all 0's
                stack.append(emb)
        # check size of non-zero's
        # print("stack size", len(stack))
        for j in range(src_len):
            emb = forward_pos_emb[i, j, :]
            if len(torch.nonzero(emb)) >= 1:
                backward_pos_emb[i, j, :] = stack.pop()
            else:
                backward_pos_emb[i, j, :] = emb
    return backward_pos_emb


class TransformerEncoderBase(FairseqEncoder):
    """
    Transformer encoder consisting of *cfg.encoder.layers* layers. Each layer
    is a :class:`TransformerEncoderLayer`.

    Args:
        args (argparse.Namespace): parsed command-line arguments
        dictionary (~fairseq.data.Dictionary): encoding dictionary
        embed_tokens (torch.nn.Embedding): input embedding
    """

    def __init__(self, cfg, dictionary, embed_tokens, return_fc=False):
        self.cfg = cfg
        super().__init__(dictionary)
        self.register_buffer("version", torch.Tensor([3]))

        self.dropout_module = FairseqDropout(
            cfg.dropout, module_name=module_name_fordropout(self.__class__.__name__)
        )
        self.encoder_layerdrop = cfg.encoder.layerdrop
        self.return_fc = return_fc

        embed_dim = embed_tokens.embedding_dim
        self.padding_idx = embed_tokens.padding_idx
        self.max_source_positions = cfg.max_source_positions

        self.embed_tokens = embed_tokens

        self.embed_scale = 1.0 if cfg.no_scale_embedding else math.sqrt(embed_dim)

        self.embed_positions = (
            PositionalEmbedding(
                cfg.max_source_positions,
                embed_dim // 2,
                self.padding_idx,
                learned=cfg.encoder.learned_pos,
            )
            if not cfg.no_token_positional_embeddings
            else None
        )
        if cfg.layernorm_embedding:
            self.layernorm_embedding = LayerNorm(embed_dim, export=cfg.export)
        else:
            self.layernorm_embedding = None

        if not cfg.adaptive_input and cfg.quant_noise.pq > 0:
            self.quant_noise = apply_quant_noise_(
                nn.Linear(embed_dim, embed_dim, bias=False),
                cfg.quant_noise.pq,
                cfg.quant_noise.pq_block_size,
            )
        else:
            self.quant_noise = None

        if self.encoder_layerdrop > 0.0:
            self.layers = LayerDropModuleList(p=self.encoder_layerdrop)
        else:
            self.layers = nn.ModuleList([])
        self.layers.extend(
            [self.build_encoder_layer(cfg) for i in range(cfg.encoder.layers)]
        )
        self.num_layers = len(self.layers)

        if cfg.encoder.normalize_before:
            self.layer_norm = LayerNorm(embed_dim, export=cfg.export)
        else:
            self.layer_norm = None
        # initialize map and embedding object
        self.char_type_map = []
        self.type_embedding_obj = fairseq.models.transformer.Embedding(3, self.embed_tokens.embedding_dim, None)

    def read_type_map(self, cfg):
        # ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
        # print("ROOT_DIR", ROOT_DIR)  # 'fairseq/models/transformer'
        # assuming the data-bin folder is a direct child of some folder which is a sibling of the fairseq folder
        data_path = "./././data-bin/{}_map.txt".format(cfg.lang)
        # todo: get path more robustly
        # print('data_path', data_path)
        with open(data_path, 'r') as data:
            for line in data:
                self.char_type_map.append(line[:-1])

    def build_type_vector(self, src_tokens):
        self.char_type_map = []
        self.read_type_map(self.cfg)
        exact_batch_size, src_length = src_tokens.shape
        type_vector = torch.zeros([exact_batch_size, src_length], dtype=torch.int, device=torch.device(
            'cuda' if torch.cuda.is_available() else 'cpu'))
        for i in range(exact_batch_size):
            for j in range(src_length):
                curr_id = src_tokens[i, j]
                if self.char_type_map[curr_id] == 'grammatical_symbol':
                    type_vector[i, j] = 1
                if self.char_type_map[curr_id] == 'character':
                    type_vector[i, j] = 2  # padding - 0, grammatical_symbol - 1, character - 2
        return type_vector

    def build_encoder_layer(self, cfg):
        layer = transformer_layer.TransformerEncoderLayerBase(
            cfg, return_fc=self.return_fc
        )
        checkpoint = cfg.checkpoint_activations
        if checkpoint:
            offload_to_cpu = cfg.offload_activations
            layer = checkpoint_wrapper(layer, offload_to_cpu=offload_to_cpu)
        # if we are checkpointing, enforce that FSDP always wraps the
        # checkpointed layer, regardless of layer size
        min_params_to_wrap = cfg.min_params_to_wrap if not checkpoint else 0
        layer = fsdp_wrap(layer, min_num_params=min_params_to_wrap)
        return layer

    def forward_embedding(
            self, src_tokens, token_embedding: Optional[torch.Tensor] = None
    ):
        # embed tokens and positions
        exact_batch_size, src_length = src_tokens.shape
        type_vector = self.build_type_vector(src_tokens)

        type_embedding = self.type_embedding_obj(type_vector)
        if token_embedding is None:
            token_embedding = self.embed_tokens(src_tokens)
        x = embed = self.embed_scale * (token_embedding + type_embedding)
        if self.embed_positions is not None:
            # get forward pos_emb, and add backward pos_emb
            raw_pos_emb = self.embed_positions(src_tokens)
            forward_pos_emb = get_pos_emb(raw_pos_emb, type_vector, exact_batch_size, src_length)
            # print("forward pos_emb", forward_pos_emb)
            # print("forward check zeroes: ", forward_pos_emb[:, :, 0])
            # print("forward pos_emb size", forward_pos_emb.shape)
            backward_pos_emb = reverse_pos_emb(forward_pos_emb, exact_batch_size, src_length)
            # print("backward pos_emb", backward_pos_emb)
            # print("backward check zeroes: ", backward_pos_emb[:, :, 0])
            # print("backward pos_emb size", backward_pos_emb.shape)
            x = embed + torch.cat((forward_pos_emb, backward_pos_emb), 2)
            # x = embed + self.embed_positions(src_tokens)
        if self.layernorm_embedding is not None:
            x = self.layernorm_embedding(x)
        x = self.dropout_module(x)
        if self.quant_noise is not None:
            x = self.quant_noise(x)
        return x, embed

    def forward(
            self,
            src_tokens,
            src_lengths: Optional[torch.Tensor] = None,
            return_all_hiddens: bool = False,
            token_embeddings: Optional[torch.Tensor] = None,
    ):
        """
        Args:
            src_tokens (LongTensor): tokens in the source language of shape
                `(batch, src_len)`
            src_lengths (torch.LongTensor): lengths of each source sentence of
                shape `(batch)`
            return_all_hiddens (bool, optional): also return all of the
                intermediate hidden states (default: False).
            token_embeddings (torch.Tensor, optional): precomputed embeddings
                default `None` will recompute embeddings

        Returns:
            dict:
                - **encoder_out** (Tensor): the last encoder layer's output of
                  shape `(src_len, batch, embed_dim)`
                - **encoder_padding_mask** (ByteTensor): the positions of
                  padding elements of shape `(batch, src_len)`
                - **encoder_embedding** (Tensor): the (scaled) embedding lookup
                  of shape `(batch, src_len, embed_dim)`
                - **encoder_states** (List[Tensor]): all intermediate
                  hidden states of shape `(src_len, batch, embed_dim)`.
                  Only populated if *return_all_hiddens* is True.
        """
        return self.forward_scriptable(
            src_tokens, src_lengths, return_all_hiddens, token_embeddings
        )

    # TorchScript doesn't support super() method so that the scriptable Subclass
    # can't access the base class model in Torchscript.
    # Current workaround is to add a helper function with different name and
    # call the helper function from scriptable Subclass.
    def forward_scriptable(
            self,
            src_tokens,
            src_lengths: Optional[torch.Tensor] = None,
            return_all_hiddens: bool = False,
            token_embeddings: Optional[torch.Tensor] = None,
    ):
        """
        Args:
            src_tokens (LongTensor): tokens in the source language of shape
                `(batch, src_len)`
            src_lengths (torch.LongTensor): lengths of each source sentence of
                shape `(batch)`
            return_all_hiddens (bool, optional): also return all of the
                intermediate hidden states (default: False).
            token_embeddings (torch.Tensor, optional): precomputed embeddings
                default `None` will recompute embeddings

        Returns:
            dict:
                - **encoder_out** (Tensor): the last encoder layer's output of
                  shape `(src_len, batch, embed_dim)`
                - **encoder_padding_mask** (ByteTensor): the positions of
                  padding elements of shape `(batch, src_len)`
                - **encoder_embedding** (Tensor): the (scaled) embedding lookup
                  of shape `(batch, src_len, embed_dim)`
                - **encoder_states** (List[Tensor]): all intermediate
                  hidden states of shape `(src_len, batch, embed_dim)`.
                  Only populated if *return_all_hiddens* is True.
        """
        # compute padding mask
        encoder_padding_mask = src_tokens.eq(self.padding_idx)
        has_pads = src_tokens.device.type == "xla" or encoder_padding_mask.any()

        x, encoder_embedding = self.forward_embedding(src_tokens, token_embeddings)

        # account for padding while computing the representation
        if has_pads:
            x = x * (1 - encoder_padding_mask.unsqueeze(-1).type_as(x))

        # B x T x C -> T x B x C
        x = x.transpose(0, 1)

        encoder_states = []
        fc_results = []

        if return_all_hiddens:
            encoder_states.append(x)

        # encoder layers
        for layer in self.layers:
            lr = layer(
                x, encoder_padding_mask=encoder_padding_mask if has_pads else None
            )

            if isinstance(lr, tuple) and len(lr) == 2:
                x, fc_result = lr
            else:
                x = lr
                fc_result = None

            if return_all_hiddens and not torch.jit.is_scripting():
                assert encoder_states is not None
                encoder_states.append(x)
                fc_results.append(fc_result)

        if self.layer_norm is not None:
            x = self.layer_norm(x)

        # The Pytorch Mobile lite interpreter does not supports returning NamedTuple in
        # `forward` so we use a dictionary instead.
        # TorchScript does not support mixed values so the values are all lists.
        # The empty list is equivalent to None.
        src_lengths = (
            src_tokens.ne(self.padding_idx)
                .sum(dim=1, dtype=torch.int32)
                .reshape(-1, 1)
                .contiguous()
        )
        return {
            "encoder_out": [x],  # T x B x C
            "encoder_padding_mask": [encoder_padding_mask],  # B x T
            "encoder_embedding": [encoder_embedding],  # B x T x C
            "encoder_states": encoder_states,  # List[T x B x C]
            "fc_results": fc_results,  # List[T x B x C]
            "src_tokens": [],
            "src_lengths": [src_lengths],
        }

    @torch.jit.export
    def reorder_encoder_out(self, encoder_out: Dict[str, List[Tensor]], new_order):
        """
        Reorder encoder output according to *new_order*.

        Args:
            encoder_out: output from the ``forward()`` method
            new_order (LongTensor): desired order

        Returns:
            *encoder_out* rearranged according to *new_order*
        """
        if len(encoder_out["encoder_out"]) == 0:
            new_encoder_out = []
        else:
            new_encoder_out = [encoder_out["encoder_out"][0].index_select(1, new_order)]
        if len(encoder_out["encoder_padding_mask"]) == 0:
            new_encoder_padding_mask = []
        else:
            new_encoder_padding_mask = [
                encoder_out["encoder_padding_mask"][0].index_select(0, new_order)
            ]
        if len(encoder_out["encoder_embedding"]) == 0:
            new_encoder_embedding = []
        else:
            new_encoder_embedding = [
                encoder_out["encoder_embedding"][0].index_select(0, new_order)
            ]

        if len(encoder_out["src_tokens"]) == 0:
            src_tokens = []
        else:
            src_tokens = [(encoder_out["src_tokens"][0]).index_select(0, new_order)]

        if len(encoder_out["src_lengths"]) == 0:
            src_lengths = []
        else:
            src_lengths = [(encoder_out["src_lengths"][0]).index_select(0, new_order)]

        encoder_states = encoder_out["encoder_states"]
        if len(encoder_states) > 0:
            for idx, state in enumerate(encoder_states):
                encoder_states[idx] = state.index_select(1, new_order)

        return {
            "encoder_out": new_encoder_out,  # T x B x C
            "encoder_padding_mask": new_encoder_padding_mask,  # B x T
            "encoder_embedding": new_encoder_embedding,  # B x T x C
            "encoder_states": encoder_states,  # List[T x B x C]
            "src_tokens": src_tokens,  # B x T
            "src_lengths": src_lengths,  # B x 1
        }

    def max_positions(self):
        """Maximum input length supported by the encoder."""
        if self.embed_positions is None:
            return self.max_source_positions
        return min(self.max_source_positions, self.embed_positions.max_positions)

    def upgrade_state_dict_named(self, state_dict, name):
        """Upgrade a (possibly old) state dict for new versions of fairseq."""
        if isinstance(self.embed_positions, SinusoidalPositionalEmbedding):
            weights_key = "{}.embed_positions.weights".format(name)
            if weights_key in state_dict:
                print("deleting {0}".format(weights_key))
                del state_dict[weights_key]
            state_dict[
                "{}.embed_positions._float_tensor".format(name)
            ] = torch.FloatTensor(1)
        for i in range(self.num_layers):
            # update layer norms
            self.layers[i].upgrade_state_dict_named(
                state_dict, "{}.layers.{}".format(name, i)
            )

        version_key = "{}.version".format(name)
        if utils.item(state_dict.get(version_key, torch.Tensor([1]))[0]) < 2:
            # earlier checkpoints did not normalize after the stack of layers
            self.layer_norm = None
            self.normalize = False
            state_dict[version_key] = torch.Tensor([1])
        return state_dict


class TransformerEncoder(TransformerEncoderBase):
    def __init__(self, args, dictionary, embed_tokens, return_fc=False):
        self.args = args
        super().__init__(
            TransformerConfig.from_namespace(args),
            dictionary,
            embed_tokens,
            return_fc=return_fc,
        )

    def build_encoder_layer(self, args):
        return super().build_encoder_layer(
            TransformerConfig.from_namespace(args),
        )
