import tensorflow as tf
import tensorflow_datasets as tfds
import tensorflow_io as tfio
from tensorflow import keras
import tensorflow_probability as tfp 

import os
import math

import numpy as np
import matplotlib.pyplot as plt
import pandas as pd
import random

#============================== PREPROCESSING ==============================

def wrapper_cast(x):
    x['audio'] = tf.cast(x['audio'], tf.float32)# / 32768.0
    x['noise_wav'] = tf.cast(x['noise_wav'], tf.float32)# / 32768.0
    return x

def wrapper_cast_label(x):
    x['label'] = tf.cast(x['label'], tf.int64)
    return x

def wrapper_spect(x, nfft, window, stride, light=True):
    x['spectrogram'] = tfio.experimental.audio.spectrogram(x['input'],
                                                           nfft=nfft,
                                                           window=window,
                                                           stride=stride)
#     x['spectrogram_sq'] = tfio.experimental.audio.spectrogram(x['input'],
#                                                            nfft=config['nfft'],
#                                                            window=config['window'],
#                                                            stride=config['stride'],
#                                                            magnitude_squared=True)
    x.pop('input')
    return x

def wrapper_mel(x, sample_rate, mels, fmin_mels, fmax_mels, top_db, db=False, light=True):
    x['mel'] = tfio.experimental.audio.melscale(x['spectrogram'],
                                                rate=sample_rate,
                                                mels=mels,
                                                fmin=fmin_mels,
                                                fmax=fmax_mels)
    if db: #to be implemented with noise
        x['db_mel'] = tfio.experimental.audio.dbscale(x['mel'], top_db=top_db)
    
    x.pop('spectrogram')
    return x

def cut_15(signal):
#     start = tfd.Categorical([1]*661000).sample(int(1))
    start = np.random.randint(0, int(len(signal)/2))
    signal = signal[start:start+int(len(signal)/2)]
    return signal

def wrapper_cut_15(x):
    out = tf.py_function(cut_15, [x['audio']], [tf.float32])
    x['audio'] = tf.squeeze(out)
    return x

#============================== AUGMENTATION ==============================

def wrapper_fade(x, fade):
    x['audio'] = tfio.experimental.audio.fade(x['audio'], fade_in=fade, fade_out=fade, mode="logarithmic")
    return x

def wrapper_trim(x, epsilon):
    position = tfio.experimental.audio.trim(x['audio'], axis=0, epsilon=epsilon)
    start = position[0]
    stop = position[1]
    x['audio'] = x['audio'][start:stop]
    return x

def wrapper_mask(x, freq_mask, time_mask, param_db, db=False):
    # freq masking
    x['mel'] = tfio.experimental.audio.freq_mask(x['mel'],
                                                 param=freq_mask)
    # Time masking
    x['mel'] = tfio.experimental.audio.time_mask(x['mel'],
                                                 param=time_mask)
    if db:
        x['db_mel'] = tfio.experimental.audio.freq_mask(x['db_mel'], param=param_db)
        x['db_mel'] = tfio.experimental.audio.time_mask(x['db_mel'], param=param_db)
    return x
    
    

def wrapper_roll(x, roll_val):
    x['mel'] = tf.roll(x['mel'], 
                       tf.random.uniform((), minval=-roll_val, maxval=roll_val, dtype=tf.dtypes.int32),
                       axis=1)
    return x
    


def pad_noise(signal, noise):
    if len(signal)>len(noise):
        _, noise = tf.keras.preprocessing.sequence.pad_sequences([signal[:int(len(signal)/4)], noise],
                                                                  maxlen=None,
                                                                  dtype='float32',
                                                                  padding='pre',
                                                                  truncating='pre',
                                                                  value=0.0)
    signal, noise = tf.keras.preprocessing.sequence.pad_sequences([signal, noise],
                                                                  maxlen=None,
                                                                  dtype='float32',
                                                                  padding='post',
                                                                  truncating='pre',
                                                                  value=0.0)
    return [signal, noise]

def wrapper_pad_noise(x):
    out = tf.py_function(pad_noise, [x['audio'], x['noise_wav']], [tf.float32, tf.float32])
    x['audio'], x['noise_wav'] = out
    return x

def get_noise_from_sound(signal,noise,SNR):
    RMS_s=math.sqrt(np.mean(signal**2))
    #required RMS of noise
    RMS_n=math.sqrt(RMS_s**2/(pow(10,SNR/10)))
    
    #current RMS of noise
    RMS_n_current=math.sqrt(np.mean(noise**2))
    noise=noise*(RMS_n/RMS_n_current)
    
    return noise

def wrapper_mix_noise(x, SNR):
    out = tf.py_function(get_noise_from_sound,
                         [x['audio'], x['noise_wav'], SNR],
                         [tf.float32])
    x['input'] = tf.squeeze(out)+x['audio']
    x.pop('audio')
    x.pop('noise_wav')
    return x

def wrapper_merge_features(ds1, ds2):
    ds1.update(ds2)
    return ds1

def wrapper_mfcc(x):
    x['mfcc'] = tf.signal.mfccs_from_log_mel_spectrograms(x['mel'])
    
    return x

def wrapper_log_mel(x):
    x['mel'] = tf.math.log(1 + x['mel'])
    
    return x

def pitch_shift_data(wave_data, shift_val, bins_per_octave):

    wave_data = wave_data.numpy()
    random_shift = np.random.randint(low=-shift_val, high=shift_val)
    wave_data = librosa.effects.pitch_shift(wave_data, sample_rate, 
                                            random_shift, bins_per_octave=bins_per_octave)
    return wave_data

def wrapper_change_pitch(x, shift_val, bins_per_octave):
    out = tf.py_function(pitch_shift_data, [x['audio'], shift_val, bins_per_octave], tf.float32)
    x['audio'] = out
    return x

def get_waveform(x, noise_root, sample_rate):
    audio_binary = tf.io.read_file(noise_root+os.sep+x['noise'])
    audio, _ = tf.audio.decode_wav(audio_binary,
                                   desired_channels=1,
                                   desired_samples=sample_rate)
    audio = tf.squeeze(audio, axis=-1)
    
    return {'noise_wav': audio,
            'noise_label': x['label'],
            'rate': sample_rate}