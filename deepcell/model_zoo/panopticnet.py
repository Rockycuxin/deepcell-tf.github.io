# Copyright 2016-2019 The Van Valen Lab at the California Institute of
# Technology (Caltech), with support from the Paul Allen Family Foundation,
# Google, & National Institutes of Health (NIH) under Grant U24CA224309-01.
# All rights reserved.
#
# Licensed under a modified Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.github.com/vanvalenlab/deepcell-tf/LICENSE
#
# The Work provided may be used for non-commercial academic purposes only.
# For any other use of the Work, including commercial use, please contact:
# vanvalenlab@gmail.com
#
# Neither the name of Caltech nor the names of its contributors may be used
# to endorse or promote products derived from this software without specific
# prior written permission.
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Feature pyramid network utility functions"""

from __future__ import absolute_import
from __future__ import print_function
from __future__ import division

import re

from tensorflow.python.keras import backend as K
from tensorflow.python.keras.models import Model
from tensorflow.python.keras.layers import Conv2D, Conv3D, TimeDistributed, ConvLSTM2D
from tensorflow.python.keras.layers import Input, Concatenate, Add
from tensorflow.python.keras.layers import Permute, Reshape
from tensorflow.python.keras.layers import Activation, Lambda, BatchNormalization, Softmax
from tensorflow.python.keras.initializers import RandomNormal
from tensorflow.python.keras.layers import UpSampling2D, UpSampling3D

from deepcell.initializers import PriorProbability
from deepcell.layers import TensorProduct, ConvGRU2D
from deepcell.layers import FilterDetections
from deepcell.layers import ImageNormalization2D, Location2D
from deepcell.layers import Anchors, RegressBoxes, ClipBoxes
from deepcell.layers import UpsampleLike
from deepcell.utils.retinanet_anchor_utils import AnchorParameters
from deepcell.model_zoo.fpn import __create_semantic_head
from deepcell.model_zoo.fpn import __create_pyramid_features
from deepcell.utils.backbone_utils import get_backbone
from deepcell.utils.misc_utils import get_sorted_keys

def __merge_temporal_features(feature, mode='conv', feature_size=256, frames_per_batch=1):
    if mode == 'conv':
        x = Conv3D(feature_size, 
                    (frames_per_batch, 3, 3), 
                    strides=(1,1,1),
                    padding='same',
                    )(feature)
        x = BatchNormalization(axis=-1)(x)
        x = Activation('relu')(x)
    elif mode == 'lstm':
        x = ConvLSTM2D(feature_size, 
                       (3, 3), 
                        padding='same',
                        activation='relu',
                        return_sequences=True)(feature)
    elif mode == 'gru':
        x = ConvGRU2D(feature_size,
                        (3, 3),
                        padding='same',
                        activation='relu',
                        return_sequences=True)(feature)

    temporal_feature = feature + x     

    return temporal_feature

def semantic_upsample(x, n_upsample, n_filters=64, ndim=2, target=None):
    """
    Performs iterative rounds of 2x upsampling and
    convolutions with a 3x3 filter to remove aliasing effects

    Args:
        x (tensor): The input tensor to be upsampled
        n_upsample (int): The number of 2x upsamplings
        n_filters (int, optional): Defaults to 256. The number of filters for
            the 3x3 convolution
        target (tensor, optional): Defaults to None. A tensor with the target
            shape. If included, then the final upsampling layer will reshape
            to the target tensor's size
        ndim (int): The spatial dimensions of the input data.
            Default is 2, but it also works with 3

    Returns:
        tensor: The upsampled tensor
    """
    acceptable_ndims = [2, 3]
    if ndim not in acceptable_ndims:
        raise ValueError('Only 2 and 3 dimensional networks are supported')

    conv = Conv2D if ndim == 2 else Conv3D
    upsampling = UpSampling2D if ndim == 2 else UpSampling3D

    for i in range(n_upsample):
        x = conv(n_filters, 3, strides=1,
                 padding='same', data_format='channels_last')(x)

        if i == n_upsample - 1 and target is not None:
            x = UpsampleLike()([x, target])
        else:
            size = (2,2) if ndim==2 else (1,2,2)
            x = upsampling(size=size)(x)

    if n_upsample == 0:
        x = conv(n_filters, 3, strides=1,
                 padding='same', data_format='channels_last')(x)

        if target is not None:
            x = UpsampleLike()([x, target])

    return x


def semantic_prediction(semantic_names,
                        semantic_features,
                        target_level=0,
                        input_target=None,
                        n_filters=64,
                        n_dense=64,
                        ndim=2,
                        n_classes=3,
                        semantic_id=0,
                        include_top=True):
    """Creates the prediction head from a list of semantic features

    Args:
        semantic_names (list): A list of the names of the semantic feature layers
        semantic_features (list): A list of semantic features
            NOTE: The semantic_names and semantic features should be in decreasing order
            e.g. [Q6, Q5, Q4, ...]
        target_level (int, optional): Defaults to 0. The level we need to reach.
            Performs 2x upsampling until we're at the target level
        input_target (tensor, optional): Defaults to None. Tensor with the input image.
        n_dense (int, optional): Defaults to 256. The number of filters for dense layers.
        n_classes (int, optional): Defaults to 3.  The number of classes to be predicted.
        semantic_id (int): Defaults to 0. An number to name the final layer. Allows for multiple
            semantic heads.
    Returns:
        tensor: The softmax prediction for the semantic segmentation head

    Raises:
        ValueError: ndim is not 2 or 3
    """

    if n_classes == 1:
        include_top = False
        
    acceptable_ndims = [2, 3]
    if ndim not in acceptable_ndims:
        raise ValueError('Only 2 and 3 dimensional networks are supported')

    if K.image_data_format() == 'channels_first':
        channel_axis = 1
    else:
        channel_axis = -1

    # Add all the semantic layers
    semantic_sum = semantic_features[0]
    for semantic_feature in semantic_features[1:]:
        semantic_sum = Add()([semantic_sum, semantic_feature])

    # Final upsampling
    min_level = int(re.findall(r'\d+', semantic_names[-1])[0])
    n_upsample = min_level - target_level
    x = semantic_upsample(semantic_sum, n_upsample,
                          target=input_target, ndim=ndim)

    # First tensor product
    x = TensorProduct(n_dense)(x)
    x = BatchNormalization(axis=channel_axis)(x)
    x = Activation('relu')(x)

    # Apply tensor product and softmax layer
    x = TensorProduct(n_classes)(x)

    if include_top:
        x = TensorProduct(n_classes)(x)
        x = Softmax(axis=channel_axis, name='semantic_{}'.format(semantic_id))(x)
    else:
        x = TensorProduct(n_classes)(x)
        x = Activation('relu', name='semantic_{}'.format(semantic_id))(x)

    return x


def __create_semantic_head(pyramid_dict,
                           input_target=None,
                           target_level=2,
                           n_classes=3,
                           n_filters=128,
                           semantic_id=0,
                           ndim=2,
                           **kwargs):
    """
    Creates a semantic head from a feature pyramid network
    Args:
        pyramid_dict: dict of pyramid names and features
        input_target (tensor, optional): Defaults to None. Tensor with the input image.
        target_level (int, optional): Defaults to 2. Upsampling level.
            Level 1 = 1/2^1 size, Level 2 = 1/2^2 size, Level 3 = 1/2^3 size, etc.
        n_classes (int, optional): Defaults to 3.  The number of classes to be predicted
        n_filters (int, optional): Defaults to 128. The number of convolutional filters.
    Returns:
        keras.layers.Layer: The semantic segmentation head
    """
    # Get pyramid names and features into list form
    pyramid_names = get_sorted_keys(pyramid_dict)
    pyramid_features = [pyramid_dict[name] for name in pyramid_names]

    # Reverse pyramid names and features
    pyramid_names.reverse()
    pyramid_features.reverse()

    semantic_features = []
    semantic_names = []

    for N, P in zip(pyramid_names, pyramid_features):
        # Get level and determine how much to upsample
        level = int(re.findall(r'\d+', N)[0])

        n_upsample = level - target_level
        target = semantic_features[-1] if len(semantic_features) > 0 else None
        # Use semantic upsample to get semantic map
        semantic_features.append(semantic_upsample(
            P, n_upsample, n_filters=n_filters, target=target, ndim=ndim))
        semantic_names.append('Q{}'.format(level))

    # Combine all of the semantic features
    x = semantic_prediction(semantic_names, semantic_features,
                            n_classes=n_classes, input_target=input_target,
                            semantic_id=semantic_id, ndim=ndim, **kwargs)

    return x

def semantic_upsample_prototype(x, n_upsample, n_filters=64, ndim=3):
    conv = Conv2D if ndim == 2 else Conv3D
    conv_kernel = (3,3) if ndim == 2 else (1,3,3)
    upsampling = UpSampling2D if ndim == 2 else UpSampling3D
    size = (2,2) if ndim == 2 else (1,2,2)
    if n_upsample > 0:
        for i in range(n_upsample):
            x = conv(n_filters, conv_kernel, strides=1,
                     padding='same', data_format='channels_last')(x)
            x = upsampling(size=size)(x)
    else:
        x = conv(n_filters, conv_kernel, strides=1,
                 padding='same', data_format='channels_last')(x)
    return x

def __create_semantic_head_prototype(pyramid_dict,
                            n_classes=3,
                            n_filters=64,
                            n_dense=128,
                            semantic_id=0,
                            ndim=3,
                            include_top=False,
                            target_level=2,
                            **kwargs):

    if K.image_data_format() == 'channels_first':
        channel_axis = 1
    else:
        channel_axis = -1

    # Get pyramid names and features into list form
    pyramid_names = get_sorted_keys(pyramid_dict)
    pyramid_features = [pyramid_dict[name] for name in pyramid_names]

    # Reverse pyramid names and features
    pyramid_names.reverse()
    pyramid_features.reverse()

    semantic_features = []
    semantic_names = []

    for N, P in zip(pyramid_names, pyramid_features):

        # Get level and determine how much to upsample
        level = int(re.findall(r'\d+', N)[0])
        n_upsample = level - target_level

        # Use semantic upsample to get semantic map
        semantic_features.append(semantic_upsample_prototype(P, n_upsample, ndim=ndim))
        semantic_names.append('Q{}'.format(level))

    # Add all the semantic layers
    semantic_sum = semantic_features[0]
    for semantic_feature in semantic_features[1:]:
        semantic_sum = Add()([semantic_sum, semantic_feature])

    semantic_sum = pyramid_features[-1]
    semantic_names = pyramid_names[-1]

    # Final upsampling
    min_level = int(re.findall(r'\d+', semantic_names[-1])[0])
    n_upsample = min_level
    x = semantic_upsample_prototype(semantic_sum, n_upsample, ndim=ndim)

    # First tensor product
    x = TensorProduct(n_dense)(x)
    x = BatchNormalization(axis=channel_axis)(x)
    x = Activation('relu')(x)

    # Apply tensor product and softmax layer
    x = TensorProduct(n_classes)(x)

    if include_top:
        x = Softmax(axis=channel_axis, name='semantic_{}'.format(semantic_id))(x)
    else:
        x = Activation('relu', name='semantic_{}'.format(semantic_id))(x)

    return x

def PanopticNet(backbone,
               input_shape,
               inputs=None,
               backbone_levels=['C3','C4','C5'],
               pyramid_levels=['P3','P4','P5','P6','P7'],
               create_pyramid_features=__create_pyramid_features,
               create_semantic_head=__create_semantic_head,
               frames_per_batch=1,
               temporal_mode=None,
               num_semantic_heads=1,
               num_semantic_classes=[3],
               required_channels=3,
               norm_method='whole_image',
               pooling=None,
               location=True,
               use_imagenet=True,
               name='panopticnet',
               **kwargs):
    
    channel_axis = 1 if K.image_data_format() == 'channels_first' else -1

    if inputs is None:
        if frames_per_batch > 1:
            if channel_axis == 1:
                input_shape_with_time = tuple(
                    [input_shape[0], frames_per_batch] + list(input_shape)[1:])
            else:
                input_shape_with_time = tuple(
                    [frames_per_batch] + list(input_shape))
            inputs = Input(shape=input_shape_with_time)
        else:
            inputs = Input(shape=input_shape)

    # force the channel size for backbone input to be `required_channels`
    if frames_per_batch > 1:
        norm = TimeDistributed(ImageNormalization2D(norm_method=norm_method))(inputs)
    else:
        norm = ImageNormalization2D(norm_method=norm_method)(inputs)

    if location:
        if frames_per_batch > 1:
            # TODO: TimeDistributed is incompatible with channels_first
            loc = TimeDistributed(Location2D(in_shape=input_shape))(norm)
        else:
            loc = Location2D(in_shape=input_shape)(norm)
        concat = Concatenate(axis=channel_axis)([norm, loc])
    else:
        concat = norm

    if frames_per_batch > 1:
        fixed_inputs = TimeDistributed(TensorProduct(required_channels))(concat)
    else:
        fixed_inputs = TensorProduct(required_channels)(concat)

    # force the input shape
    axis = 0 if K.image_data_format() == 'channels_first' else -1
    fixed_input_shape = list(input_shape)
    fixed_input_shape[axis] = required_channels
    fixed_input_shape = tuple(fixed_input_shape)

    model_kwargs = {
        'include_top': False,
        'weights': None,
        'input_shape': fixed_input_shape,
        'pooling': pooling
    }

    _, backbone_dict = get_backbone(backbone, fixed_inputs,
                                    use_imagenet=use_imagenet,
                                    frames_per_batch=frames_per_batch,
                                    return_dict=True, **model_kwargs)

    backbone_dict_reduced = {k: backbone_dict[k] for k in backbone_dict
                             if k in backbone_levels}
    ndim = 2 if frames_per_batch == 1 else 3
    pyramid_dict = create_pyramid_features(backbone_dict_reduced, ndim=ndim)

    features = [pyramid_dict[key] for key in pyramid_levels]    

    if frames_per_batch > 1:
        if temporal_mode in ['conv', 'lstm', 'gru']:
            temporal_features = [__merge_temporal_features(feature, mode=temporal_mode) for feature in features]
            for f, k in zip(temporal_features, pyramid_dict.keys()):
                pyramid_dict[k] = f

    semantic_levels = [int(re.findall(r'\d+', k)[0]) for k in pyramid_dict]
    target_level = min(semantic_levels)

    semantic_head_list = []
    for i in range(num_semantic_heads):
        semantic_head_list.append(create_semantic_head(
            pyramid_dict, n_classes=num_semantic_classes[i],
            input_target=inputs, target_level=target_level,
            semantic_id=i, ndim=ndim, **kwargs)) 
    
    outputs=semantic_head_list
    
    model = Model(inputs=inputs, outputs=outputs, name=name)
    return model