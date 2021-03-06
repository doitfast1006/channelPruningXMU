# -*- coding: utf-8 -*-

from xmumodel.model import Model
import tensorlayer.layers as tl
import tensorflow as tf
from xmuutil import utils
from xmuutil.scalelayer import ScaleLayer
from xmuutil.relulayer import ReluLayer
from xmuutil.transposed_conv2d_layer import TransposedConv2dLayer
from tqdm import tqdm
import shutil
import os
import numpy as np
import random
from sklearn.linear_model import Lasso


"""
EDSR upsample-layer
subpixel -> deconv 
"""
class EDSR(Model):
    def buildModel(self):
        print("Building EDSR...")
        self.norm_input = utils.normalize_color_tf(self.input)
        self.norm_target = utils.normalize_color_tf(self.target)
        self.op = []
        #input layer
        x = tl.InputLayer(self.norm_input, name='inputlayer')

        # One convolution before res blocks and to convert to required feature depth
        x = tl.Conv2d(x, self.feature_size, [3, 3], name='c')
        #x.print_layers()
        # Store the output of the first convolution to add later
        conv_1 = x

        scaling_factor = 0.1
        # Add the residual blocks to the model
        for i in range(self.num_layers):
            x = self.__resBlock(x, self.feature_size, scale=scaling_factor,layer=i)

        # One more convolution, and then we add the output of our first conv layer
        x = tl.Conv2d(x, self.feature_size, [3, 3], act = None, name = 'm1')
        x = tl.ElementwiseLayer([conv_1,x],tf.add, name='res_add')

        #x = TransposedConv2dLayer(x, self.feature_size-self.prunedlist[16], [5,5], [2,2], name='deconv_1')
        x = TransposedConv2dLayer(x, self.feature_size, [5,5], [2,2], name='deconv_1')
        #self.op.append(x.outputs)
        x = tl.Conv2d(x, self.feature_size, [3, 3], act=tf.nn.relu, name='deconv_conv_1')

        #x = TransposedConv2dLayer(x, self.feature_size-self.prunedlist[17], [5, 5], [2, 2], name='deconv_2')
        x = TransposedConv2dLayer(x, self.feature_size, [5, 5], [2, 2], name='deconv_2')
        #self.op.append(x.outputs)
        if self.scale==8:
            x = tl.Conv2d(x, self.feature_size, [3, 3], act=tf.nn.relu, name='deconv_conv_2')

            x = TransposedConv2dLayer(x, self.feature_size, [5, 5], [2, 2], name='deconv_3')
            #x = TransposedConv2dLayer(x, self.feature_size-self.prunedlist[18], [5, 5], [2, 2], name='deconv_3')

        # One final convolution on the upsampling output
        #self.op.append(x.outputs)
        output = tl.Conv2d(x,self.output_channels,[1,1],act=tf.nn.relu, name='lastLayer')
        # output = tl.Conv2d(x, self.output_channels, [1, 1], act=None, name='lastLayer')
        self.output = output.outputs
        self.output = tf.clip_by_value(output.outputs, 0.0, 1.0)
        self.cacuLoss()

        # Tensorflow graph setup... session, saver, etc.
        session_conf = tf.ConfigProto(allow_soft_placement=True,log_device_placement=False)
        session_conf.gpu_options.allocator_type = 'BFC'
        self.sess = tf.Session(config=session_conf)
        self.saver = tf.train.Saver(var_list = tf.trainable_variables(),max_to_keep=100)
        print("Done building!")

    def __resBlock(self, x, channels = 64, kernel_size = (3, 3), scale = 1.0,layer = 0):
        nn = ReluLayer(x, name='res%d/ru1'%(layer))
        #这里是每层的输入Y1
        #self.op.append(nn.outputs)
        nn = tl.Conv2d(nn, channels-self.prunedlist[layer], kernel_size, act=tf.nn.relu, name='res%d/c1'%(layer))
        self.op.append(nn.outputs)
        #self.out_t = nn.outputs
        #from c1_nn get a rand input [3,3,input_channels]
        #nn.print_layers()
        nn = tl.Conv2d(nn, channels, kernel_size, act=None, name='res%d/c2'%(layer))
        nn = ScaleLayer(nn,scale, name='res%d/scale'%(layer))
        n = tl.ElementwiseLayer([x,nn],tf.add, name='res%d/res_add'%(layer))
        return n

    def cacuLoss(self):
        self.loss = tf.reduce_mean(tf.losses.absolute_difference(self.norm_target, self.output))
        PSNR = utils.psnr_tf(self.norm_target, self.output, is_norm=True)

        # Scalar to keep track for loss
        summary_loss = tf.summary.scalar("loss", self.loss)
        summary_psnr = tf.summary.scalar("PSNR", PSNR)

        streaming_loss, self.streaming_loss_update = tf.contrib.metrics.streaming_mean(self.loss)
        streaming_loss_scalar = tf.summary.scalar('loss',streaming_loss)

        streaming_psnr, self.streaming_psnr_update = tf.contrib.metrics.streaming_mean(PSNR)
        streaming_psnr_scalar = tf.summary.scalar('PSNR',streaming_psnr)

        self.train_merge = tf.summary.merge([summary_loss,summary_psnr])
        self.test_merge = tf.summary.merge([streaming_loss_scalar,streaming_psnr_scalar])

    def train(self,batch_size= 10, iterations=200, lr_init=1e-4, lr_decay=0.5, decay_every=2e5,
              save_dir="saved_models",reuse=False,reuse_dir=None,reuse_step=None, log_dir="log"):
        #create the save directory if not exist
        if os.path.exists(save_dir):
            shutil.rmtree(save_dir)
        if os.path.exists(log_dir):
            shutil.rmtree(log_dir)
        os.mkdir(log_dir)
        #Make new save directory
        os.mkdir(save_dir)

        with tf.variable_scope('learning_rate'):
            lr_v = tf.Variable(lr_init, trainable=False)
        # Using adam optimizer as mentioned in the paper
        optimizer = tf.train.AdamOptimizer(learning_rate=lr_v)
        # optimizer = tf.train.RMSPropOptimizer(lr_v, decay=0.95, momentum=0.9, epsilon=1e-8)
        # This is the train operation for our objective
        #self.train_op = optimizer.minimize(self.loss)
        self.train_op = optimizer.compute_gradients(self.loss)

        #Operation to initialize all variables
        init = tf.global_variables_initializer()
        print("Begin training...")
        with self.sess as sess:
            #Initialize all variables
            sess.run(init)
            if reuse:
                self.resume(reuse_dir, global_step=reuse_step)

            #create summary writer for train
            train_writer = tf.summary.FileWriter(log_dir+"/train",sess.graph)

            #If we're using a test set, include another summary writer for that
            test_writer = tf.summary.FileWriter(log_dir+"/test",sess.graph)
            test_feed = []
            while True:
                test_x,test_y = self.data.get_test_set(batch_size)
                if test_x!=None and test_y!=None:
                    test_feed.append({
                            self.input:test_x,
                            self.target:test_y
                    })
                else:
                    break

            sess.run(tf.assign(lr_v, lr_init))
            #This is our training loop
            #samp_res=[]
            samp_x = []
            samp_y = []
            samp_w=[]
            for i in range(16):
                #samp_res.append([])
                samp_x.append([])
                samp_y.append([])
                samp_w.append([])

            for i in tqdm(range(iterations)):
                if i != 0 and (i % decay_every == 0):
                    new_lr_decay = lr_decay ** (i // decay_every)
                    sess.run(tf.assign(lr_v, lr_init * new_lr_decay))
                #Use the data function we were passed to get a batch every iteration
                x,y = self.data.get_batch(batch_size)
                #Create feed dictionary for the batch
                feed = {
                    self.input:x,
                    self.target:y
                }
                #Run the train op and calculate the train summary
                out = sess.run([self.op[:]],feed)
                out = out[0]
                for layer,ele in enumerate(out):
                    #这里的index是什么意思
                    for index,ele in enumerate(ele):
                        nn_np = np.array(ele)
                        #if(index==0 and layer==0):
                        #    print(nn_np.shape)
                        nn_len1 = len(nn_np)
                        nn_len2 = len(nn_np[0])
                        nn_len3 = len(nn_np[0][0])#input_channels
                        random1 = np.random.randint(0,nn_len1-3+1)
                        random2 = np.random.randint(0,nn_len2-3+1)
                        input_sample = nn_np[random1:random1+3,random2:random2+3,:]
                        
                        
                        #from c2 kernel get a rand input [3,3,input_channels]
                        if(layer<18):    
                            random3 = np.random.randint(0,self.feature_size-self.prunedlist[layer])
							#num_filter =self.feature_size-self.prunedlist[layer]
                        else:
                            random3 = np.random.randint(0,3)
                        
                        if(layer<16):
                            var_tensor = tl.get_variables_with_name("res"+str(layer)+"/"+"c2")
                        
                        if(layer == 16):
                            var_tensor = tl.get_variables_with_name("deconv_conv_1")
                        if(layer == 17):
                            var_tensor = tl.get_variables_with_name("deconv_conv_2")
                        if(layer == 18):
                            var_tensor = tl.get_variables_with_name("lastLayer")   
                        w = var_tensor[0].eval()
                        
                        '''
                        if(index==0 and layer==0):
                            print(w.shape)
                        '''
                        
                        #w = w[:,:,:,random3]
                        #b = var_tensor[1].eval()
                        #get y = [x1,x2,x3...x_inputchannels]
                        
                        '''
                        #这里是作为shortcut的输入
                        res_n = np.sum(np.sum(input_sample,0),0)
                        samp_res[layer].append(res_n)
                        '''
                        input_sample=input_sample[:,:,:,np.newaxis]
                        '''
                        if(layer == 0 and index==0):
                            print("input_sample shape %s "%input_sample.shape)
                        '''
                        x_n = input_sample*w
                        '''
                        if(index==0 and layer==0):
                            print(x_n.shape)
                        '''
                        x_n = np.sum(np.sum(x_n,0),0)#x^这里是把3*3*c*n合并为c*n
                        '''
                        if(index==0 and layer==0):
                            print("x_n shape %s:"%x_n.shape)
                        '''    
                        samp_x[layer].append(x_n)
                        
                        y_n = np.sum(x_n,0)#y^这里应该是将c个通道再合并，每个y_n就是一个1*n的值

                        samp_y[layer].append(y_n)

                            
            #sample ready then do choose
            candidate = []
            idx=[]
            lasso_alpha=1e-3
            _solver = Lasso(alpha=1e-4, warm_start=True,selection='random' )
            
            def solve(alpha,x,y):
            
                _solver.alpha=alpha
                _solver.fit(x, y) 
                #'''y是beta*x的目的，拟合的参数是beta'''
                #idxs = _solver.coef_ != 0. #'''idxs是Lasso之后beta不为0对应的index'''
                idxs = _solver.coef_ 
                tmp = sum(_solver.coef_ != 0.) #'''idxs是Lasso之后beta不为0的索引，tmp是个数'''
                return idxs, tmp
            
            
            for layer in range(16):
                candidate.append([])
                idx.append([])
                print("pruning %d layer..."%layer)
                temp_idxs=[]
                #candidate.append([])
                
                #res=samp_res[layer]
                x = samp_x[layer]
                y = samp_y[layer]

                x=np.array(x)
                y=np.array(y)
                
                N=x.shape[0]#本层的样本数
                c=x.shape[1]#本层的通道数

                
                left=0
                right=lasso_alpha 
                #rank是本层的保留数目
                rank=self.prunesize
                #scale=0.1
                #每层可以保留的通道左右上限
                lbound = rank + 0.1 * rank
                rbound = rank + 0.2 * rank
                    
                
                #if(layer!=0):
                if False:
                    #x的shape应该是N*c*n，这里要根据idxs[layer-1]去拿到上一层的结果
                    print("res_pruning for %d layer..."%layer)
                    rey=samp_y[layer-1]
                    rex=samp_x[layer-1]
                    #去除上一次被减掉的通道
                    rex=np.delete(rex, candidate[layer-1], 1)
                    rey_p = np.sum(rex,1)
                    #print(y.shape) 
                    #print(np.array(rey).shape)
                    #print(rey_p.shape)        
                    y=y+rey-rey_p
                
                x=np.rollaxis(x.reshape((N, c, -1)), 1, 0)
                z=x.reshape((c, -1)).T
                y=y.reshape(-1)

                #print(np.array(z).shape)
                #print(np.array(y).shape)
                #在本循环内得到可以接受通道数的剪枝idxs
                while True:
                    alpha = (left+right)/2
                    temp_idxs, tmp = solve(alpha,z,y)
                    #if verbose:print(tmp, alpha, left, right)
                    if tmp > rbound:
                        left=alpha
                    elif tmp < lbound:
                        right=alpha
                    else:
                        break

                print("prun %d layer.done,the candidate:"%layer)
                print(temp_idxs)
                for index,is_purned in enumerate(temp_idxs):
                    idx[layer].append(is_purned)
                    if(is_purned== 0.):
                        candidate[layer].append(index)
                
            '''       
            for layer,element in enumerate(idxs):
                for index,is_purned in enumerate(element)
                    if(is_purned==0 or is_purned==False):
                        candidate[layer].append(index)  
            ''' 
            #candidate[layer].append(min_i)
            
            deconv_var = tl.get_variables_with_name("Conv2d_transpose")
            for index,element in enumerate(candidate):
                if(index < 16):
                    var_tensor = tl.get_variables_with_name("res"+str(index)+"/"+"c1")
                    w_next = tl.get_variables_with_name("res"+str(index)+"/"+"c2")[0]
                    w = var_tensor[0]
                    b = var_tensor[1]
                if(index == 16):
                    w = deconv_var[0]
                    b = deconv_var[1] 
                    w_next = tl.get_variables_with_name("deconv_conv_1")[0]
                if(index == 17):
                    w = deconv_var[2]
                    b = deconv_var[3]
                    w_next = tl.get_variables_with_name("deconv_conv_2")[0]
                if(index == 18):
                    w = deconv_var[4]
                    b = deconv_var[5]
                    w_next = tl.get_variables_with_name("lastLayer")[0]
            

                w_np = w.eval()
                b_np = b.eval()
                w_next_np = w_next.eval()
                if(index>=16):
                    w_np = np.delete(w_np, element, -2)
                else:
                    w_np = np.delete(w_np, element, -1)
                b_np = np.delete(b_np, element, 0)
                
                #todo 这里需要将β乘上
                
                for i,beta in enumerate(idx[index]):
                    if(index==0):
                        print("beta is %f"%beta)
                        print(w_next_np[:,:,i,:].shape)
                    w_next_np[:,:,i,:]=beta*w_next_np[:,:,i,:]
                
                w_next_np = np.delete(w_next_np, element, -2)
                
                w_new = tf.convert_to_tensor(w_np)
                #print(w_new.shape)
                b_new = tf.convert_to_tensor(b_np)
                #print(b_new.shape)
                w_next_new = tf.convert_to_tensor(w_next_np)
                #print(w_next_new.shape)

                sess.run(tf.assign(w, w_new, False))
                sess.run(tf.assign(b, b_new, False))
                sess.run(tf.assign(w_next, w_next_new, False))

            save_prunedlist = []
            for i,e in enumerate(self.prunedlist):
                if (i<16):
                    save_prunedlist.append(int(e+len(candidate[i])))
            np.savetxt(save_dir+"/prunedlist",np.array(save_prunedlist),fmt="%d",delimiter=",")
            self.save(save_dir, i)

            test_writer.close()
            train_writer.close()
            
        
