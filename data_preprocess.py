# -*- coding: utf-8 -*-
"""Data_preprocess.ipynb

Automatically generated by Colaboratory.


# DATA
"""

import numpy as np
import glob
import random
import struct
import csv
from tensorflow.core.example import example_pb2
import tensorflow as tf

from threading import Thread
from queue import Queue
import time
import threading

"""## Vocabulary"""

SENTENCE_START  = '<s>'
SENTENCE_END = '</s>'

PAD_TOKEN = '[PAD]'
UNKNOWN_TOKEN = '[UNK]'
START_DECODING = '[START]'
STOP_DECODING = '[STOP]'

class Vocab:
  
  def __init__(self, vocab_file, max_size):
    
    self.word2id = {UNKNOWN_TOKEN : 0, PAD_TOKEN : 1, START_DECODING : 2, STOP_DECODING : 3}
    self.id2word = {0 : UNKNOWN_TOKEN, 1 : PAD_TOKEN, 2 : START_DECODING, 3 : STOP_DECODING}
    self.count = 4
    
    with open(vocab_file, 'r') as f:
      for line in f:
        pieces = line.split()
        if len(pieces) != 2 :
          print('Warning : incorrectly formatted line in vocabulary file : %s\n' % line)
          continue
          
        w = pieces[0]
        if w in [SENTENCE_START, SENTENCE_END, UNKNOWN_TOKEN, PAD_TOKEN, START_DECODING, STOP_DECODING]:
          raise Exception('<s>, </s>, [UNK], [PAD], [START] and [STOP] shouldn\'t be in the vocab file, but %s is' % w)
        
        if w in self.word2id:
          raise Exception('Duplicated word in vocabulary file: %s' % w)
        
        self.word2id[w] = self.count
        self.id2word[self.count] = w
        self.count += 1
        if max_size != 0 and self.count >= max_size:
          print("max_size of vocab was specified as %i; we now have %i words. Stopping reading." % (max_size, self.count))
          break

    print("Finished constructing vocabulary of %i total words. Last word added: %s" % (self.count, self.id2word[self.count-1]))

      
  def word_to_id(self, word):
    if word not in self.word2id:
      return self.word2id[UNKNOWN_TOKEN]
    return self.word2id[word]
  
  def id_to_word(self, word_id):
    if word_id not in self.id2word:
      raise ValueError('Id not found in vocab: %d' % word_id)
    return self.id2word[word_id]
  
  def size(self):
    return self.count



"""## Data helpers"""

def article_to_ids(article_words, vocab):
  ids = []
  oovs = []
  unk_id = vocab.word_to_id(UNKNOWN_TOKEN)
  for w in article_words:
    i = vocab.word_to_id(w)
    if i == unk_id: # If w is OOV
      if w not in oovs: # Add to list of OOVs
        oovs.append(w)
      oov_num = oovs.index(w) # This is 0 for the first article OOV, 1 for the second article OOV...
      ids.append(vocab.size() + oov_num) # This is e.g. 50000 for the first article OOV, 50001 for the second...
    else:
      ids.append(i)
  return ids, oovs


def abstract_to_ids(abstract_words, vocab, article_oovs):
  ids = []
  unk_id = vocab.word_to_id(UNKNOWN_TOKEN)
  for w in abstract_words:
    i = vocab.word_to_id(w)
    if i == unk_id: # If w is an OOV word
      if w in article_oovs: # If w is an in-article OOV
        vocab_idx = vocab.size() + article_oovs.index(w) # Map to its temporary article OOV number
        ids.append(vocab_idx)
      else: # If w is an out-of-article OOV
        ids.append(unk_id) # Map to the UNK token id
    else:
      ids.append(i)
  return ids



