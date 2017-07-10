# coding=utf-8

import numpy as np
from keras import backend as K

##############################################
# GLOBALS
##############################################

_EPSILON = 10e-8


##############################################
# UTILITY FUNCTIONS
##############################################

def _tf_filter_nans(t, epsilon):
    """
    Filter NaNs from a tensor 't' and replace with value epsilon

    # Arguments
        t: A tensor to filter
        epsilon: Value to replace NaNs with
    # Returns
        A tensor of same shape as t with NaN values replaced by epsilon.
    """

    return K.tf.where(K.tf.is_nan(t), K.tf.ones_like(t) * epsilon, t)


def _tf_clamp_to_min(t, epsilon):
    return K.tf.where(K.tf.less(t, epsilon), K.tf.ones_like(t) * epsilon, t)


def _tf_initialize_local_variables():
    """
    Initializes all the global and local variables of the Keras Tensorflow backend
    session.
    """
    sess = K.get_session()

    local_init = K.tf.local_variables_initializer()
    global_init = K.tf.global_variables_initializer()
    sess.run(local_init)
    sess.run(global_init)


def _to_tensor(x, dtype):
    """
    Convert the input `x` to a tensor of type `dtype`.

    # Arguments
        x: An object to be converted (numpy array, list, tensors).
        dtype: The destination type.
    # Returns
        A tensor.
    """

    x = K.tf.convert_to_tensor(x)
    if x.dtype != dtype:
        x = K.tf.cast(x, dtype)
    return x


def _tf_log2(x):
    numerator = K.tf.log(x)
    denominator = K.tf.log(K.tf.constant(2, dtype=numerator.dtype))
    return numerator / denominator


def _tf_softmax(y_pred, epsilon=None):
    epsilon = _to_tensor(_EPSILON, y_pred.dtype.base_dtype)

    softmax = K.tf.nn.softmax(y_pred)
    softmax = K.tf.clip_by_value(softmax, epsilon, 1. - epsilon)
    softmax = _tf_filter_nans(softmax, epsilon)

    return softmax


def _tf_weighted_pixelwise_cross_entropy(softmax, y_true, class_weights):
    epsilon = _to_tensor(_EPSILON, softmax.dtype.base_dtype)

    xent = K.tf.multiply(y_true * K.tf.log(softmax), class_weights)
    xent = _tf_filter_nans(xent, epsilon)
    xent = -K.tf.reduce_mean(K.tf.reduce_sum(xent, axis=(1, 2, 3)))

    return xent


def _tf_calculate_image_entropy(i, y_true_unlabeled, y_pred_unlabeled, num_classes, batch_entropy):

    num_superpixels = K.tf.reduce_max(y_true_unlabeled[i])
    # TODO: Check whether the superpixel groups start from 1 or 0
    for_each_superpixel_cond = lambda j, p1, p2, p3, p4: K.tf.less(j, num_superpixels)

    def calculate_superpixel_entropy(j, unlabeled_true, unlabeled_pred, num_classes, image_entropy):
        # j is the superpixel label - select only the area of the unlabeled_true
        # image where the values match the superpixel index
        superpixel_mask = K.tf.cast(K.tf.equal(unlabeled_true, j), K.tf.int32)
        rest_mask = K.tf.cast(K.tf.not_equal(unlabeled_true, j), K.tf.int32)
        num_pixels_in_superpixel = K.tf.cast(K.tf.reduce_sum(superpixel_mask), K.tf.float32)

        # Turn the rest of the image into num_classes + 1, so that they are not included
        # in the bincount
        unlabeled_rest = K.tf.multiply(rest_mask, num_classes+1)
        unlabeled_superpixel = K.tf.multiply(unlabeled_pred, superpixel_mask)
        int_unlabeled = K.tf.add(unlabeled_superpixel, unlabeled_rest)

        # Count the occurrences of different classes within the superpixel
        # Note: Tensorflow ignores the num_classes + 1 values
        class_occurrences = K.tf.bincount(int_unlabeled, minlength=num_classes, maxlength=num_classes, dtype=K.tf.float32)

        # Calculate the entropy of this superpixel
        entropy = K.tf.div(class_occurrences, num_pixels_in_superpixel)
        entropy = -K.tf.reduce_sum(K.tf.multiply(entropy, _tf_log2(entropy)))

        # Add to the image entropy accumulator and increase the loop variable
        K.tf.add(image_entropy, entropy)
        j += 1

        return j, unlabeled_true, unlabeled_pred, num_classes, image_entropy

    _, _, _, _, image_entropy = K.tf.while_loop(for_each_superpixel_cond,
                                                calculate_superpixel_entropy,
                                                [K.tf.constant(0), y_true_unlabeled[i], y_pred_unlabeled[i], num_classes, K.tf.constant(0, dtype=K.tf.float32)])

    # Add the image entropy to the batch entropy and increase the loop variable
    K.tf.add(batch_entropy, image_entropy)
    i += 1
    return i, y_true_unlabeled, y_pred_unlabeled, num_classes, batch_entropy


