#!/usr/bin/env python
# -*- coding: UTF-8 -*-
# File: cifar10-resnet.py
# Author: Yuxin Wu <ppwwyyxxc@gmail.com>

import numpy as np
import argparse
import os
import math
import matplotlib as mpl
mpl.use('Agg')
import matplotlib.pyplot as plt

import sys
sys.path.append('../../../tensorpack')
from tensorpack import *
from tensorpack.tfutils.symbolic_functions import *
from tensorpack.tfutils.summary import *
from tensorpack.tfutils.gradproc import SummaryGradient

from EpsilonResNetBase import *

import tensorflow as tf
from tensorflow.contrib.layers import variance_scaling_initializer
"""
CIFAR10 ResNet example. See:
Deep Residual Learning for Image Recognition, arxiv:1512.03385
This implementation uses the variants proposed in:
Identity Mappings in Deep Residual Networks, arxiv:1603.05027

I can reproduce the results on 2 TitanX for
n=5, about 7.1% val error after 67k steps (20.4 step/s)
n=18, about 5.95% val error after 80k steps (5.6 step/s, not converged)
n=30: a 182-layer network, about 5.6% val error after 51k steps (3.4 step/s)
This model uses the whole training set instead of a train-val split.

To train:
    ./cifar10-resnet.py --gpu 0,1
"""