def output_to_words(id_list, vocab, article_oovs):
  words = []
  for i in id_list:
    try:
      w = vocab.id_to_word(i) # might be [UNK]
    except ValueError as e: # w is OOV
      assert article_oovs is not None, "Error: model produced a word ID that isn't in the vocabulary. This should not happen in baseline (no pointer-generator) mode"
      article_oov_idx = i - vocab.size()
      try:
        w = article_oovs[article_oov_idx]
      except ValueError as e: # i doesn't correspond to an article oov
        raise ValueError('Error: model produced word ID %i which corresponds to article OOV %i but this example only has %i article OOVs' % (i, article_oov_idx, len(article_oovs)))
    words.append(w)
  return words



def abstract_to_sents(abstract):
  """Splits abstract text from datafile into list of sentences.
  Args:
    abstract: string containing <s> and </s> tags for starts and ends of sentences
  Returns:
    sents: List of sentence strings (no tags)"""
  cur = 0
  sents = []
  while True:
    try:
      start_p = abstract.index(SENTENCE_START, cur)
      end_p = abstract.index(SENTENCE_END, start_p + 1)
      cur = end_p + len(SENTENCE_END)
      sents.append(abstract[start_p+len(SENTENCE_START):end_p])
    except ValueError as e: # no more sentences
      return sents



def example_generator(data_path, hpm):
  while True:
    filelist = glob.glob(data_path) # get the list of datafiles
    assert filelist, ('Error: Empty filelist at %s' % data_path) # check filelist isn't empty
    if hpm['singlepass']:
      filelist = sorted(filelist)
    else:
      random.shuffle(filelist)
    for f in filelist:
      reader = open(f, 'rb')
      while True:
        len_bytes = reader.read(8)
        if not len_bytes: break # finished reading this file
        str_len = struct.unpack('q', len_bytes)[0]
        example_str = struct.unpack('%ds' % str_len, reader.read(str_len))[0]
        yield example_pb2.Example.FromString(example_str)
    if hpm['singlepass'] or hpm['finished']:
      print("example_generator completed reading all datafiles. No more data.")
      break



"""# Batcher"""

class Example(object):
  """Class representing a train/val/test example for text summarization."""
  def __init__(self, article, abstract_sentences, vocab, hpm):
    """Initializes the Example, performing tokenization and truncation to produce the encoder, decoder and target sequences, which are stored in self.
    Args:
      article: source text; a string. each token is separated by a single space.
      abstract_sentences: list of strings, one per abstract sentence. In each sentence, each token is separated by a single space.
      vocab: Vocabulary object
      hps: hyperparameters
    """
    self.hpm = hpm

    # Get ids of special tokens
    start_decoding = vocab.word_to_id(START_DECODING)
    stop_decoding = vocab.word_to_id(STOP_DECODING)

    # Process the article
    article_words = article.split()
    if len(article_words) > hpm['max_enc_len']:
      article_words = article_words[:hpm['max_enc_len']]
    self.enc_len = len(article_words) # store the length after truncation but before padding
    self.enc_input = [vocab.word_to_id(w) for w in article_words] # list of word ids; OOVs are represented by the id for UNK token

    # Process the abstract
    abstract = ' '.join(abstract_sentences) # string
    abstract_words = abstract.split() # list of strings
    abs_ids = [vocab.word_to_id(w) for w in abstract_words] # list of word ids; OOVs are represented by the id for UNK token

    # Get the decoder input sequence and target sequence
    self.dec_input, self.target = self.get_dec_inp_targ_seqs(abs_ids, hpm['max_dec_len'], start_decoding, stop_decoding)
    self.dec_len = len(self.dec_input)

    # If using pointer-generator mode, we need to store some extra info
    if hpm['pointer_gen']:
      # Store a version of the enc_input where in-article OOVs are represented by their temporary OOV id; also store the in-article OOVs words themselves
      self.enc_input_extend_vocab, self.article_oovs = article_to_ids(article_words, vocab)

      # Get a verison of the reference summary where in-article OOVs are represented by their temporary article OOV id
      abs_ids_extend_vocab = abstract_to_ids(abstract_words, vocab, self.article_oovs)

      # Overwrite decoder target sequence so it uses the temp article OOV ids
      _, self.target = self.get_dec_inp_targ_seqs(abs_ids_extend_vocab, hpm['max_dec_len'], start_decoding, stop_decoding)

    # Store the original strings
    self.original_article = article
    self.original_abstract = abstract
    self.original_abstract_sents = abstract_sentences


  def get_dec_inp_targ_seqs(self, sequence, max_len, start_id, stop_id):
    """Given the reference summary as a sequence of tokens, return the input sequence for the decoder, and the target sequence which we will use to calculate loss. The sequence will be truncated if it is longer than max_len. The input sequence must start with the start_id and the target sequence must end with the stop_id (but not if it's been truncated).
    Args:
      sequence: List of ids (integers)
      max_len: integer
      start_id: integer
      stop_id: integer
    Returns:
      inp: sequence length <=max_len starting with start_id
      target: sequence same length as input, ending with stop_id only if there was no truncation
    """
    inp = [start_id] + sequence[:]
    target = sequence[:]
    if len(inp) > max_len: # truncate
      inp = inp[:max_len]
      target = target[:max_len] # no end_token
    else: # no truncation
      target.append(stop_id) # end token
    assert len(inp) == len(target)
    return inp, target


  def pad_decoder_inp_targ(self, max_len, pad_id):
    """Pad decoder input and target sequences with pad_id up to max_len."""
    while len(self.dec_input) < max_len:
      self.dec_input.append(pad_id)
    while len(self.target) < max_len:
      self.target.append(pad_id)


  def pad_encoder_input(self, max_len, pad_id):
    """Pad the encoder input sequence with pad_id up to max_len."""
    while len(self.enc_input) < max_len:
      self.enc_input.append(pad_id)
    if self.hpm['pointer_gen']:
      while len(self.enc_input_extend_vocab) < max_len:
        self.enc_input_extend_vocab.append(pad_id)




