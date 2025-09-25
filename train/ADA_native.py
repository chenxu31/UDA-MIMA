import sys
sys.path.append('/home/chenxu/github/UDA-MIMA')

#from yacs.config import CfgNode as CN
import time
import argparse

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.optim.lr_scheduler import StepLR, LambdaLR
from torch.utils import data
import numpy
#from build_dataset import build_dataset_preDA,build_dataset_DA
#from build_adadataset import build_dataset_DA
#from experiment_config import EXPERIMENTS,EXPERIMENTS_m
from utils.transforms import random_flip_rotate
from models.layers import conv_block, up_conv
from utils.metrics import MHDValue, DiceScore
from utils.loss import dice_loss
from utils.utils import set_requires_grad, load_pretrained, setup_seed #, eightway_affinity_kld,fourway_affinity_kld

import os
import pdb
import shutil
from runx.logx import logx
import platform



if platform.system() == 'Windows':
    NUM_WORKERS = 0
    UTIL_DIR = r"E:\我的坚果云\sourcecode\python\util"
else:
    NUM_WORKERS = 4
    UTIL_DIR = r"/home/chenxu/我的坚果云/sourcecode/python/util"

sys.path.append(UTIL_DIR)
import common_net_pt as common_net
import common_metrics
import common_pelvic_pt as common_pelvic
import common_amos


# SOURCE = CN()
# SOURCE.dataset = 'IBSR'
# SOURCE.PATH = '/home/huqian/baby/DA_code/IBSR_18/IBSR_18_re' #
# SOURCE.label_s = (9, 10, 11, 12, 13, 17, 18, 48, 49, 50, 51, 52, 53, 54)
# SOURCE.label_t = (1, 1, 2, 3, 4, 5, 6, 1, 1, 2, 3, 4, 5, 6)
# SOURCE.IDs_train = ['08', '09', '02', '07', '04', '05', '16', '03', '06']

# TARGET = CN()
# TARGET.dataset = 'MALC'
# TARGET.PATH = '/home/huqian/baby/DA_code/MICCAI/MALC_re' #
# TARGET.label_s = (59, 60, 36, 37, 57, 58, 55, 56, 47, 48, 31, 32)
# TARGET.label_t = (1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6)
# TARGET.IDs_train = ['20', '28', '08', '31', '06', '35', '34', '25', '13', '05', '01', '21',
#                     '17', '27', '33', '11', '12', '16', '10', '32', '18', '04', '14', '02',
#                     '22', '09', '19']
# TARGET.IDs_eval = ['29', '03', '26', '23']
# TARGET.IDs_test = ['07', '15', '24', '30']

class U_Net_4(nn.Module):

    def __init__(self, in_ch=3, num_classes=7):
        super(U_Net_4, self).__init__()
        n1 = 64
        filters = [n1, n1 * 2, n1 * 4, n1 * 8, n1 * 16]

        self.Maxpool = nn.MaxPool2d(kernel_size=2, stride=2)

        self.Conv1 = conv_block(in_ch, filters[0])
        self.Conv2 = conv_block(filters[0], filters[1])
        self.Conv3 = conv_block(filters[1], filters[2])
        self.Conv4 = conv_block(filters[2], filters[3])
        self.Conv5 = conv_block(filters[3], filters[4])

        self.Up4 = up_conv(filters[4], filters[3])
        self.Up_conv4 = conv_block(filters[4], filters[3])

        self.Up3 = up_conv(filters[3], filters[2])
        self.Up_conv3 = conv_block(filters[3], filters[2])

        self.Up2 = up_conv(filters[2], filters[1])
        self.Up_conv2 = conv_block(filters[2], filters[1])

        self.Up1 = up_conv(filters[1], filters[0])
        self.Up_conv1 = conv_block(filters[1], filters[0])

        self.cls = nn.Conv2d(filters[0], num_classes, kernel_size=1, stride=1, padding=0)

    def forward(self, x):
        e1 = self.Conv1(x)

        e2 = self.Maxpool(e1)
        e2 = self.Conv2(e2)

        e3 = self.Maxpool(e2)
        e3 = self.Conv3(e3)

        e4 = self.Maxpool(e3)
        e4 = self.Conv4(e4)

        e5 = self.Maxpool(e4)
        e5 = self.Conv5(e5)

        d4 = self.Up4(e5)
        d4 = torch.cat((e4, d4), dim=1)
        d4 = self.Up_conv4(d4)

        d3 = self.Up3(d4)                
        d3 = torch.cat((e3, d3), dim=1)
        d3 = self.Up_conv3(d3)

        d2 = self.Up2(d3)
        d2 = torch.cat((e2, d2), dim=1)
        d2 = self.Up_conv2(d2)

        d1 = self.Up1(d2)
        d1 = torch.cat((e1, d1), dim=1)
        d1 = self.Up_conv1(d1)

        out = self.cls(d1)

        return d1, out


