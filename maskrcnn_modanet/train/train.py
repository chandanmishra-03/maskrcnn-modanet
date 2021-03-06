#!/usr/bin/env python

"""
Copyright 2017-2018 Fizyr (https://fizyr.com)

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

run with
maskrcnn-modanet train --epochs 15 --workers 0 --batch-size 1 coco
"""

import argparse
import os
import sys
# import comet_ml in the top of your file
from comet_ml import Experiment

# Add the following code anywhere in your machine learning file
experiment = Experiment(api_key="HFZFSbhqA92gfY0DT1ZCOnk9Y",
                        project_name="mask-rcnn", workspace="chandan")

import keras
import keras.preprocessing.image
import tensorflow as tf

import keras_retinanet.losses
from keras_retinanet.callbacks import RedirectModel
from keras_retinanet.utils.config import read_config_file, parse_anchor_parameters
from keras_retinanet.utils.transform import random_transform_generator
from keras_retinanet.utils.keras_version import check_keras_version
from keras_retinanet.utils.model import freeze as freeze_model
import keras.layers as KL
import keras.models as KM
import keras.backend as K

# Allow relative imports when being executed as script.
# if __name__ == "__main__" and __package__ is None:
#     sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
#     import keras_maskrcnn.bin
#     __package__ = "keras_maskrcnn.bin"

# Change these to absolute imports if you copy this script outside the keras_retinanet package.
from keras_maskrcnn import losses
from keras_maskrcnn import models
from keras_maskrcnn.callbacks.eval import Evaluate


def make_parallel(keras_model, gpu_count):
    """Creates a new wrapper model that consists of multiple replicas of
    the original model placed on different GPUs.
    Args:
        keras_model: the input model to replicate on multiple gpus
        gpu_count: the number of replicas to build
    Returns:
        Multi-gpu model
    """
    # Slice inputs. Slice inputs on the CPU to avoid sending a copy
    # of the full inputs to all GPUs. Saves on bandwidth and memory.
    input_slices = {name: tf.split(x, gpu_count)
                    for name, x in zip(keras_model.input_names,
                                       keras_model.inputs)}

    output_names = keras_model.output_names
    outputs_all = []
    for i in range(len(keras_model.outputs)):
        outputs_all.append([])

    # Run the model call() on each GPU to place the ops there
    for i in range(gpu_count):
        with tf.device('/gpu:%d' % i):
            with tf.name_scope('tower_%d' % i):
                # Run a slice of inputs through this replica
                zipped_inputs = zip(keras_model.input_names,
                                    keras_model.inputs)
                inputs = [
                    KL.Lambda(lambda s: input_slices[name][i],
                              output_shape=lambda s: (None,) + s[1:])(tensor)
                    for name, tensor in zipped_inputs]
                # Create the model replica and get the outputs
                outputs = keras_model(inputs)
                if not isinstance(outputs, list):
                    outputs = [outputs]
                # Save the outputs for merging back together later
                for l, o in enumerate(outputs):
                    outputs_all[l].append(o)

    # Merge outputs on CPU
    with tf.device('/cpu:0'):
        merged = []
        for outputs, name in zip(outputs_all, output_names):
            # Concatenate or average outputs?
            # Outputs usually have a batch dimension and we concatenate
            # across it. If they don't, then the output is likely a loss
            # or a metric value that gets averaged across the batch.
            # Keras expects losses and metrics to be scalars.
            if K.int_shape(outputs[0]) == ():
                # Average
                m = KL.Lambda(lambda o: tf.add_n(
                    o) / len(outputs), name=name)(outputs)
            else:
                # Concatenate
                m = KL.Concatenate(axis=0, name=name)(outputs)
            merged.append(m)
    return merged