BATCH_SIZE = 128
EPSILON = 2.5
NUM_UNITS = None 
class Model(ModelDesc):

    def __init__(self, n):
        super(Model, self).__init__()
        self.n = n

    def _get_inputs(self):
        return [InputDesc(tf.float32, [None, 32, 32, 3], 'input'),
                InputDesc(tf.int32, [None], 'label')]

    def _build_graph(self, inputs):
        image, label = inputs
        image = image / 128.0
        assert tf.test.is_gpu_available()
        image = tf.transpose(image, [0, 3, 1, 2])
        
        all_cnt = tf.constant(self.n * 3+2, tf.float32, name="all_cnt")
        preds = []

        epsilon = get_scalar_var('epsilon', EPSILON, summary=True)

        def residual_convs(l, first,out_channel,stride1):
            b1 = l if first else BNReLU(l)
            c1 = Conv2D('conv1', b1, out_channel, stride=stride1, nl=BNReLU)
            c2 = Conv2D('conv2', c1, out_channel)
            return c2
                
        def residual(name, l, increase_dim=False, first=False):
            shape = l.get_shape().as_list()
            in_channel = shape[1]

            if increase_dim:
                out_channel = in_channel * 2
                stride1 = 2
                short_cut = AvgPooling('pool', l, 2)
                short_cut = tf.pad(short_cut, [[0, 0], [in_channel // 2, in_channel // 2], [0, 0], [0, 0]])
            else:
                out_channel = in_channel
                stride1 = 1
                short_cut = l

            with tf.variable_scope(name) as scope:
                # identity_w: save the result of sparsity promoting function
                identity_w = tf.get_variable('identity', dtype=tf.float32,
                    initializer=tf.constant(1.0), trainable = False)
                ctx = get_current_tower_context()
                if ctx.is_training:
                    l = residual_convs(l,first,out_channel,stride1)
                    w = strict_identity(l, EPSILON)
                    # update identity_w in training
                    identity_w = identity_w.assign(w)
                    # apply strict identity
                    l = identity_w * l + short_cut
                else:
                    # utilize identity_w directly in test
                    l = tf.where(tf.equal(identity_w, 0.0),short_cut,
                            residual_convs(l,first,out_channel,stride1) + short_cut)
                # monitor is_discarded
                is_discarded = tf.where(
                        tf.equal(identity_w,0.0), 1.0, 0.0, 'is_discarded')
                add_moving_summary(is_discarded)
                preds.append(is_discarded)
            return l
            
        
        side_output_cost = []
        with argscope([Conv2D, AvgPooling, BatchNorm, GlobalAvgPooling], data_format='NCHW'), \
                argscope(Conv2D, nl=tf.identity, use_bias=False, kernel_shape=3,
                         W_init=variance_scaling_initializer(mode='FAN_OUT')):
            l = Conv2D('conv0', image, 16, nl=BNReLU)
            l = residual('res1.0', l, first=True)
            for k in range(1, self.n):
                l= residual('res1.{}'.format(k), l)
            # 32,c=16
            
            l = residual('res2.0', l, increase_dim=True)
            for k in range(1, self.n):
                l = residual('res2.{}'.format(k), l)
                if(k == self.n/2):
                    side_output_cost.append(side_output('res2.{}'.format(k), l, label, 10))
            # 16,c=32
            l = residual('res3.0', l, increase_dim=True)
            for k in range(1, self.n):
                l = residual('res3.' + str(k), l)
            l = BNReLU('bnlast', l)
            # 8,c=64
            l = GlobalAvgPooling('gap', l)
        #for cifar10 
        logits = FullyConnected('linear', l, out_dim=10, nl=tf.identity)
        cost = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=logits, labels=label)
        cost = tf.reduce_mean(cost, name='cross_entropy_loss')
        wrong = prediction_incorrect(logits, label)
        # monitor training error
        add_moving_summary(tf.reduce_mean(wrong, name='train_error'))
        
        discarded_cnt = tf.add_n(preds, name="discarded_cnt")
        discarded_ratio = tf.divide(
                discarded_cnt, all_cnt, name="discarded_ratio")
        add_moving_summary(discarded_cnt, discarded_ratio) 
        
        # weight decay on all W of fc layers
        wd_w = tf.train.exponential_decay(0.0002, get_global_step_var(),
                                          480000, 0.2, True)
        wd_cost = tf.multiply(wd_w, regularize_cost('.*/W', tf.nn.l2_loss), name='wd_cost')
        
        add_moving_summary(cost, wd_cost)

        add_param_summary(('.*/W', ['histogram']))   # monitor W
        side_loss_w = [0.1]
        side_output_cost = [tf.multiply(side_loss_w[i], side_output_cost[i])\
                for i in range(len(side_output_cost))]
        loss = side_output_cost + [cost, wd_cost] 
        self.cost = tf.add_n(loss, name='cost')
        
    def _get_optimizer(self):
        lr = get_scalar_var('learning_rate', 0.1, summary=True)
        opt = tf.train.MomentumOptimizer(lr, 0.90)
        return opt


def get_data(train_or_test):
    isTrain = train_or_test == 'train'
    ds = dataset.Cifar10(train_or_test)
    pp_mean = ds.get_per_pixel_mean()
    if isTrain:
        augmentors = [
            imgaug.CenterPaste((40, 40)),
            imgaug.RandomCrop((32, 32)),
            imgaug.Flip(horiz=True),
            imgaug.MapImage(lambda x: x - pp_mean),
        ]
    else:
        augmentors = [
            imgaug.MapImage(lambda x: x - pp_mean)
        ]
    ds = AugmentImageComponent(ds, augmentors)
    ds = BatchData(ds, BATCH_SIZE, remainder=not isTrain)
    if isTrain:
        ds = PrefetchData(ds, 3, 2)
    return ds

def get_config(out_dir):
    print("outdir: %s"%out_dir)
    logger.auto_set_dir(outdir=out_dir)
    dataset_train = get_data('train')
    dataset_test = get_data('test')
    MAX_EPOCH = 1000
    side_name = "side_output/res2.{}".format(NUM_UNITS/2)
    return TrainConfig(
        dataflow=dataset_train,
        callbacks=[
            ModelSaver(),
            InferenceRunner(dataset_test, 
                [ScalarStats('cost'),ClassificationError(),
                    ClassificationError(
                        '{}/incorrect_vector'.format(side_name),
                        '{}/val_error'.format(side_name))]),
            LearningRateSetter('learning_rate','discarded_cnt',
                [(0, 0.1), (82, 0.01), (123, 0.001), (300,0.0002)],
                [(0, 0.1), (41, 0.01), (61, 0.001), (150,0.0002)],
                1,1),
        ],
        model=Model(n=NUM_UNITS),
        max_epoch = MAX_EPOCH,
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--gpu', help='comma separated list of GPU(s) to use.')
    parser.add_argument('-n', '--num_units',
                        help='number of units in each stage',
                        type=int, default=18)
    parser.add_argument('--load', help='load model')
    parser.add_argument('-e', '--epsilon', help='set epsilon')
    parser.add_argument('-o', '--output', help='output')
    
    parser.set_defaults(feature=True)

    args = parser.parse_args()
    NUM_UNITS = args.num_units
    if args.gpu:
        os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu
    if args.epsilon:
        EPSILON = float(args.epsilon)
    out_dir = ""
    if args.output:
        out_dir = "." + args.output
    print('epsilon = %f' % EPSILON)
    config = get_config(out_dir)
    if args.load:
        config.session_init = SaverRestore(args.load)
    if args.gpu:
        config.nr_tower = len(args.gpu.split(','))
    SyncMultiGPUTrainer(config).train()
    #SimpleTrainer(config).train()
