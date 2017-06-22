# coding=utf-8

from keras import backend as K
from keras.layers import Layer


class MaxPoolingWithArgmax2D(Layer):

    def __init__(self, pool_size=(2, 2), strides=(2, 2), padding='same', **kwargs):
        super(MaxPoolingWithArgmax2D, self).__init__(**kwargs)

        self.pool_size = pool_size
        self.strides = strides
        self.padding = padding

        if K.backend() == 'tensorflow':
            # Check whether we are running on GPU to decide which version of pooling to use
            from tensorflow.python.client import device_lib

            local_device_protos = device_lib.list_local_devices()
            gpus = [x.name for x in local_device_protos if x.device_type == 'GPU']
            self.running_on_gpu = len(gpus) > 0

            if not self.running_on_gpu:
                raise NotImplementedError('MaxPoolingWithArgmax2D works only on GPU')
        else:
            raise NotImplementedError('{} backend is not supported for layer {}'.format(K.backend(), type(self).__name__))

    def build(self, input_shape):
        # Create a trainable weight variable for this layer.
        super(MaxPoolingWithArgmax2D, self).build(input_shape)  # Be sure to call this somewhere!

    def call(self, inputs, **kwargs):
        pool_size = self.pool_size
        padding = self.padding
        strides = self.strides

        if K.backend() == 'tensorflow':
            # tf.nn.max_pool_with_argmax works only on GPU
            # See: https://stackoverflow.com/questions/39493229/how-to-use-tf-nn-max-pool-with-argmax-correctly
            ksize = [1, pool_size[0], pool_size[1], 1]
            padding = padding.upper()
            strides = [1, strides[0], strides[1], 1]
            output, argmax = K.tf.nn.max_pool_with_argmax(inputs, ksize=ksize, strides=strides, padding=padding)
        else:
            raise NotImplementedError('{} backend is not supported for layer {}'.format(K.backend(), type(self).__name__))

        argmax = K.cast(argmax, K.floatx())
        return [output, argmax]

    def compute_output_shape(self, input_shape):
        ratio = (1, 2, 2, 1)
        output_shape = [dim / ratio[idx] if dim is not None else None for idx, dim in enumerate(input_shape)]
        output_shape = tuple(output_shape)
        return [output_shape, output_shape]

    def compute_mask(self, inputs, mask=None):
        return 2 * [None]


class MaxUnpooling2D(Layer):

    def __init__(self, size=(2, 2), **kwargs):
        super(MaxUnpooling2D, self).__init__(**kwargs)
        self.size = size
        self.kwargs = kwargs

    def build(self, input_shape):
        super(MaxUnpooling2D, self).build(input_shape)

    def call(self, inputs, output_shape=None):
        """
        Seen on https://github.com/tensorflow/tensorflow/issues/2169
        Replace with unpool op when/if issue merged
        Add Theano backend
        """
        updates, mask = inputs[0], inputs[1]

        with K.tf.variable_scope(self.name):
            mask = K.cast(mask, 'int32')
            input_shape = K.tf.shape(updates, out_type='int32')

            #  calculation new shape
            if output_shape is None:
                output_shape = (input_shape[0], input_shape[1] * self.size[0], input_shape[2] * self.size[1], input_shape[3])
            self.output_shape1 = output_shape

            # calculation indices for batch, height, width and feature maps
            one_like_mask = K.ones_like(mask, dtype='int32')
            batch_shape = K.concatenate([[input_shape[0]], [1], [1], [1]], axis=0)
            batch_range = K.reshape(K.tf.range(output_shape[0], dtype='int32'), shape=batch_shape)
            b = one_like_mask * batch_range
            y = mask // (output_shape[2] * output_shape[3])
            x = (mask // output_shape[3]) % output_shape[2]
            feature_range = K.tf.range(output_shape[3], dtype='int32')
            f = one_like_mask * feature_range

            # transpose indices & reshape update values to one dimension
            updates_size = K.tf.size(updates)
            indices = K.transpose(K.reshape(K.stack([b, y, x, f]), [4, updates_size]))
            values = K.reshape(updates, [updates_size])
            ret = K.tf.scatter_nd(indices, values, output_shape)
            return ret

    def compute_output_shape(self, input_shape):
        mask_shape = input_shape[1]
        dim0 = mask_shape[0]
        dim1 = mask_shape[1] * self.size[0] if mask_shape[1] is not None else None
        dim2 = mask_shape[2] * self.size[1] if mask_shape[2] is not None else None
        dim3 = mask_shape[3]
        return dim0, dim1, dim2, dim3