class Batch(object):
  """Class representing a minibatch of train/val/test examples for text summarization."""

  def __init__(self, example_list, hpm, vocab):
    """Turns the example_list into a Batch object.
    Args:
       example_list: List of Example objects
       hpm: hyperparameters
       vocab: Vocabulary object
    """
    self.pad_id = vocab.word_to_id(PAD_TOKEN) # id of the PAD token used to pad sequences
    self.init_encoder_seq(example_list, hpm) # initialize the input to the encoder
    self.init_decoder_seq(example_list, hpm) # initialize the input and targets for the decoder
    self.store_orig_strings(example_list) # store the original strings

  def init_encoder_seq(self, example_list, hpm):
    """Initializes the following:
        self.enc_batch:
          numpy array of shape (batch_size, <=max_enc_steps) containing integer ids (all OOVs represented by UNK id), padded to length of longest sequence in the batch
        self.enc_lens:
          numpy array of shape (batch_size) containing integers. The (truncated) length of each encoder input sequence (pre-padding).
        self.enc_padding_mask:
          numpy array of shape (batch_size, <=max_enc_steps), containing 1s and 0s. 1s correspond to real tokens in enc_batch and target_batch; 0s correspond to padding.
      If hps.pointer_gen, additionally initializes the following:
        self.max_art_oovs:
          maximum number of in-article OOVs in the batch
        self.art_oovs:
          list of list of in-article OOVs (strings), for each example in the batch
        self.enc_batch_extend_vocab:
          Same as self.enc_batch, but in-article OOVs are represented by their temporary article OOV number.
    """
    # Determine the maximum length of the encoder input sequence in this batch
    max_enc_seq_len = max([ex.enc_len for ex in example_list])

    # Pad the encoder input sequences up to the length of the longest sequence
    for ex in example_list:
      ex.pad_encoder_input(max_enc_seq_len, self.pad_id)

    # Initialize the numpy arrays
    # Note: our enc_batch can have different length (second dimension) for each batch because we use dynamic_rnn for the encoder.
    self.enc_batch = np.zeros((hpm['batch_size'], max_enc_seq_len), dtype=np.int32)
    self.enc_lens = np.zeros((hpm['batch_size']), dtype=np.int32)
    self.enc_padding_mask = np.zeros((hpm['batch_size'], max_enc_seq_len), dtype=np.float32)

    # Fill in the numpy arrays
    for i, ex in enumerate(example_list):
      self.enc_batch[i, :] = ex.enc_input[:]
      self.enc_lens[i] = ex.enc_len
      for j in range(ex.enc_len):
        self.enc_padding_mask[i][j] = 1

    # For pointer-generator mode, need to store some extra info
    if hpm['pointer_gen']:
      # Determine the max number of in-article OOVs in this batch
      self.max_art_oovs = max([len(ex.article_oovs) for ex in example_list])
      # Store the in-article OOVs themselves
      self.art_oovs = [ex.article_oovs for ex in example_list]
      # Store the version of the enc_batch that uses the article OOV ids
      self.enc_batch_extend_vocab = np.zeros((hpm['batch_size'], max_enc_seq_len), dtype=np.int32)
      for i, ex in enumerate(example_list):
        self.enc_batch_extend_vocab[i, :] = ex.enc_input_extend_vocab[:]

  def init_decoder_seq(self, example_list, hpm):
    """Initializes the following:
        self.dec_batch:
          numpy array of shape (batch_size, max_dec_steps), containing integer ids as input for the decoder, padded to max_dec_steps length.
        self.target_batch:
          numpy array of shape (batch_size, max_dec_steps), containing integer ids for the target sequence, padded to max_dec_steps length.
        self.dec_padding_mask:
          numpy array of shape (batch_size, max_dec_steps), containing 1s and 0s. 1s correspond to real tokens in dec_batch and target_batch; 0s correspond to padding.
        """
    # Pad the inputs and targets
    for ex in example_list:
      ex.pad_decoder_inp_targ(hpm['max_dec_len'], self.pad_id)

    # Initialize the numpy arrays.
    # Note: our decoder inputs and targets must be the same length for each batch (second dimension = max_dec_steps) because we do not use a dynamic_rnn for decoding. However I believe this is possible, or will soon be possible, with Tensorflow 1.0, in which case it may be best to upgrade to that.
    self.dec_batch = np.zeros((hpm['batch_size'], hpm['max_dec_len']), dtype=np.int32)
    self.target_batch = np.zeros((hpm['batch_size'], hpm['max_dec_len']), dtype=np.int32)
    self.dec_padding_mask = np.zeros((hpm['batch_size'], hpm['max_dec_len']), dtype=np.float32)

    # Fill in the numpy arrays
    for i, ex in enumerate(example_list):
      self.dec_batch[i, :] = ex.dec_input[:]
      self.target_batch[i, :] = ex.target[:]
      for j in range(ex.dec_len):
        self.dec_padding_mask[i][j] = 1

  def store_orig_strings(self, example_list):
    """Store the original article and abstract strings in the Batch object"""
    self.original_articles = [ex.original_article for ex in example_list] # list of lists
    self.original_abstracts = [ex.original_abstract for ex in example_list] # list of lists
    self.original_abstracts_sents = [ex.original_abstract_sents for ex in example_list] # list of list of lists




