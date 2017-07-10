# coding = utf-8

import os
import json
import random
import datetime
import time
import numpy as np

from PIL import ImageFile
from abc import ABCMeta, abstractmethod
from typing import Callable

import keras
import keras.backend as K
from keras.optimizers import SGD, Adam
from keras.callbacks import ModelCheckpoint, TensorBoard, CSVLogger, ReduceLROnPlateau
from keras.preprocessing.image import list_pictures

from callbacks.optimizer_checkpoint import OptimizerCheckpoint
from models.extended_model import ExtendedModel
from generators import SemisupervisedSegmentationDataGenerator, SegmentationDataGenerator
from generators import DataGeneratorParameters, DataAugmentationParameters

from utils import dataset_utils
from models.models import ModelType, get_model

import losses
import metrics


#############################################
# TRAINER
#############################################


class TrainerBase:
    """
    An abstract base class that implements methods shared between different
    types of trainers, e.g. SupervisedTrainer, SemisupervisedSegmentationTrainer
    or ClassificationTrainer.
    """

    __metaclass__ = ABCMeta

    def __init__(self,
                 config_file_path,
                 data_augmentation_parameters=None):
        # type: (str) -> ()

        """
        Initializes the trainer i.e. seeds random, loads material class information and
        data sets etc.

        # Arguments
            :param config_file_path: path to the configuration file
        # Returns
            Nothing
        """

        self.data_augmentation_parameters = data_augmentation_parameters

        # Without this some truncated images can throw errors
        ImageFile.LOAD_TRUNCATED_IMAGES = True

        self.config = self._load_config_json(config_file_path)
        print 'Configuration file read successfully'

        # Setup the log file path to enable logging
        self.log_file = None
        self.log_file_path = self.get_config_value('log_file_path')
        self.log_to_stdout = self.get_config_value('log_to_stdout')

        # Log the Keras and Tensorflow versions
        self.log('\n\n############################################################\n')
        self.log('Using Keras version: {}'.format(keras.__version__))
        self.log('Using Tensorflow version: {}'.format(K.tf.__version__))

        # Seed the random in order to be able to reproduce the results
        # Note: both random and np.random
        self.log('Initializing random and np.random with random seed: {}'.format(self.get_config_value('random_seed')))
        random.seed(self.get_config_value('random_seed'))
        np.random.seed(self.get_config_value('random_seed'))

        # Set image data format
        self.log('Setting Keras image data format to: {}'.format(self.get_config_value('image_data_format')))
        K.set_image_data_format(self.get_config_value('image_data_format'))

        self._init_data()
        self._init_models()
        self._init_data_generators()

    @abstractmethod
    def _init_data(self):
        self.log('Initializing data')

    @abstractmethod
    def _init_models(self):
        self.log('Initializing models')

    @abstractmethod
    def _init_data_generators(self):
        self.log('Initializing data generators')

    def log(self, s, log_to_stdout=None):
        # Create and open the log file
        if not self.log_file:
            if self.log_file_path:
                TrainerBase._create_path_if_not_existing(self.log_file_path)
                self.log_file = open(self.log_file_path, 'w')
            else:
                raise ValueError('The log file path is None, cannot log')

        # Log to file - make sure there is a newline
        if not s.endswith('\n'):
            self.log_file.write(s + "\n")
        else:
            self.log_file.write(s)

        # Log to stdout - no newline needed
        if (log_to_stdout is None and self.log_to_stdout) or log_to_stdout:
            print s.strip()

    @staticmethod
    def _load_config_json(path):
        with open(path) as f:
            data = f.read()
            return json.loads(data)

    def get_config_value(self, key):
        return self.config[key] if key in self.config else None

    def set_config_value(self, key, value):
        self.config[key] = value

    @staticmethod
    def _create_path_if_not_existing(path):
        if not path:
            return

        if not os.path.exists(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path))

    def get_callbacks(self):
        keras_model_checkpoint_file_path=self.get_config_value('keras_model_checkpoint_file_path')
        keras_tensorboard_log_path=self.get_config_value('keras_tensorboard_log_path')
        keras_csv_log_file_path=self.get_config_value('keras_csv_log_file_path')
        reduce_lr_on_plateau=self.get_config_value('reduce_lr_on_plateau')
        optimizer_checkpoint_file_path=self.get_config_value('optimizer_checkpoint_file_path')

        callbacks = []

        # Make sure the model checkpoints directory exists
        TrainerBase._create_path_if_not_existing(keras_model_checkpoint_file_path)

        model_checkpoint_callback = ModelCheckpoint(
            filepath=keras_model_checkpoint_file_path,
            monitor='val_loss',
            verbose=1,
            save_best_only=False,
            save_weights_only=False,
            mode='auto',
            period=1)

        callbacks.append(model_checkpoint_callback)

        # Tensorboard checkpoint callback to save on every epoch
        if keras_tensorboard_log_path is not None:
            TrainerBase._create_path_if_not_existing(keras_tensorboard_log_path)

            tensorboard_checkpoint_callback = TensorBoard(
                log_dir=keras_tensorboard_log_path,
                histogram_freq=1,
                write_graph=True,
                write_images=True,
                write_grads=False,  # Note: writing grads for a bit network takes about an hour
                embeddings_freq=0,
                embeddings_layer_names=None,
                embeddings_metadata=None)

            callbacks.append(tensorboard_checkpoint_callback)

        # CSV logger for streaming epoch results
        if keras_csv_log_file_path is not None:
            TrainerBase._create_path_if_not_existing(keras_csv_log_file_path)

            csv_logger_callback = CSVLogger(
                keras_csv_log_file_path,
                separator=',',
                append=False)

            callbacks.append(csv_logger_callback)

        if reduce_lr_on_plateau is not None:
            factor = reduce_lr_on_plateau.get('factor') or 0.1
            patience = reduce_lr_on_plateau.get('patience') or 10
            min_lr = reduce_lr_on_plateau.get('min_lr') or 0
            epsilon = reduce_lr_on_plateau.get('epsilon') or 0.0001
            cooldown = reduce_lr_on_plateau.get('cooldown') or 0
            verbose = reduce_lr_on_plateau.get('verbose') or 0

            reduce_lr = ReduceLROnPlateau(
                monitor='val_loss',
                factor=factor,
                patience=patience,
                min_lr=min_lr,
                epsilon=epsilon,
                cooldown=cooldown,
                verbose=verbose)

            callbacks.append(reduce_lr)

        # Optimizer checkpoint
        if optimizer_checkpoint_file_path is not None:
            TrainerBase._create_path_if_not_existing(optimizer_checkpoint_file_path)
            optimizer_checkpoint = OptimizerCheckpoint(optimizer_checkpoint_file_path)
            callbacks.append(optimizer_checkpoint)

        return callbacks

    def get_loss_function(self, loss_function_name, training_set, material_class_information):
        loss_function = None

        if loss_function_name == 'pixelwise_crossentropy':
            loss_function = losses.pixelwise_crossentropy
        elif loss_function_name == 'weighted_pixelwise_crossentropy':
            # Get or calculate the median frequency balancing weights
            median_frequency_balancing_weights = self.get_config_value('class_weights')

            if median_frequency_balancing_weights is None or \
                    (len(median_frequency_balancing_weights) != len(material_class_information)):
                self.log('Class weights were not found or did not match the number of material classes')
                self.log('Calculating new median frequency balancing weights for the training set')
                training_set_masks = [sample[1] for sample in training_set]
                median_frequency_balancing_weights = dataset_utils.calculate_median_frequency_balancing_weights(
                    training_set_masks, material_class_information)

                self.log('Median frequency balancing weights calculated: {}'.format(median_frequency_balancing_weights))
            else:
                self.log('Using existing class weights: {}'.format(median_frequency_balancing_weights))
                median_frequency_balancing_weights = np.array(median_frequency_balancing_weights)

            median_frequency_balancing_weights = K.constant(value=median_frequency_balancing_weights)
            loss_function = losses.weighted_pixelwise_crossentropy(median_frequency_balancing_weights)
        elif loss_function_name == 'dummy':
            loss_function = losses.dummy_loss
        else:
            raise ValueError('Unsupported loss function: {}'.format(loss_function_name))

        self.log('Using {} loss function'.format(loss_function_name))
        return loss_function

    def get_optimizer(self, continue_from_optimizer_checkpoint):
        optimizer_info = self.get_config_value('optimizer')
        optimizer_configuration = None
        optimizer = None
        optimizer_name = optimizer_info['name'].lower()

        if continue_from_optimizer_checkpoint:
            optimizer_configuration_file_path = self.get_config_value('optimizer_checkpoint_file_path')
            self.log('Loading optimizer configuration from file: {}'.format(optimizer_configuration_file_path))

            try:
                with open(optimizer_configuration_file_path, 'r') as f:
                    data = f.read()
                    optimizer_configuration = json.loads(data)
            except IOError as e:
                self.log('Could not load optimizer configuration from file: {}, error: {}. Continuing without config.'
                         .format(optimizer_configuration_file_path, e.message))
                optimizer_configuration = None

        if optimizer_name == 'adam':
            if optimizer_configuration is not None:
                optimizer = Adam.from_config(optimizer_configuration)
            else:
                lr = optimizer_info['learning_rate']
                decay = optimizer_info['decay']
                optimizer = Adam(lr=lr, decay=decay)

            self.log('Using {} optimizer with learning rate: {}, decay: {}, beta_1: {}, beta_2: {}'
                .format(optimizer.__class__.__name__,
                        K.get_value(optimizer.lr),
                        K.get_value(optimizer.decay),
                        K.get_value(optimizer.beta_1),
                        K.get_value(optimizer.beta_2)))

        elif optimizer_name == 'sgd':
            if optimizer_configuration is not None:
                optimizer = SGD.from_config(SGD, optimizer_configuration)
            else:
                lr = optimizer_info['learning_rate']
                decay = optimizer_info['decay']
                momentum = optimizer_info['momentum']
                optimizer = SGD(lr=lr, momentum=momentum, decay=decay)

            self.log('Using {} optimizer with learning rate: {}, momentum: {}, decay: {}'
                .format(optimizer.__class__.__name__,
                        K.get_value(optimizer.lr),
                        K.get_value(optimizer.momentum),
                        K.get_value(optimizer.decay)))

        else:
            raise ValueError('Unsupported optimizer name: {}'.format(optimizer_name))

        return optimizer

    @staticmethod
    def get_latest_weights_file_path(weights_folder_path):
        weight_files = dataset_utils.get_files(weights_folder_path)

        if len(weight_files) > 0:
            weight_files.sort()
            weight_file = weight_files[-1]

            if os.path.isfile(os.path.join(weights_folder_path, weight_file)) and weight_file.endswith(".hdf5"):
                return os.path.join(weights_folder_path, weight_file)

        return None

    def load_latest_weights_for_model(self, model, weights_directory_path):
        initial_epoch = 0

        try:
            # Try to find weights from the checkpoint path
            weights_folder = os.path.dirname(weights_directory_path)
            self.log('Searching for existing weights from checkpoint path: {}'.format(weights_folder))
            weight_file_path = TrainerBase.get_latest_weights_file_path(weights_folder)

            if weight_file_path is None:
                self.log('Could not locate any suitable weight files from the given path')
                return 0

            weight_file = weight_file_path.split('/')[-1]

            if weight_file:
                self.log('Loading network weights from file: {}'.format(weight_file_path))
                model.load_weights(weight_file_path)

                # Parse the epoch number: <epoch>-<val_loss>
                epoch_val_loss = weight_file.split('.')[1]
                initial_epoch = int(epoch_val_loss.split('-')[0]) + 1
                self.log('Continuing training from epoch: {}'.format(initial_epoch))
            else:
                self.log('No existing weights were found')

        except Exception as e:
            self.log('Searching for existing weights finished with an error: {}'.format(e.message))
            return 0

        return initial_epoch

    def transfer_weights(self, to_model_wrapper, transfer_weights_options):
        # type: (ModelBase, dict) -> ()

        transfer_model_name = transfer_weights_options['transfer_model_name']
        transfer_model_input_shape = tuple(transfer_weights_options['transfer_model_input_shape'])
        transfer_model_num_classes = transfer_weights_options['transfer_model_num_classes']
        transfer_model_weights_file_path = transfer_weights_options['transfer_model_weights_file_path']

        self.log('Creating transfer model: {} with input shape: {}, num classes: {}'
            .format(transfer_model_name, transfer_model_input_shape, transfer_model_num_classes))
        transfer_model_wrapper = get_model(transfer_model_name,
                                           transfer_model_input_shape,
                                           transfer_model_num_classes)
        transfer_model = transfer_model_wrapper.model
        transfer_model.summary()

        self.log('Loading transfer weights to transfer model from file: {}'.format(transfer_model_weights_file_path))
        transfer_model.load_weights(transfer_model_weights_file_path)

        from_layer_index = transfer_weights_options['from_layer_index']
        to_layer_index = transfer_weights_options['to_layer_index']
        freeze_transferred_layers = transfer_weights_options['freeze_transferred_layers']
        self.log('Transferring weights from layer range: [{}:{}], freeze transferred layers: {}'
            .format(from_layer_index, to_layer_index, freeze_transferred_layers))

        transferred_layers, last_transferred_layer = to_model_wrapper.transfer_weights(
            from_model=transfer_model,
            from_layer_index=from_layer_index,
            to_layer_index=to_layer_index,
            freeze_transferred_layers=freeze_transferred_layers)

        self.log('Weight transfer completed with {} transferred layers, last transferred layer: {}'
            .format(transferred_layers, last_transferred_layer))

    @abstractmethod
    def train(self):
        self.log('Starting training at local time {}\n'.format(datetime.datetime.now()))

    def modify_batch_data(self, batch_index, x, y):
        pass

    def on_batch_end(self, batch_index):
        pass

    def on_epoch_end(self, epoch_index, logs):
        pass


