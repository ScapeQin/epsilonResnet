#!/usr/bin/env python
# -*- coding: UTF-8 -*-
# File: imagenet-resnet.py

import cv2
import sys
import argparse
import numpy as np
import os
import multiprocessing

sys.path.append('../../')

import tensorflow as tf
from tensorflow.contrib.layers import variance_scaling_initializer
from tensorpack import *
from tensorpack.utils.stats import RatioCounter
from tensorpack.tfutils.symbolic_functions import *
from tensorpack.tfutils.summary import *

TOTAL_BATCH_SIZE = 256
INPUT_SHAPE = 224
DEPTH = None
SIDE_POSITION = None
EPSILON = 2.0

class Model(ModelDesc):
    def __init__(self, data_format='NCHW'):
        if data_format == 'NCHW':
            assert tf.test.is_gpu_available()
        self.data_format = data_format

    def _get_inputs(self):
        # uint8 instead of float32 is used as input type to reduce copy overhead.
        # It might hurt the performance a liiiitle bit.
        # The pretrained models were trained with float32.
        return [InputDesc(tf.uint8, [None, INPUT_SHAPE, INPUT_SHAPE, 3], 'input'),
                InputDesc(tf.int32, [None], 'label')]

    def _build_graph(self, inputs):
        image, label = inputs
        image = tf.cast(image, tf.float32) * (1.0 / 255)

        # Wrong mean/std are used for compatibility with pre-trained models.
        # Should actually add a RGB-BGR conversion here.
        image_mean = tf.constant([0.485, 0.456, 0.406], dtype=tf.float32)
        image_std = tf.constant([0.229, 0.224, 0.225], dtype=tf.float32)
        image = (image - image_mean) / image_std
        if self.data_format == 'NCHW':
            image = tf.transpose(image, [0, 3, 1, 2])
        
        # collect the state for each sparsity promoting function
        preds = []
        # collect outputs of side suprvision
        side_output_cost = []
        epsilon = get_scalar_var('epsilon', EPSILON, summary=True)

        def shortcut(l, n_in, n_out, stride):
            if n_in != n_out:
                return Conv2D('convshortcut', l, n_out, 1, stride=stride)
            else:
                return l
        
        def basicblock(l, ch_out, stride, preact):
            return residual_convs(l, ch_out, stride, True)

        def bottleneck(l, ch_out, stride, preact):
            return residual_convs(l, ch_out, stride, False)
        
        def residual_convs(l, ch_out, stride, is_basicblock):
            if is_basicblock:
                l = Conv2D('conv1', l, ch_out, 3, stride=stride, nl=BNReLU)
                l = Conv2D('conv2', l, ch_out, 3)
            else:
                l = Conv2D('conv1', l, ch_out, 1, nl=BNReLU)
                l = Conv2D('conv2', l, ch_out, 3, stride=stride, nl=BNReLU)
                l = Conv2D('conv3', l, ch_out * 4, 1)
            return l    

        def residual(l, ch_out, stride, preact, is_basicblock):
            ch_in = l.get_shape().as_list()[1]
            if preact == 'both_preact':
                l = BNReLU('preact', l)
                input = l
            elif preact != 'no_preact':
                input = l
                l = BNReLU('preact', l)
            else:
                input = l
            #identity_w: save the result of sparsity promoting function
            identity_w = tf.get_variable('identity', dtype=tf.float32,
                initializer=tf.constant(1.0), trainable = False)
            ctx = get_current_tower_context()
            short_cut = shortcut(input, ch_in, ch_out * 4, stride)
            if ctx.is_training:
                l = residual_convs(l, ch_out, stride, is_basicblock)
                w = strict_identity(l, EPSILON)
                # update identity_w in training
                identity_w = identity_w.assign(w)
                l = identity_w * l + short_cut
            else:
                # utilize identity_w directly in test
                l = tf.where(tf.equal(identity_w, 0.0),short_cut,
                    residual_convs(l, ch_out, stride, is_basicblock) + short_cut)
            # monitor is_discarded
            is_discarded = tf.where(
                tf.equal(identity_w,0.0), 1.0, 0.0, 'is_discarded')
            add_moving_summary(is_discarded)
            preds.append(is_discarded)
            return l

        cfg = {
            18: ([2, 2, 2, 2], basicblock),
            34: ([3, 4, 6, 3], basicblock),
            50: ([3, 4, 6, 3], bottleneck),
            101: ([3, 4, 23, 3], bottleneck),
            152: ([3, 8, 36, 3], bottleneck)
        }
        defs, block_func = cfg[DEPTH]
        # SIDE_POSITION: side supervision is placed after SIDE_POSITION-th block in group2
        SIDE_POSITION = sum(defs)/2 - sum(defs[:2]) - 1

        def layer(l, layername, block_func, features, count, stride, first=False):
            with tf.variable_scope(layername):
                with tf.variable_scope('block0'):
                    l = block_func(l, features, stride,
                                   'no_preact' if first else 'both_preact')
                # add side supervision at the middle of the network
                if layername == 'group2' && SIDE_POSITION == 0
                    side_output_cost.append(side_output('block0', l, 1000))
                for i in range(1, count):
                    with tf.variable_scope('block{}'.format(i)):
                        l = block_func(l, features, 1, 'default')
                        # add side supervision at the middle of the network
                        if layername == 'group2' && i == SIDE_POSITION:
                            side_output_cost.append(
                                side_output('block{}'.format(i), l, 1000))
                return l


        with argscope(Conv2D, nl=tf.identity, use_bias=False,
                      W_init=variance_scaling_initializer(mode='FAN_OUT')), \
                argscope([Conv2D, MaxPooling, GlobalAvgPooling, BatchNorm], data_format=self.data_format):
            logits = (LinearWrap(image)
                      .Conv2D('conv0', 64, 7, stride=2, nl=BNReLU)
                      .MaxPooling('pool0', shape=3, stride=2, padding='SAME')
                      .apply(layer, 'group0', block_func, 64, defs[0], 1, first=True)
                      .apply(layer, 'group1', block_func, 128, defs[1], 2)
                      .apply(layer, 'group2', block_func, 256, defs[2], 2)
                      .apply(layer, 'group3', block_func, 512, defs[3], 2)
                      .BNReLU('bnlast')
                      .GlobalAvgPooling('gap')
                      .FullyConnected('linear', 1000, nl=tf.identity)())

        loss = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=logits, labels=label)
        loss = tf.reduce_mean(loss, name='xentropy-loss')

        wrong = prediction_incorrect(logits, label, 1, name='wrong-top1')
        add_moving_summary(tf.reduce_mean(wrong, name='train-error-top1'))

        wrong = prediction_incorrect(logits, label, 5, name='wrong-top5')
        add_moving_summary(tf.reduce_mean(wrong, name='train-error-top5'))

        wd_cost = regularize_cost('.*/W', l2_regularizer(1e-4), name='l2_regularize_loss')
        add_moving_summary(loss, wd_cost)
       
        # take side loss into the final loss
        side_loss_w = [0.1]
        side_output_cost = [tf.multiply(side_loss_w[i], side_output_cost[i])\
            for i in range(len(side_output_cost))]
        self.cost = tf.add_n([loss, wd_cost, side_output_cost], name='cost')

    def _get_optimizer(self):
        lr = get_scalar_var('learning_rate', 0.1, summary=True)
        return tf.train.MomentumOptimizer(lr, 0.9, use_nesterov=True)


