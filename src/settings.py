# coding=utf-8

"""
This file is used to store project-wide global values.
"""

MAX_NUMBER_OF_JOBS = 32
DATA_GENERATION_THREADS_PER_PROCESS = 4
LOG_RUSAGE = True
LOG_RUSAGE_INTERVAL = 2000  # How many steps between RUSAGE logs

# Enables various debug prints and saving of batch images etc.
DEBUG = False

# Enables profiling information to be shown - runs a shorter run for profiling
PROFILE = False
PROFILE_STEPS_PER_EPOCH = 3
PROFILE_NUM_EPOCHS = 1

DEFAULT_IMAGE_DATA_FORMAT = 'channels_last'
DEFAULT_NUMPY_FLOAT_DTYPE = 'float32'