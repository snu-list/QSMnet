import tensorflow as tf
import matplotlib.colors
import matplotlib.pyplot as plt
import numpy as np
import h5py
import scipy.io
import os
import nibabel as nib
import random
from tqdm import tqdm
if hasattr(tqdm,'_instances'):
    tqdm._instances.clear()

from training_params import *

#
# Description:
#  Loss function for training QSMnet and QSMnet+
#
#  Copyright @ Woojin Jung & Jaeyeon Yoon
#  Laboratory for Imaging Science and Technology
#  Seoul National University
#  email : wjjung93@snu.ac.kr
#

#%% Dataset class
class dataset():
    def __init__(self):
        f = h5py.File(train_data_path, "r")
        f2i = scipy.io.loadmat(val_input_path)
        f2l = scipy.io.loadmat(val_label_path)
        f3 = scipy.io.loadmat(save_path + 'norm_factor.mat')

        self.trfield = f['temp_i']
        self.trsusc = f['temp_l']
        
        self.tefield = f2i['phs_tissue']
        self.tesusc = f2l['chi_cosmos']
        self.tefield = np.expand_dims(self.tefield, axis=0)
        self.tefield = np.expand_dims(self.tefield, axis=4)
        self.tesusc = np.expand_dims(self.tesusc, axis=0)
        self.tesusc = np.expand_dims(self.tesusc, axis=4)
        
        self.X_mean = f3["input_mean"]
        self.X_std = f3["input_std"]
        self.Y_mean = f3["label_mean"]
        self.Y_std = f3["label_std"]
        

        
#%% batch normalization, 3D convoluation, deconvolution, max pooling
def batch_norm(x, channel, isTrain, decay=0.99, name="bn"):

   with tf.compat.v1.variable_scope(name):
      beta = tf.compat.v1.get_variable(initializer=tf.constant(0.0, shape=[channel]), name='beta')
      gamma = tf.compat.v1.get_variable(initializer=tf.constant(1.0, shape=[channel]), name='gamma')
      batch_mean, batch_var = tf.nn.moments(x, [0, 1, 2, 3], name='moments')
      mean_sh = tf.compat.v1.get_variable(initializer=tf.zeros([channel]), name="mean_sh", trainable=False)
      var_sh = tf.compat.v1.get_variable(initializer=tf.ones([channel]), name="var_sh", trainable=False)

      def mean_var_with_update():
         mean_assign_op = tf.compat.v1.assign(mean_sh, mean_sh * decay + (1 - decay) * batch_mean)
         var_assign_op = tf.compat.v1.assign(var_sh, var_sh * decay + (1 - decay) * batch_var)
         with tf.control_dependencies([mean_assign_op, var_assign_op]):
            return tf.identity(batch_mean), tf.identity(batch_var)

      mean, var = tf.cond(tf.cast(isTrain, tf.bool), mean_var_with_update, lambda: (mean_sh, var_sh))
      normed = tf.nn.batch_normalization(x, mean, var, beta, gamma, 1e-3, name="normed")

   return normed


def Conv3d(layer_name, inputs, out_channel, ks, act_func, reuse, isTrain):
    with tf.compat.v1.variable_scope(layer_name, reuse=reuse) as scope:
        weights = tf.compat.v1.get_variable("conv_weights", [ks[0], ks[1], ks[2], inputs.get_shape()[4].value, out_channel],
                                            initializer=tf.contrib.layers.variance_scaling_initializer(), trainable=isTrain)
        conv_3d = tf.nn.conv3d(inputs, weights, strides=[1, 1, 1, 1, 1], padding='SAME')
        biases = tf.compat.v1.get_variable("biases", [out_channel],
                                           initializer=tf.random_normal_initializer(), trainable=isTrain)
        conv_3d = tf.nn.bias_add(conv_3d, biases)
        channel = conv_3d.get_shape().as_list()[-1]
        bn_x = batch_norm(conv_3d, channel, isTrain)
        if act_func is 'relu':
            return tf.nn.relu(bn_x)
        elif act_func is 'leaky_relu':
            return tf.nn.leaky_relu(bn_x, alpha = 0.1)
        scope.reuse_variables()

def Conv(layer_name, inputs, out_channel, ks, reuse, isTrain):
    with tf.compat.v1.variable_scope(layer_name, reuse=reuse) as scope:
        weights = tf.compat.v1.get_variable("weights", [ks[0], ks[1], ks[2], inputs.get_shape()[4].value, out_channel],
                                            initializer=tf.contrib.layers.variance_scaling_initializer(), trainable=isTrain)
        biases = tf.compat.v1.get_variable("biases", [out_channel],
                                           initializer=tf.random_normal_initializer(), trainable=isTrain)
        return tf.nn.conv3d(inputs, weights, strides=[1, 1, 1, 1, 1], padding='SAME') + biases
        scope.reuse_variables()
        