def get_data(train_or_test, fake=False):
    if fake:
        return FakeData([[64, 224, 224, 3], [64]], 1000, random=False, dtype='uint8')
    isTrain = train_or_test == 'train'

    datadir = args.data
    ds = dataset.ILSVRC12(datadir, train_or_test,
                          shuffle=True if isTrain else False, dir_structure='original')
    if isTrain:
        class Resize(imgaug.ImageAugmentor):
            """
            crop 8%~100% of the original image
            See `Going Deeper with Convolutions` by Google.
            """
            def _augment(self, img, _):
                h, w = img.shape[:2]
                area = h * w
                for _ in range(10):
                    targetArea = self.rng.uniform(0.08, 1.0) * area
                    aspectR = self.rng.uniform(0.75, 1.333)
                    ww = int(np.sqrt(targetArea * aspectR))
                    hh = int(np.sqrt(targetArea / aspectR))
                    if self.rng.uniform() < 0.5:
                        ww, hh = hh, ww
                    if hh <= h and ww <= w:
                        x1 = 0 if w == ww else self.rng.randint(0, w - ww)
                        y1 = 0 if h == hh else self.rng.randint(0, h - hh)
                        out = img[y1:y1 + hh, x1:x1 + ww]
                        out = cv2.resize(out, (224, 224), interpolation=cv2.INTER_CUBIC)
                        return out
                out = cv2.resize(img, (224, 224), interpolation=cv2.INTER_CUBIC)
                return out

        augmentors = [
            Resize(),
            imgaug.RandomOrderAug(
                [imgaug.Brightness(30, clip=False),
                 imgaug.Contrast((0.8, 1.2), clip=False),
                 imgaug.Saturation(0.4),
                 # rgb-bgr conversion
                 imgaug.Lighting(0.1,
                                 eigval=[0.2175, 0.0188, 0.0045][::-1],
                                 eigvec=np.array(
                                     [[-0.5675, 0.7192, 0.4009],
                                      [-0.5808, -0.0045, -0.8140],
                                      [-0.5836, -0.6948, 0.4203]],
                                     dtype='float32')[::-1, ::-1]
                                 )]),
            imgaug.Clip(),
            imgaug.Flip(horiz=True),
            imgaug.ToUint8()
        ]
    else:
        augmentors = [
            imgaug.ResizeShortestEdge(256),
            imgaug.CenterCrop((224, 224)),
            imgaug.ToUint8()
        ]
    ds = AugmentImageComponent(ds, augmentors, copy=False)
    if isTrain:
        ds = PrefetchDataZMQ(ds, min(20, multiprocessing.cpu_count()))
    ds = BatchData(ds, BATCH_SIZE, remainder=not isTrain)
    return ds