class ParallelModel(KM.Model):
    """Subclasses the standard Keras Model and adds multi-GPU support.
    It works by creating a copy of the model on each GPU. Then it slices
    the inputs and sends a slice to each copy of the model, and then
    merges the outputs together and applies the loss on the combined
    outputs.
    """

    def __init__(self, keras_model, gpu_count):
        """Class constructor.
        keras_model: The Keras model to parallelize
        gpu_count: Number of GPUs. Must be > 1
        """
        merged_outputs = make_parallel(
            keras_model=keras_model, gpu_count=gpu_count)
        super(ParallelModel, self).__init__(inputs=keras_model.inputs,
                                            outputs=merged_outputs)
        self.inner_model = keras_model

    def __getattribute__(self, attrname):
        """Redirect loading and saving methods to the inner model. That's where
        the weights are stored."""
        if 'load' in attrname or 'save' in attrname:
            return getattr(self.inner_model, attrname)
        return super(ParallelModel, self).__getattribute__(attrname)

    def summary(self, *args, **kwargs):
        """Override summary() to display summaries of both, the wrapper
        and inner models."""
        super(ParallelModel, self).summary(*args, **kwargs)
        self.inner_model.summary(*args, **kwargs)


def get_session():
    config = tf.ConfigProto()  # allow_soft_placement=True) #, log_device_placement=True)
    config.gpu_options.allow_growth = True
    return tf.Session(config=config)


def model_with_weights(model, weights, skip_mismatch):
    if weights is not None:
        model.load_weights(weights, by_name=True, skip_mismatch=skip_mismatch)
    return model


def create_models(backbone_retinanet, num_classes, weights, freeze_backbone=False, class_specific_filter=True,
                  anchor_params=None):
    modifier = freeze_model if freeze_backbone else None

    model = model_with_weights(
        backbone_retinanet(
            num_classes,
            nms=True,
            class_specific_filter=class_specific_filter,
            modifier=modifier,
            anchor_params=anchor_params
        ), weights=weights, skip_mismatch=True)
    GPU_COUNT = 2
    # model = ParallelModel(model, GPU_COUNT)

    # model = keras.utils.multi_gpu_model(model, gpus=2)
    training_model = model
    prediction_model = model

    # compile model
    training_model.compile(
        loss={
            'regression': keras_retinanet.losses.smooth_l1(),
            'classification': keras_retinanet.losses.focal(),
            'masks': losses.mask(),
        },
        optimizer=keras.optimizers.adam(lr=1e-5, clipnorm=0.001)
    )

    return model, training_model, prediction_model


def create_callbacks(model, training_model, prediction_model, validation_generator, args):
    callbacks = []

    # save the prediction model
    if args.snapshots:
        # ensure directory created first; otherwise h5py will error after epoch.
        os.makedirs(args.snapshot_path, exist_ok=True)
        checkpoint = keras.callbacks.ModelCheckpoint(
            os.path.join(
                args.snapshot_path,
                '{backbone}_{dataset_type}_{{epoch:02d}}.h5'.format(backbone=args.backbone,
                                                                    dataset_type=args.dataset_type)
            ),
            verbose=1
        )
        checkpoint = RedirectModel(checkpoint, prediction_model)
        callbacks.append(checkpoint)

    tensorboard_callback = None

    if args.tensorboard_dir:
        tensorboard_callback = keras.callbacks.TensorBoard(
            log_dir=args.tensorboard_dir,
            histogram_freq=0,
            batch_size=args.batch_size,
            write_graph=True,
            write_grads=False,
            write_images=False,
            embeddings_freq=0,
            embeddings_layer_names=None,
            embeddings_metadata=None
        )
        callbacks.append(tensorboard_callback)

    if args.evaluation and validation_generator:
        if args.dataset_type == 'coco':
            from keras_maskrcnn.callbacks.coco import CocoEval

            # use prediction model for evaluation
            evaluation = CocoEval(validation_generator)
        else:
            evaluation = Evaluate(validation_generator, tensorboard=tensorboard_callback,
                                  weighted_average=args.weighted_average)
        evaluation = RedirectModel(evaluation, prediction_model)
        callbacks.append(evaluation)

    callbacks.append(keras.callbacks.ReduceLROnPlateau(
        monitor='loss',
        factor=0.1,
        patience=2,
        verbose=1,
        mode='auto',
        epsilon=0.0001,
        cooldown=0,
        min_lr=0
    ))

    return callbacks


