from data import *
from data import VOC_CLASSES as voc_labelmap
from data import COCO_CLASSES as coco_labelmap
from data import WEISHI_CLASSES as weishi_labelmap
from utils.augmentations import SSDAugmentation
from layers.modules import RefineMultiBoxLoss
from layers.functions import Detect, PriorBox
from models.RefineSSD_vgg import build_refine
import os
import sys
import time
import torch
from torch.autograd import Variable
import torch.nn as nn
import torch.optim as optim
import torch.backends.cudnn as cudnn
import torch.nn.init as init
import torch.utils.data as data
import numpy as np
import argparse
# for evaluation
from utils.eval_tools import * #val_dataset_root

#os.environ['CUDA_VISIBLE_DEVICES'] = '6'

def str2bool(v):
    return v.lower() in ("yes", "true", "t", "1")


parser = argparse.ArgumentParser(
    description='Single Shot MultiBox Detector Training With Pytorch')
train_set = parser.add_mutually_exclusive_group()
parser.add_argument('--dataset', default='VOC', choices=['VOC', 'COCO', 'WEISHI'],
                    type=str, help='VOC or COCO or WEISHI')
parser.add_argument('--dataset_root', default=VOC_ROOT,
                    help='Dataset root directory path')
parser.add_argument('--basenet', default='vgg16_reducedfc.pth',
                    help='Pretrained base model')
parser.add_argument('--batch_size', default=32, type=int,
                    help='Batch size for training')
parser.add_argument('--resume', default=None, type=str,
                    help='Checkpoint state_dict file to resume training from')
parser.add_argument('--start_iter', default=0, type=int,
                    help='Resume training at this iter')
parser.add_argument('--num_workers', default=4, type=int,
                    help='Number of workers used in dataloading')
parser.add_argument('--cuda', default=True, type=str2bool,
                    help='Use CUDA to train model')
parser.add_argument('--lr', '--learning-rate', default=1e-3, type=float,
                    help='initial learning rate')
parser.add_argument('--lr_step', default=30,
                    help='Epoch interval for updating lr')
parser.add_argument('--momentum', default=0.9, type=float,
                    help='Momentum value for optim')
parser.add_argument('--weight_decay', default=5e-4, type=float,
                    help='Weight decay for SGD')
parser.add_argument('--gamma', default=0.1, type=float,
                    help='Gamma update for SGD')
parser.add_argument('--visdom', default=False, type=str2bool,
                    help='Use visdom for loss visualization')
parser.add_argument('--save_folder', default='weights/',
                    help='Directory for saving checkpoint models')
# Below args must be specified if want to eval during training
parser.add_argument('--evaluate', default=False, type=str2bool,
                    help='Evaluate at every epoch during training')
parser.add_argument('--eval_folder', default='evals/',
                    help='Directory for saving eval results')
parser.add_argument('--confidence_threshold', default=0.01, type=float,
                    help='Detection confidence threshold')
parser.add_argument('--top_k', default=5, type=int,
                    help='Further restrict the number of predictions to parse')
# for WEISHI dataset
parser.add_argument('--jpg_xml_path', default='',
                    help='Image XML mapping path')
parser.add_argument('--label_name_path', default='',
                    help='Label Name file path')
args = parser.parse_args()


if torch.cuda.is_available():
    if args.cuda:
        torch.set_default_tensor_type('torch.cuda.FloatTensor')
    if not args.cuda:
        print("WARNING: It looks like you have a CUDA device, but aren't " +
              "using CUDA.\nRun with --cuda for optimal training speed.")
        torch.set_default_tensor_type('torch.FloatTensor')
else:
    torch.set_default_tensor_type('torch.FloatTensor')

if not os.path.exists(args.save_folder):
    os.mkdir(args.save_folder)
if not os.path.exists(args.eval_folder):
    os.mkdir(args.eval_folder)