def Max_pool(layer_name, x, ks, reuse):
    with tf.compat.v1.variable_scope(layer_name, reuse=reuse) as scope:
        return tf.nn.max_pool3d(x, ksize=[1, ks[0], ks[1], ks[2], 1], strides=[1, ks[0], ks[1], ks[2], 1], padding='SAME')
        scope.reuse_variables()
    

def Deconv3d(layer_name, inputs, out_channel, ks, stride, reuse, isTrain):
    with tf.compat.v1.variable_scope(layer_name, reuse=reuse) as scope:
        x_shape = tf.shape(inputs)
        weights = tf.compat.v1.get_variable("deconv_weights", [ks[0], ks[1], ks[2], out_channel, inputs.get_shape()[4].value],
                                            initializer=tf.contrib.layers.variance_scaling_initializer(), trainable=isTrain)
        biases = tf.compat.v1.get_variable("biases", [out_channel],
                                           initializer=tf.random_normal_initializer(), trainable=isTrain)        
        output_shape = tf.stack([x_shape[0], x_shape[1] * 2, x_shape[2] * 2, x_shape[3] * 2, x_shape[4] // 2])
        return tf.nn.conv3d_transpose(inputs, weights, output_shape, strides=[1, stride[0], stride[1], stride[2], 1],
                                  padding='SAME') + biases
        scope.reuse_variables()
        
def Concat(layer_name, x, y, reuse):
    with tf.compat.v1.variable_scope(layer_name, reuse=reuse) as scope:
        return tf.concat([x, y], axis=4)
    scope.reuse_variables()


#%% Loss function
def l1(x, y):

    l1 = tf.reduce_mean(tf.reduce_mean(tf.abs(x - y), [1, 2, 3, 4]))

    return l1


#%% Display validation images
def display_slice(display_num, Pred, Label):
     fig = plt.figure(figsize=(12,10))
     nonorm = matplotlib.colors.NoNorm()
     col = np.size(display_num)
     for i in range(col):

         subplot = fig.add_subplot(3, col, i + 1)
         subplot.set_xticks([]), subplot.set_yticks([])
         subplot.imshow(np.rot90(np.clip(Pred[:,:,display_num[i]], -0.1, 0.1) * 5 + 0.5, -1),
                        cmap = plt.cm.gray, norm=nonorm)
         if i == 0:
             subplot.set_ylabel('Prediction', fontsize=18)
             
         subplot = fig.add_subplot(3, col, i + 1 + col)
         subplot.set_xticks([]), subplot.set_yticks([])
         subplot.imshow(np.rot90(np.clip(Label[:,:,display_num[i]], -0.1, 0.1) * 5 + 0.5,-1),
                         cmap = plt.cm.gray, norm=nonorm)
         if i == 0:
             subplot.set_ylabel('Label', fontsize=18)
             
         subplot = fig.add_subplot(3, col, i + 1 + col*2)
         subplot.set_xticks([]), subplot.set_yticks([])
         subplot.imshow(np.rot90(np.clip((Label[:,:,display_num[i]]-Pred[:,:,display_num[i]]),
                                          -0.1, 0.1) * 5 + 0.5, -1),cmap = plt.cm.gray, norm=nonorm)
         if i == 0:
             subplot.set_ylabel('Dif', fontsize=18)
     plt.show()
     plt.close()

#%% Training process
def Training_network(dataset, X, Y, X_val, Y_val, predX_val, loss, loss_val, train_op, keep_prob, net_saver):
    with tf.compat.v1.Session() as sess:
        X_mean = dataset.X_mean
        X_std = dataset.X_std
        Y_mean = dataset.Y_mean
        Y_std = dataset.Y_std
        #%% Intializaion of all variables            
        sess.run(tf.compat.v1.global_variables_initializer())
        #%% Training
        print("Training Start!")
        ind = list(range(len(dataset.trfield))) 
        for epoch in range(train_epochs):
            random.shuffle(ind)
            avg_cost = 0
            total_batch = int(len(ind)/batch_size)    
            for i in tqdm(range(0, len(ind), batch_size)):
                ind_batch = ind[i:i + batch_size]
                ind_batch = np.sort(ind_batch)
                x_batch = (dataset.trfield[ind_batch, :, :, :, :] - X_mean) / X_std
                y_batch = (dataset.trsusc[ind_batch, :, :, :, :] - Y_mean) / Y_std
                cost, _ = sess.run([loss, train_op],
                                            feed_dict={X: x_batch, Y: y_batch, keep_prob: 0.5})
                avg_cost += cost / total_batch
                                
            print("Epoch:", '%04d' % (epoch+1), "Training_cost=", "{:.5f}".format(avg_cost))
            #%% Save network
            if (epoch + 1) % save_step == 0:
                net_saver.save(sess, save_path + net_name + '_' + str(PS), global_step = epoch + 1)

            #%% Validation
            if (epoch + 1) % display_step == 0:
                input_val = (dataset.tefield - X_mean ) / X_std
                label_val = (dataset.tesusc - Y_mean ) / Y_std
                im_val, cost_val = sess.run([predX_val, loss_val], 
                                            feed_dict={X_val: input_val, Y_val: label_val, keep_prob : 1.0})
                print("Epoch:", '%04d' % (epoch+1), "Validation_cost=", "{:.5f}".format(cost_val))
                im_val = dataset.Y_std * im_val.squeeze() + dataset.Y_mean
                
                scipy.io.savemat(save_path + 'validation_result/im_epoch' + str(epoch+1) + '.mat', mdict={'val_pred': im_val})
                display_slice(display_slice_num, im_val, dataset.tesusc.squeeze())
                
#%% Utils for inference
     
def save_nii(data, save_folder, name):
    if not os.path.exists(save_folder):
        os.mkdir(save_folder)

    nifti_affine = np.array([[1,0,0,1], [0,1,0,1], [0,0,1,1], [0,0,0,1]], dtype=np.float)

    data = np.fliplr(data) 
    nifti = nib.Nifti1Image(data, affine=nifti_affine)
    nib.save(nifti, os.path.join(save_folder, name + '.nii.gz'))

def padding_data(input_field):
    N = np.shape(input_field)
    N_16 = np.ceil(np.divide(N,16.))*16
    N_dif = np.int16((N_16 - N) / 2)
    npad = ((N_dif[0],N_dif[0]),(N_dif[1],N_dif[1]),(N_dif[2],N_dif[2]))
    pad_field = np.pad(input_field, pad_width = npad, mode = 'constant', constant_values = 0)
    pad_field = np.expand_dims(pad_field, axis=0)
    pad_field = np.expand_dims(pad_field, axis=4)
    return pad_field, N_dif, N_16


def crop_data(result_pad, N_dif):
    result_pad = result_pad.squeeze()
    N_p = np.shape(result_pad)
    result_final  = result_pad[N_dif[0]:N_p[0]-N_dif[0],N_dif[1]:N_p[1]-N_dif[1],N_dif[2]:N_p[2]-N_dif[2]]
    return result_final

def display_slice_inf(display_num, Pred):
     fig = plt.figure(figsize=(12,10))
     nonorm = matplotlib.colors.NoNorm()
     col = np.size(display_num)
     for i in range(col):

         subplot = fig.add_subplot(3, col, i + 1)
         subplot.set_xticks([]), subplot.set_yticks([])
         subplot.imshow(np.rot90(np.clip(Pred[:,:,display_num[i]], -0.1, 0.1) * 5 + 0.5, -1),
                        cmap = plt.cm.gray, norm=nonorm)
         if i == 0:
             subplot.set_ylabel('Prediction', fontsize=18)

     plt.show()
     plt.close()
     
     
#%% Previous version
def conv3d(x, w_shape, b_shape, act_func, isTrain):
    weights = tf.compat.v1.get_variable("conv_weights", w_shape,
                              initializer=tf.contrib.layers.variance_scaling_initializer(), trainable=isTrain)
    conv_3d = tf.nn.conv3d(x, weights, strides=[1, 1, 1, 1, 1], padding='SAME')
    biases = tf.compat.v1.get_variable("biases", b_shape,
                             initializer=tf.random_normal_initializer(), trainable=isTrain)

    conv_3d = tf.nn.bias_add(conv_3d, biases)
    channel = conv_3d.get_shape().as_list()[-1]
    bn_x = batch_norm(conv_3d, channel, isTrain)
    
    if act_func is 'relu':
        return tf.nn.relu(bn_x)
    elif act_func is 'leaky_relu':
        return tf.nn.leaky_relu(bn_x, alpha = 0.1)

def conv(x, w_shape, b_shape, isTrain):
    weights = tf.compat.v1.get_variable("weights", w_shape,
                              initializer=tf.contrib.layers.variance_scaling_initializer(), trainable=isTrain)
    biases = tf.compat.v1.get_variable("biases", b_shape,
                             initializer=tf.random_normal_initializer(), trainable=isTrain)
    return tf.nn.conv3d(x, weights, strides=[1, 1, 1, 1, 1], padding='SAME') + biases

def max_pool(x, n):
    return tf.nn.max_pool3d(x, ksize=[1, n, n, n, 1], strides=[1, n, n, n, 1], padding='SAME')

def deconv3d(x, w_shape, b_shape, stride, isTrain):
    x_shape = tf.shape(x)
    weights = tf.compat.v1.get_variable("deconv_weights", w_shape,
                              initializer=tf.contrib.layers.variance_scaling_initializer(), trainable=isTrain)
    biases = tf.compat.v1.get_variable("biases", b_shape,
                             initializer=tf.random_normal_initializer(), trainable=isTrain)

    output_shape = tf.stack([x_shape[0], x_shape[1] * 2, x_shape[2] * 2, x_shape[3] * 2, x_shape[4] // 2])
    return tf.nn.conv3d_transpose(x, weights, output_shape, strides=[1, stride, stride, stride, 1],
                                  padding='SAME') + biases