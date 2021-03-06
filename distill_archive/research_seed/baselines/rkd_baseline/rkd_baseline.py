"""
This file defines the core research contribution   
"""
import os
import torch
from torch.nn import functional as F
import torch.nn as nn
import torchvision
from torch.utils.data import DataLoader
import torchvision.transforms as transforms
from argparse import ArgumentParser, Action
from research_seed.baselines.model.model_factory import create_cnn_model, is_resnet
import torch.optim as optim
import pytorch_lightning as pl
import numpy as np
from collections import OrderedDict
import losses
import pairs
from enum import Enum
from embedding import LinearEmbedding
import argparse
from metrics import recall, pdist

class Train_Mode(Enum):
    TEACHER = 1
    STUDENT = 2

def str2bool(v):
	if v.lower() in ('yes', 'true', 't', 'y', '1'):
		return True
	else:
		return False

def load_model_chk(model, path):
    chkp = torch.load(path)
    new_state_dict = OrderedDict()
    for k, v in chkp['state_dict'].items():
        name = k[6:] # remove `model.`
        new_state_dict[name] = v
    model.load_state_dict(new_state_dict)
    return model

def addEmbedding(base, hparams):
    embed = LinearEmbedding(base, 
        output_size=hparams.output_size, 
        embedding_size=hparams.embedding_size, 
        normalize=hparams.l2normalize == 'true')

    return embed