def _tf_unlabeled_superpixel_loss(y_true_unlabeled, y_pred_unlabeled, num_unlabeled, num_classes):
    """
    Calculates loss for a batch of unlabeled images. The function assumes that the
    ground truth labels are SLIC superpixel segmentations with index encoded superpixel
    boundaries.

    # Arguments
        :param y_true_unlabeled: ground truth labels (index encoded) (dtype=int32)
        :param y_pred_unlabeled: predicted labels (index encoded) (dtype=int32)
        :param num_unlabeled: number of unlabeled images (dtype=int32)
        :param num_classes: number of classes (dtype=int32)
    # Returns
        :return: the mean (image-level) unlabeled superpixel loss for the batch
    """
    for_each_unlabeled_image_cond = lambda i, p1, p2, p3, p4: K.tf.less(i, num_unlabeled)

    _, _, _, _, batch_entropy = K.tf.while_loop(for_each_unlabeled_image_cond,
                                                _tf_calculate_image_entropy,
                                                [K.tf.constant(0), y_true_unlabeled, y_pred_unlabeled, num_classes, K.tf.constant(0, dtype=K.tf.float32)])

    return batch_entropy


##############################################
# ACTIVATION FUNCTIONS
##############################################

def _depth_softmax(matrix):
    """
    A per-pixel softmax i.e. each pixel is considered as a sample and the
    class probabilities for each pixel sum to one.

    # Background
        Keras softmax doesn't work for N-dimensional tensors. The function
        takes in a keras matrix of size HxWxNUM_CLASSES and applies
        'depth-wise' softmax to the matrix. The output is thus a matrix of
        size HxWxNUM_CLASSES where for each WxH entry the depth slice
        of NUM_CLASSES entries sum to 1.
    # Arguments
        matrix: A tensor from a network layer with dimensions HxWxNUM_CLASSES
    """
    sigmoid = lambda x: 1.0 / (1.0 + K.exp(-x))
    sigmoided_matrix = sigmoid(matrix)
    softmax_matrix = sigmoided_matrix / K.sum(sigmoided_matrix, axis=-1, keepdims=True)
    return softmax_matrix


def np_softmax(X, theta=1.0, axis=None):
    """
    Compute the softmax of each element along an axis of X.

    Parameters
    ----------
    X: ND-Array. Probably should be floats.
    theta (optional): float parameter, used as a multiplier
        prior to exponentiation. Default = 1.0
    axis (optional): axis to compute values along. Default is the
        first non-singleton axis.

    Returns an array the same size as X. The result will sum to 1
    along the specified axis.
    """

    # make X at least 2d
    y = np.atleast_2d(X)

    # find axis
    if axis is None:
        axis = next(j[0] for j in enumerate(y.shape) if j[1] > 1)

    # multiply y against the theta parameter,
    y = y * float(theta)

    # subtract the max for numerical stability
    y = y - np.expand_dims(np.max(y, axis=axis), axis)

    # exponentiate y
    y = np.exp(y)

    # take the sum along the specified axis
    ax_sum = np.expand_dims(np.sum(y, axis=axis), axis)

    # finally: divide elementwise
    p = y / ax_sum

    # flatten if X was 1D
    if len(X.shape) == 1: p = p.flatten()

    return p


