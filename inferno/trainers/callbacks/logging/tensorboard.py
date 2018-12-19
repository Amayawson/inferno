import tensorboardX as tX
import numpy as np
import warnings
from scipy.misc import toimage
from .base import Logger
from ....utils import torch_utils as tu
from ....utils import python_utils as pyu
from ....utils import train_utils as tru
from ....utils.exceptions import assert_


class TaggedImage(object):
    def __init__(self, array, tag):
        self.array = array
        self.tag = tag


class TensorboardLogger(Logger):
    """Class to enable logging of training progress to Tensorboard.

    Currently supports logging scalars and images.
    """
    # This is hard coded because tensorboardX doesn't have a __version__
    TENSORBOARDX_IMAGE_FORMAT = 'CHW'

    def __init__(self, log_directory=None,
                 log_scalars_every=None, log_images_every=None, log_histograms_every=None,
                 send_image_at_batch_indices='all', send_image_at_channel_indices='all',
                 send_volume_at_z_indices='mid'):
        """
        Parameters
        ----------
        log_directory : str
            Path to the directory where the log files will be placed.
        log_scalars_every : str or tuple or inferno.utils.train_utils.Frequency
            How often scalars should be logged to Tensorboard. By default, once every iteration.
        log_images_every : str or tuple or inferno.utils.train_utils.Frequency
            How often images should be logged to Tensorboard. By default, once every iteration.
        log_histograms_every : str or tuple or inferno.utils.train_utils.Frequency
            How often histograms should be logged to Tensorboard. By default, once every iteration.
        send_image_at_batch_indices : list or str
            The indices of the batches to be logged. An `image_batch` usually has the shape
            (num_samples, num_channels, num_rows, num_cols). By setting this argument to say
            [0, 2], only images corresponding to `image_batch[0]` and `image_batch[2]` are
            logged. When a str, it should be 'all', in which case, all samples are logged.
        send_image_at_channel_indices : list or str
            Similar to `send_image_at_batch_indices`, but applying to channels.
        send_volume_at_z_indices : list or str
            For 3D batches of shape (num_samples, num_channels, num_z_slices, num_rows, num_cols),
            select the indices of the z slices to be logged. When a str, it could be 'all' or
            'mid' (to log the central z slice).

        Warnings
        --------
        Leaving log_images_every to the default (i.e. once every iteration) might generate a
        large logfile and/or slow down the training.
        """
        super(TensorboardLogger, self).__init__(log_directory=log_directory)
        self._log_scalars_every = None
        self._log_images_every = None
        self._log_histograms_every = None
        self._writer = None
        self._config = {'image_batch_indices': send_image_at_batch_indices,
                        'image_channel_indices': send_image_at_channel_indices,
                        'volume_z_indices': send_volume_at_z_indices}
        # We ought to know the trainer states we're observing (and plotting to tensorboard).
        # These are the defaults.
        self._trainer_states_being_observed_while_training = {'training_loss',
                                                              'training_error',
                                                              'training_prediction',
                                                              'training_inputs',
                                                              'training_target',
                                                              'learning_rate'}
        self._trainer_states_being_observed_while_validating = {'validation_error_averaged',
                                                                'validation_loss_averaged'}
        if log_scalars_every is not None:
            self.log_scalars_every = log_scalars_every
        if log_images_every is not None:
            self.log_images_every = log_images_every
        if log_histograms_every is not None:
            self.log_histograms_every = log_histograms_every

    @property
    def writer(self):
        if self._writer is None:
            self._writer = tX.SummaryWriter(self.log_directory)
        return self._writer

    @property
    def log_scalars_every(self):
        if self._log_scalars_every is None:
            self._log_scalars_every = tru.Frequency(1, 'iterations')
        return self._log_scalars_every

    @log_scalars_every.setter
    def log_scalars_every(self, value):
        self._log_scalars_every = tru.Frequency.build_from(value)

    @property
    def log_scalars_now(self):
        # Using persistent=True in a property getter is probably not a very good idea...
        # We need to make sure that this getter is called only once per callback-call.
        return self.log_scalars_every.match(iteration_count=self.trainer.iteration_count,
                                            epoch_count=self.trainer.epoch_count,
                                            persistent=True)

    @property
    def log_images_every(self):
        if self._log_images_every is None:
            self._log_images_every = tru.Frequency(1, 'iterations')
        return self._log_images_every

    @log_images_every.setter
    def log_images_every(self, value):
        self._log_images_every = tru.Frequency.build_from(value)

    @property
    def log_images_now(self):
        # Using persistent=True in a property getter is probably not a very good idea...
        # We need to make sure that this getter is called only once per callback-call.
        return self.log_images_every.match(iteration_count=self.trainer.iteration_count,
                                           epoch_count=self.trainer.epoch_count,
                                           persistent=True)

    @property
    def log_histograms_every(self):
        if self._log_histograms_every is None:
            self._log_histograms_every = tru.Frequency(1, 'iterations')
        return self._log_histograms_every

    @log_histograms_every.setter
    def log_histograms_every(self, value):
        self._log_histograms_every = tru.Frequency.build_from(value)

    @property
    def log_histograms_now(self):
        # Using persistent=True in a property getter is probably not a very good idea...
        # We need to make sure that this getter is called only once per callback-call.
        return self.log_histograms_every.match(iteration_count=self.trainer.iteration_count,
                                               epoch_count=self.trainer.epoch_count,
                                               persistent=True)

    def observe_state(self, key, observe_while='training'):
        # Validate arguments
        keyword_mapping = {'train': 'training',
                           'training': 'training',
                           'validation': 'validating',
                           'validating': 'validating'}
        observe_while = keyword_mapping.get(observe_while)
        assert_(observe_while is not None,
                "The keyword observe_while must be one of: {}."
                .format(set(keyword_mapping.keys())),
                ValueError)
        assert_(isinstance(key, str),
                "State key must be a string, got {} instead.".format(type(key).__name__),
                TypeError)
        # Add to set of observed states
        if observe_while == 'training':
            self._trainer_states_being_observed_while_training.add(key)
        elif observe_while == 'validating':
            self._trainer_states_being_observed_while_validating.add(key)
        else:
            raise NotImplementedError
        return self

    def unobserve_state(self, key, observe_while='training'):
        if observe_while == 'training':
            self._trainer_states_being_observed_while_training.remove(key)
        elif observe_while == 'validating':
            self._trainer_states_being_observed_while_validating.remove(key)
        else:
            raise NotImplementedError
        return self

    def unobserve_states(self, keys, observe_while='training'):
        for key in keys:
            self.unobserve_state(key, observe_while=observe_while)
        return self

    def observe_training_and_validation_state(self, key):
        for mode in ['training', 'validation']:
            self.observe_state('{}_{}'.format(mode, key), observe_while=mode)

    def observe_states(self, keys, observe_while='training'):
        for key in keys:
            self.observe_state(key, observe_while=observe_while)
        return self

    def observe_training_and_validation_states(self, keys):
        for key in keys:
            self.observe_training_and_validation_state(key)
        return self

    def log_object(self, tag, object_,
                   allow_scalar_logging=True, allow_image_logging=True, allow_histogram_logging=True):
        assert isinstance(tag, str)
        if isinstance(object_, (list, tuple)):
            for object_num, _object in enumerate(object_):
                self.log_object("{}_{}".format(tag, object_num),
                                _object,
                                allow_scalar_logging,
                                allow_image_logging,
                                allow_histogram_logging)
            return

        # FIXME this can throw ugly warnings
        # Check whether object is a scalar
        if tu.is_scalar_tensor(object_) and allow_scalar_logging:
            # Log scalar
            value = tu.unwrap(object_.float(), extract_item=True)
            self.log_scalar(tag, value, step=self.trainer.iteration_count)
        elif isinstance(object_, (float, int)) and allow_scalar_logging:
            value = float(object_)
            self.log_scalar(tag, value, step=self.trainer.iteration_count)
        elif tu.is_label_image_or_volume_tensor(object_) and allow_image_logging:
            # Add a channel axis and log as images
            self.log_image_or_volume_batch(tag, object_[:, None, ...],
                                           self.trainer.iteration_count)
        elif tu.is_image_or_volume_tensor(object_):
            if allow_image_logging:
                # Log images
                self.log_image_or_volume_batch(tag, object_, self.trainer.iteration_count)
        elif tu.is_vector_tensor(object_) and allow_histogram_logging:
            # Log histograms
            values = tu.unwrap(object_, as_numpy=True)
            self.log_histogram(tag, values, self.trainer.iteration_count)
        else:
            # Object is neither a scalar nor an image nor a vector, there's nothing we can do
            if tu.is_tensor(object_):
                warnings.warn("Unsupported attempt to log tensor `{}` of shape `{}`".format(tag, object_.size()))

    def end_of_training_iteration(self, **_):
        log_scalars_now = self.log_scalars_now
        log_images_now = self.log_images_now
        if not log_scalars_now and not log_images_now:
            # Nothing to log, so we won't bother
            return
        # Read states
        for state_key in self._trainer_states_being_observed_while_training:
            state = self.trainer.get_state(state_key, default=None)
            if state is None:
                # State not found in trainer but don't throw a hissy fit
                continue
            self.log_object(state_key, state,
                            allow_scalar_logging=log_scalars_now,
                            allow_image_logging=log_images_now)

    def end_of_validation_run(self, **_):
        # Log everything
        # Read states
        for state_key in self._trainer_states_being_observed_while_validating:
            state = self.trainer.get_state(state_key, default=None)
            if state is None:
                # State not found in trainer but don't throw a hissy fit
                continue
            self.log_object(state_key, state,
                            allow_scalar_logging=True,
                            allow_image_logging=True)

    def _tag_image(self, image, base_tag, prefix=None, instance_num=None, channel_num=None,
                   slice_num=None):
        tag = base_tag
        if prefix is not None:
            tag = '{}/{}'.format(base_tag, prefix)
        if instance_num is not None:
            tag = '{}/instance_{}'.format(tag, instance_num)
        if channel_num is not None:
            tag = '{}/channel_{}'.format(tag, channel_num)
        if slice_num is not None:
            tag = '{}/slice_{}'.format(tag, slice_num)
        return TaggedImage(image, tag)

    def extract_images_from_batch(self, batch, base_tag=None, prefix=None):
        if base_tag is None:
            assert_(prefix is None,
                    "`base_tag` is not provided - `prefix` must be None in this case.",
                    ValueError)
        # Special case when batch is a list or tuple of batches
        if isinstance(batch, (list, tuple)):
            image_list = []
            for batch_num, _batch in batch:
                image_list.extend(
                    self.extract_images_from_batch(_batch, base_tag=base_tag,
                                                   prefix='batch_{}'.format(batch_num)))
            return image_list
        # `batch` really is a tensor from now on.
        batch_is_image_tensor = tu.is_image_tensor(batch)
        batch_is_volume_tensor = tu.is_volume_tensor(batch)
        assert batch_is_volume_tensor != batch_is_image_tensor, \
            "Batch must either be a image or a volume tensor."
        # Convert to numpy
        batch = batch.float().numpy()
        # Get the indices of the batches we want to send to tensorboard
        batch_indices = self._config.get('image_batch_indices', 'all')
        if batch_indices == 'all':
            batch_indices = list(range(batch.shape[0]))
        elif isinstance(batch_indices, (list, tuple)):
            pass
        elif isinstance(batch_indices, int):
            batch_indices = [batch_indices]
        else:
            raise NotImplementedError
        # Get the indices of the channels we want to send to tensorboard
        channel_indices = self._config.get('image_channel_indices', 'all')
        if channel_indices == 'all':
            channel_indices = list(range(batch.shape[1]))
        elif isinstance(channel_indices, (list, tuple)):
            pass
        elif isinstance(channel_indices, int):
            channel_indices = [channel_indices]
        else:
            raise NotImplementedError
        # Extract images from batch
        if batch_is_image_tensor:
            image_list = [(self._tag_image(image,
                                           base_tag=base_tag, prefix=prefix,
                                           instance_num=instance_num,
                                           channel_num=channel_num)
                           if base_tag is not None else image)
                          for instance_num, instance in enumerate(batch)
                          for channel_num, image in enumerate(instance)
                          if instance_num in batch_indices and channel_num in channel_indices]
        else:
            assert batch_is_volume_tensor
            # Trim away along the z axis
            z_indices = self._config.get('volume_z_indices', 'mid')
            if z_indices == 'all':
                z_indices = list(range(batch.shape[2]))
            elif z_indices == 'mid':
                z_indices = [batch.shape[2] // 2]
            elif isinstance(z_indices, (list, tuple)):
                pass
            elif isinstance(z_indices, int):
                z_indices = [z_indices]
            else:
                raise NotImplementedError
            # I'm going to hell for this.
            image_list = [(self._tag_image(image,
                                           base_tag=base_tag, prefix=prefix,
                                           instance_num=instance_num,
                                           channel_num=channel_num,
                                           slice_num=slice_num)
                           if base_tag is not None else image)
                          for instance_num, instance in enumerate(batch)
                          for channel_num, volume in enumerate(instance)
                          for slice_num, image in enumerate(volume)
                          if instance_num in batch_indices and
                          channel_num in channel_indices and
                          slice_num in z_indices]
        # Done.
        return image_list

    def log_image_or_volume_batch(self, tag, batch, step=None):
        assert pyu.is_maybe_list_of(tu.is_image_or_volume_tensor)(batch)
        step = step or self.trainer.iteration_count
        image_list = self.extract_images_from_batch(batch, base_tag=tag)
        self.log_images(tag, image_list, step)

    def log_scalar(self, tag, value, step):
        """
        Parameter
        ----------
        tag : basestring
            Name of the scalar
        value
        step : int
            training iteration
        """
        self.writer.add_scalar(tag=tag, scalar_value=value, global_step=step)

    def log_images(self, tag, images, step, image_format='CHW'):
        """Logs a list of images."""
        assert_(image_format.upper() in ['CHW', 'HWC'],
                "Image format must be either 'CHW' or 'HWC'. Got {} instead.".format(image_format),
                ValueError)
        for image_num, image in enumerate(images):
            if isinstance(image, TaggedImage):
                tag = image.tag
                image = image.array
            else:
                tag = "{}/{}".format(tag, image_num)
            # This will fail for the wrong tensorboard version.
            image = self._order_image_axes(image, image_format, self.TENSORBOARDX_IMAGE_FORMAT)
            # unfortunately tensorboardX does not have a __version__ attribute
            # so I don't see how to check for the version and provide backwards
            # compatability here
            # tensorboardX borks if the number of image channels is not 3
            # if image.shape[-1] == 1:
            #     image = image[..., [0, 0, 0]]
            image = self._normalize_image(image)
            # print(image.dtype, image.shape)
            self.writer.add_image(tag, img_tensor=image, global_step=step)

    @staticmethod
    def _order_image_axes(image, image_format='CHW', target_format='CHW'):
        # image axis gymnastics
        _not_implemented_message = "target_format must be 'CHW' or 'HCW'."
        if image.ndim == 2:
            if target_format == 'CHW':
                # image is 2D - tensorboardX 1.4+ needs a channel axis in the front
                image = image[None, ...]
            elif target_format == 'HWC':
                # image is 2D - tensorboardX 1.3- needs a channel axis in the end
                image = image[..., None]
            else:
                raise NotImplementedError(_not_implemented_message)
        elif image.ndim == 3 and image_format.upper() == 'CHW':
            if target_format == 'CHW':
                # Nothing to do here
                pass
            elif target_format == 'HCW':
                # We have a CHW image, but need HWC.
                image = np.moveaxis(image, 0, 2)
            else:
                raise NotImplementedError(_not_implemented_message)
        elif image.ndim == 3 and image_format.upper() == 'HWC':
            if target_format == 'CHW':
                # We have a HWC image, but need CHW
                image = np.moveaxis(image, 2, 0)
            elif target_format == 'HWC':
                # Nothing to do here
                pass
            else:
                raise NotImplementedError(_not_implemented_message)
        else:
            raise RuntimeError
        return image

    @staticmethod
    def _normalize_image(image):
        normalized_image = image - image.min()
        maxval = normalized_image.max()
        if maxval > 0:
            normalized_image = normalized_image / maxval
        return normalized_image

    def log_histogram(self, tag, values, step, bins=1000):
        """Logs the histogram of a list/vector of values."""
        # TODO
        raise NotImplementedError

    def get_config(self):
        # Apparently, some SwigPyObject objects cannot be pickled - so we need to build the
        # writer on the fly.
        config = super(TensorboardLogger, self).get_config()
        config.update({'_writer': None})
        return config