def create_generators(args):
    # create random transform generator for augmenting training data
    transform_generator = random_transform_generator(flip_x_chance=0.5)

    if args.dataset_type == 'coco':
        # import here to prevent unnecessary dependency on cocoapi
        from maskrcnn_modanet.train.coco import CocoGenerator

        train_generator = CocoGenerator(
            args.coco_path,
            'train',
            transform_generator=transform_generator,
            batch_size=args.batch_size,
            config=args.config,
            image_min_side=800,
            image_max_side=1333
        )

        validation_generator = CocoGenerator(
            args.coco_path,
            'val',
            batch_size=args.batch_size,
            config=args.config,
            image_min_side=800,
            image_max_side=1333
        )
    elif args.dataset_type == 'csv':
        from keras_maskrcnn.preprocessing.csv_generator import CSVGenerator

        train_generator = CSVGenerator(
            args.annotations,
            args.classes,
            transform_generator=transform_generator,
            batch_size=args.batch_size,
            config=args.config,
            image_min_side=800,
            image_max_side=1333
        )

        if args.val_annotations:
            validation_generator = CSVGenerator(
                args.val_annotations,
                args.classes,
                batch_size=args.batch_size,
                config=args.config,
                image_min_side=800,
                image_max_side=1333
            )
        else:
            validation_generator = None
    else:
        raise ValueError('Invalid data type received: {}'.format(args.dataset_type))

    return train_generator, validation_generator


def check_args(parsed_args):
    """
    Function to check for inherent contradictions within parsed arguments.
    For example, batch_size < num_gpus
    Intended to raise errors prior to backend initialisation.

    :param parsed_args: parser.parse_args()
    :return: parsed_args
    """

    return parsed_args


def parse_args(args, savedvars):
    parser = argparse.ArgumentParser(prog='maskrcnn-modanet train',
                                     description='Simple training script for training a RetinaNet mask network.')
    subparsers = parser.add_subparsers(help='Arguments for specific dataset types.', dest='dataset_type')
    subparsers.required = True

    coco_parser = subparsers.add_parser('coco')
    coco_parser.add_argument('--coco-path', help='Path to dataset directory (ie. /tmp/COCO).',
                             default=savedvars['datapath'] + 'datasets/coco/')

    csv_parser = subparsers.add_parser('csv')
    csv_parser.add_argument('annotations', help='Path to CSV file containing annotations for training.')
    csv_parser.add_argument('classes', help='Path to a CSV file containing class label mapping.')
    csv_parser.add_argument('--val-annotations',
                            help='Path to CSV file containing annotations for validation (optional).')

    group = parser.add_mutually_exclusive_group()
    group.add_argument('--snapshot', help='Resume training from a snapshot.')
    group.add_argument('--imagenet-weights',
                       help='Initialize the model with pretrained imagenet weights. This is the default behaviour.',
                       action='store_const', const=True, default=True)
    group.add_argument('--weights', help='Initialize the model with weights from a file.')
    group.add_argument('--no-weights', help='Don\'t initialize the model with any weights.', dest='imagenet_weights',
                       action='store_const', const=False)

    parser.add_argument('--backbone', help='Backbone model used by retinanet.', default='resnet50', type=str)
    parser.add_argument('--batch-size', help='Size of the batches.', default=1, type=int)
    parser.add_argument('--gpu', help='Id of the GPU to use (as reported by nvidia-smi).')
    parser.add_argument('--epochs', help='Number of epochs to train.', type=int, default=50)
    parser.add_argument('--steps', help='Number of steps per epoch.', type=int, default=10000)
    parser.add_argument('--snapshot-path',
                        help='Path to store snapshots of models during training (defaults to '
                             '\'yourpath/results/snapshots/\')',
                        default=savedvars['datapath'] + 'results/snapshots/')
    parser.add_argument('--tensorboard-dir', help='Log directory for Tensorboard output',
                        default=savedvars['datapath'] + 'results/logs/')
    parser.add_argument('--no-snapshots', help='Disable saving snapshots.', dest='snapshots', action='store_false')
    parser.add_argument('--no-evaluation', help='Disable per epoch evaluation.', dest='evaluation',
                        action='store_false')
    parser.add_argument('--freeze-backbone', help='Freeze training of backbone layers.', action='store_true')
    parser.add_argument('--no-class-specific-filter', help='Disables class specific filtering.',
                        dest='class_specific_filter', action='store_false')
    parser.add_argument('--config', help='Path to a configuration parameters .ini file.')
    parser.add_argument('--weighted-average',
                        help='Compute the mAP using the weighted average of precisions among classes.',
                        action='store_true')

    # Fit generator arguments
    parser.add_argument('--workers',
                        help='Number of multiprocessing workers. To disable multiprocessing, set workers to 0',
                        type=int, default=1)
    parser.add_argument('--max-queue-size', help='Queue length for multiprocessing workers in fit generator.', type=int,
                        default=10)

    return check_args(parser.parse_args(args))


