# -*- coding: utf-8 -*-
"""utils.ipynb

Automatically generated by Colaboratory.

"""

import numpy as np
import random
import tensorflow as tf
import tensorflow.nn as nn


class Linear():
  '''Class of object that apply a linear transformation of a 3D or 2D tensor'''
  '''
    Args : 
          output_size : Integer. The final size of the last dimension of the tensor after linear transformation
          bias : Boolean. If true, we add a bias vector to the tensor after linear transformation
          name : String. Name of the parameters
          init : Initializer object for the weight parameters
  '''
  def __init__(self, output_size, bias, name, init=None):
    self.output_size = output_size
    self.bias = bias
    self.name = name
    self.init = init
  
  '''The call method to apply a linear tranformation when we call the Linear object (see the method linear below)'''
  def __call__(self, inp):
    return self.linear(inp)

  ''' Method for the linear transformation '''
  def linear(self, inp):
    '''
      Args:
          inp : 2D or 3D tensor
      Returns:
          a tensor with the same shape as the input, except the last dimension which equals output_size
    '''
    inp_shape = inp.get_shape().as_list() # list of the dimensions of the input tensor

    weights= tf.get_variable(name = "w_"+self.name, shape =[inp_shape[-1], self.output_size], initializer=self.init) # weight w : shape = [<inp_last_old_dim>, output_size]
    if self.bias:
      biais = tf.get_variable(name="b_"+self.name, shape = self.output_size, initializer=self.init) # bias : shape = [output_size]
    else:
      biais = 0

    if len(inp_shape) == 2:
      return tf.matmul(inp, weights)+biais
    elif len(inp_shape) == 3:
      inp2 = tf.reshape(inp, [-1, inp_shape[-1]])
      out = tf.matmul(inp2, weights)+biais
      return tf.reshape(out, [inp_shape[0], -1, self.output_size])
    else:
      raise Exception("3D or 2D tensors waited !!!") # we raise an exception if the the tensor is not a 2D or 3D tensor



def apply_mask_normalize( vec, mask):
  """ Applies mask to values and normalize them 
      Args:
        vec : a list length max_dec_steps containing arrays shape :  [batch_size, <array_dim>]
  """
  v = tf.multiply(vec, tf.cast(mask, tf.float32))
  return tf.divide(v, tf.reduce_sum(v,axis=1, keepdims=True))
 
  
         
def _mask_and_avg( values, padding_mask):
  """Applies mask to values then returns overall average (a scalar)
  Args:
    values: a list length max_dec_steps containing arrays shape (batch_size).
    padding_mask: tensor shape (batch_size, max_dec_steps) containing 1s and 0s.
    
  Returns:
    a scalar
  """
  dec_lens = tf.reduce_sum(padding_mask, axis=1) # shape batch_size. float32
  values_per_step = [v * padding_mask[:,dec_step] for dec_step,v in enumerate(values)]
  values_per_ex = sum(values_per_step)/dec_lens # shape (batch_size); normalized value for each batch member
  return tf.reduce_mean(values_per_ex) # overall average
  
  
  
  
def _calc_final_dist( _enc_batch_extend_vocab, vocab_dists, attn_dists, p_gens, batch_oov_len, hpm):
  """Calculate the final distribution, for the pointer-generator model
  
  Args:
    vocab_dists: The vocabulary distributions. List length max_dec_steps of (batch_size, vsize) arrays. The words are in the order they appear in the vocabulary file.
    attn_dists: The attention distributions. List length max_dec_steps of (batch_size, attn_len) arrays
  
  Returns:
    final_dists: The final distributions. List length max_dec_steps of (batch_size, extended_vsize) arrays.
  """
  with tf.variable_scope('final_distribution'):
    """# Multiply vocab dists by p_gen and attention dists by (1-p_gen)
                vocab_dists = [ dist for (p_gen,dist) in zip(p_gens, vocab_dists)]
                attn_dists = [ dist for (p_gen,dist) in zip(p_gens, attn_dists)]"""

    # Concatenate some zeros to each vocabulary dist, to hold the probabilities for in-article OOV words
    extended_vsize = hpm['vocab_size'] + batch_oov_len # the maximum (over the batch) size of the extended vocabulary
    extra_zeros = tf.zeros((hpm['batch_size'], batch_oov_len ))
    vocab_dists_extended = [tf.concat(axis=1, values=[dist, extra_zeros]) for dist in vocab_dists] # list length max_dec_steps of shape (batch_size, extended_vsize)

    # Project the values in the attention distributions onto the appropriate entries in the final distributions
    # This means that if a_i = 0.1 and the ith encoder word is w, and w has index 500 in the vocabulary, then we add 0.1 onto the 500th entry of the final distribution
    # This is done for each decoder timestep.
    # This is fiddly; we use tf.scatter_nd to do the projection
    batch_nums = tf.range(0, limit=hpm['batch_size']) # shape (batch_size)
    batch_nums = tf.expand_dims(batch_nums, 1) # shape (batch_size, 1)
    attn_len = tf.shape(_enc_batch_extend_vocab)[1] # number of states we attend over
    batch_nums = tf.tile(batch_nums, [1, attn_len]) # shape (batch_size, attn_len)
    indices = tf.stack( (batch_nums, _enc_batch_extend_vocab), axis=2) # shape (batch_size, enc_t, 2)
    shape = [hpm['batch_size'], extended_vsize]
    attn_dists_projected = [tf.scatter_nd(indices, copy_dist, shape) for copy_dist in attn_dists] # list length max_dec_steps (batch_size, extended_vsize)

    # Add the vocab distributions and the copy distributions together to get the final distributions
    # final_dists is a list length max_dec_steps; each entry is a tensor shape (batch_size, extended_vsize) giving the final distribution for that decoder timestep
    # Note that for decoder timesteps and examples corresponding to a [PAD] token, this is junk - ignore.
    final_dists = [p_gen * vocab_dist + (1-p_gen) * copy_dist for (vocab_dist,copy_dist, p_gen) in zip(vocab_dists_extended, attn_dists_projected, p_gens)]

    return final_dists, attn_dists_projected, vocab_dists_extended