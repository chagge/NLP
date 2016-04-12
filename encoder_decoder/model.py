# -*- coding: utf-8 -*-
import os, time
import numpy as np
import chainer
from chainer import cuda, Variable, optimizers, serializers, function, link
from chainer.utils import type_check
from chainer import functions as F
from chainer import links as L
from config import config
from activations import activations
import model

class LSTM(chainer.Chain):
	def __init__(self, **layers):
		super(LSTM, self).__init__(**layers)
		self.n_layers = 0
		self.activation_function = None
		self.apply_dropout = False
		self.apply_batchnorm = False
		self.apply_batchnorm_to_input = False

	def forward_one_step(self, x, test):
		chain = [x]
		embed = self.embed_id(chain[-1])
		chain.append(embed)

		# Hidden layers
		for i in range(self.n_layers):
			u = getattr(self, "layer_%i" % i)(chain[-1])
			if i == 0:
				if self.apply_batchnorm_to_input:
					u = getattr(self, "batchnorm_%i" % i)(u, test=test)
			else:
				if self.apply_batchnorm:
						u = getattr(self, "batchnorm_%i" % i)(u, test=test)
			output = u
			if self.apply_dropout:
				output = F.dropout(output, train=not test)
			chain.append(output)

		return chain[-1]

	def reset_state(self):
		for i in range(self.n_layers):
			getattr(self, "layer_%i" % i).reset_state()

	def __call__(self, x, test=False):
		return self.forward_one_step(x, test=test)

class FullyConnectedNetwork(chainer.Chain):
	def __init__(self, **layers):
		super(FullyConnectedNetwork, self).__init__(**layers)
		self.n_hidden_layers = 0
		self.activation_function = "elu"
		self.apply_dropout = False
		self.apply_batchnorm = False
		self.apply_batchnorm_to_input = False
		self.apply_batchnorm_to_output = False

	def forward_one_step(self, x, test):
		f = activations[self.activation_function]
		chain = [x]

		# Hidden layers
		for i in range(self.n_hidden_layers):
			u = getattr(self, "layer_%i" % i)(chain[-1])
			if i == 0:
				if self.apply_batchnorm_to_input:
					u = getattr(self, "batchnorm_%i" % i)(u, test=test)
			else:
				if self.apply_batchnorm:
					u = getattr(self, "batchnorm_%i" % i)(u, test=test)
			output = f(u)
			if self.apply_dropout:
				output = F.dropout(output, train=not test)
			chain.append(output)

		# Output
		u = getattr(self, "layer_%i" % self.n_hidden_layers)(chain[-1])
		if self.apply_batchnorm_to_output:
			u = getattr(self, "batchnorm_%i" % self.n_hidden_layers)(u, test=test)
		chain.append(f(u))

		return chain[-1]

	def __call__(self, x, test=False):
		return self.forward_one_step(x, test=test)

