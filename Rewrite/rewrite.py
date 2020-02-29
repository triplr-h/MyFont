# -*- coding: utf-8 -*-

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import shutil
import argparse
import glob
import tensorflow as tf
import numpy as np
import imageio
from dataset import read_font_data, FontDataManager
from utils import render_fonts_image

FLAGS = None


def conv2d_block(x, shape, strides, padding, scope='conv2d'):
    """
    2D convolution block.
    """
    with tf.name_scope(scope):
        if not strides:
            strides = [1, 1, 1, 1]
        out_filters = shape[-1]
        W = tf.Variable(tf.truncated_normal(shape, stddev=0.01),
                        name="W")
        b = tf.Variable(tf.constant(0.1, shape=[out_filters]),
                        name="b")
        Wconv_plus_b = tf.nn.conv2d(x, W, strides, padding) + b
    return Wconv_plus_b


def batch_norm(x, phase_train, scope='bn'):
    """
    Batch normalization on convolutional maps.
    Borrowed and modified from: https://goo.gl/ckZxs8
    answered by user http://stackoverflow.com/users/3632556/bgshi
    """
    with tf.name_scope(scope):
        out_filters = x.get_shape()[-1]
        beta = tf.Variable(tf.constant(0.0, shape=[out_filters]),
                           name='beta', trainable=True)
        gamma = tf.Variable(tf.constant(1.0, shape=[out_filters]),
                            name='gamma', trainable=True)
        batch_mean, batch_var = tf.nn.moments(x, [0, 1, 2], name='moments')
        ema = tf.train.ExponentialMovingAverage(decay=0.9)

        def mean_var_with_update():
            ema_apply_op = ema.apply([batch_mean, batch_var])
            with tf.control_dependencies([ema_apply_op]):
                return tf.identity(batch_mean), tf.identity(batch_var)

        mean, var = tf.cond(phase_train,
                            mean_var_with_update,
                            lambda: (ema.average(batch_mean), ema.average(batch_var)))
        normed = tf.nn.batch_normalization(x, mean, var, beta, gamma, 1e-3)
    return normed


def leaky_relu(x, alpha):
    # TODO: is this memory efficient?
    return tf.maximum(x, x * alpha)


def block(x, shape, phase_train, strides=None, padding='SAME', scope='_block'):
    """
    Build block of the network. A three tier stacked subnet:
    conv2d -> batch_norm -> relu
    """
    with tf.name_scope(scope):
        conv = conv2d_block(x, shape, strides, padding)
        conv_bn = batch_norm(conv, phase_train)
        if FLAGS.alpha < 0:
            _relu = tf.nn.relu(conv_bn)
        else:
            _relu = leaky_relu(conv_bn, FLAGS.alpha)
    return _relu


def block_group(x, size, in_filters, out_filters, layers, phase_train, strides=None, scope="block_group"):
    with tf.name_scope(scope):
        # first layer has different input_filters
        conv1 = block(x, [size, size, in_filters, out_filters], phase_train,
                      strides, scope="conv1_%dx%d" % (out_filters, out_filters))
        cur_conv = conv1
        for i in range(layers - 1):
            next_conv = block(cur_conv, [size, size, out_filters, out_filters], phase_train,
                              strides, scope="conv%d_%dx%d" % (i + 2, out_filters, out_filters))
            cur_conv = next_conv
        return cur_conv


def max_pool_2x2(x, scope="max_pool_2x2"):
    with tf.name_scope(scope):
        return tf.nn.max_pool(x, ksize=[1, 2, 2, 1],
                              strides=[1, 2, 2, 1], padding='SAME')


def total_variation_loss(x, side):
    """
    Total variation loss for regularization of image smoothness
    """
    loss = tf.nn.l2_loss(x[:, 1:, :, :] - x[:, :side - 1, :, :]) / side + \
           tf.nn.l2_loss(x[:, :, 1:, :] - x[:, :, :side - 1, :]) / side
    return loss


def render_frame(x, frame_dir, step, img_per_row=10):
    frame_path = os.path.join(frame_dir, "step_%04d.png" % step)
    return render_fonts_image(x, frame_path, img_per_row)


def compile_frames_to_gif(frame_dir, gif_file):
    frames = sorted(glob.glob(os.path.join(frame_dir, "*.png")))
    images = [imageio.imread(f) for f in frames]
    imageio.mimsave(gif_file, images, duration=0.1)
    return gif_file


