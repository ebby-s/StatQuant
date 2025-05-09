import os
import time
import numpy as np
import torch
import torch.nn as nn
from torch.autograd import Variable
from . import logger as log
from . import resnet as models
from . import utils
from .debug import dump, fast_dump, plot_bin_hist, write_errors, fast_dump_2, variance_profile, get_var, plot_weight_hist

from torch.cuda import amp

# try:
#     from apex.parallel import DistributedDataParallel as DDP
#     from apex.fp16_utils import *
#     from apex import amp
# except ImportError:
#     raise ImportError("Please install apex from https://www.github.com/nvidia/apex to run this example.")


class ModelAndLoss(nn.Module):
    def __init__(self, arch, loss, pretrained_weights=None, cuda=True, fp16=False):
        super(ModelAndLoss, self).__init__()
        self.arch = arch

        print("=> creating model '{}'".format(arch))
        model = models.build_resnet(arch[0], arch[1])
        if pretrained_weights is not None:
            print("=> using pre-trained model from a file '{}'".format(arch))
            model.load_state_dict(pretrained_weights)

        if cuda:
            model = model.cuda()
        if fp16:
            # model = network_to_half(model)
            pass

        # define loss function (criterion) and optimizer
        criterion = loss()

        if cuda:
            criterion = criterion.cuda()

        self.model = model
        self.loss = criterion

    def forward(self, data, target):
        with amp.autocast():
            output = self.model(data)
            loss = self.loss(output, target)

        return loss, output

    def distributed(self):
        # self.model = DDP(self.model)
        pass

    def load_model_state(self, state):
        if not state is None:
            self.model.load_state_dict(state)


def get_optimizer(parameters, fp16, lr, momentum, weight_decay,
                  nesterov=False,
                  state=None,
                  static_loss_scale=1., dynamic_loss_scale=False,
                  bn_weight_decay = False):

    if bn_weight_decay:
        print(" ! Weight decay applied to BN parameters ")
        optimizer = torch.optim.SGD([v for n, v in parameters], lr,
                                    momentum=momentum,
                                    weight_decay=weight_decay,
                                    nesterov = nesterov)
    else:
        print(" ! Weight decay NOT applied to BN parameters ")
        bn_params = [v for n, v in parameters if 'bn' in n]
        rest_params = [v for n, v in parameters if not 'bn' in n]
        print(len(bn_params))
        print(len(rest_params))
        optimizer = torch.optim.SGD([{'params': bn_params, 'weight_decay' : 0},
                                     {'params': rest_params, 'weight_decay' : weight_decay}],
                                    lr,
                                    momentum=momentum,
                                    weight_decay=weight_decay,
                                    nesterov = nesterov)
    if fp16:
        # optimizer = FP16_Optimizer(optimizer,
        #                            static_loss_scale=static_loss_scale,
        #                            dynamic_loss_scale=dynamic_loss_scale,
        #                            verbose=False)
        pass

    if not state is None:
        optimizer.load_state_dict(state)

    return optimizer


def lr_policy(lr_fn, logger=None):
    if logger is not None:
        logger.register_metric('lr', log.IterationMeter(), log_level=1)
    def _alr(optimizer, iteration, epoch):
        lr = lr_fn(iteration, epoch)

        if logger is not None:
            logger.log_metric('lr', lr)
        for param_group in optimizer.param_groups:
            param_group['lr'] = lr

    return _alr


def lr_step_policy(base_lr, steps, decay_factor, warmup_length, logger=None):
    def _lr_fn(iteration, epoch):
        if epoch < warmup_length:
            lr = base_lr * (epoch + 1) / warmup_length
        else:
            lr = base_lr
            for s in steps:
                if epoch >= s:
                    lr *= decay_factor
        return lr

    return lr_policy(_lr_fn, logger=logger)


def lr_linear_policy(base_lr, warmup_length, epochs, logger=None):
    def _lr_fn(iteration, epoch):
        if epoch < warmup_length:
            lr = base_lr * (epoch + 1) / warmup_length
        else:
            e = epoch - warmup_length
            es = epochs - warmup_length
            lr = base_lr * (1-(e/es))
        return lr

    return lr_policy(_lr_fn, logger=logger)


def lr_cosine_policy(base_lr, warmup_length, epochs, logger=None):
    def _lr_fn(iteration, epoch):
        if epoch < warmup_length:
            lr = base_lr * (epoch + 1) / warmup_length
        else:
            e = epoch - warmup_length
            es = epochs - warmup_length
            lr = 0.5 * (1 + np.cos(np.pi * e / es)) * base_lr
        return lr

    return lr_policy(_lr_fn, logger=logger)


