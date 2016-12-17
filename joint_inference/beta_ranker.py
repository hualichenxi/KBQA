import sys
sys.path.insert(0, '../')
import tensorflow as tf
from relation_matcher import encoder
import numpy as np
activation_map = {
    "tanh": tf.nn.tanh,
    "sigmoid": tf.nn.sigmoid,
    "relu": tf.nn.relu
}

optimizer_map = {
    'sgd': tf.train.GradientDescentOptimizer,
    "adam": tf.train.AdamOptimizer
}


def fully_connected(input, hidden_layer_sizes, activations, reuse):
    initializer = tf.contrib.layers.xavier_initializer(uniform=True, seed=None, dtype=tf.float32)
    input_dim = input.get_shape()[1]
    layer_sizes = [input_dim] + hidden_layer_sizes
    activations = [""] + activations
    layers = [input]
    for i in range(1, len(layer_sizes)):
        with tf.variable_scope('fully-connected-%s' % i, reuse):
            w = tf.get_variable('w', [layer_sizes[i-1], layer_sizes[i]], initializer=initializer)
            b = tf.get_variable('b', [layer_sizes[i]], initializer=tf.constant_initializer(0))
            layers.append(activation_map[activations[i]](tf.add(tf.matmul(layers[-1], w), b)))
    return layers[-1]