def main(_):
    side = 80
    batch_size = 16
    if FLAGS.model == 'small':
        print("small model is chosen, shrink number of layers to 2")
        layers = 2
    elif FLAGS.model == 'big':
        print("big model is chosen, increase number of layers to 4")
        layers = 4
    else:
        layers = 3

    learning_rate = tf.placeholder(tf.float32, name="learning_rate")
    phase_train = tf.placeholder(tf.bool, name='phase_train')
    keep_prob = tf.placeholder(tf.float32, name="keep_prob")
    default_gif_name = "transition.gif"

    # Create the model
    with tf.name_scope("input"):
        x = tf.placeholder(tf.float32, [None, 160, 160], name='x')
        y = tf.placeholder(tf.float32, [None, 80, 80], name='y')
        x_image = tf.reshape(x, shape=(-1, 160, 160, 1))
        y_image = tf.reshape(y, shape=(-1, 80, 80, 1))

    # block layers
    conv_64x64 = block_group(x_image, size=64, in_filters=1, out_filters=8,
                             layers=2, phase_train=phase_train, scope="conv_64_group")
    conv_32x32 = block_group(conv_64x64, size=32, in_filters=8, out_filters=32,
                             layers=layers, phase_train=phase_train, scope="conv_32_group")
    conv_16x16 = block_group(conv_32x32, size=16, in_filters=32, out_filters=64,
                             layers=layers, phase_train=phase_train, scope="conv_16_group")
    conv_7x7 = block_group(conv_16x16, size=7, in_filters=64, out_filters=128,
                           layers=layers, phase_train=phase_train, scope="conv_16_group")
    with tf.name_scope("conv_3_group"):
        conv_3x3_1 = block(conv_7x7, [3, 3, 128, 128], phase_train, scope="conv_3x3_1")
        conv_3x3_2 = block(conv_3x3_1, [3, 3, 128, 1], phase_train, scope="conv_3x3_2")

    # using max pool for downsampling
    pooled = max_pool_2x2(conv_3x3_2)

    with tf.name_scope("normalization"):
        dropped = tf.nn.dropout(pooled, keep_prob=keep_prob)
        # sigmoid is used to ensure value range in between (0, 1)
        y_hat_image = tf.sigmoid(dropped)

    with tf.name_scope("train"):
        with tf.name_scope("losses"):
            # MAE is used instead of MSE because it yield sharper
            # output images in practice
            pixel_abs_loss = tf.reduce_mean(tf.abs(y_image - y_hat_image))
            tv_loss = FLAGS.tv * total_variation_loss(y_hat_image, side)
            combined_loss = pixel_abs_loss + tv_loss
        train_step = tf.train.RMSPropOptimizer(learning_rate).minimize(combined_loss)

    with tf.name_scope("convert_bitmaps"):
        convert_bitmap = tf.reshape(y_hat_image, shape=[-1, 80, 80])

    tf.scalar_summary('pixel_abs_loss', pixel_abs_loss)
    tf.scalar_summary('combined_loss', combined_loss)
    tf.scalar_summary('tv_loss', tv_loss)
    merged = tf.merge_all_summaries()

    sess = tf.InteractiveSession()
    if FLAGS.mode == 'train':
        # in case train
        source_font = FLAGS.source_font
        target_font = FLAGS.target_font
        num_examples = FLAGS.num_examples
        num_validation = FLAGS.num_validations
        split = num_examples - num_validation
        train_keep_prob = FLAGS.keep_prob
        num_iter = FLAGS.iter
        frame_dir = FLAGS.frame_dir
        checkpoint_steps = FLAGS.ckpt_steps
        num_checkpoints = FLAGS.num_ckpt
        checkpoints_dir = FLAGS.ckpt_dir

        dataset = FontDataManager(source_font, target_font, num_examples, split)
        saver = tf.train.Saver(max_to_keep=num_checkpoints)

        train_writer = tf.train.SummaryWriter(os.path.join(FLAGS.summary_dir, 'train'),
                                              sess.graph)
        validation_writer = tf.train.SummaryWriter(os.path.join(FLAGS.summary_dir, 'validation'))
        sess.run(tf.initialize_all_variables())
        if FLAGS.capture_frame:
            print("frame capture enabled. frames saved at %s" % frame_dir)
        if FLAGS.alpha > 0:
            print("leaky relu is used. alpha %.2f" % FLAGS.alpha)
        for i in range(num_iter):
            steps = i + 1
            batch_x, batch_y = dataset.next_train_batch(batch_size)
            if steps % 10 == 0:
                validation_x, validation_y = dataset.get_validation()
                summary, validation_loss, bitmaps = sess.run([merged, combined_loss, convert_bitmap],
                                                             feed_dict={x: validation_x,
                                                                        y: validation_y,
                                                                        phase_train: False,
                                                                        keep_prob: 1.0})
                train_summary, train_loss = sess.run([merged, combined_loss], feed_dict={
                    x: batch_x,
                    y: batch_y,
                    phase_train: False,
                    keep_prob: 1.0}, )
                if FLAGS.capture_frame:
                    render_frame(bitmaps, frame_dir, steps)
                validation_writer.add_summary(summary, steps)
                train_writer.add_summary(train_summary, steps)
                print("step %d, validation loss %g, training loss %g" % (steps, validation_loss, train_loss))
            if steps % checkpoint_steps == 0:
                # do checkpointing
                ckpt_path = os.path.join(checkpoints_dir, "model.ckpt")
                print("checkpoint at step %d" % steps)
                saver.save(sess, ckpt_path, global_step=steps)
            train_step.run(feed_dict={x: batch_x,
                                      y: batch_y,
                                      phase_train: True,
                                      learning_rate: FLAGS.lr,
                                      keep_prob: train_keep_prob})
        if FLAGS.capture_frame:
            print("compile frames in %s to gif" % FLAGS.frame_dir)
            gif = compile_frames_to_gif(frame_dir, os.path.join(frame_dir, default_gif_name))
            print("gif saved at %s" % gif)
    elif FLAGS.mode == 'infer':
        infer_batch_size = 64
        saver = tf.train.Saver()
        print("checkpoint located %s" % FLAGS.ckpt)
        saver.restore(sess, FLAGS.ckpt)
        font_bitmaps = read_font_data(FLAGS.source_font, True)
        print("found %d source fonts" % font_bitmaps.shape[0])
        total_batches = int(np.ceil(font_bitmaps.shape[0] / infer_batch_size))
        print("batch size %d. %d batches in total" % (infer_batch_size,
                                                      total_batches))
        target = list()
        batch_count = 0
        for i in range(0, font_bitmaps.shape[0], infer_batch_size):
            i2 = i + infer_batch_size
            batch_x = font_bitmaps[i: i2]
            batch_count += 1
            if batch_count % 10 == 0:
                print("%d batches has completed" % batch_count)
            target_bitmaps, = sess.run([convert_bitmap], feed_dict={
                x: batch_x,
                phase_train: False,
                keep_prob: 1.0
            })
            target_bitmaps = (target_bitmaps * 255.).astype(dtype=np.int16) % 256
            for tb in target_bitmaps:
                target.append(tb)
        target = np.asarray(target)
        target_path = os.path.join(FLAGS.bitmap_dir, "target.bitmap.npy")
        print("inferred bitmap save at %s" % target_path)
        render_batch = 100
        for i in range(0, target.shape[0], render_batch):
            render_fonts_image(target[i: i + render_batch],
                               os.path.join(FLAGS.bitmap_dir, "fonts_%04d_to_%04d.png" % (i, i + render_batch)), 10,
                               False)
        np.save(target_path, target)
    else:
        raise Exception("unknown mode %s" % FLAGS.mode)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', type=str, default='train',
                        help='could be either infer or train')
    parser.add_argument('--model', type=str, default='medium',
                        help='type of model, could small, medium or big')
    parser.add_argument('--source_font', type=str, default=None,
                        help='npy bitmap for the source font')
    parser.add_argument('--target_font', type=str, default=None,
                        help='npy bitmap for the target font')
    parser.add_argument('--num_examples', type=int, default=2000,
                        help='number of examples for training')
    parser.add_argument('--num_validations', type=int, default=50,
                        help='number of chars for validation')
    parser.add_argument('--lr', type=float, default=0.01,
                        help='learning rate, default to 0.01')
    parser.add_argument('--keep_prob', type=float, default=0.9,
                        help='keep probability for dropout layer, defaults to 0.9')
    parser.add_argument('--iter', type=int, default=1000,
                        help='number of iterations')
    parser.add_argument('--tv', type=float, default=0.0002,
                        help='weight for tv loss, use to force smooth output')
    parser.add_argument('--alpha', type=float, default=-1.0,
                        help='alpha slope for leaky relu if non-negative, otherwise use relu')
    parser.add_argument('--ckpt_steps', type=int, default=50,
                        help='number of steps between two checkpoints')
    parser.add_argument('--num_ckpt', type=int, default=5,
                        help='number of model checkpoints to keep')
    parser.add_argument('--ckpt_dir', type=str, default='/tmp/checkpoints',
                        help='directory for store checkpoints')
    parser.add_argument('--ckpt', type=str, default=None,
                        help='checkpoint file path to restore for inference')
    parser.add_argument('--capture_frame', type=bool, default=True,
                        help='capture font images between iterations and compiled to gif')
    parser.add_argument('--frame_dir', type=str, default='/tmp/frames',
                        help='temporary directory to store font image frames')
    parser.add_argument('--summary_dir', type=str, default='/tmp/summary',
                        help='directory for storing data')
    parser.add_argument('--bitmap_dir', type=str, default='/tmp/bitmap',
                        help='directory for saving inferred bitmap')
    FLAGS = parser.parse_args()
    try:
        if FLAGS.mode == 'train':
            if FLAGS.source_font is None or FLAGS.target_font is None:
                raise RuntimeError("source_font or target_font not specified")
            if FLAGS.capture_frame:
                if os.path.exists(FLAGS.frame_dir):
                    print("removing exisiting frame dirs %s" % FLAGS.frame_dir)
                    shutil.rmtree(FLAGS.frame_dir)
                os.mkdir(FLAGS.frame_dir)
            if os.path.exists(FLAGS.summary_dir):
                print("removing existing summary dir %s" % FLAGS.summary_dir)
                shutil.rmtree(FLAGS.summary_dir)
            if not os.path.exists(FLAGS.ckpt_dir):
                print("create checkpoints dir %s" % FLAGS.ckpt_dir)
                os.makedirs(FLAGS.ckpt_dir)
        if FLAGS.mode == 'infer':
            if not os.path.exists(FLAGS.bitmap_dir):
                print("create target bitmap dir %s" % FLAGS.bitmap_dir)
                os.makedirs(FLAGS.bitmap_dir)
    except Exception as e:
        print("initial validation failed")
        raise e
    tf.app.run()