class C(nn.Module):
    def __init__(self, num_classes=7):
        super(C, self).__init__() 
        self.cls = nn.Sequential(nn.Conv2d(64, 32, kernel_size=1, stride=1, padding=0),
                                nn.ReLU(inplace=True),
                                nn.Conv2d(32, num_classes, kernel_size=1, stride=1, padding=0))
    def forward(self, x):
        return self.cls(x)
        
class PixelDiscriminator(nn.Module):
    def __init__(self, input_nc, ndf=64, num_classes=7):
        super(PixelDiscriminator, self).__init__()

        self.D = nn.Sequential(
            nn.Conv2d(input_nc, ndf, kernel_size=3, stride=1, padding=1),
            nn.LeakyReLU(negative_slope=0.2, inplace=True),
            nn.Conv2d(ndf, ndf*2, kernel_size=3, stride=1, padding=1),
            nn.LeakyReLU(negative_slope=0.2, inplace=True)
        )
        self.cls = nn.Conv2d(ndf*2, num_classes, kernel_size=1, stride=1)

    def forward(self, x):
       # x = fourway_affinity_kld(x) #fourway_affinity_kld eightway_affinity_kld
        out = self.cls(self.D(x))
        return out


def validation(model, patch_shape, val_data, val_label, num_classes):
    model.eval()

    val_dsc = numpy.zeros((val_data.shape[0], num_classes - 1), numpy.float32)
    with torch.no_grad():
        for i in range(len(val_data)):
            pred = common_net.produce_results(next(model.parameters()).device,
                                              lambda x: torch.softmax(model(x)[1].unsqueeze(2), 1),
                                              [patch_shape, ], [val_data[i], ], data_shape=val_data.shape[1:],
                                              patch_shape=patch_shape, is_seg=True, num_classes=num_classes)
            pred = pred.argmax(0).astype(numpy.float32)

            if num_classes > 2:
                val_dsc[i] = common_metrics.calc_multi_dice(pred, val_label[i], num_cls=num_classes)
            else:
                val_dsc[i] = common_metrics.dc(pred, val_label[i])

    return val_dsc


def test(model, best_checkpoint, test_loader):
    checkpoint = torch.load(best_checkpoint)
    model_state_dict = checkpoint['model_state_dict']
    load_pretrained(model, model_state_dict)
    model.eval()
    pred_all  = []
    label_all = []
    for inputs in test_loader:
        img, label = inputs
        img = img.cuda()
        with torch.no_grad():
            _, outputs = model(img)
            outputs = outputs[0, :, :, :]
        pred = outputs.data.max(0)[1].cpu()
        pred_all.append(pred)
        label_all.append(label)       
    pred_all  = torch.stack(pred_all, dim=0)
    label_all = torch.cat(label_all, dim=0)
    score     = DiceScore(pred_all, label_all, 7)
    score_mhd = MHDValue(pred_all, label_all, 7)

    logx.msg('test:')
    logx.msg('Mean Dice: {}'.format(score['Mean Dice']))
    logx.msg('Thalamus: {}'.format(score['Dice'][0]))
    logx.msg('Caudate: {}'.format(score['Dice'][1]))
    logx.msg('Putamen: {}'.format(score['Dice'][2]))
    logx.msg('Pallidum: {}'.format(score['Dice'][3]))
    logx.msg('Hippocampus: {}'.format(score['Dice'][4]))
    logx.msg('Amygdala: {}'.format(score['Dice'][5]))
    logx.msg('MHD Thalamus: {}'.format(score_mhd['MHD'][0]))
    logx.msg('MHD Caudate: {}'.format(score_mhd['MHD'][1]))
    logx.msg('MHD Putamen: {}'.format(score_mhd['MHD'][2]))
    logx.msg('MHD Pallidum: {}'.format(score_mhd['MHD'][3]))
    logx.msg('MHD Hippocampus: {}'.format(score_mhd['MHD'][4]))
    logx.msg('MHD Amygdala: {}'.format(score_mhd['MHD'][5]))
    logx.msg('----------------------------------------------------------------')

    log_(score, score_mhd, 'val')