class Model:
	def __init__(self, encoder_lstm, decoder_lstm, decoder_fc):
		self.encoder_lstm = encoder_lstm
		self.optimizer_encoder_lstm = optimizers.Adam(alpha=config.learning_rate, beta1=config.gradient_momentum)
		self.optimizer_encoder_lstm.setup(self.encoder_lstm)
		self.optimizer_encoder_lstm.add_hook(chainer.optimizer.GradientClipping(10.0))

		self.decoder_lstm = decoder_lstm
		self.optimizer_decoder_lstm = optimizers.Adam(alpha=config.learning_rate, beta1=config.gradient_momentum)
		self.optimizer_decoder_lstm.setup(self.decoder_lstm)
		self.optimizer_decoder_lstm.add_hook(chainer.optimizer.GradientClipping(10.0))

		self.decoder_fc = decoder_fc
		self.optimizer_decoder_fc = optimizers.Adam(alpha=config.learning_rate, beta1=config.gradient_momentum)
		self.optimizer_decoder_fc.setup(self.decoder_fc)
		self.optimizer_decoder_fc.add_hook(chainer.optimizer.GradientClipping(10.0))

	# Inputs:	Numpy / CuPy
	# Returns:	Variable
	def encode(self, x_seq, test=False):
		self.encoder_lstm.reset_state()
		for i, x in enumerate(x_seq):
			x = Variable(x)
			output = self.encoder_lstm(x, test=test)
		return output

	# Inputs:	Variable, Variable
	# Returns:	Confidence of each word ID
	# Note:		decoderのLSTMの内部状態は適当にリセットしておいてください
	def decode_one_step(self, summary, prev_y, test=False, softmax=True):
		input = append_variable(summary, prev_y)
		h = self.decoder_lstm(input, test=test)
		output = self.fc(h, test=test)
		if softmax:
			output = F.softmax(output)
		return output

	# Inputs:	Numpy / CuPy
	# Outputs:	Numpy / CuPy
	# Note:		sampling_yがTrueだと出力されるIDはsoftmax出力からサンプリングします。
	#			そうでない場合は確信度が最も高いものを取ります。（訓練データを再生するだけの意味のないものになるかもしれません）
	def decode(self, x_seq, test=False, limit=1000, sampling_y=True):
		xp = self.xp
		n_batch = x_seq.shape[0]
		summary = self.encode(x_seq, test=test)
		y_seq = xp.zeros((n_batch, 1), dtype=xp.uint8)
		ids = xp.arange(config.n_vocab, dtype=np.uint8)

		prev_y = Variable(xp.zeros((n_batch, config.n_vocab), dtype=xp.int32))
		for t in xrange(limit):
			if sampling_y:
				distribution = self.decode_one_step(summary, prev_y, test=test, softmax=True)
				y = xp.random.choice(ids, 1, p=distribution.data)
			else:
				confidence = self.decode_one_step(summary, prev_y, test=test)
				y = xp.argmax(confidence.data, axis=1)
			y_seq.append(y)
			prev_y = Variable(y)

	@property
	def xp(self):
		return np if self.encoder_lstm.layer_0._cpu else cuda.cupy

	def reset_state(self):
		self.encoder_lstm.reset_state()

	def predict(self, word, gpu=True, test=True):
		xp = self.xp
		c0 = Variable(xp.asarray([word], dtype=np.int32))
		output = self(c0, test=test, softmax=False)
		ids = xp.argmax(output.data, axis=1)
		return ids

	def distribution(self, word, gpu=True, test=True):
		xp = self.xp
		c0 = Variable(xp.asarray([word], dtype=np.int32))
		output = self(c0, test=test, softmax=True)
		if gpu:
			output.to_cpu()
		return output.data

	def learn(self, seq_batch, gpu=True, test=False):
		self.encoder_lstm.reset_state()
		self.decoder_lstm.reset_state()
		xp = self.xp
		sum_loss = 0
		seq_batch = seq_batch.T
		for c0, c1 in zip(seq_batch[:-1], seq_batch[1:]):
			c0[c0 == -1] = 0
			c0 = Variable(xp.asanyarray(c0, dtype=np.int32))
			c1 = Variable(xp.asanyarray(c1, dtype=np.int32))
			output = self(c0, test=test, softmax=False)
			loss = F.softmax_cross_entropy(output, c1)
			sum_loss += loss
		self.optimizer_encoder_lstm.zero_grads()
		self.optimizer_decoder_fc.zero_grads()
		sum_loss.backward()
		self.optimizer_encoder_lstm.update()
		self.optimizer_decoder_fc.update()
		return sum_loss.data

	def load(self, dir=None, name="encoder_lstm"):
		if dir is None:
			raise Exception()
		filename = dir + "/%s_fc.model" % name
		if os.path.isfile(filename):
			serializers.load_hdf5(filename, self.decoder_fc)
			print filename, "loaded."
		filename = dir + "/%s_lstm.model" % name
		if os.path.isfile(filename):
			serializers.load_hdf5(filename, self.encoder_lstm)
			print filename, "loaded."
		filename = dir + "/%s_fc.optimizer" % name
		if os.path.isfile(filename):
			serializers.load_hdf5(filename, self.optimizer_decoder_fc)
			print filename, "loaded."
		filename = dir + "/%s_lstm.optimizer" % name
		if os.path.isfile(filename):
			serializers.load_hdf5(filename, self.optimizer_encoder_lstm)
			print filename, "loaded."

	def save(self, dir=None, name="encoder_lstm"):
		if dir is None:
			raise Exception()
		try:
			os.mkdir(dir)
		except:
			pass
		serializers.save_hdf5(dir + "/%s_fc.model" % name, self.decoder_fc)
		serializers.save_hdf5(dir + "/%s_lstm.model" % name, self.encoder_lstm)
		print "model saved."
		serializers.save_hdf5(dir + "/%s_fc.optimizer" % name, self.optimizer_decoder_fc)
		serializers.save_hdf5(dir + "/%s_lstm.optimizer" % name, self.optimizer_encoder_lstm)
		print "optimizer saved."