class RKD_Cifar(pl.LightningModule):

    def __init__(self, student_base, teacher_base=None, hparams=None, mode=Train_Mode.TEACHER):
        super(RKD_Cifar, self).__init__()
        # not the best model...
        self.hparams = hparams
        self.mode = mode

        if self.mode == Train_Mode.STUDENT:
            raise ValueError("No implemented yet !")
        elif self.mode == Train_Mode.TEACHER:
            for m in student_base.modules():
                m.requires_grad = False
            self.student = addEmbedding(student_base, hparams)
            self.student.train()


        if self.mode == Train_Mode.TEACHER:
            self.criterionFM = losses.L2Triplet(sampler=hparams.sample(), margin=hparams.margin)
            

        self.train_step = 0
        self.train_num_correct = 0

        self.val_step = 0
        self.val_num_correct = 0

        self.embeddings_all, self.labels_all = [], []
        self.K = hparams.recall


    def loss_fn_kd(self, outputs, labels, teacher_outputs):
        """
        Credits: https://github.com/peterliht/knowledge-distillation-pytorch/blob/e4c40132fed5a45e39a6ef7a77b15e5d389186f8/model/net.py#L100

        Compute the knowledge-distillation (KD) loss given outputs, labels.
        "Hyperparameters": temperature and alpha
        NOTE: the KL Divergence for PyTorch comparing the softmaxs of teacher
        and student expects the input tensor to be log probabilities! See Issue #2
        """
        
        alpha = self.hparams.alpha
        T = self.hparams.temperature
        loss = nn.KLDivLoss()(F.log_softmax(outputs/T, dim=1),
                                F.softmax(teacher_outputs/T, dim=1)) * (alpha * T * T) + \
                F.cross_entropy(outputs, labels) * (1. - alpha)
        
        return loss

    def forward(self, x, mode):
        if mode == 'student':
            return self.student(x)
        elif mode == 'teacher':
            return self.teacher(x)
        else:
            raise ValueError("mode should be teacher or student")

    def training_step(self, batch, batch_idx):

        x, y = batch
        
        if self.mode == Train_Mode.TEACHER:
            
            embedding = self.student(x)
            loss = self.criterionFM(embedding, y)

            loss_metrics = {
                'train_loss' : loss.item(),
            }

        return {
            'loss': loss,
            'log' : loss_metrics
        }


    def validation_step(self, batch, batch_idx):
        self.student.eval()
        x, y = batch

        if self.mode == Train_Mode.TEACHER:
            embedding = self.student(x)
            val_loss = self.criterionFM(embedding, y)
            self.embeddings_all.append(embedding.data)
            self.labels_all.append(y.data)

            return {
                'val_loss': val_loss,
            }

    def validation_end(self, outputs):
        # OPTIONAL
        avg_loss = torch.stack([x['val_loss'] for x in outputs]).mean()

        if self.mode == Train_Mode.TEACHER:
            self.embeddings_all = torch.cat(self.embeddings_all).cpu()
            self.labels_all = torch.cat(self.labels_all).cpu()
            rec = recall(self.embeddings_all, self.labels_all, K=self.K)

            log_metrics = {
                    "recall" : rec[0],
                    "val_loss": avg_loss.item(),
            }
        
        self.embeddings_all, self.labels_all = [], []

        return { 'val_loss': avg_loss, 'log': log_metrics}
        

    def configure_optimizers(self):
        # REQUIRED
        # can return multiple optimizers and learning_rate schedulers

        self.optimizer = optim.Adam(self.student.parameters(), lr=self.hparams.lr, weight_decay=1e-5)
        self.scheduler = optim.lr_scheduler.MultiStepLR(
                        self.optimizer, milestones=self.hparams.lr_decay_epochs, 
                        gamma=self.hparams.lr_decay_gamma)
        
        return self.optimizer

    @pl.data_loader
    def train_dataloader(self):

        if self.hparams.dataset == 'cifar10' or self.hparams.dataset == 'cifar100':
            transform_train = transforms.Compose([
                transforms.Pad(4, padding_mode="reflect"),
                transforms.RandomCrop(32, padding=4),
                transforms.RandomHorizontalFlip(),
                transforms.ToTensor(),
                transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
            ])
        else:
            raise ValueError('Dataset not supported !')

        trainset = torchvision.datasets.CIFAR10(root=self.hparams.dataset_dir, train=True,
												 download=True, transform=transform_train)
        if self.hparams.gpus > 1:
            dist_sampler = torch.utils.data.distributed.DistributedSampler(trainset)
        else:
            dist_sampler = None

        return DataLoader(trainset, batch_size=self.hparams.batch_size, 
                            num_workers=self.hparams.num_workers, sampler=dist_sampler)

    @pl.data_loader
    def val_dataloader(self):
        
        if self.hparams.dataset == 'cifar10' or self.hparams.dataset == 'cifar100':
            transform_test = transforms.Compose([
                transforms.ToTensor(),
                transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
            ])
        else:
            raise ValueError('Dataset not supported !')

        valset = torchvision.datasets.CIFAR10(root=self.hparams.dataset_dir, train=False,
												download=True, transform=transform_test)
        if self.hparams.gpus > 1:
            dist_sampler = torch.utils.data.distributed.DistributedSampler(valset)
        else:
            dist_sampler = None

        return DataLoader(valset, batch_size=self.hparams.batch_size, num_workers=self.hparams.num_workers, sampler=dist_sampler)

    @pl.data_loader
    def test_dataloader(self):
        
        if self.hparams.dataset == 'cifar10' or self.hparams.dataset == 'cifar100':
            transform_test = transforms.Compose([
                transforms.ToTensor(),
                transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
            ])
        else:
            raise ValueError('Dataset not supported !')

        testset = torchvision.datasets.CIFAR10(root=self.hparams.dataset_dir, train=False,
												download=True, transform=transform_test)
        if self.hparams.gpus > 1:
            dist_sampler = torch.utils.data.distributed.DistributedSampler(testset)
        else:
            dist_sampler = None

        return DataLoader(testset, batch_size=self.hparams.batch_size, num_workers=self.hparams.num_workers, sampler=dist_sampler)


    @staticmethod
    def add_model_specific_args(parent_parser):
        """
        Specify the hyperparams for this LightningModule
        """
        # MODEL specific
        LookupChoices = type('', (argparse.Action, ), dict(__call__=lambda a, p, n, v, o: setattr(n, a.dest, a.choices[v])))

        parser = ArgumentParser(parents=[parent_parser])
        parser.add_argument('--dataset', default='cifar10', type=str, help='dataset. can be either cifar10 or cifar100')
        parser.add_argument('--batch-size', default=128, type=int, help='batch_size')
        parser.add_argument('--learning-rate', default=0.001, type=float, help='initial learning rate')
        parser.add_argument('--momentum', default=0.9, type=float,  help='SGD momentum')
        parser.add_argument('--weight-decay', default=1e-4, type=float, help='SGD weight decay (default: 1e-4)')
        parser.add_argument('--dataset-dir', default='./data', type=str,  help='dataset directory')
        parser.add_argument('--optim', default='adam', type=str, help='Optimizer')
        parser.add_argument('--num-workers', default=4, type=float,  help='Num workers for data loader')
        parser.add_argument('--student-model', default='resnet8', type=str, help='teacher student name')
        parser.add_argument('--teacher-model', default='resnet110', type=str, help='teacher student name')
        parser.add_argument('--path-to-teacher', default='', type=str, help='teacher chkp path')
        parser.add_argument('--temperature', default=10, type=float, help='Temperature for knowledge distillation')
        parser.add_argument('--alpha', default=0.7, type=float, help='Alpha for knowledge distillation')

        parser.add_argument('--triplet_ratio', default=0, type=float)
        parser.add_argument('--dist_ratio', default=0, type=float)
        parser.add_argument('--angle_ratio', default=0, type=float)

        parser.add_argument('--dark_ratio', default=0, type=float)
        parser.add_argument('--dark_alpha', default=2, type=float)
        parser.add_argument('--dark_beta', default=3, type=float)

        parser.add_argument('--at_ratio', default=0, type=float)

        parser.add_argument('--triplet_sample',
                            choices=dict(random=pairs.RandomNegative,
                                        hard=pairs.HardNegative,
                                        all=pairs.AllPairs,
                                        semihard=pairs.SemiHardNegative,
                                        distance=pairs.DistanceWeighted),
                            default=pairs.DistanceWeighted,
                            action=LookupChoices)
        
        parser.add_argument('--sample',
                    choices=dict(random=pairs.RandomNegative,
                                 hard=pairs.HardNegative,
                                 all=pairs.AllPairs,
                                 semihard=pairs.SemiHardNegative,
                                 distance=pairs.DistanceWeighted),
                    default=pairs.AllPairs,
                    action=LookupChoices)

        parser.add_argument('--triplet_margin', type=float, default=0.2)
        parser.add_argument('--l2normalize', choices=['true', 'false'], default='true')
        parser.add_argument('--embedding_size', default=128, type=int)

        parser.add_argument('--teacher_l2normalize', choices=['true', 'false'], default='true')
        parser.add_argument('--teacher_embedding_size', default=128, type=int)

                
        parser.add_argument('--lr', default=1e-5, type=float)
        parser.add_argument('--lr_decay_epochs', type=int,
                            default=[25, 30, 35], nargs='+')
        parser.add_argument('--lr_decay_gamma', default=0.5, type=float)
        parser.add_argument('--data', default='data')
        parser.add_argument('--batch', default=64, type=int)
        parser.add_argument('--iter_per_epoch', default=100, type=int)
        parser.add_argument('--output-size', type=int, default=4096)
        parser.add_argument('--recall', default=[1], type=int, nargs='+')
        parser.add_argument('--save_dir', default=None)
        parser.add_argument('--load', default=None)
        parser.add_argument('--margin', type=float, default=0.2)

        return parser