def log_(score, score_mhd, phase = 'val', epoch=None):
    log = {
         'Mean Dice'      : score['Mean Dice'],
         'Thalamus'   : score['Dice'][0],
         'Caudate'    : score['Dice'][1],
         'Putamen'    : score['Dice'][2],
         'Pallidum'   : score['Dice'][3],
         'Hippocampus': score['Dice'][4],
         'Amygdala'   : score['Dice'][5],
         'MHD Thalamus'   : score_mhd['MHD'][0],
         'MHD Caudate'    : score_mhd['MHD'][1],
         'MHD Putamen'    : score_mhd['MHD'][2],
         'MHD Pallidum'   : score_mhd['MHD'][3],
         'MHD Hippocampus': score_mhd['MHD'][4],
         'MHD Amygdala'   : score_mhd['MHD'][5]
         }
    logx.metric(phase=phase, metrics=log, epoch=epoch)

def train(model, D, num_iters,dataloader_s, dataloader_t, optimizer, optimizer_D, config):
    model.train()
    D.train()
    source_label = 0
    target_label = 1

    for i, (data_s, data_t) in enumerate(zip(dataloader_s, dataloader_t)):
        # train segmentation network
        set_requires_grad(D, requires_grad=False)
        model.zero_grad()
        img_source, label_source, img_target = data_s["image"].cuda(), data_s["label"].squeeze(1).cuda(), data_t["image"].cuda()
        _, outputs_s = model(img_source)
        _, outputs_t = model(img_target)
        #print(label_source.shape) #4,96,128
        #print(outputs_s.shape) #4,7,96,128
        loss_seg = F.cross_entropy(outputs_s, label_source) + dice_loss(outputs_s, label_source)
                
        D_t = D(F.softmax(outputs_t, dim=1))
        loss_adv = F.binary_cross_entropy_with_logits(D_t, torch.FloatTensor(D_t.data.size()).fill_(source_label).cuda())
        loss = loss_seg + config.lambda_adv * loss_adv
        loss.backward() 
        optimizer.step() 
    
        # train Discriminator
        set_requires_grad(D, requires_grad=True)
            
        # train with source 
        D.zero_grad()
        outputs_s = outputs_s.detach()
        D_s = D(F.softmax(outputs_s, dim=1))
        loss_D_s = F.binary_cross_entropy_with_logits(D_s, torch.FloatTensor(D_s.data.size()).fill_(source_label).cuda())
        
        # train with target
        outputs_t = outputs_t.detach()
        D_t = D(F.softmax(outputs_t, dim=1))
        loss_D_t = F.binary_cross_entropy_with_logits(D_t, torch.FloatTensor(D_t.data.size()).fill_(target_label).cuda())
        loss_D = config.lambda_D * (loss_D_s + loss_D_t)
        loss_D.backward()
        optimizer_D.step()
            