def lr_exponential_policy(base_lr, warmup_length, epochs, final_multiplier=0.001, logger=None):
    es = epochs - warmup_length
    epoch_decay = np.power(2, np.log2(final_multiplier)/es)

    def _lr_fn(iteration, epoch):
        if epoch < warmup_length:
            lr = base_lr * (epoch + 1) / warmup_length
        else:
            e = epoch - warmup_length
            lr = base_lr * (epoch_decay ** e)
        return lr

    return lr_policy(_lr_fn, logger=logger)



def get_train_step(model_and_loss, optimizer, fp16, use_amp = False, batch_size_multiplier = 1):
    def _step(input, target, optimizer_step = True):
        input_var = Variable(input)
        target_var = Variable(target)
        loss, output = model_and_loss(input_var, target_var)
        prec1, prec5 = torch.zeros(1), torch.zeros(1) #utils.accuracy(output.data, target, topk=(1, 5))

        if torch.distributed.is_initialized():
            reduced_loss = utils.reduce_tensor(loss.data)
            #prec1 = reduce_tensor(prec1)
            #prec5 = reduce_tensor(prec5)
        else:
            reduced_loss = loss.data

        if fp16:
            optimizer.backward(loss)
        elif use_amp:
            # with amp.scale_loss(loss, optimizer) as scaled_loss:
            #     scaled_loss.backward()
            pass
        else:
            loss.backward()

        if optimizer_step:
            # opt = optimizer.optimizer if isinstance(optimizer, FP16_Optimizer) else optimizer
            opt = optimizer
            for param_group in opt.param_groups:
                for param in param_group['params']:
                    param.grad /= batch_size_multiplier

            optimizer.step()
            optimizer.zero_grad()

        torch.cuda.synchronize()

        return reduced_loss, prec1, prec5

    return _step


def train(train_loader, model_and_loss, optimizer, lr_scheduler, fp16, logger, epoch, use_amp=False, prof=-1, batch_size_multiplier=1, register_metrics=True):

    if register_metrics and logger is not None:
        logger.register_metric('train.top1', log.AverageMeter(), log_level = 0)
        logger.register_metric('train.top5', log.AverageMeter(), log_level = 0)
        logger.register_metric('train.loss', log.AverageMeter(), log_level = 0)
        logger.register_metric('train.compute_ips', log.AverageMeter(), log_level=1)
        logger.register_metric('train.total_ips', log.AverageMeter(), log_level=0)
        logger.register_metric('train.data_time', log.AverageMeter(), log_level=1)
        logger.register_metric('train.compute_time', log.AverageMeter(), log_level=1)

    step = get_train_step(model_and_loss, optimizer, fp16, use_amp = use_amp, batch_size_multiplier = batch_size_multiplier)

    model_and_loss.train()
    end = time.time()

    optimizer.zero_grad()

    data_iter = enumerate(train_loader)
    if logger is not None:
        data_iter = logger.iteration_generator_wrapper(data_iter)

    for i, (input, target) in data_iter:
        bs = input.size(0)
        lr_scheduler(optimizer, i, epoch)
        data_time = time.time() - end

        if prof > 0:
            if i >= prof:
                break

        optimizer_step = ((i + 1) % batch_size_multiplier) == 0
        loss, prec1, prec5 = step(input, target, optimizer_step = optimizer_step)

        it_time = time.time() - end

        if logger is not None:
            logger.log_metric('train.top1', prec1.item())
            logger.log_metric('train.top5', prec5.item())
            logger.log_metric('train.loss', loss.item())
            logger.log_metric('train.compute_ips', calc_ips(bs, it_time - data_time))
            logger.log_metric('train.total_ips', calc_ips(bs, it_time))
            logger.log_metric('train.data_time', data_time)
            logger.log_metric('train.compute_time', it_time - data_time)

        end = time.time()



def get_val_step(model_and_loss):
    def _step(input, target):
        input_var = Variable(input)
        target_var = Variable(target)

        with torch.no_grad():
            loss, output = model_and_loss(input_var, target_var)

        prec1, prec5 = utils.accuracy(output.data, target, topk=(1, 5))

        if torch.distributed.is_initialized():
            reduced_loss = utils.reduce_tensor(loss.data)
            prec1 = utils.reduce_tensor(prec1)
            prec5 = utils.reduce_tensor(prec5)
        else:
            reduced_loss = loss.data

        torch.cuda.synchronize()

        return reduced_loss, prec1, prec5

    return _step