def build(n_vocab=0):
	config.check()
	wscale = 1.0

	lstm_attributes = {}
	lstm_units = zip(config.lstm_units[:-1], config.lstm_units[1:])

	for i, (n_in, n_out) in enumerate(lstm_units):
		lstm_attributes["layer_%i" % i] = L.LSTM(n_in, n_out)
		lstm_attributes["batchnorm_%i" % i] = L.BatchNormalization(n_out)
	lstm_attributes["embed_id"] = L.EmbedID(n_vocab, config.lstm_units[0])

	lstm = LSTM(**lstm_attributes)
	lstm.n_layers = len(lstm_units)
	lstm.apply_batchnorm = config.lstm_apply_batchnorm
	lstm.apply_batchnorm_to_input = config.lstm_apply_batchnorm_to_input
	lstm.apply_dropout = config.lstm_apply_dropout
	if config.use_gpu:
		lstm.to_gpu()

	fc_attributes = {}
	fc_units = zip(config.fc_units[:-1], config.fc_units[1:])
	fc_units += [(config.fc_units[-1], n_vocab)]

	for i, (n_in, n_out) in enumerate(fc_units):
		fc_attributes["layer_%i" % i] = L.Linear(n_in, n_out, wscale=wscale)
		fc_attributes["batchnorm_%i" % i] = L.BatchNormalization(n_out)

	fc = FullyConnectedNetwork(**fc_attributes)
	fc.n_hidden_layers = len(fc_units) - 1
	fc.activation_function = config.fc_activation_function
	fc.apply_batchnorm_to_input = config.fc_apply_batchnorm_to_input
	fc.apply_batchnorm_to_output = config.fc_apply_batchnorm_to_output
	fc.apply_batchnorm = config.fc_apply_batchnorm
	fc.apply_dropout = config.fc_apply_dropout
	if config.use_gpu:
		fc.to_gpu()

	return Model(lstm, fc)

class Append(function.Function):
	def check_type_forward(self, in_types):
		n_in = in_types.size()
		type_check.expect(n_in == 2)
		summary_type, prev_y_type = in_types

		type_check.expect(
			summary_type.dtype == np.float32,
			prev_y_type.dtype == np.float32,
			summary_type.ndim == 2,
			prev_y_type.ndim == 2,
		)

	def forward(self, inputs):
		xp = cuda.get_array_module(inputs[0])
		summary, prev_y = inputs
		n_batch = summary.shape[0]
		output = xp.empty((n_batch, summary.shape[1] + prev_y.shape[1]), dtype=xp.float32)
		output[:,:summary.shape[1]] = summary
		output[:,summary.shape[1]:] = prev_y
		return output,

	def backward(self, inputs, grad_outputs):
		summary, prev_y = inputs
		return grad_outputs[0][:,:summary.shape[1]], grad_outputs[0][:,summary.shape[1]:]

def append_variable(summary, prev_y):
	return Adder()(summary, prev_y)