def main(config):
    os.environ["CUDA_VISIBLE_DEVICES"] = str(config.gpu_id)
    setup_seed(config.seed)

    if config.task == "pelvic":
        common_file = common_pelvic
        dataset_s = common_pelvic.Dataset(config.data_dir, "ct", n_slices=1, debug=config.debug)
        dataset_t = common_pelvic.Dataset(config.data_dir, "cbct", n_slices=1, debug=config.debug)
        _, val_data, _, val_label = common_pelvic.load_val_data(config.data_dir)
    elif config.task == "amos":
        common_file = common_amos
        dataset_s = common_amos.Dataset(config.data_dir, modality="ct", n_slices=1, debug=config.debug)
        dataset_t = common_ixi.Dataset(config.data_dir, modality="mr", n_slices=1, debug=config.debug)
        _, val_data, _, val_label = common_amos.load_test_data(config.data_dir, "val")
    else:
        assert 0

    if config.debug:
        val_data = val_data[:1]
        val_label = val_label[:1]

    patch_shape = (1, dataset_s.patch_height, dataset_s.patch_width)
    dataloader_s = torch.utils.data.DataLoader(dataset_s, batch_size=config.batch_size, shuffle=True, pin_memory=True,
                                               drop_last=True)
    dataloader_t = torch.utils.data.DataLoader(dataset_t, batch_size=config.batch_size, shuffle=True, pin_memory=True,
                                               drop_last=True)

    model = U_Net_4(1, num_classes=common_file.NUM_CLASSES).cuda()
    D = PixelDiscriminator(common_file.NUM_CLASSES, num_classes=common_file.NUM_CLASSES).cuda()

    optimizer = torch.optim.SGD(model.parameters(), lr=config.init_lr, momentum=0.9, weight_decay=5e-4)
    optimizer_D = torch.optim.Adam(D.parameters(), lr=config.init_lr_D, betas=(0.5, 0.999))

    scheduler = StepLR(optimizer, step_size=config.step_size, gamma=config.gamma)
  
    lr_decay_function = lambda epoch: 1.0 - max(0, epoch - 20) / float(40)
    scheduler_D = LambdaLR(optimizer_D, lr_lambda=lr_decay_function)

    #t_start = time.time()
    best_dsc = 0
    for epoch in range(config.n_epochs):
        logx.msg('epoch: {}'.format(epoch))
        t_epoch = time.time()

        train(model, D,config.num_iters, dataloader_s, dataloader_t, optimizer, optimizer_D, config)
        scheduler.step() 
        scheduler_D.step()
        t_train = time.time()
        logx.msg('cost {:.2f} seconds in this train epoch'.format(t_train - t_epoch))
        val_dsc = validation(model, patch_shape, val_data, val_label, common_file.NUM_CLASSES)
        if val_dsc.mean() > best_dsc:
            best_dsc = val_dsc.mean()
            save_dict = {
                'model_state_dict': model.state_dict(),
            }
            logx.save_model(save_dict, metric=val_dsc.mean(), epoch="best", higher_better=True)

        logx.msg('Epoch %d  val_dsc: %.4f  best_dsc: %.4f' % (epoch, val_dsc.mean(), best_dsc))

    #best_checkpoint = logx.get_best_checkpoint()
    #test(model, best_checkpoint, eval_loader)
    #t_end = time.time()
    #logx.msg('cost {:.2f} minutes in this train epoch'.format((t_end - t_start)/60))

if __name__ == '__main__':

    parser = argparse.ArgumentParser(description='Hyperparams')
    parser.add_argument('--gpu_id', nargs='?', type=int, default=3)
    parser.add_argument('--seed', nargs='?', type=int, default=200)
    parser.add_argument('--num_iters', nargs='?', type=int, default=250)
    parser.add_argument('--batch_size', nargs='?', type=int, default=4)
    parser.add_argument('--n_epochs', nargs='?', type=int, default=20)
    parser.add_argument('--init_lr', nargs='?', type=float, default=1e-2)
    parser.add_argument('--init_lr_D', nargs='?', type=float, default=1e-4)
    parser.add_argument('--step_size', nargs='?', type=int, default=10)
    parser.add_argument('--gamma', nargs='?', type=float, default=0.1)
    parser.add_argument('--task', type=str, default="pelvic", choices=("pelvic", "amos"))
    parser.add_argument('--data_dir', type=str, default="")
    parser.add_argument('--debug', type=int, default=0)
    parser.add_argument('--experiment', nargs='?', type=int, default=0)#IBSR:7 MALC:0
    parser.add_argument('--logdir', nargs='?', type=str, default='/home/data/hq/DA/train/test')

    parser.add_argument('--lambda_adv', nargs='?', type=float, default=1)
    parser.add_argument('--lambda_D', nargs='?', type=float, default=0.5)

    config = parser.parse_args()
    if os.path.exists(config.logdir):
        shutil.rmtree(config.logdir)
    logx.initialize(logdir=config.logdir, coolname=False, tensorboard=False, hparams=vars(config), no_timestamp=True)

    main(config)