def train():
    if args.dataset == 'COCO':
        if args.dataset_root == VOC_ROOT:
            if not os.path.exists(COCO_ROOT):
                parser.error('Must specify dataset_root if specifying dataset')
            print("WARNING: Using default COCO dataset_root because " +
                  "--dataset_root was not specified.")
            args.dataset_root = COCO_ROOT
        cfg = coco
        set_name = 'coco'
        dataset = COCODetection(root=args.dataset_root,
                                transform=SSDAugmentation(cfg['min_dim'],
                                                          MEANS))
        # only support VOC evaluation now
        val_dataset = COCODetection(root=coco_val_dataset_root,
                                transform=BaseTransform(300, coco_dataset_mean))
    elif args.dataset == 'VOC':
        if args.dataset_root == COCO_ROOT:
            #by default dataset_root is VOC_ROOT, so when you have COCO_ROOT, it means you specify dataset_root, but dataset is still VOC, then error!
            parser.error('Must specify dataset if specifying dataset_root')
        cfg = voc
        set_name = 'voc'
        dataset = VOCDetection(root=args.dataset_root,
                               transform=SSDAugmentation(cfg['min_dim'],
                                                         MEANS))
        val_dataset = VOCDetection(root=voc_val_dataset_root, image_sets=[('2007', set_type)],
                                transform=BaseTransform(300, voc_dataset_mean))
    elif args.dataset == 'WEISHI':
        if args.jpg_xml_path == '':
            parser.error('Must specify jpg_xml_path if using WEISHI')
        if args.label_name_path == '':
            parser.error('Must specify label_name_path if using WEISHI')
        cfg = weishi
        set_name = 'weishi'
        dataset = WeishiDetection(image_xml_path=args.jpg_xml_path, label_file_path=args.label_name_path,
                               transform=SSDAugmentation(cfg['min_dim'],
                                                         MEANS))
        val_dataset = WeishiDetection(image_xml_path=weishi_val_imgxml_path, label_file_path=args.label_name_path,
                                transform=BaseTransform(300, weishi_dataset_mean))

    if args.visdom:
        import visdom
        viz = visdom.Visdom()

    ssd_net = build_refine('train', cfg['min_dim'], cfg['num_classes'], use_refine = True)
    net = ssd_net

    if args.cuda:
        net = torch.nn.DataParallel(ssd_net)
        cudnn.benchmark = True

    if args.resume:
        print('Resuming training, loading {}...'.format(args.resume))
        ssd_net.load_weights(args.resume)
    else:
        vgg_weights = torch.load(args.save_folder + args.basenet)
        print('Loading base network...')
        ssd_net.vgg.load_state_dict(vgg_weights)

    if args.cuda:
        net = net.cuda()

    if not args.resume:
        print('Initializing weights...')
        # initialize newly added layers' weights with xavier method
        ssd_net.extras.apply(weights_init)
        ssd_net.trans_layers.apply(weights_init)
        ssd_net.latent_layrs.apply(weights_init)
        ssd_net.up_layers.apply(weights_init)
        ssd_net.arm_loc.apply(weights_init)
        ssd_net.arm_conf.apply(weights_init)
        ssd_net.odm_loc.apply(weights_init)
        ssd_net.odm_conf.apply(weights_init)

    optimizer = optim.SGD(net.parameters(), lr=args.lr, momentum=args.momentum,
                          weight_decay=args.weight_decay)

    arm_criterion = RefineMultiBoxLoss(2, 0.5, True, 0, True, 3, 0.5, False, 0, args.cuda)
    odm_criterion = RefineMultiBoxLoss(cfg['num_classes'], 0.5, True, 0, True, 3, 0.5, False, 0.01, args.cuda)# 0.01 -> 0.99 negative confidence threshold
    priorbox = PriorBox(cfg)# refineDet is different from normal ssd, where the PriorBox is stored inside SSD object
#    detector = Detect(num_classes,0,cfg,object_score=0.01) # for test_net()
    priors = Variable(priorbox.forward(), volatile=True)

    net.train()
    # loss counters
    loc_loss = 0
    conf_loss = 0
    epoch = 0
    print('Loading the dataset...')

    epoch_size = len(dataset) // args.batch_size
    print('Training SSD on:', dataset.name)
    print('Using the specified args:')
    print(args)

    if args.visdom:
        # initialize visdom loss plot
        vis_title = 'SSD.PyTorch on ' + dataset.name
        vis_legend = ['Loc Loss', 'Conf Loss', 'Total Loss']
        iter_plot = create_vis_plot('Iteration', 'Loss', vis_title, vis_legend)
        epoch_plot = create_vis_plot('Epoch', 'Loss', vis_title, vis_legend)

    # training data loader
    data_loader = data.DataLoader(dataset, args.batch_size,
                                  num_workers=args.num_workers,
                                  shuffle=True, collate_fn=detection_collate,
                                  pin_memory=True)
    # create batch iterator
    batch_iterator = iter(data_loader)
    mean_odm_loss_c = 0
    mean_odm_loss_l = 0
    mean_arm_loss_c = 0
    mean_arm_loss_l = 0
    # max_iter = cfg['max_epoch'] * epoch_size
    for iteration in range(args.start_iter, cfg['max_epoch']*epoch_size):
        try:
            images, targets = next(batch_iterator)
        except StopIteration:
            batch_iterator = iter(data_loader)# the dataloader cannot re-initilize
            images, targets = next(batch_iterator)

        if args.visdom and iteration != 0 and (iteration % epoch_size == 0):
            # update visdom loss plot
            update_vis_plot(epoch, loc_loss, conf_loss, epoch_plot, None,
                            'append', epoch_size)
            # reset epoch loss counters
            loc_loss = 0
            conf_loss = 0