def validate(val_loader, model_and_loss, fp16, logger, epoch, prof=-1, register_metrics=True):
    if register_metrics and logger is not None:
        logger.register_metric('val.top1',         log.AverageMeter(), log_level = 0)
        logger.register_metric('val.top5',         log.AverageMeter(), log_level = 0)
        logger.register_metric('val.loss',         log.AverageMeter(), log_level = 0)
        logger.register_metric('val.compute_ips',  log.AverageMeter(), log_level = 1)
        logger.register_metric('val.total_ips',    log.AverageMeter(), log_level = 1)
        logger.register_metric('val.data_time',    log.AverageMeter(), log_level = 1)
        logger.register_metric('val.compute_time', log.AverageMeter(), log_level = 1)

    step = get_val_step(model_and_loss)

    top1 = log.AverageMeter()
    # switch to evaluate mode
    model_and_loss.eval()

    end = time.time()

    data_iter = enumerate(val_loader)
    if not logger is None:
        data_iter = logger.iteration_generator_wrapper(data_iter, val=True)

    for i, (input, target) in data_iter:
        bs = input.size(0)
        data_time = time.time() - end
        if prof > 0:
            if i > prof:
                break

        loss, prec1, prec5 = step(input, target)

        it_time = time.time() - end

        top1.record(prec1.item(), bs)
        if logger is not None:
            logger.log_metric('val.top1', prec1.item())
            logger.log_metric('val.top5', prec5.item())
            logger.log_metric('val.loss', loss.item())
            logger.log_metric('val.compute_ips', calc_ips(bs, it_time - data_time))
            logger.log_metric('val.total_ips', calc_ips(bs, it_time))
            logger.log_metric('val.data_time', data_time)
            logger.log_metric('val.compute_time', it_time - data_time)

        end = time.time()

    return top1.get_val()

# Train loop {{{
def calc_ips(batch_size, time):
    world_size = torch.distributed.get_world_size() if torch.distributed.is_initialized() else 1
    tbs = world_size * batch_size
    return tbs/time

def train_loop(model_and_loss, optimizer, lr_scheduler, train_loader, val_loader, debug_loader, epochs, fp16, logger,
               should_backup_checkpoint, use_amp=False,
               batch_size_multiplier = 1,
               best_prec1 = 0, start_epoch = 0, prof = -1, skip_training = False, skip_validation = False, save_checkpoints = True, checkpoint_dir='./'):
    prec1 = -1

    epoch_iter = range(start_epoch, epochs)
    if logger is not None:
        epoch_iter = logger.epoch_generator_wrapper(epoch_iter)
    for epoch in epoch_iter:
        print('Epoch ', epoch)
        if not skip_training:
            train(train_loader, model_and_loss, optimizer, lr_scheduler, fp16, logger, epoch, use_amp = use_amp, prof = prof, register_metrics=epoch==start_epoch, batch_size_multiplier=batch_size_multiplier)

        if not skip_validation:
            prec1 = validate(val_loader, model_and_loss, fp16, logger, epoch, prof = prof, register_metrics=epoch==start_epoch)

        if save_checkpoints and (not torch.distributed.is_initialized() or torch.distributed.get_rank() == 0):
            if not skip_training:
                is_best = prec1 > best_prec1
                best_prec1 = max(prec1, best_prec1)

                if should_backup_checkpoint(epoch):
                    backup_filename = 'checkpoint-{}.pth.tar'.format(epoch + 1)
                else:
                    backup_filename = None
                utils.save_checkpoint({
                    'epoch': epoch + 1,
                    'arch': model_and_loss.arch,
                    'state_dict': model_and_loss.model.state_dict(),
                    'best_prec1': best_prec1,
                    'optimizer' : optimizer.state_dict(),
                }, is_best, checkpoint_dir=checkpoint_dir, backup_filename=backup_filename)

        if not torch.distributed.is_initialized() or torch.distributed.get_rank() == 0:
            logger.end()

    if skip_training:
        # fast_dump_2(model_and_loss, optimizer, train_loader, checkpoint_dir)
        # dump(model_and_loss, optimizer, train_loader, checkpoint_dir)
        plot_bin_hist(model_and_loss, optimizer, val_loader)
        # write_errors(model_and_loss, optimizer, debug_loader)
        # variance_profile(model_and_loss, optimizer, debug_loader)
        # get_var(model_and_loss, optimizer, train_loader)
        # plot_weight_hist(model_and_loss, optimizer, train_loader)