class Batcher():
  
  def __init__(self,data_path, hpm, vocab):
    self.hpm = hpm
    self.vocab = vocab
    self.max_examples_buffer_len = hpm['examples_max_buffer_len']
    self.max_batch_buffer_len = hpm['batch_max_buffer_len']
    self.max_batch_bucket_len = hpm['max_batch_bucket_len']
    self.gen = self.thread_safe_generator(self.generator(example_generator(data_path, hpm)))
    self.num_fill_examples_threads = 4
    self.num_fill_batches_threads = 4
    self.elements_queue = Queue(self.max_examples_buffer_len)
    self.batch_queue = Queue(self.max_batch_buffer_len)
    self.launch_watch_threads()
  
  
  class thread_safe_generator(object):
    def __init__(self, gen):
        self.gen = gen
        self.lock = threading.Lock()

    def __next__(self):
        with self.lock:
            return next(self.gen)
          
          
  def generator(self, example_gen):
    while True :
      e = next(example_gen)
      try:
        article_text = e.features.feature['article'].bytes_list.value[0].decode()
        abstract_text = e.features.feature['abstract'].bytes_list.value[0].decode()
      except ValueError:
        tf.logging.error('Failed to get article or abstract from example')
        continue
      if len(article_text) == 0   :
        tf.logging.warning('Found an example with empty article text. Skipping it.')
        
      else:
        yield (article_text, abstract_text)
      

     
  def fill_examples_queue(self):
    while True:
        try:
          article, abstract = next(self.gen)
          abst = [sent.strip() for sent in abstract_to_sents(abstract)]
          ex = Example(article, abst,self.vocab, self.hpm)
          self.elements_queue.put(ex)
        except  :
          break
          
          
          
  def fill_batch_queue(self):
    while True:
      try:
        if not self.hpm['decode']:
          batch = []
          for _ in range(self.hpm['batch_size']*self.hpm['max_batch_bucket_len']):
            batch.append(self.elements_queue.get())

          batch = sorted(batch, key=lambda x : x.enc_len)
          batches= []
          i = 0
          while i+self.hpm['batch_size'] <= len(batch):
            batches.append(batch[i:i+self.hpm['batch_size']])
            i = i + self.hpm['batch_size']

          if i < len(batch):
            batches.append(batch[i:len(batch)])

          if not self.hpm['singlepass']:
            random.shuffle(batches)

          for b in batches:
            # here again we crete batch object before doing pushing it to the batch queue
            self.batch_queue.put(Batch(b, self.hpm, self.vocab))
        else:
          ex = self.elements_queue.get()
          b = [ex for _ in range(self.hpm['batch_size'])]
          self.batch_queue.put(Batch(b, self.hpm, self.vocab))

      except  :
        break
       
  def launch_watch_threads(self):
    
    self.elements_queue_threads = []
    for i in range(self.num_fill_examples_threads):
      self.elements_queue_threads.append(Thread(target=self.fill_examples_queue))
      self.elements_queue_threads[-1].setDaemon(True)
      self.elements_queue_threads[-1].start()


    self.batch_queue_threads = []
    for j in range(self.num_fill_batches_threads):
      self.batch_queue_threads.append(Thread(target = self.fill_batch_queue))
      self.batch_queue_threads[-1].setDaemon(True)
      self.batch_queue_threads[-1].start()
      
      
    def watch():
      while True:
        time.sleep(60)
        for id, t in enumerate(self.elements_queue_threads):
          if not t.is_alive() :
            print("thread dead")
            new_t = Thread(target = self.fill_batch_queue)
            self.elements_queue_threads[id] = new_t
            new_t.daemon = True
            new_t.start()

        for id, t in enumerate(self.batch_queue_threads):
          if not t.is_alive() :
            print("batch thread dead")
            new_t = Thread(target=self.fill_batch_queue)
            self.batch_queue_threads[id] = new_t
            new_t.setDaemon(True)
            new_t.start()

    if not self.hpm['singlepass']  : 
      self.watcher = Thread(target = watch)
      self.watcher.setDaemon(True)
      self.watcher.start()

    
    
    
  def next_batch(self):
    
    if self.batch_queue.qsize() ==0:
      tf.logging.warning('Bucket input queue is empty when calling next_batch. Bucket queue size: %i, Input queue size: %i', self.batch_queue.qsize(), self.elements_queue.qsize())
      if self.hpm['singlepass'] or self.hpm['finished']:
        tf.logging.info("Finished reading dataset in single_pass mode.")
        return None
    return self.batch_queue.get()