##############################################
# LOSS FUNCTIONS
##############################################

def pixelwise_crossentropy(y_true, y_pred):
    """
    Pixel-wise categorical cross-entropy between an output
    tensor and a target tensor.

    # Arguments
        y_pred: A tensor resulting from a softmax.
        y_true: A tensor of the same shape as `output`.
    # Returns
        Output tensor.
    """
    labels = K.tf.cast(K.tf.argmax(y_true, axis=-1), K.tf.int32)

    # Cross-entropy is calculated for each pixel i.e. the xent shape is
    # B_SIZExHxW - calculate the sums for each image and take the mean for the
    # batch
    xent = K.tf.nn.sparse_softmax_cross_entropy_with_logits(logits=y_pred, labels=labels)
    xent = K.tf.reduce_sum(xent, axis=(1, 2))

    return K.tf.reduce_mean(xent)

def weighted_pixelwise_crossentropy(class_weights):
    def loss(y_true, y_pred):
        """
        Pixel-wise weighted categorical cross-entropy between an
        output tensor and a target tensor.

        # Arguments
            :param y_pred: A tensor resulting from the last convolutional layer.
            :param y_true: A tensor of the same shape as `y_pred`.
            :param class_weights: Weights for each class
        # Returns
            :return: loss as a Tensorflow tensor.
        """

        # Calculate cross-entropy loss
        epsilon = _to_tensor(_EPSILON, y_pred.dtype.base_dtype)
        softmax = _tf_softmax(y_pred, epsilon)
        xent = _tf_weighted_pixelwise_cross_entropy(softmax, y_true, class_weights)
        return xent

    return loss


def dummy_loss(y_true, y_pred):
    return y_pred


def default_mean_teacher_lambda_loss(args):
    # type: (list[K.tf.Tensor]) -> K.tf.Tensor

    """
    Calculates the Mean Teacher loss function, which consists of
    classification cost and consistency cost as presented in:

        https://arxiv.org/pdf/1703.01780.pdf

    The function is used in conjunction with a Lambda layer to create
    a layer which can calculate the loss. This is done because the
    parameters to the function change on each training step and thus
    need to be passed through the network as inputs.

    # Arguments
        :param args: a list of Tensorflow tensors, described below

        0: y_pred: predictions from the network (logits)
        1: y_true: ground truth labels in index encoded format
        2: num_unlabeled: number of unlabeled data
        3: mt_pred: mean teacher predictions from the teacher network (logits)
        4: cons_coefficient: consistency cost coefficient
        5: (optional) class_weights: class weights - only applied to classification cost
    # Returns
        :return: the mean teacher loss
    """

    # TODO: Create option to apply/not apply consistency to labeled data
    # TODO: Optimize, now might calculate softmax twice for student (not a major thing)
    # TODO: Figure out how to pass class weights
    # see: https://github.com/CuriousAI/mean-teacher/blob/master/mean_teacher/model.py#L410

    # Extract arguments
    class_weights = None

    if len(args) == 5:
        y_pred, y_true, num_unlabeled, mt_pred, cons_coefficient = args
    elif len(args) == 6:
        y_pred, y_true, num_unlabeled, mt_pred, cons_coefficient, class_weights = args
    else:
        raise ValueError('Expected 5 or 6 arguments, got: {}'.format(len(args)))

    num_labeled = K.tf.squeeze(K.tf.subtract(K.tf.shape(y_pred)[0], K.tf.to_int32(num_unlabeled[0])))
    y_pred_labeled = y_pred[0:num_labeled]
    y_true_labeled = y_true[0:num_labeled]
    #y_pred_unlabeled = y_pred[num_unlabeled:]
    #y_true_unlabeled = y_true[num_unlabeled:]

    """
    Classification cost calculation - only for labeled
    """

    classification_costs = None

    if class_weights is not None:
        # Weighted pixelwise cross-entropy
        # The labels are index encoded - expand to one hot encoding for weighted_pixelwise_crossentropy calculation
        y_pred_labeled = K.tf.one_hot(y_pred_labeled, y_pred.shape[-1])
        classification_costs = weighted_pixelwise_crossentropy(class_weights)(y_true_labeled, y_pred_labeled)
    else:
        # Pixelwise cross-entropy
        y_true_labeled = K.tf.cast(y_true_labeled, K.tf.int32)
        xent = K.tf.nn.sparse_softmax_cross_entropy_with_logits(logits=y_pred_labeled, labels=y_true_labeled)
        # Returns cross-entropy loss for each pixel, i.e. B_SIZExHxW
        # calculate the sum of pixel cross-entropies for each image and take the mean of images in the batch
        xent = K.tf.reduce_sum(xent, axis=(1, 2))
        classification_costs = K.tf.reduce_mean(xent)

    """
    Consistency costs - for labeled and unlabeled
    """

    student_softmax = _tf_softmax(y_pred)
    teacher_softmax = _tf_softmax(mt_pred)

    # Calculate the L2 distance between the predictions (softmax)
    l2_softmax_dist = (student_softmax - teacher_softmax) ** 2

    # Output of the softmax is B_SIZExHxWxN_CLASSES
    # Sum the last three axes to get the total loss over images
    l2_softmax_dist = K.tf.reduce_sum(l2_softmax_dist, axis=(1, 2, 3))

    # Take the mean of the loss per image and multiply by the consistency coefficient
    consistency_costs = K.tf.reduce_mean(l2_softmax_dist) * cons_coefficient[0]

    # Total cost
    total_costs = classification_costs + consistency_costs
    return total_costs