class BetaRanker:

    def __init__(self, params):
        assert params['hidden_layer_sizes'][-1] == 1
        max_sentence_len = params['max_sentence_len']
        max_word_len = params['max_word_len']
        max_name_len = params['max_name_len']
        self.pattern_word_ids = [
            tf.placeholder(tf.int32, [None, max_sentence_len])
            for i in range(2)]
        self.sentence_lengths = [tf.placeholder(tf.int32, [None]) for i in range(2)]

        self.pattern_char_ids = [tf.placeholder(tf.int32, [None, max_sentence_len, max_word_len]) for i in range(2)]
        self.word_lengths = [tf.placeholder(tf.int32, [None, max_sentence_len]) for i in range(2)]
        self.relation_ids = [tf.placeholder(tf.int32, [None, 3]) for i in range(2)]
        self.mention_char_ids = [tf.placeholder(tf.int32, [None, max_name_len])
                                 for i in range(2)]
        self.topic_char_ids = [tf.placeholder(tf.int32, [None, max_name_len])
                               for i in range(2)]
        self.mention_lengths = [tf.placeholder(tf.int32, [None]) for i in range(2)]
        self.topic_lengths = [tf.placeholder(tf.int32, [None]) for i in range(2)]
        self.extras = [tf.placeholder(tf.float32, [None, len(params['extra_keys'])])
                       for i in range(2)]
        self.dropout_keep_prob = tf.placeholder(tf.float32, name='dropout_keep_prob')

        features = [[], []]
        with tf.device('/gpu:%s' % params.get('gpu', 1)):
            if params['relation_config']['encoder'] == 'ADD':
                with tf.variable_scope('semantic_layer', regularizer=tf.contrib.layers.l2_regularizer(params['embedding_l2_scale'])):
                    pattern_encoder = encoder.ADDEncoder(params['pattern_config'], 'pattern_add')
                    relation_encoder = encoder.ADDEncoder(params['relation_config'], 'relation_add')

                    patterns = [pattern_encoder.encode(self.pattern_word_ids[i], self.sentence_lengths[i], i == 1) for i in range(2)]
                    relations = [relation_encoder.encode(self.relation_ids[i], None, i==1) for i in range(2)]
                    patterns = [patterns[i] / tf.sqrt(tf.reduce_sum(patterns[i] ** 2, 1, keep_dims=True)) for i in range(2)]
                    relations = [relations[i] / tf.sqrt(tf.reduce_sum(relations[i] ** 2, 1, keep_dims=True)) for i in range(2)]
            # elif params['relation_config']['encoder'] == 'CNN':
            #     pattern_encoder = encoder.CNNEncoder(params['pattern_config'], 'pattern_cnn')
            #     # relation_encoder = encoder.CNNEncoder(params['relation_config'], 'relation_cnn')
            #     relation_encoder = encoder.ADDEncoder(params['relation_config'], 'relation_add')
            #     if 'char_dim' in params['question_config']:
            #         patterns = [pattern_encoder.encode(self.pattern_char_ids[i], i == 1) for i in range(2)]
            #     else:
            #         patterns = [pattern_encoder.encode(self.pattern_word_ids, i == 1) for i in range(2)]
            #     relations = [relation_encoder.encode(self.relation_ids[i], None, i == 1) for i in range(2)]
            # elif params['relation_config']['encoder'] == 'RNN':
            #     pattern_encoder = encoder.RNNEncoder(params['pattern_config'], 'pattern_rnn')
            #     relation_encoder = encoder.RNNEncoder(params['relation_config'], 'relation_rnn')
            #     patterns = [pattern_encoder.encode(self.pattern_word_ids, self.sentence_lengths, self.pattern_char_ids, self.word_lengths, i == 1) for i in range(2)]
            #     relations = [relation_encoder.encode(self.relation_ids, None, None, None, i == 1) for i in range(2)]
            else:
                raise ValueError('relation_encoder should be one of [CNN, ADD, RNN]')


            # Use char-based CNN or RNN to encode mention and topic name
            if 'topic_config' in params:
                if params['topic_config']['encoder'] == 'CNN':
                    char_encoder = encoder.CNNEncoder(params['topic_config'], 'char_cnn')
                    self.topics = [char_encoder.encode(self.topic_char_ids[i], i == 1) for i in range(2)]
                    self.mentions = [char_encoder.encode(self.mention_char_ids[i], True) for i in range(2)]
                elif params['topic_config']['encoder'] == 'RNN':
                    char_encoder = encoder.RNNEncoder(params['topic_config'], 'char_cnn')
                    self.topics = [char_encoder.encode(self.topic_char_ids[i], self.topic_lengths[i], None, None, i == 1, max_pool=False) for i in range(2)]
                    self.mentions = [char_encoder.encode(self.mention_char_ids[i], self.mention_lengths[i], None, None, True, max_pool=False) for i in range(2)]
                else:
                    raise ValueError('topic_encoder should be one of [CNN, RNN]')
                topics_drops = [tf.nn.dropout(self.topics[i], self.dropout_keep_prob) for i in range(2)]
                mentions_drops = [tf.nn.dropout(self.mentions[i], self.dropout_keep_prob) for i in range(2)]
                self.topic_mention_scores = [tf.expand_dims(self.cosine_sim(topics_drops[i], mentions_drops[i]), dim=1) for i in range(2)]
                for i in [0, 1]:
                    features[i].append(self.topic_mention_scores[i])
                    if params['topic_config']['use_repr']:
                        features[i].append(topics_drops[i])
                        features[i].append(mentions_drops[i])
            # Dropout
            pat_drops = [tf.nn.dropout(patterns[i], self.dropout_keep_prob) for i in range(2)]
            rel_drops = [tf.nn.dropout(relations[i], self.dropout_keep_prob) for i in range(2)]

            # Bilinear similarity
            dim = patterns[0].get_shape()[1]
            initializer = tf.contrib.layers.xavier_initializer(uniform=True, seed=None, dtype=tf.float32)
            with tf.variable_scope('bilinear_sim', regularizer=tf.contrib.layers.l2_regularizer(params['l2_scale'])):
                bi_m = tf.get_variable('bi_m', [dim, dim], initializer=initializer)
                self.bi_sims = [tf.reduce_sum(tf.mul(tf.matmul(pat_drops[i], bi_m), rel_drops[i]), 1, keep_dims=True) for i in range(2)]
            for i in [0, 1]:
                features[i].append(pat_drops[i])
                features[i].append(rel_drops[i])
                features[i].append(self.bi_sims[i])
                features[i].append(self.extras[i])

            # Concat features
            features = [tf.concat(1, features[i]) for i in range(2)]

            # Fully connected layer
            with tf.variable_scope('hidden_layer', regularizer=tf.contrib.layers.l2_regularizer(params['l2_scale'])):
                self.scores = [tf.squeeze(
                    fully_connected(features[i], params['hidden_layer_sizes'], params['activations'], i == 1),
                    squeeze_dims=[1]) for i in range(2)]

            reg_vars = tf.get_collection(tf.GraphKeys.REGULARIZATION_LOSSES)
            if len(reg_vars) == 0:
                self.reg_loss = tf.constant(0.)
            else:
                self.reg_loss = tf.add_n(reg_vars)

            # scores = [pos_score, neg_score]
            self.margin_loss = tf.reduce_mean(tf.maximum(0., self.scores[1] + params['margin'] - self.scores[0]))
            self.loss = self.reg_loss + self.margin_loss

            # tvars = tf.trainable_variables()
            # max_grad_norm = 5
            # self.grads, _ = tf.clip_by_global_norm(tf.gradients(self.loss, tvars), max_grad_norm)
            # self.train_op = tf.train.AdadeltaOptimizer(params['lr']).minimize(self.loss)
            # self.train_op = optimizer.apply_gradients(zip(self.grads, tvars))

            self.train_op = optimizer_map[params['optimizer']](params['lr']).minimize(self.loss)

        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True
        config.allow_soft_placement = True
        config.log_device_placement = False
        self.session = tf.Session(config=config)
        self.saver = tf.train.Saver(tf.all_variables(), max_to_keep=1)
        if params['load_path']:
            self.saver.restore(self.session, params['load_path'])
        else:
            self.session.run(tf.initialize_all_variables())

        self.params = params

    @staticmethod
    def cosine_sim(u, v):
        dot = tf.reduce_sum(tf.mul(u, v), 1)
        sqrt_u = tf.sqrt(tf.reduce_sum(u ** 2, 1))
        sqrt_v = tf.sqrt(tf.reduce_sum(v ** 2, 1))
        epsilon = 1e-5
        cosine = dot / (sqrt_u * sqrt_v)
        # cosine = dot / (tf.maximum(sqrt_u * sqrt_v, epsilon))
        # cosine = tf.maximum(dot / (tf.maximum(sqrt_u * sqrt_v, epsilon)), epsilon)
        return cosine

    def fit(self,
            pattern_word_ids,
            sentence_lengths,
            pattern_char_ids,
            word_lengths,
            relation_ids,
            mention_char_ids,
            topic_char_ids,
            mention_lengths,
            topic_lengths,
            extras,
            dropout_keep_prob):
        feed_dict = dict()

        if 'word_dim' in self.params['pattern_config']:
            for i in [0, 1]:
                feed_dict[self.pattern_word_ids[i]] = pattern_word_ids[i]
                feed_dict[self.sentence_lengths[i]] = sentence_lengths[i]

        if 'char_dim' in self.params['pattern_config']:
            for i in [0, 1]:
                feed_dict[self.pattern_char_ids[i]] = pattern_char_ids[i]
                feed_dict[self.word_lengths[i]] = word_lengths[i]

        for i in [0, 1]:
            feed_dict[self.relation_ids[i]] = relation_ids[i]
            if "topic_config" in self.params:
                feed_dict[self.mention_char_ids[i]] = mention_char_ids[i]
                feed_dict[self.topic_char_ids[i]] = topic_char_ids[i]
                feed_dict[self.mention_lengths[i]] = mention_lengths[i]
                feed_dict[self.topic_lengths[i]] = topic_lengths[i]
            feed_dict[self.extras[i]] = extras[i]

        feed_dict[self.dropout_keep_prob] = dropout_keep_prob

        _, loss, margin_loss, reg_loss = self.session.run([self.train_op, self.loss, self.margin_loss, self.reg_loss], feed_dict)
        return loss, margin_loss, reg_loss

    def predict(self,
                pattern_word_id,
                sentence_length,
                pattern_char_id,
                word_length,
                relation_id,
                mention_char_id,
                topic_char_id,
                mention_lengths,
                topic_lengths,
                extras):
        feed_dict = dict()
        if 'word_dim' in self.params['pattern_config']:

            feed_dict[self.pattern_word_ids[0]] = pattern_word_id
            feed_dict[self.sentence_lengths[0]] = sentence_length

        if 'char_dim' in self.params['pattern_config']:
            feed_dict[self.pattern_char_ids[0]] = pattern_char_id
            feed_dict[self.word_lengths[0]] = word_length
        feed_dict[self.dropout_keep_prob] = 1
        feed_dict[self.relation_ids[0]] = relation_id
        if "topic_config" in self.params:
            feed_dict[self.mention_char_ids[0]] = mention_char_id
            feed_dict[self.topic_char_ids[0]] = topic_char_id
            feed_dict[self.mention_lengths[0]] = mention_lengths
            feed_dict[self.topic_lengths[0]] = topic_lengths
        feed_dict[self.extras[0]] = extras
        return self.session.run(self.scores[0], feed_dict)
    #
    # def get_question_repr(self,
    #                       question_word_ids,
    #                       question_sentence_lengths,
    #                       question_char_ids,
    #                       question_char_lengths):
    #     feed_dict = dict()
    #     if 'word_dim' in self.params['question_config']:
    #         feed_dict[self.q_word_ids] = question_word_ids
    #         feed_dict[self.q_sentence_lengths] = question_sentence_lengths
    #
    #     if 'char_dim' in self.params['question_config']:
    #         feed_dict[self.q_char_ids] = question_char_ids
    #         feed_dict[self.q_word_lengths] = question_char_lengths
    #     feed_dict[self.dropout_keep_prob] = 1
    #     return self.session.run(self.question_drop, feed_dict)
    #
    # def get_relation_repr(self,
    #                       relation_ids):
    #     feed_dict = dict()
    #     feed_dict[self.pos_relation_ids] = relation_ids
    #     feed_dict[self.dropout_keep_prob] = 1
    #     return self.session.run(self.pos_relation_drop, feed_dict)

    def save(self, save_path):
        return self.saver.save(self.session, save_path)

    def get_all_variables(self):
        variable_names = [v.name for v in tf.all_variables()]
        variable_values = self.session.run(tf.all_variables())
        variable = dict()
        for i in xrange(len(variable_names)):
            variable[variable_names[i]] = variable_values[i].tolist()
        return variable