#############################################
# SEGMENTATION TRAINER
#############################################


class SegmentationTrainer(TrainerBase):

    def __init__(self,
                 config_file_path,
                 data_augmentation_parameters):
        # type: (str, DataAugmentationParameters) -> ()

        # Declare variables that are going to be initialized in the _init_ functions
        self.material_class_information = None
        self.num_classes = -1

        self.labeled_photo_files = None
        self.labeled_mask_files = None

        self.training_set = None
        self.validation_set = None
        self.test_set = None

        self.model_wrapper = None
        self.model = None
        self.initial_epoch = 0

        self.training_data_generator = None
        self.validation_data_generator = None

        super(SegmentationTrainer, self).__init__(config_file_path, data_augmentation_parameters)

    def _init_data(self):
        super(SegmentationTrainer, self)._init_data()

        # Load material class information
        self.log('Loading material class information')
        self.material_class_information = dataset_utils.load_material_class_information(self.get_config_value('path_to_material_class_file'))
        self.num_classes = len(self.material_class_information)
        self.log('Loaded {} material classes successfully'.format(self.num_classes))

        # Labeled photos
        self.log('Reading labeled photo files from: {}'.format(self.get_config_value('path_to_labeled_photos')))
        self.labeled_photo_files = list_pictures(self.get_config_value('path_to_labeled_photos'))
        self.log('Found {} labeled photo files'.format(len(self.labeled_photo_files)))

        # Labeled masks
        self.log('Reading labeled mask files from: {}'.format(self.get_config_value('path_to_labeled_masks')))
        self.labeled_mask_files = list_pictures(self.get_config_value('path_to_labeled_masks'))
        self.log('Found {} labeled mask files'.format(len(self.labeled_mask_files)))

        if len(self.labeled_photo_files) != len(self.labeled_mask_files):
            raise ValueError('Unmatching labeled photo - labeled mask file list sizes: photos: {}, masks: {}'
                             .format(len(self.labeled_photo_files), len(self.labeled_mask_files)))

        # Generate random splits of the supervised data for training, validation and test
        self.log('Splitting data to training, validation and test sets of sizes (%) of the labeled dataset of size {}: {}'
            .format(len(self.labeled_photo_files), self.get_config_value('dataset_splits')))

        self.training_set, self.validation_set, self.test_set = \
            dataset_utils.split_labeled_dataset(self.labeled_photo_files, self.labeled_mask_files, self.get_config_value('dataset_splits'))

        self.log('Dataset split complete')
        self.log('Labeled training set size: {}'.format(len(self.training_set)))
        self.log('Labeled validation set size: {}'.format(len(self.validation_set)))
        self.log('Labeled test set size: {}'.format(len(self.test_set)))

        self.log('Saving the labeled data set splits to log file\n')
        self.log('Labeled training set: {}\n'.format(self.training_set), False)
        self.log('Labeled validation_set: {}\n'.format(self.validation_set), False)
        self.log('Labeled test set: {}\n'.format(self.test_set), False)

    def _init_models(self):
        super(SegmentationTrainer, self)._init_models()

        # Model creation
        model_name = self.get_config_value('model')
        input_shape = self.get_config_value('input_shape')

        self.log('Creating model {} instance with input shape: {}, num classes: {}'.format(model_name, input_shape, self.num_classes))
        self.model_wrapper = get_model(model_name, input_shape, self.num_classes)
        self.model = self.model_wrapper.model
        self.model.summary()

        if bool(self.get_config_value('continue_from_last_checkpoint')):
            weights_directory_path = self.get_config_value('keras_model_checkpoint_file_path')
            self.initial_epoch = self.load_latest_weights_for_model(self.model, weights_directory_path)

        if bool(self.get_config_value('transfer_weights')):
            if self.initial_epoch != 0:
                self.log('Cannot transfer weights when continuing from last checkpoint. Skipping weight transfer')
            else:
                self.transfer_weights(self.model_wrapper, self.get_config_value('transfer_options'))

        # Get the optimizer for the student model
        continue_from_optimizer_checkpoint = bool(self.get_config_value('continue_from_optimizer_checkpoint'))

        if continue_from_optimizer_checkpoint and self.initial_epoch == 0:
            self.log('Cannot continue from optimizer checkpoint if initial epoch is 0. Ignoring optimizer checkpoint.')
            continue_from_optimizer_checkpoint = False

        optimizer = self.get_optimizer(continue_from_optimizer_checkpoint)

        # Get the loss function for the student model
        loss_function_name = self.get_config_value('loss_function')
        loss_function = self.get_loss_function(loss_function_name, self.training_set, self.material_class_information)

        # Compile the student model
        self.model.compile(optimizer=optimizer,
                           loss=loss_function,
                           metrics=['accuracy',
                                    metrics.mean_iou(self.num_classes),
                                    metrics.mean_per_class_accuracy(self.num_classes)])

    def _init_data_generators(self):
        super(SegmentationTrainer, self)._init_data_generators()

        use_data_augmentation = bool(self.get_config_value('use_data_augmentation'))
        num_color_channels = self.get_config_value('num_color_channels')

        self.log('Creating training data generator')

        training_data_generator_params = DataGeneratorParameters(
            material_class_information=self.material_class_information,
            num_color_channels=num_color_channels,
            random_seed=self.get_config_value('random_seed'),
            use_per_channel_mean_normalization=True,
            per_channel_mean=self.get_config_value('per_channel_mean'),
            use_per_channel_stddev_normalization=True,
            per_channel_stddev=self.get_config_value('per_channel_stddev'),
            use_data_augmentation=use_data_augmentation,
            data_augmentation_params=self.data_augmentation_parameters,
            shuffle_data_after_epoch=True)

        self.training_data_generator = SegmentationDataGenerator(
            labeled_data=self.training_set,
            params=training_data_generator_params)

        self.log('Creating validation data generator')

        validation_data_generator_params = DataGeneratorParameters(
            material_class_information=self.material_class_information,
            num_color_channels=num_color_channels,
            random_seed=self.get_config_value('random_seed'),
            use_per_channel_mean_normalization=True,
            per_channel_mean=training_data_generator_params.per_channel_mean,
            use_per_channel_stddev_normalization=True,
            per_channel_stddev=training_data_generator_params.per_channel_stddev,
            use_data_augmentation=False,
            data_augmentation_params=None,
            shuffle_data_after_epoch=True)

        self.validation_data_generator = SegmentationDataGenerator(
            labeled_data=self.validation_set,
            params=validation_data_generator_params)

        self.log('Using per-channel mean: {}'.format(self.training_data_generator.per_channel_mean))
        self.log('Using per-channel stddev: {}'.format(self.training_data_generator.per_channel_stddev))

    def train(self):
        super(SegmentationTrainer, self).train()

        num_epochs = self.get_config_value('num_epochs')
        batch_size = self.get_config_value('num_labeled_per_batch')
        crop_shape = self.get_config_value('crop_shape')

        # Labeled data set size determines the epochs
        training_steps_per_epoch = dataset_utils.get_number_of_batches(len(self.training_set), batch_size)
        validation_steps_per_epoch = dataset_utils.get_number_of_batches(len(self.validation_set), batch_size)

        self.log('Num epochs: {}, batch size: {}, crop shape: {}, training steps per epoch: {}, '
                 'validation steps per epoch: {}'
                 .format(num_epochs, batch_size, crop_shape, training_steps_per_epoch, validation_steps_per_epoch))

        # Get a list of callbacks
        callbacks = self.get_callbacks()

        self.model.fit_generator(
            generator=self.training_data_generator.get_flow(
                batch_size=batch_size,
                crop_shape=crop_shape
            ),
            steps_per_epoch=training_steps_per_epoch,
            epochs=num_epochs,
            initial_epoch=self.initial_epoch,
            validation_data=self.validation_data_generator.get_flow(
                batch_size=batch_size,
                crop_shape=crop_shape
            ),
            validation_steps=validation_steps_per_epoch,
            verbose=1,
            callbacks=callbacks)

        self.log('The training session ended at local time {}\n'.format(datetime.datetime.now()))
        self.log_file.close()