def default_semisupervised_lambda_loss(args):
    if len(args) != 3:
        raise ValueError('Expected 3 values (y_pred, y_true, num_unlabeled)')

    y_pred, y_true, num_unlabeled = args

    int_num_unlabeled = K.tf.squeeze(K.tf.to_int32(num_unlabeled[0]))
    int_num_labeled = K.tf.squeeze(K.tf.subtract(K.tf.shape(y_pred)[0], K.tf.to_int32(num_unlabeled[0])))
    int_num_classes = K.tf.shape(y_pred)[-1]

    y_pred_labeled = y_pred[0:int_num_labeled]
    y_true_labeled = y_true[0:int_num_labeled]

    """
    Labeled loss - pixelwise cross-entropy
    """
    # Pixelwise cross-entropy
    y_true_labeled = K.tf.cast(y_true_labeled, K.tf.int32)
    xent = K.tf.nn.sparse_softmax_cross_entropy_with_logits(logits=y_pred_labeled, labels=y_true_labeled)
    # Returns cross-entropy loss for each pixel, i.e. B_SIZExHxW
    # calculate the sum of pixel cross-entropies for each image and take the mean of images in the batch
    xent = K.tf.reduce_sum(xent, axis=(1, 2))
    labeled_loss = K.tf.reduce_mean(xent)

    """
    Unlabeled superpixel loss - assumes the y_true_unlabeled have been made using SLIC
    """
    y_pred_unlabeled = K.tf.cast(K.tf.argmax(y_pred[int_num_labeled:], axis=-1), dtype=K.tf.int32)
    y_true_unlabeled = K.tf.cast(y_true[int_num_labeled:], dtype=K.tf.int32)
    unlabeled_loss = _tf_unlabeled_superpixel_loss(y_true_unlabeled, y_pred_unlabeled, int_num_unlabeled, int_num_classes)

    #unlabeled_loss = K.tf.Print(unlabeled_loss, [unlabeled_loss], message="Unlabeled loss: ")
    # TODO: Coefficient for the unlabeled loss
    return labeled_loss + unlabeled_loss
