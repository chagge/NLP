# -*- coding: utf-8 -*-
import argparse
from activations import activations

class Config(object):
	def __init__(self):
		self.use_gpu = True
		self.learning_rate = 0.00025
		self.gradient_momentum = 0.95
		self.n_vocab = -1

		self.char_embed_ndim = 200
		self.intermidiate_ndim = 300
		self.representation_ndim = 400

		self.bi_lstm_units = [self.char_embed_ndim, 1024]
		self.bi_lstm_apply_dropout = False

		self.attention_fc_units = [self.intermidiate_ndim, 1]
		self.attention_fc_activation_function = "elu"
		self.attention_fc_apply_dropout = False

	def check(self):
		if len(self.bi_lstm_units) < 1:
			raise Exception("You need to add one or more hidden layers to LSTM network.")

config = Config()
