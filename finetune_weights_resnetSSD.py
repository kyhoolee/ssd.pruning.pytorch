'''
	Use absolute weights-based criterion for filter pruning on resnetSSD (Train/Test on VOC)
	Execute: python3 finetune_weights_resnetSSD.py --prune --trained_model weights/_your_trained_model_.pth
    Author: xuhuahuang as intern in YouTu 07/2018
'''
import torch
from torch.autograd import Variable
#from torchvision import models
import cv2
cv2.setNumThreads(0) # pytorch issue 1355: possible deadlock in DataLoader
# OpenCL may be enabled by default in OpenCV3;
# disable it because it because it's not thread safe and causes unwanted GPU memory allocations
cv2.ocl.setUseOpenCL(False)
import sys
import numpy as np
import torchvision
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
#import dataset
from pruning.prune_resnet import *
import argparse
from operator import itemgetter
from heapq import nsmallest #heap queue algorithm
import time

# for testing
import pickle
import os
from data import *
from data import VOC_CLASSES as labelmap
import torch.utils.data as data
from utils.augmentations import SSDAugmentation
from layers.modules import MultiBoxLoss
from models.SSD_vggres import build_ssd

def str2bool(v):
    return v.lower() in ("yes", "true", "t", "1")

parser = argparse.ArgumentParser()
parser.add_argument("--train", dest="train", action="store_true")
parser.add_argument("--prune", dest="prune", action="store_true")
parser.add_argument("--prune_folder", default = "prunes/")
parser.add_argument("--trained_model", default = "prunes/vggSSD_trained.pth")
parser.add_argument('--dataset_root', default=VOC_ROOT)
parser.add_argument('--cuda', default=True, type=str2bool, help='Use cuda to train model')
parser.set_defaults(train=False)
parser.set_defaults(prune=False)
args = parser.parse_args()

cfg = voc

def test_net(save_folder, net, cuda,
             testset, transform, max_per_image=300, thresh=0.05):

    if not os.path.exists(save_folder):
        os.mkdir(save_folder)

    num_images = len(testset)
    num_classes = len(labelmap)                      # +1 for background
    # all detections are collected into:
    #    all_boxes[cls][image] = N x 5 array of detections in
    #    (x1, y1, x2, y2, score)
    all_boxes = [[[] for _ in range(num_images)]
                 for _ in range(num_classes)]

    # timers
    _t = {'im_detect': Timer(), 'misc': Timer()}
    #output_dir = get_output_dir('ssd300_120000', set_type) #directory storing output results
    #det_file = os.path.join(output_dir, 'detections.pkl') #file storing output result under output_dir
    det_file = os.path.join(save_folder, 'detections.pkl')

    for i in range(num_images):
        im, gt, h, w = testset.pull_item(i) # include BaseTransform inside

        x = Variable(im.unsqueeze(0)) #insert a dimension of size one at the dim 0
        if cuda:
            x = x.cuda()

        _t['im_detect'].tic()
        detections = net(x=x, test=True).data # get the detection results
        detect_time = _t['im_detect'].toc(average=False) #store the detection time

        # skip j = 0, because it's the background class
        for j in range(1, detections.size(1)): # for every class
            dets = detections[0, j, :]#size( ** , 5)
            mask = dets[:, 0].gt(0.).expand(5, dets.size(0)).t()
            dets = torch.masked_select(dets, mask).view(-1, 5)
            if dets.dim() == 0:
                continue
            #if dets.size(0) == 0:
            #    continue
            boxes = dets[:, 1:]
            boxes[:, 0] *= w
            boxes[:, 2] *= w
            boxes[:, 1] *= h
            boxes[:, 3] *= h
            scores = dets[:, 0].cpu().numpy()
            cls_dets = np.hstack((boxes.cpu().numpy(),
                                  scores[:, np.newaxis])).astype(np.float32,
                                                                 copy=False)
            all_boxes[j][i] = cls_dets #[class][imageID] = 1 x 5 where 5 is box_coord + score

        if (i + 1) % 100 == 0:
            print('im_detect: {:d}/{:d} {:.3f}s'.format(i + 1, num_images, detect_time))

    #write the detection results into det_file
    with open(det_file, 'wb') as f:
        pickle.dump(all_boxes, f, pickle.HIGHEST_PROTOCOL)

    print('Evaluating detections')
    APs,mAP = testset.evaluate_detections(all_boxes, save_folder)

# --------------------------------------------------------------------------- Pruning Part
