from collections import namedtuple, OrderedDict
from typing import Dict, List

import torch
from torch import nn as nn

from model.decoder import Decoder
from model.encoder import Encoder
from utils import util, nn_util
from utils.ast import AbstractSyntaxTree
from utils.vocab import Vocab, SAME_VARIABLE_TOKEN, END_OF_VARIABLE_TOKEN


class RecurrentSubtokenDecoder(Decoder):
    def __init__(self, ast_node_encoding_size: int, hidden_size: int, dropout: float, vocab: Vocab):
        super(Decoder, self).__init__()

        self.vocab = vocab

        self.lstm_cell = nn.LSTMCell(ast_node_encoding_size + ast_node_encoding_size, hidden_size)
        self.decoder_cell_init = nn.Linear(ast_node_encoding_size, hidden_size)
        self.state2names = nn.Linear(ast_node_encoding_size, len(vocab.target), bias=True)
        self.dropout = nn.Dropout(dropout)
        self.config: Dict = None

        self.Hypothesis = namedtuple('Hypothesis', ['variable_list', 'variable_ptr', 'score'])

    @property
    def device(self):
        return self.state2names.weight.device

    @classmethod
    def default_params(cls):
        return {
            'vocab_file': None,
            'ast_node_encoding_size': 128,
            'hidden_size': 128,
            'input_feed': False,
            'dropout': 0.2,
            'beam_size': 5,
            'max_prediction_time_step': 5000
        }

    @classmethod
    def build(cls, config):
        params = util.update(cls.default_params(), config)

        vocab = Vocab.load(params['vocab_file'])
        model = cls(params['ast_node_encoding_size'], params['hidden_size'], params['dropout'], vocab)
        model.config = params

        return model

    def get_init_state(self, src_ast_encoding):
        # compute initial decoder's state via average pooling

        # (packed_graph_size, encoding_size)
        packed_tree_node_encoding = src_ast_encoding['packed_tree_node_encoding']

        tree_num = src_ast_encoding['tree_num']
        total_node_num = src_ast_encoding['tree_node_to_tree_id_map'].size(0)
        encoding_size = packed_tree_node_encoding.size(-1)

        zero_encoding = packed_tree_node_encoding.new_zeros(tree_num, encoding_size)

        node_encoding_sum = zero_encoding.scatter_add_(0,
                                                       src_ast_encoding['tree_node_to_tree_id_map'].unsqueeze(-1).expand(-1, encoding_size),
                                                       packed_tree_node_encoding)
        tree_node_num = packed_tree_node_encoding.new_zeros(tree_num).scatter_add_(0, src_ast_encoding['tree_node_to_tree_id_map'],
                                                                                   packed_tree_node_encoding.new_zeros(total_node_num).fill_(1.))
        avg_node_encoding = node_encoding_sum / tree_node_num.unsqueeze(-1)

        c_0 = self.decoder_cell_init(avg_node_encoding)
        h_0 = torch.tanh(c_0)

        return h_0, c_0

    def rnn_step(self, x, h_tm1, src_ast_encoding):
        h_t = self.lstm_cell(x, h_tm1)
        # TODO: implement attention?
        # att_t = torch.tanh(self.att_vec_linear(torch.cat([h_t], 1)))
        q_t = self.dropout(h_t[0])

        return h_t, q_t, None

    def forward(self, src_ast_encoding, prediction_target):
        # (prediction_node_num, encoding_size)
        variable_master_node_encoding = src_ast_encoding['variable_master_node_encoding']
        # (batch_size, max_time_step)
        variable_encoding_restoration_indices = prediction_target['variable_encoding_restoration_indices']
        variable_encoding_restoration_indices_mask = prediction_target['variable_encoding_restoration_indices_mask']

        # (batch_size, max_time_step, encoding_size)
        variable_encoding = variable_master_node_encoding[variable_encoding_restoration_indices]
        # (batch_size, max_time_step, encoding_size)
        variable_tgt_name_id = prediction_target['variable_tgt_name_id']

        batch_size = variable_tgt_name_id.size(0)
        variable_encoding_size = variable_encoding.size(-1)

        h_0 = self.get_init_state(src_ast_encoding)
        att_tm1 = variable_encoding.new_zeros(src_ast_encoding['tree_num'], variable_encoding_size)

        h_tm1 = h_0
        query_vecs = []
        for t, variable_encoding_t in enumerate(variable_encoding.split(split_size=1, dim=1)):
            # variable_encoding_t: (batch_size, encoding_size)
            variable_encoding_t = variable_encoding_t.squeeze(1)
            if t > 0:
                # (batch_size)
                v_tm1_name_id = variable_tgt_name_id[:, t - 1]
                # (batch_size, encoding_size)
                v_tm1_name_embed = self.state2names.weight[v_tm1_name_id]
            else:
                # (batch_size, encoding_size)
                v_tm1_name_embed = torch.zeros(batch_size, variable_encoding_size, device=self.device)

            if self.config['input_feed']:
                x = torch.cat([variable_encoding_t, v_tm1_name_embed, att_tm1], dim=-1)
            else:
                x = torch.cat([variable_encoding_t, v_tm1_name_embed], dim=-1)

            h_t, q_t, alpha_t = self.rnn_step(x, h_tm1, src_ast_encoding)

            att_tm1 = q_t
            h_tm1 = h_t
            query_vecs.append(q_t)

        # (batch_size, max_prediction_node_num, encoding_size)
        query_vecs = torch.stack(query_vecs).permute(1, 0, 2)

        # (batch_size, max_prediction_node_num, vocab_size)
        logits = self.state2names(query_vecs)
        var_name_log_probs = torch.log_softmax(logits, dim=-1)
        var_name_log_probs = var_name_log_probs * variable_encoding_restoration_indices_mask.unsqueeze(-1)

        return var_name_log_probs

    def get_target_log_prob(self, var_name_log_probs, prediction_target, src_ast_encoding):
        # (batch_size, max_prediction_node_num)
        variable_tgt_name_id = prediction_target['variable_tgt_name_id']
        tgt_var_name_log_prob = torch.gather(var_name_log_probs,
                                             dim=-1, index=variable_tgt_name_id.unsqueeze(-1)).squeeze(-1)

        tgt_var_name_log_prob = tgt_var_name_log_prob * prediction_target['variable_encoding_restoration_indices_mask']

        result = dict(tgt_var_name_log_prob=tgt_var_name_log_prob)

        return result

    def predict(self, source_asts: List[AbstractSyntaxTree], encoder: Encoder) -> List[Dict]:
        beam_size = self.config['beam_size']
        same_variable_id = self.vocab.target[SAME_VARIABLE_TOKEN]
        end_of_variable_id = self.vocab.target[END_OF_VARIABLE_TOKEN]

        variable_nums = []
        for ast_id, ast in enumerate(source_asts):
            variable_nums.append(len(ast.variables))

        beams = OrderedDict((ast_id, [self.Hypothesis([], 0, 0.)]) for ast_id, ast in enumerate(source_asts))
        hyp_scores_tm1 = torch.zeros(len(beams), device=self.device)
        completed_hyps = [[] for _ in source_asts]
        tgt_vocab_size = len(self.vocab.target)

        tensor_dict = self.batcher.to_tensor_dict(source_asts=source_asts)
        nn_util.to(tensor_dict, self.device)

        context_encoding = encoder(tensor_dict)
        h_tm1 = h_0 = self.get_init_state(context_encoding)

        # (variable_master_node_num, encoding_size)
        variable_master_node_encoding = context_encoding['variable_master_node_encoding']
        encoding_size = variable_master_node_encoding.size(1)
        # (batch_size, variable_master_node_num)
        variable_master_node_restoration_indices = context_encoding['variable_master_node_restoration_indices']

        # (batch_size, variable_master_node_num, encoding_size)
        variable_master_node_encoding = variable_master_node_encoding[variable_master_node_restoration_indices]
        # (batch_size, encoding_size)
        variable_name_embed_tm1 = att_tm1 = torch.zeros(len(source_asts), encoding_size, device=self.device)

        max_prediction_time_step = self.config['max_prediction_time_step']
        for t in range(0, max_prediction_time_step):
            # (total_live_hyp_num, encoding_size)
            if t > 0:
                variable_encoding_t = variable_master_node_encoding[hyp_ast_ids_t, hyp_variable_ptrs_t]
            else:
                variable_encoding_t = variable_master_node_encoding[:, 0]

            if self.config['input_feed']:
                x = torch.cat([variable_encoding_t, variable_name_embed_tm1, att_tm1], dim=-1)
            else:
                x = torch.cat([variable_encoding_t, variable_name_embed_tm1], dim=-1)

            h_t, q_t, alpha_t = self.rnn_step(x, h_tm1, context_encoding)

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

                beam_start_hyp_pos = beam_end_hyp_pos

            if live_beam_ids:
                hyp_scores_tm1 = torch.tensor(new_hyp_scores, device=self.device)
                h_tm1 = (h_t[0][live_prev_hyp_ids], h_t[1][live_prev_hyp_ids])
                att_tm1 = q_t[live_prev_hyp_ids]

                variable_name_embed_tm1 = self.state2names.weight[new_hyp_var_name_ids]
                hyp_ast_ids_t = new_hyp_ast_ids
                hyp_variable_ptrs_t = new_hyp_variable_ptrs

                beams = new_beams
            else:
                break

        variable_rename_results = []
        for i, hyps in enumerate(completed_hyps):
            ast = source_asts[i]
            hyps = sorted(hyps, key=lambda hyp: -hyp.score)
            top_hyp = hyps[0]
            variable_rename_result = dict()
            sub_token_ptr = 0
            for old_name in ast.variables:
                sub_token_begin = sub_token_ptr
                while top_hyp.variable_list[sub_token_ptr] != end_of_variable_id:
                    sub_token_ptr += 1
                sub_token_ptr += 1  # point to first sub-token of next variable
                sub_token_end = sub_token_ptr

                var_name_token_ids = top_hyp.variable_list[sub_token_begin: sub_token_end]  # include ending </s>
                if var_name_token_ids == [same_variable_id]:
                    new_var_name = old_name
                else:
                    new_var_name = self.vocab.target.subtoken_model.decode_ids(var_name_token_ids)

                variable_rename_result[old_name] = {'new_name': new_var_name,
                                                    'prob': top_hyp.score}

            variable_rename_results.append(variable_rename_result)

        return variable_rename_results