#        if iteration in cfg['lr_steps']:
        if iteration != 0 and (iteration % epoch_size == 0):
            adjust_learning_rate(optimizer, args.gamma, epoch)
            epoch += 1
            # evaluation
            if args.evaluate == True:
                # load net
                if set_name=='weishi':
                    lm = weishi_labelmap
                    val_dataset_mean = weishi_dataset_mean # import from eval_tools
                elif set_name=='coco':
                    lm = coco_labelmap
                    val_dataset_mean = coco_dataset_mean
                else:
                    lm = voc_labelmap
                    val_dataset_mean = voc_dataset_mean
                num_classes = len(lm) + 1                      # +1 for background
                val_net = build_ssd('test', 300, num_classes, base='vgg')            # initialize SSD
                val_net.load_state_dict(ssd_net.state_dict())
                val_net.eval() # switch to eval mode
                print("\nStarting the evaluation mode...")
                test_net(args.eval_folder, val_net, args.cuda, val_dataset,
                         BaseTransform(val_net.size, val_dataset_mean), args.top_k, 300,
                         thresh=args.confidence_threshold, set=set_name)
                print("Finishing the evaluation mode...")

        if args.cuda:
            images = Variable(images.cuda())
            targets = [Variable(ann.cuda(), volatile=True) for ann in targets]
        else:
            images = Variable(images)
            targets = [Variable(ann, volatile=True) for ann in targets]
        # forward
        t0 = time.time()
        out = net(images)
        arm_loc, arm_conf, odm_loc, odm_conf = out
        # backprop
        optimizer.zero_grad()
        #arm branch loss
        arm_loss_l,arm_loss_c = arm_criterion((arm_loc,arm_conf),priors,targets)
        #odm branch loss
        odm_loss_l, odm_loss_c = odm_criterion((odm_loc,odm_conf),priors,targets,(arm_loc,arm_conf),False)

        mean_arm_loss_c += arm_loss_c.data[0]
        mean_arm_loss_l += arm_loss_l.data[0]
        mean_odm_loss_c += odm_loss_c.data[0]
        mean_odm_loss_l += odm_loss_l.data[0]

        loss = arm_loss_l + arm_loss_c + odm_loss_l + odm_loss_c
        loss.backward()
        optimizer.step()
        t1 = time.time()

        if iteration % 10 == 0:
            print('timer: %.4f sec.' % (t1 - t0))
            print('iter ' + repr(iteration) + ' || Loss: %.4f ||' % (loss.data[0]), end=' ')
#            print('Epoch:' + repr(epoch) + ' || epochiter: ' + repr(iteration % epoch_size) + '/' + repr(epoch_size)
#                  + '|| Total iter ' +
#                  repr(iteration))
            print(' || AL: %.4f AC: %.4f OL: %.4f OC: %.4f||' % (
                mean_arm_loss_l/10, mean_arm_loss_c/10, mean_odm_loss_l/10, mean_odm_loss_c/10))

            mean_odm_loss_c = 0
            mean_odm_loss_l = 0
            mean_arm_loss_c = 0
            mean_arm_loss_l = 0

#        if args.visdom:
#            update_vis_plot(iteration, loss_l.data[0], loss_c.data[0],
#                            iter_plot, epoch_plot, 'append')

        if iteration != 0 and iteration % 5000 == 0:
            print('Saving state, iter:', iteration)
            #whatever dataset you use, the name is COCO
            torch.save(ssd_net.state_dict(), 'weights/ssd300_COCO_' +
                       repr(iteration) + '.pth')
    torch.save(ssd_net.state_dict(),
               args.save_folder + '' + args.dataset + '.pth')

def adjust_learning_rate(optimizer, gamma, epoch):
    """Sets the learning rate to the initial LR decayed by 10 at
        specified epoch
    # Adapted from PyTorch Imagenet example:
    # https://github.com/pytorch/examples/blob/master/imagenet/main.py
    """
    step = epoch//args.lr_step # every 30 epoch by default
    lr = args.lr * (gamma ** (step))
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr


def xavier(param):
    init.xavier_uniform(param)

# initialize the weights for conv2d
def weights_init(m):
    if isinstance(m, nn.Conv2d):
        xavier(m.weight.data)
        m.bias.data.zero_()


def create_vis_plot(_xlabel, _ylabel, _title, _legend):
    return viz.line(
        X=torch.zeros((1,)).cpu(),
        Y=torch.zeros((1, 3)).cpu(),
        opts=dict(
            xlabel=_xlabel,
            ylabel=_ylabel,
            title=_title,
            legend=_legend
        )
    )


def update_vis_plot(iteration, loc, conf, window1, window2, update_type,
                    epoch_size=1):
    viz.line(
        X=torch.ones((1, 3)).cpu() * iteration,
        Y=torch.Tensor([loc, conf, loc + conf]).unsqueeze(0).cpu() / epoch_size,
        win=window1,
        update=update_type
    )
    # initialize epoch plot on first iteration
    if iteration == 0:
        viz.line(
            X=torch.zeros((1, 3)).cpu(),
            Y=torch.Tensor([loc, conf, loc + conf]).unsqueeze(0).cpu(),
            win=window2,
            update=True
        )


if __name__ == '__main__':
    train()
