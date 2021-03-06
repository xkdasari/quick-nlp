import torch
import torch.nn as nn

from fastai.model import Stepper, update_fp32_grads, IS_TORCH_04, trainable_params_, torch_item, copy_fp32_to_model


class S2SStepper(Stepper):
    def __init__(self, m, opt, crit, clip=0, reg_fn=None, fp16=False, loss_scale=1, teacher_forcing_cycle=None,
                 max_kld_step: int = None, teacher_forcing_static_prob: float = None):
        super().__init__(m=m, opt=opt, crit=crit, clip=clip, reg_fn=reg_fn, fp16=fp16,
                         loss_scale=loss_scale)
        self.teacher_forcing_epochs = teacher_forcing_cycle  # if None to disable teacher forcing schedule
        self.max_kld_step = max_kld_step  # optionally used in CVAE
        self.static_teacher_forcing_probability = teacher_forcing_static_prob  # if a float use a static teacher forcing probability

    def step(self, xs, y, epoch):
        xtra = []
        # teacher forcing setup
        if self.static_teacher_forcing_probability is not None:
            self.m.pr_force = self.static_teacher_forcing_probability
        elif self.teacher_forcing_epochs is None:
            self.m.pr_force = 1.
        elif 0 <= epoch < self.teacher_forcing_epochs:
            self.m.pr_force = (self.teacher_forcing_epochs - epoch) / self.teacher_forcing_epochs
        else:
            self.m.pr_force = 0.
        output = self.m(*xs)
        if isinstance(output, tuple): output, *xtra = output
        if self.fp16:
            self.m.zero_grad()
        else:
            self.opt.zero_grad()
        loss = raw_loss = self.crit(output, y, step=epoch, max_kld_step=self.max_kld_step)
        if self.loss_scale != 1: assert (self.fp16); loss = loss * self.loss_scale
        if self.reg_fn: loss = self.reg_fn(output, xtra, raw_loss)
        loss.backward()
        if self.fp16: update_fp32_grads(self.fp32_params, self.m)
        if self.loss_scale != 1:
            for param in self.fp32_params: param.grad.data.div_(self.loss_scale)
        if self.clip:  # Gradient clipping
            if IS_TORCH_04:
                nn.utils.clip_grad_norm_(trainable_params_(self.m), self.clip)
            else:
                nn.utils.clip_grad_norm(trainable_params_(self.m), self.clip)
        if 'wd' in self.opt.param_groups and self.opt.param_groups['wd'] != 0:
            # Weight decay out of the loss. After the gradient computation but before the step.
            for group in self.opt.param_groups:
                lr, wd = group['lr'], group['wd']
                for p in group['params']:
                    if p.grad is not None: p.data = p.data.add(-wd * lr, p.data)
        self.opt.step()
        if self.fp16:
            copy_fp32_to_model(self.m, self.fp32_params)
            torch.cuda.synchronize()
        return torch_item(raw_loss.data)

    def evaluate(self, xs, y):
        preds = self.m(*xs)
        if isinstance(preds, tuple): preds = preds[0]
        return preds, self.crit(preds, y)
