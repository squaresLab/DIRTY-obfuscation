import sys
from collections import namedtuple, OrderedDict
from typing import Dict, List

import torch
from torch import nn as nn

from model.decoder import Decoder
from model.encoder import Encoder
from utils import util, nn_util
from utils.ast import AbstractSyntaxTree
from utils.dataset import Example
from utils.vocab import Vocab, SAME_VARIABLE_TOKEN, END_OF_VARIABLE_TOKEN

from model.recurrent_subtoken_decoder import RecurrentSubtokenDecoder
from utils.vocab import Vocab


class AttentionalRecurrentSubtokenDecoder(RecurrentSubtokenDecoder):
    def __init__(self, ast_node_encoding_size: int, hidden_size: int, dropout: float, vocab: Vocab):
        super(AttentionalRecurrentSubtokenDecoder, self).__init__(ast_node_encoding_size, hidden_size, dropout, vocab)

        self.att_src_linear = nn.Linear(ast_node_encoding_size, hidden_size, bias=False)
        self.att_vec_linear = nn.Linear(ast_node_encoding_size + hidden_size, hidden_size, bias=False)

    @classmethod
    def default_params(cls):
        params = RecurrentSubtokenDecoder.default_params()
        params.update({
            'attention_target': 'ast_nodes'  # terminal_nodes
        })

        return params

    def rnn_step(self, x, h_tm1, context_encoding):
        h_t, cell_t = self.lstm_cell(x, h_tm1)

        ctx_t, alpha_t = nn_util.dot_prod_attention(h_t,
                                                    context_encoding['attention_value'],
                                                    context_encoding['attention_key'],
                                                    context_encoding['attention_value_mask'])

        att_t = torch.tanh(self.att_vec_linear(torch.cat([h_t, ctx_t], 1)))
        att_t = self.dropout(att_t)

        return (h_t, cell_t), att_t, ctx_t

    def get_attention_memory(self, context_encoding):
        att_tgt = self.config['attention_target']
        value, mask = self.encoder.get_attention_memory(context_encoding, att_tgt)
        key = self.att_src_linear(value)

        return {'attention_key': key,
                'attention_value': value,
                'attention_value_mask': mask}

    def forward(self, src_ast_encoding, prediction_target):
        # prepare tensors for attention
        attention_memory = self.get_attention_memory(src_ast_encoding)
        src_ast_encoding.update(attention_memory)

        return RecurrentSubtokenDecoder.forward(self, src_ast_encoding, prediction_target)

    def predict(self, examples: List[Example], encoder: Encoder) -> List[Dict]:
        batch_size = len(examples)
        beam_size = self.config['beam_size']
        same_variable_id = self.vocab.target[SAME_VARIABLE_TOKEN]
        end_of_variable_id = self.vocab.target[END_OF_VARIABLE_TOKEN]

        variable_nums = []
        for ast_id, example in enumerate(examples):
            variable_nums.append(len(example.ast.variables))

        beams = OrderedDict((ast_id, [self.Hypothesis([], 0, 0.)]) for ast_id in range(batch_size))
        hyp_scores_tm1 = torch.zeros(len(beams), device=self.device)
        completed_hyps = [[] for _ in range(batch_size)]
        tgt_vocab_size = len(self.vocab.target)

        tensor_dict = self.batcher.to_tensor_dict(examples)
        nn_util.to(tensor_dict, self.device)

        context_encoding = encoder(tensor_dict)
        # prepare tensors for attention
        attention_memory = self.get_attention_memory(context_encoding)
        context_encoding_t = attention_memory

        h_tm1 = h_0 = self.get_init_state(context_encoding)

        # Note that we are using the `restoration_indices` from `context_encoding`, which is the word-level restoration index
        # (batch_size, variable_master_node_num, encoding_size)
        variable_encoding = context_encoding['variable_encoding']
        # (batch_size, encoding_size)
        variable_name_embed_tm1 = att_tm1 = torch.zeros(batch_size, self.lstm_cell.hidden_size, device=self.device)

        max_prediction_time_step = self.config['max_prediction_time_step']
        for t in range(0, max_prediction_time_step):
            # (total_live_hyp_num, encoding_size)
            if t > 0:
                variable_encoding_t = variable_encoding[hyp_ast_ids_t, hyp_variable_ptrs_t]
            else:
                variable_encoding_t = variable_encoding[:, 0]

            if self.config['input_feed']:
                x = torch.cat([variable_encoding_t, variable_name_embed_tm1, att_tm1], dim=-1)
            else:
                x = torch.cat([variable_encoding_t, variable_name_embed_tm1], dim=-1)

            h_t, q_t, alpha_t = self.rnn_step(x, h_tm1, context_encoding_t)

            # (total_live_hyp_num, vocab_size)
            hyp_var_name_scores_t = torch.log_softmax(self.state2names(q_t), dim=-1)

            cont_cand_hyp_scores = hyp_scores_tm1.unsqueeze(-1) + hyp_var_name_scores_t

            new_beams = OrderedDict()
            live_beam_ids = []
            new_hyp_scores = []
            live_prev_hyp_ids = []
            new_hyp_var_name_ids = []
            new_hyp_ast_ids = []
            new_hyp_variable_ptrs = []
            is_same_variable_mask = []
            beam_start_hyp_pos = 0
            for beam_id, (ast_id, beam) in enumerate(beams.items()):
                beam_end_hyp_pos = beam_start_hyp_pos + len(beam)
                # (live_beam_size, vocab_size)
                beam_cont_cand_hyp_scores = cont_cand_hyp_scores[beam_start_hyp_pos: beam_end_hyp_pos]
                cont_beam_size = beam_size - len(completed_hyps[ast_id])
                beam_new_hyp_scores, beam_new_hyp_positions = torch.topk(beam_cont_cand_hyp_scores.view(-1),
                                                                         k=cont_beam_size,
                                                                         dim=-1)

                # (cont_beam_size)
                beam_prev_hyp_ids = beam_new_hyp_positions / tgt_vocab_size
                beam_hyp_var_name_ids = beam_new_hyp_positions % tgt_vocab_size

                _prev_hyp_ids = beam_prev_hyp_ids.cpu()
                _hyp_var_name_ids = beam_hyp_var_name_ids.cpu()
                _new_hyp_scores = beam_new_hyp_scores.cpu()

                for i in range(cont_beam_size):
                    prev_hyp_id = _prev_hyp_ids[i].item()
                    prev_hyp = beam[prev_hyp_id]
                    hyp_var_name_id = _hyp_var_name_ids[i].item()
                    new_hyp_score = _new_hyp_scores[i].item()

                    variable_ptr = prev_hyp.variable_ptr
                    if hyp_var_name_id == end_of_variable_id:
                        variable_ptr += 1

                        # remove empty cases
                        if len(prev_hyp.variable_list) == 0 or prev_hyp.variable_list[-1] == end_of_variable_id:
                            continue

                    new_hyp = self.Hypothesis(variable_list=list(prev_hyp.variable_list) + [hyp_var_name_id],
                                              variable_ptr=variable_ptr,
                                              score=new_hyp_score)

                    if variable_ptr == variable_nums[ast_id]:
                        completed_hyps[ast_id].append(new_hyp)
                    else:
                        new_beams.setdefault(ast_id, []).append(new_hyp)
                        live_beam_ids.append(beam_id)
                        new_hyp_scores.append(new_hyp_score)
                        live_prev_hyp_ids.append(beam_start_hyp_pos + prev_hyp_id)
                        new_hyp_var_name_ids.append(hyp_var_name_id)
                        new_hyp_ast_ids.append(ast_id)
                        new_hyp_variable_ptrs.append(variable_ptr)
                        is_same_variable_mask.append(1. if prev_hyp.variable_ptr == variable_ptr else 0.)

                beam_start_hyp_pos = beam_end_hyp_pos

            if live_beam_ids:
                hyp_scores_tm1 = torch.tensor(new_hyp_scores, device=self.device)
                h_tm1 = (h_t[0][live_prev_hyp_ids], h_t[1][live_prev_hyp_ids])
                att_tm1 = q_t[live_prev_hyp_ids]

                variable_name_embed_tm1 = self.state2names.weight[new_hyp_var_name_ids]
                hyp_ast_ids_t = new_hyp_ast_ids
                hyp_variable_ptrs_t = new_hyp_variable_ptrs

                beams = new_beams

                # (total_hyp_num, max_tree_size, node_encoding_size)
                context_encoding_t = dict(attention_key=attention_memory['attention_key'][hyp_ast_ids_t],
                                          attention_value=attention_memory['attention_value'][hyp_ast_ids_t],
                                          attention_value_mask=attention_memory['attention_value_mask'][hyp_ast_ids_t])

                if self.independent_prediction_for_each_variable:
                    is_same_variable_mask = torch.tensor(is_same_variable_mask, device=self.device, dtype=torch.float).unsqueeze(-1)
                    h_tm1 = (h_tm1[0] * is_same_variable_mask, h_tm1[1] * is_same_variable_mask)
                    att_tm1 = att_tm1 * is_same_variable_mask
                    variable_name_embed_tm1 = variable_name_embed_tm1 * is_same_variable_mask
            else:
                break

        variable_rename_results = []
        for i, hyps in enumerate(completed_hyps):
            variable_rename_result = dict()
            ast = examples[i].ast
            hyps = sorted(hyps, key=lambda hyp: -hyp.score)

            if not hyps:
                # return identity renamings
                print(f'Failed to found a hypothesis for function {ast.compilation_unit}', file=sys.stderr)
                for old_name in ast.variables:
                    variable_rename_result[old_name] = {'new_name': old_name,
                                                        'prob': 0.}
            else:
                top_hyp = hyps[0]
                sub_token_ptr = 0
                for old_name in ast.variables:
                    sub_token_begin = sub_token_ptr
                    while top_hyp.variable_list[sub_token_ptr] != end_of_variable_id:
                        sub_token_ptr += 1
                    sub_token_ptr += 1  # point to first sub-token of next variable
                    sub_token_end = sub_token_ptr

                    var_name_token_ids = top_hyp.variable_list[sub_token_begin: sub_token_end]  # include ending </s>
                    if var_name_token_ids == [same_variable_id, end_of_variable_id]:
                        new_var_name = old_name
                    else:
                        new_var_name = self.vocab.target.subtoken_model.decode_ids(var_name_token_ids)

                    variable_rename_result[old_name] = {'new_name': new_var_name,
                                                        'prob': top_hyp.score}

            variable_rename_results.append(variable_rename_result)

        return variable_rename_results