def get_config(fake=False, data_format='NCHW'):
    dataset_train = get_data('train', fake=fake)
    dataset_val = get_data('val', fake=fake)
    
    side_name = 'group2/side_output/block{}'.format(SIDE_POSITION)
    return TrainConfig(
        model=Model(data_format=data_format),
        dataflow=dataset_train,
        callbacks=[
            ModelSaver(),
            InferenceRunner(dataset_val, [
                # add callback for side supervision loss
                ClassificationError('{}/incorrect_vector'.format(side_name),
                    '{}/val_error'.format(side_name)),
                ClassificationError('wrong-top1', 'val-error-top1'),
                ClassificationError('wrong-top5', 'val-error-top5')]),
            ScheduledHyperParamSetter('learning_rate',
                                      [(30, 1e-2), (60, 1e-3), (85, 1e-4), (95, 1e-5)]),
            HumanHyperParamSetter('learning_rate'),
        ],
        steps_per_epoch=5000,
        max_epoch=110,
    )


def eval_on_ILSVRC12(model_file, data_dir):
    ds = get_data('val')
    pred_config = PredictConfig(
        model=Model(),
        session_init=get_model_loader(model_file),
        input_names=['input', 'label'],
        output_names=['wrong-top1', 'wrong-top5']
    )
    pred = SimpleDatasetPredictor(pred_config, ds)
    acc1, acc5 = RatioCounter(), RatioCounter()
    for o in pred.get_result():
        batch_size = o[0].shape[0]
        acc1.feed(o[0].sum(), batch_size)
        acc5.feed(o[1].sum(), batch_size)
    print("Top1 Error: {}".format(acc1.ratio))
    print("Top5 Error: {}".format(acc5.ratio))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--gpu', help='comma separated list of GPU(s) to use.', required=True)
    parser.add_argument('--data', help='ILSVRC dataset dir')
    parser.add_argument('--load', help='load model')
    parser.add_argument('--fake', help='use fakedata to test or benchmark this model', action='store_true')
    parser.add_argument('--data_format', help='specify NCHW or NHWC',
                        type=str, default='NCHW')
    parser.add_argument('-d', '--depth', help='resnet depth',
                        type=int, default=18, choices=[18, 34, 50, 101,152])
    parser.add_argument('--eval', action='store_true')
    parser.add_argument('-o', '--output', help='output')
    parser.add_argument('-e', '--epsilon', help='epsilon', 
                        type=float, default='2.0')
    args = parser.parse_args()

    DEPTH = args.depth
    os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu

    if args.eval:
        BATCH_SIZE = 128    # something that can run on one gpu
        eval_on_ILSVRC12(args.load, args.data)
        sys.exit()

    NR_GPU = len(args.gpu.split(','))
    BATCH_SIZE = TOTAL_BATCH_SIZE // NR_GPU

    if args.output:
        logger.auto_set_dir("."+args.output)
    else:
        logger.auto_set_dir()
    config = get_config(fake=args.fake, data_format=args.data_format)
    if args.load:
        config.session_init = SaverRestore(args.load)
    config.nr_tower = NR_GPU
    SyncMultiGPUTrainer(config).train()