#############################################
# SEMISUPERVISED SEGMENTATION TRAINER
#############################################


class SemisupervisedSegmentationTrainer(TrainerBase):

    def __init__(self,
                 config_file_path,
                 data_augmentation_parameters=None,
                 label_generation_function=None,
                 consistency_cost_coefficient_function=None,
                 ema_smoothing_coefficient_function=None,
                 lambda_loss_function=None):

        # type: (str, DataAugmentationParameters, Callable[[np.array[np.float32]], np.array], Callable[[int], float], Callable[[int], float]) -> ()

        self.label_generation_function = label_generation_function
        self.consistency_cost_coefficient_function = consistency_cost_coefficient_function
        self.ema_smoothing_coefficient_function = ema_smoothing_coefficient_function
        self.lambda_loss_function = lambda_loss_function

        # Declare variables that are going to be initialized in the _init_ functions
        self.material_class_information = None
        self.num_classes = -1

        self.labeled_photo_files = None
        self.labeled_mask_files = None
        self.unlabeled_photo_files = None

        self.training_set = None
        self.validation_set = None
        self.test_set = None

        self.use_mean_teacher_method = False
        self.model_wrapper = None
        self.model = None
        self.teacher_model_wrapper = None
        self.teacher_model = None
        self.initial_epoch = 0

        self.training_data_generator = None
        self.validation_data_generator = None

        super(SemisupervisedSegmentationTrainer, self).__init__(config_file_path, data_augmentation_parameters)

    def _init_data(self):
        super(SemisupervisedSegmentationTrainer, self)._init_data()

        # Load material class information
        self.log('Loading material class information')
        self.material_class_information = dataset_utils.load_material_class_information(self.get_config_value('path_to_material_class_file'))
        self.num_classes = len(self.material_class_information)
        self.log('Loaded {} material classes successfully'.format(self.num_classes))

        # Labeled photos
        self.log('Reading labeled photo files from: {}'.format(self.get_config_value('path_to_labeled_photos')))
        self.labeled_photo_files = list_pictures(self.get_config_value('path_to_labeled_photos'))
        self.log('Found {} labeled photo files'.format(len(self.labeled_photo_files)))

        # Labeled masks
        self.log('Reading labeled mask files from: {}'.format(self.get_config_value('path_to_labeled_masks')))
        self.labeled_mask_files = list_pictures(self.get_config_value('path_to_labeled_masks'))
        self.log('Found {} labeled mask files'.format(len(self.labeled_mask_files)))

        if len(self.labeled_photo_files) != len(self.labeled_mask_files):
            raise ValueError('Unmatching labeled photo - labeled mask file list sizes: photos: {}, masks: {}'
                             .format(len(self.labeled_photo_files), len(self.labeled_mask_files)))

        # Generate random splits of the supervised data for training, validation and test
        self.log('Splitting data to training, validation and test sets of sizes (%) of the labeled dataset of size {}: {}'
            .format(len(self.labeled_photo_files), self.get_config_value('dataset_splits')))

        self.training_set, self.validation_set, self.test_set = \
            dataset_utils.split_labeled_dataset(self.labeled_photo_files, self.labeled_mask_files, self.get_config_value('dataset_splits'))

        self.log('Dataset split complete')
        self.log('Labeled training set size: {}'.format(len(self.training_set)))
        self.log('Labeled validation set size: {}'.format(len(self.validation_set)))
        self.log('Labeled test set size: {}'.format(len(self.test_set)))

        self.log('Saving the labeled data set splits to log file\n')
        self.log('Labeled training set: {}\n'.format(self.training_set), False)
        self.log('Labeled validation_set: {}\n'.format(self.validation_set), False)
        self.log('Labeled test set: {}\n'.format(self.test_set), False)

        # Unlabeled photos
        self.log('Reading unlabeled photo files from: {}'.format(self.get_config_value('path_to_unlabeled_photos')))
        self.unlabeled_photo_files = list_pictures(self.get_config_value('path_to_unlabeled_photos'))
        self.log('Found {} unlabeled photo files'.format(len(self.unlabeled_photo_files)))

        if len(self.test_set) == 0 and len(self.unlabeled_photo_files) == 0:
            raise ValueError('No training data found')

    def _init_models(self):
        super(SemisupervisedSegmentationTrainer, self)._init_models()

        # Are we using the mean teacher method?
        self.use_mean_teacher_method = bool(self.get_config_value('use_mean_teacher_method'))
        self.log('Use mean teacher method for training: {}'.format(self.use_mean_teacher_method))

        # Model creation
        model_name = self.get_config_value('model')
        input_shape = self.get_config_value('input_shape')

        self.log('Creating student model {} instance with input shape: {}, num classes: {}'.format(model_name, input_shape, self.num_classes))
        if self.use_mean_teacher_method:
            lambda_loss_func = self.lambda_loss_function if self.lambda_loss_function is not None else losses.default_mean_teacher_lambda_loss

            self.model_wrapper = get_model(model_name,
                                           input_shape,
                                           self.num_classes,
                                           model_type=ModelType.MEAN_TEACHER_STUDENT,
                                           lambda_loss_function=lambda_loss_func)
        else:
            lambda_loss_func = self.lambda_loss_function if self.lambda_loss_function is not None else losses.default_semisupervised_lambda_loss

            self.model_wrapper = get_model(model_name,
                                           input_shape,
                                           self.num_classes,
                                           model_type=ModelType.SEMISUPERVISED,
                                           lambda_loss_function=lambda_loss_func)

        self.model = self.model_wrapper.model
        self.model.summary()

        if bool(self.get_config_value('continue_from_last_checkpoint')):
            weights_directory_path = self.get_config_value('keras_model_checkpoint_file_path')
            self.initial_epoch = self.load_latest_weights_for_model(self.model, weights_directory_path)

        if bool(self.get_config_value('transfer_weights')):
            if self.initial_epoch != 0:
                self.log('Cannot transfer weights when continuing from last checkpoint. Skipping weight transfer')
            else:
                self.transfer_weights(self.model_wrapper, self.get_config_value('transfer_options'))

        # Get the optimizer for the student model
        continue_from_optimizer_checkpoint = bool(self.get_config_value('continue_from_optimizer_checkpoint'))

        if continue_from_optimizer_checkpoint and self.initial_epoch == 0:
            self.log('Cannot continue from optimizer checkpoint if initial epoch is 0. Ignoring optimizer checkpoint.')
            continue_from_optimizer_checkpoint = False

        optimizer = self.get_optimizer(continue_from_optimizer_checkpoint)

        # Get the loss function for the student model
        loss_function_name = self.get_config_value('loss_function')

        if self.use_mean_teacher_method and loss_function_name != 'dummy':
            self.log('Mean teacher method should use \'dummy\' loss function, got: {}. Ignoring passed loss function.'
                     .format(loss_function_name))
            loss_function_name = 'dummy'

        loss_function = self.get_loss_function(loss_function_name, self.training_set, self.material_class_information)

        # Compile the student model
        self.model.compile(optimizer=optimizer,
                           loss=loss_function)#,
                           #metrics=['accuracy',
                           #         metrics.mean_iou(self.num_classes),
                           #         metrics.mean_per_class_accuracy(self.num_classes)])

        # If we are using the mean teacher method create the teacher model
        if self.use_mean_teacher_method:
            self.log('Creating teacher model {} instance with input shape: {}, num classes: {}'.format(model_name, input_shape, self.num_classes))
            self.teacher_model_wrapper = get_model(model_name, input_shape, self.num_classes, ModelType.NORMAL)
            self.teacher_model = self.teacher_model_wrapper.model
            self.teacher_model.summary()
            self.teacher_model.set_weights(self.model.get_weights())

    def _init_data_generators(self):
        super(SemisupervisedSegmentationTrainer, self)._init_data_generators()

        use_data_augmentation = bool(self.get_config_value('use_data_augmentation'))
        num_color_channels = self.get_config_value('num_color_channels')

        # Create training data and validation data generators
        # Note: training data comes from semi-supervised segmentation data generator and validation
        # and test data come from regular segmentation data generator
        self.log('Creating training data generator')

        training_data_generator_params = DataGeneratorParameters(
            material_class_information=self.material_class_information,
            num_color_channels=num_color_channels,
            random_seed=self.get_config_value('random_seed'),
            use_per_channel_mean_normalization=True,
            per_channel_mean=self.get_config_value('per_channel_mean'),
            use_per_channel_stddev_normalization=True,
            per_channel_stddev=self.get_config_value('per_channel_stddev'),
            use_data_augmentation=use_data_augmentation,
            data_augmentation_params=self.data_augmentation_parameters,
            shuffle_data_after_epoch=True)

        self.training_data_generator = SemisupervisedSegmentationDataGenerator(
            labeled_data=self.training_set,
            unlabeled_data=self.unlabeled_photo_files,
            params=training_data_generator_params,
            label_generation_function=self.label_generation_function)

        self.log('Creating validation data generator')

        validation_data_generator_params = DataGeneratorParameters(
            material_class_information=self.material_class_information,
            num_color_channels=num_color_channels,
            random_seed=self.get_config_value('random_seed'),
            use_per_channel_mean_normalization=True,
            per_channel_mean=training_data_generator_params.per_channel_mean,
            use_per_channel_stddev_normalization=True,
            per_channel_stddev=training_data_generator_params.per_channel_stddev,
            use_data_augmentation=False,
            data_augmentation_params=None,
            shuffle_data_after_epoch=True
        )

        self.validation_data_generator = SegmentationDataGenerator(
            labeled_data=self.validation_set,
            params=validation_data_generator_params)

        self.log('Using per-channel mean: {}'.format(self.training_data_generator.per_channel_mean))
        self.log('Using per-channel stddev: {}'.format(self.training_data_generator.per_channel_stddev))

    def train(self):
        super(SemisupervisedSegmentationTrainer, self).train()

        num_epochs = self.get_config_value('num_epochs')
        num_labeled_per_batch = self.get_config_value('num_labeled_per_batch')
        num_unlabeled_per_batch = self.get_config_value('num_unlabeled_per_batch')
        labeled_data_set_size = len(self.training_set)
        unlabeled_data_set_size = len(self.unlabeled_photo_files)
        crop_shape = self.get_config_value('crop_shape')
        total_batch_size = num_labeled_per_batch + num_unlabeled_per_batch

        # Labeled data set size determines the epochs
        training_steps_per_epoch = dataset_utils.get_number_of_batches(labeled_data_set_size, num_labeled_per_batch)
        validation_steps_per_epoch = dataset_utils.get_number_of_batches(len(self.validation_set), num_labeled_per_batch)

        self.log('Labeled data set size: {}, num labeled per batch: {}, '
                 'unlabeled data set size: {}, num unlabeled per batch: {}'
                 .format(labeled_data_set_size, num_labeled_per_batch, unlabeled_data_set_size, num_unlabeled_per_batch))

        self.log('Num epochs: {}, total batch size: {}, crop shape: {}, training steps per epoch: {}, '
                 'validation steps per epoch: {}'
                 .format(num_epochs, total_batch_size, crop_shape, training_steps_per_epoch, validation_steps_per_epoch))

        # Get a list of callbacks
        callbacks = self.get_callbacks()

        # Sanity check
        if self.use_mean_teacher_method:
            if not isinstance(self.model, ExtendedModel):
                raise ValueError('When using mean teacher training the student must be an instance of ExtendedModel')

        # TODO: We also need to save the teacher model at the end of epochs
        self.model.fit_generator(
            generator=self.training_data_generator.get_flow(
                num_labeled_per_batch=num_labeled_per_batch,
                num_unlabeled_per_batch=num_unlabeled_per_batch,
                crop_shape=crop_shape
            ),
            steps_per_epoch=training_steps_per_epoch,
            epochs=num_epochs,
            initial_epoch=self.initial_epoch,
            validation_data=self.validation_data_generator.get_flow(
                batch_size=num_labeled_per_batch,
                crop_shape=crop_shape
            ),
            validation_steps=validation_steps_per_epoch,
            verbose=1,
            trainer=self if self.use_mean_teacher_method else None,
            callbacks=callbacks)

        self.log('The training session ended at local time {}\n'.format(datetime.datetime.now()))
        self.log_file.close()

    def modify_batch_data(self, step_index, x, y):
        # type: (int, list[np.array[np.float32]], np.array) -> (list[np.array[np.float32]], np.array)

        """
        Invoked by the ExtendedModel right before train_on_batch:

        Modifies the batch data by appending the mean teacher predictions as the last
        element of the input data X if we are using mean teacher training. Otherwise,
        returns the same x and y.

        # Arguments
            :param step_index: the training step index
            :param x: input data
            :param y: output data
        # Returns
            :return: a tuple of (input data, output data)
        """
        if self.use_mean_teacher_method:
            if self.teacher_model is not None:
                # First dimension in all of the input data should be the batch size
                batch_size = x[0].shape[0]

                images = x[0]
                mean_teacher_predictions = self.teacher_model.predict_on_batch(images)
                consistency_coefficient = self.consistency_cost_coefficient_function(step_index)
                np_consistency_coefficients = np.ones(shape=[batch_size]) * consistency_coefficient
                x = x + [mean_teacher_predictions, np_consistency_coefficients]
            else:
                raise RuntimeError('Teacher model is not set, cannot run predictions')

        return x, y

    def on_batch_end(self, step_index):
        # type: (int) -> ()

        """
        Invoked by the ExtendedModel right after train_on_batch:

        Updates the teacher model weights if using the mean teacher method for
        training, otherwise does nothing.

        # Arguments
            :param step_index: the training step index
        # Returns
            Nothing
        """
        if self.use_mean_teacher_method:
            if self.teacher_model is not None:
                a = self.ema_smoothing_coefficient_function(step_index)

                # Perform the EMA weight update: theta'_t = a * theta'_t-1 + (1 - a) * theta_t
                s_time = time.time()
                t_weights = self.teacher_model.get_weights()
                s_weights = self.model.get_weights()

                if len(t_weights) != len(s_weights):
                    raise ValueError('The weight arrays are not of the same length for the student and teacher: {} vs {}'
                                     .format(len(t_weights), len(s_weights)))

                num_weights = len(t_weights)

                for i in range(0, num_weights):
                    t_weights[i] = a * t_weights[i] + (1.0 - a) * s_weights[i]

            else:
                raise RuntimeError('Teacher model is not set, cannot run EMA update')

    def on_epoch_end(self, epoch_index, logs):
        # type: (int) -> ()

        """
        Invoked by the ExtendedModel right after the epoch is over.

        Evaluates mean teacher model on the validation data and saves the mean teacher
        model weights.

        # Arguments
            :param epoch_index: index of the epoch that has finished
            :param logs: logs from the epoch (for the student model)
        # Returns
            Nothing
        """
        if self.use_mean_teacher_method:
            # Default to -1.0 validation loss if nothing else is given
            val_loss = -1.0

            if self.validation_data_generator is not None:
                # Evaluate the mean teacher on the validation data
                num_labeled_per_batch = self.get_config_value('num_labeled_per_batch')
                validation_steps_per_epoch = dataset_utils.get_number_of_batches(len(self.validation_set), num_labeled_per_batch)
                crop_shape = self.get_config_value('crop_shape')

                val_outs = self.teacher_model.evaluate_generator(
                    generator=self.validation_data_generator.get_flow(
                        batch_size=num_labeled_per_batch,
                        crop_shape=crop_shape
                    ),
                    steps=validation_steps_per_epoch)

                val_loss = val_outs[0]
                self.log('Epoch {} mean teacher validation loss {}'.format(epoch_index, val_loss))

            # Save the weights
            teacher_model_checkpoint_file_path = self.get_config_value('teacher_model_checkpoint_file_path')
            file_path = teacher_model_checkpoint_file_path.format(epoch=epoch_index, val_loss=val_loss)
            self.log('Saving mean teacher model weights to file: {}'.format(file_path))
            self.teacher_model.save_weights(file_path, overwrite=True)