def main(args=None):
    import json
    with open(os.path.expanduser('~') + '/.maskrcnn-modanet/' + 'savedvars.json') as f:
        savedvars = json.load(f)

    # parse arguments
    if args is None:
        print('\n\n\nExample usage: maskrcnn-modanet train --epochs 15 --workers 0 --batch-size 1 coco\n\n\n')
        args = ['-h']
    args = parse_args(args, savedvars)

    # make sure keras is the minimum required version
    check_keras_version()

    # create object that stores backbone information
    backbone = models.backbone(args.backbone)

    # optionally choose specific GPU
    if args.gpu:
        os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu
    keras.backend.tensorflow_backend.set_session(get_session())

    # optionally load config parameters
    if args.config:
        args.config = read_config_file(args.config)

    # create the generators
    train_generator, validation_generator = create_generators(args)

    # create the model
    if args.snapshot is not None:
        print('Loading model, this may take a second...')
        model = models.load_model(args.snapshot, backbone_name=args.backbone)
        training_model = model
        prediction_model = model
    else:
        weights = args.weights
        # default to imagenet if nothing else is specified
        if weights is None and args.imagenet_weights:
            weights = backbone.download_imagenet()

        anchor_params = None
        if args.config and 'anchor_parameters' in args.config:
            anchor_params = parse_anchor_parameters(args.config)

        print('Creating model, this may take a second...')
        model, training_model, prediction_model = create_models(
            backbone_retinanet=backbone.maskrcnn,
            num_classes=train_generator.num_classes(),
            weights=weights,
            freeze_backbone=args.freeze_backbone,
            class_specific_filter=args.class_specific_filter,
            anchor_params=anchor_params
        )

    # print model summary
    print(model.summary())

    # create the callbacks
    callbacks = create_callbacks(
        model,
        training_model,
        prediction_model,
        validation_generator,
        args,
    )

    # Use multiprocessing if workers > 0
    if args.workers > 0:
        use_multiprocessing = True
    else:
        use_multiprocessing = False

    # start training
    training_model.fit_generator(
        generator=train_generator,
        steps_per_epoch=args.steps,
        epochs=args.epochs,
        verbose=1,
        callbacks=callbacks,
        workers=args.workers,
        use_multiprocessing=use_multiprocessing,
        max_queue_size=args.max_queue_size
    )


if __name__ == '__main__':
    main()
