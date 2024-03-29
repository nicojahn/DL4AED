import pandas as pd
import logging
from prep_utils import *
from utils import update
import tensorflow_datasets as tfds

AUTOTUNE = tf.data.AUTOTUNE
''' The preprocessor class which does everything related to data (loading, preprocessing, etc.)'''

class Preprocessor():
    '''
    <insert docstring>
    '''
    def __init__(self, config):
        self._config = config
        self.logger = None        
        self.datasets = { key : None for key in self.available_modi}
        
    def get_config(self):
        return self._config
    
    def set_config(self, update_dict):
        self._config = update(self._config, update_dict)
        return self._config
    
    @tf.autograph.experimental.do_not_convert
    def load_fma(self):
        meta = pd.read_csv(self._config["fma_labels"])
        filename = meta["filename"].values
        labels = meta["label"].values
        ds = tf.data.Dataset.from_tensor_slices({"noise": filename, "label": labels})
        ds = ds.map(lambda x: get_waveform(x, self._config["fma_root"], self._config['desired_channels'], -1), num_parallel_calls=AUTOTUNE)
        self.datasets['fma'] = ds

    @tf.autograph.experimental.do_not_convert
    def load_data(self, download=False):
        # NOTE: data has to be downloaded first 
        dataset, self.ds_info = tfds.load(self._config['data_root'],
                                               with_info=True,
                                               data_dir=self._config["data_dir"],
                                               download=download,
                                               split=['train'])
        dataset = dataset[0]
        dataset_size = dataset.cardinality().numpy()
        train_size = int(self._config['train_size'] * dataset_size)
        val_size = int(self._config['val_size'] * dataset_size)
        test_size = int(self._config['test_size'] * dataset_size)
        
        # split data
        # TODO: check if the data is thoroughly mixed
        dataset = dataset.shuffle(dataset_size) # is it smart to do it this way?
        self.datasets["train"] = dataset.take(train_size)
        self.datasets["test"] = dataset.skip(train_size)
        self.datasets["val"] = self.datasets["test"].skip(test_size)
        self.datasets["test"] = self.datasets["test"].take(test_size)

        meta = pd.read_csv(self._config["noise_labels"], index_col="fname")
        labels = meta.label
        
        noise_classes = self._config["noise_classes"]
        noise_data = meta.loc[meta["label"].isin(noise_classes)].index.values

        # the "label" is not important as it is discarded later on
        noise_ds = tf.data.Dataset.from_tensor_slices({"noise": noise_data,
                                                        "label": [0]*len(noise_data)})

        self.datasets['noise'] = noise_ds.map(lambda x: get_waveform(x,
                                                                  self._config['noise_root'],
                                                                  self._config['desired_channels'],
                                                                  self._config['desired_samples']),
                                           num_parallel_calls=AUTOTUNE)
        
        if self.logger is not None:
            self.logger.info('dataset loaded')
    
    def create_logger(self, filepath='preprocessing.log', handler='file'):
        assert handler in ['file', 'stream'], 'handler can either be "file" or "stream"'
        logging.getLogger().handlers.clear()
        # create logger
        self.logger = logging.getLogger('preprocessing_logger')
        self.logger.setLevel(logging.DEBUG)
        # create console handler and set level to debug
        ch = logging.FileHandler(filepath)
        ch.setLevel(logging.DEBUG)
        # create formatter
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s') 
        # add formatter to ch
        ch.setFormatter(formatter)
        # add ch to logger
        self.logger.addHandler(ch)
        
        self.logger.propagate = False
        
        return self.logger
    
    def launch_trainval_pipeline(self, mode='train'):
        assert self.logger is not None
        assert mode in ["train", "val"]
        
        self.logger.info(f'started preprocessing and augmenting {mode} data')
        
        ds_s = self.datasets[mode]
        ds_s = ds_s.shuffle(self._config['shuffle_buffer_size'])

        ds_n = self.datasets["noise"].repeat().shuffle(self._config['shuffle_buffer_size'])
        
        # zip noise dataset
        ds = tf.data.Dataset.zip((ds_s, ds_n))
        
        # merge features into one featureDict
        ds = ds.map(lambda x, y: wrapper_merge_features(x, y), num_parallel_calls=AUTOTUNE)
        
        # make to length which the network understands (common_divider)
        ds = ds.map(lambda x: wrapper_shape_to_proper_length(x, self._config["common_divider"], clip=True), num_parallel_calls=AUTOTUNE)
        
        ds = ds.map(lambda x: wrapper_roll(x, self._config['roll_val']), num_parallel_calls=AUTOTUNE) # do we wanna roll? (yes)
        ds = ds.map(lambda x: wrapper_mix_noise(x, SNR=self._config['SNR']), num_parallel_calls=AUTOTUNE)
        ds = ds.map(lambda x: wrapper_mask(x, self._config['freq_mask'], self._config['time_mask'], self._config['param_db'], db=0), num_parallel_calls=AUTOTUNE)
        
        # extract tensors
        ds = ds.map(lambda x: wrapper_dict2tensor(x, features=['mel', 'label']), num_parallel_calls=AUTOTUNE)
        ds = ds.flat_map(lambda x,y: self.flatten(x,y))
        ds = ds.map(lambda x, y: (x, {"decoder": x, "classifier": y}), num_parallel_calls=AUTOTUNE)

        ds = ds.shuffle(self._config['shuffle_buffer_size'])
        ds = ds.batch(self._config['batch_size']).prefetch(AUTOTUNE)
        self.datasets[mode] = ds
        self.logger.info(f'done preprocessing and augmenting {mode} data')

    def launch_test_pipeline(self, mode="test"):
        assert self.logger is not None
        assert mode in ["test", "fma"]

        self.logger.info('started preprocessing and augmenting test data')
        
        ds = self.datasets[mode]
        ds = ds.map(lambda x: wrapper_shape_to_proper_length(x, self._config["common_divider"], clip=True, testing=True), num_parallel_calls=AUTOTUNE)

        # extract tensors
        ds = ds.map(lambda x: wrapper_dict2tensor(x, features=["mel", "label"]), num_parallel_calls=AUTOTUNE)
        # here we do an reshape of the image to subimages and replicate the label for all of them
        ds = ds.flat_map(lambda x,y: self.flatten(x,y))
        ds = ds.map(lambda x, y: (x, {"classifier": y}), num_parallel_calls=AUTOTUNE)

        ds = ds.batch(self._config["inference_batch_size"]).prefetch(AUTOTUNE)
        self.datasets[mode] = ds
        self.logger.info('done preprocessing and augmenting test data')
    
    def preprocess_wav(self, ds):
        ds = ds.map(lambda x: wrapper_cast_offline(x), num_parallel_calls=AUTOTUNE)
        ds = ds.map(lambda x: wrapper_rescale(x), num_parallel_calls=AUTOTUNE)        
        return ds

    def to_log_mel(self, ds):
        ds = ds.map(lambda x: wrapper_spect(x,
                                            self._config['nfft'],
                                            self._config['window'],
                                            self._config['stride'],
                                            self.logger),
                    num_parallel_calls=AUTOTUNE)
        ds = ds.map(lambda x: wrapper_mel(x,
                                          self._config['sample_rate'], self._config['mels'],
                                          self._config['fmin_mels'], self._config['fmax_mels'],
                                          self._config['top_db'], db=0, logger=self.logger),
                    num_parallel_calls=AUTOTUNE) # Convert to mel-spectrogram
        ds = ds.map(lambda x: wrapper_log_mel(x), num_parallel_calls=AUTOTUNE)
        ds = ds.map(lambda x: wrapper_normalize(x), num_parallel_calls=AUTOTUNE)
        ds = ds.map(lambda x: wrapper_extract_shape(x), num_parallel_calls=AUTOTUNE)
        
        return ds

    def offline_preprocessing(self, mode='val'):
        '''
        create spectrograms to spare processing time due to fourier transforms
        '''
        assert mode in self.available_modi
        
        ds = self.datasets[mode]
        
        # wav-level
        ds = self.preprocess_wav(ds)
        # spect-level
        ds = self.to_log_mel(ds)
        
        self.datasets[mode] = ds
        
        self.logger.info('done preprocessing and augmenting data')
    
    def save_mels(self, skip=[]):
        for mode in self.available_modi:
            if mode in skip:
                continue
            ds = self.datasets[mode]
            assert not ds is None
            
            # extract shape and save it with the samples
            ds = ds.map(lambda x: wrapper_extract_shape(x), num_parallel_calls=AUTOTUNE)
            ds = ds.map(lambda x: wrapper_pack(x), num_parallel_calls=AUTOTUNE)
            
            # saving
            tf.data.experimental.save(ds, f"{self._config['tf_dataset_save_path']}/{mode}/", compression=None, shard_func=None)
            self.datasets[mode] = ds
            
            self.logger.info(f'done saving data for {mode}')

    def load_mels(self, skip=[]):
        for mode in self.available_modi:
            if mode in skip:
                continue

            # loading
            ds = tf.data.experimental.load(f"{self._config['tf_dataset_save_path']}/{mode}/", (
                tf.TensorSpec(shape=(), dtype=tf.int32), # label
                tf.TensorSpec(shape=(None, None), dtype=tf.float32), # mel
                tf.TensorSpec(shape=(2), dtype=tf.int32), # shape
            ), compression=None, reader_func=None)
            ds = ds.map(lambda label, mel, shape: {"label":label, "mel":mel, "shape":shape}, num_parallel_calls=AUTOTUNE)

            self.datasets[mode] = ds.cache()
            
            self.logger.info(f'done loading data for {mode}')

    def flatten(self, x, y):
        x_ds = tf.data.Dataset.from_tensor_slices(tf.reshape(x, (-1, self._config["common_divider"], self._config["common_divider"], 1)))
        y_ds = tf.data.Dataset.from_tensor_slices(tf.reshape(y,(1,-1))).repeat()
        y_ds = y_ds.map(lambda y: tf.one_hot(y, self._config["num_classes"]))
        return tf.data.Dataset.zip((x_ds.flat_map(lambda x: tf.data.Dataset.from_tensor_slices([x])), y_ds))

    @property
    def available_modi(self):
        return ['train', 'val', 'test', 'noise', 'fma']
    
    @property
    def train_ds(self):
        return self.datasets['train']
    
    @property
    def val_ds(self):
        return self.datasets['val']
    
    @property
    def test_ds(self):
        return self.datasets['test']
    
    @property
    def noise_ds(self):
        return self.datasets['noise']
    
    @property
    def fma_ds(self):
        return self.datasets['fma']