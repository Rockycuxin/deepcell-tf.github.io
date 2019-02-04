# ==============================================================================
# Functions for testing convolutional GRU
# 
# Referenced from Interior-Edge Segmentation 3D Fully Convolutional.ipynb
# ==============================================================================


from __future__ import absolute_import
from __future__ import print_function
from __future__ import division

import datetime
import os
import sys
import errno

path = sys.path[0]
parentdir = path.replace("scripts/recurr_gru","")
sys.path.insert(0,parentdir) 

import deepcell
from deepcell import losses
from deepcell import image_generators
from deepcell import model_zoo
from deepcell.utils.data_utils import get_data
from deepcell.scripts.recurr_gru.train import feature_net_3D

import numpy as np
from skimage.measure import label
from skimage import morphology
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.animation as animation

from tensorflow.python.client import device_lib
print(device_lib.list_local_devices())


# ==============================================================================
# Initialize new model
# ==============================================================================

test_size = .10  # % of data saved as test
receptive_field = 61  # should be adjusted for the scale of the data

data_filename = 'nuclear_movie_hela0-7_same.npz'
train_dict, test_dict = get_data(data_filename, test_size=0.2)

MODEL_DIR = os.path.join(sys.path[0], 'scripts/recurr_gru/models')

conv_gru_model_name = 'conv_gru_model'
fgbg_model_name = 'conv_fgbg_model'

fgbg_weights_file = os.path.join(MODEL_DIR, '{}.h5'.format(fgbg_model_name))
conv_gru_weights_file = os.path.join(MODEL_DIR, '{}.h5'.format(conv_gru_model_name))


run_fgbg_model = feature_net_3D(
    input_shape=tuple(X_test.shape[1:]),
    receptive_field=receptive_field,
    n_features=2, 
    n_frames=frames_per_batch,
    n_conv_filters=32,
    n_dense_filters=128,
    norm_method=norm_method)
run_fgbg_model.load_weights(fgbg_weights_file)


run_conv_model = model_zoo.feature_net_3D(
    input_shape=tuple(X_test.shape[1:]),
    model=run_fgbg_model,
    receptive_field=receptive_field,
    n_skips=n_skips,
    n_features=4,  # (background edge, interior edge, cell interior, background)
    n_frames=frames_per_batch,
    n_conv_filters=32,
    n_dense_filters=128,
    norm_method=norm_method)
run_conv_model.load_weights(conv_weights_file)


X_test, y_test = test_dict['X'][:4], test_dict['y'][:4]
print("X_test.shape: ", X_test.shape)

test_images = run_conv_model.predict(X_test)[-1]
test_images_fgbg = run_fgbg_model.predict(X_test)[-1]

print('edge/interior prediction shape:', test_images.shape)
print('fgbg mask shape:', test_images_fgbg.shape)

# ==============================================================================
# Post processing
# ==============================================================================

# threshold the foreground/background
# and remove back ground from edge transform
threshold = 0.9

fg_thresh = test_images_fgbg[..., 1] > threshold
fg_thresh = np.expand_dims(fg_thresh, axis=-1)

test_images_post_fgbg = test_images * fg_thresh


# Label interior predictions

labeled_images = []
for i in range(test_images_post_fgbg.shape[0]):
    interior = test_images_post_fgbg[i, ..., 2] > .2
    labeled_image = label(interior)
    labeled_image = morphology.remove_small_objects(
        labeled_image, min_size=50, connectivity=1)
    labeled_images.append(labeled_image)
labeled_images = np.array(labeled_images)
labeled_images = np.expand_dims(labeled_images, axis=-1)

print('labeled_images shape:', labeled_images.shape)


# ==============================================================================
# Plot the results
# ==============================================================================

index = np.random.randint(low=0, high=labeled_images.shape[0])
frame = np.random.randint(low=0, high=labeled_images.shape[1])

fig, axes = plt.subplots(ncols=3, nrows=2, figsize=(15, 15), sharex=True, sharey=True)
ax = axes.ravel()

ax[0].imshow(X_test[index, frame, ..., 0])
ax[0].set_title('Source Image')

ax[1].imshow(test_images_fgbg[index, frame, ..., 1])
ax[1].set_title('FGBG Prediction')

ax[2].imshow(fg_thresh[index, frame, ..., 0], cmap='jet')
ax[2].set_title('FGBG Threshold {}%'.format(threshold * 100))

ax[3].imshow(test_images[index, frame, ..., 0] + test_images[index, frame, ..., 1], cmap='jet')
ax[3].set_title('Edge Prediction')

ax[4].imshow(test_images[index, frame, ..., 2], cmap='jet')
ax[4].set_title('Interior Prediction')

ax[5].imshow(labeled_images[index, frame, ..., 0], cmap='jet')
ax[5].set_title('Instance Segmentation')

fig.tight_layout()
plt.show()
plt.savefig('predictions.png')





