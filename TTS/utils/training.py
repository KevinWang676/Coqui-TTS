import torch
import numpy as np


def setup_torch_training_env(cudnn_enable, cudnn_benchmark):
    torch.backends.cudnn.enabled = cudnn_enable
    torch.backends.cudnn.benchmark = cudnn_benchmark
    torch.manual_seed(54321)
    use_cuda = torch.cuda.is_available()
    num_gpus = torch.cuda.device_count()
    print(" > Using CUDA: ", use_cuda)
    print(" > Number of GPUs: ", num_gpus)
    return use_cuda, num_gpus


def check_update(model, grad_clip, ignore_stopnet=False, amp_opt_params=None):
    r'''Check model gradient against unexpected jumps and failures'''
    skip_flag = False
    if ignore_stopnet:
        if not amp_opt_params:
            grad_norm = torch.nn.utils.clip_grad_norm_(
                [param for name, param in model.named_parameters() if 'stopnet' not in name], grad_clip)
        else:
            grad_norm = torch.nn.utils.clip_grad_norm_(amp_opt_params, grad_clip)
    else:
        if not amp_opt_params:
            grad_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), grad_clip)
        else:
            grad_norm = torch.nn.utils.clip_grad_norm_(amp_opt_params, grad_clip)

    # compatibility with different torch versions
    if isinstance(grad_norm, float):
        if np.isinf(grad_norm):
            print(" | > Gradient is INF !!")
            skip_flag = True
    else:
        if torch.isinf(grad_norm):
            print(" | > Gradient is INF !!")
            skip_flag = True
    return grad_norm, skip_flag


def lr_decay(init_lr, global_step, warmup_steps):
    r'''from https://github.com/r9y9/tacotron_pytorch/blob/master/train.py'''
    warmup_steps = float(warmup_steps)
    step = global_step + 1.
    lr = init_lr * warmup_steps**0.5 * np.minimum(step * warmup_steps**-1.5,
                                                  step**-0.5)
    return lr


def adam_weight_decay(optimizer):
    """
    Custom weight decay operation, not effecting grad values.
    """
    for group in optimizer.param_groups:
        for param in group['params']:
            current_lr = group['lr']
            weight_decay = group['weight_decay']
            factor = -weight_decay * group['lr']
            param.data = param.data.add(param.data,
                                        alpha=factor)
    return optimizer, current_lr

# pylint: disable=dangerous-default-value
def set_weight_decay(model, weight_decay, skip_list={"decoder.attention.v", "rnn", "lstm", "gru", "embedding"}):
    """
    Skip biases, BatchNorm parameters, rnns.
    and attention projection layer v
    """
    decay = []
    no_decay = []
    for name, param in model.named_parameters():
        # skip beta because we're sending it to another optimizer
        if name == 'capacitron_layer.beta':
            continue
        if not param.requires_grad:
            continue

        if len(param.shape) == 1 or any([skip_name in name for skip_name in skip_list]):
            no_decay.append(param)
        else:
            decay.append(param)
    return [{
        'params': no_decay,
        'weight_decay': 0.
    }, {
        'params': decay,
        'weight_decay': weight_decay
    }]


# pylint: disable=protected-access
class NoamLR(torch.optim.lr_scheduler._LRScheduler):
    def __init__(self, optimizer, warmup_steps=0.1, last_epoch=-1):
        self.warmup_steps = float(warmup_steps)
        super(NoamLR, self).__init__(optimizer, last_epoch)

    def get_lr(self):
        step = max(self.last_epoch, 1)
        return [
            base_lr * self.warmup_steps**0.5 *
            min(step * self.warmup_steps**-1.5, step**-0.5)
            for base_lr in self.base_lrs
        ]

class StepwiseGradualLR(torch.optim.lr_scheduler._LRScheduler):
    def __init__(self, optimizer, config, last_epoch=-1):
        self.config = config
        super(StepwiseGradualLR, self).__init__(optimizer, last_epoch)

    def get_lr(self):
        step = max(self.last_epoch, 1)
        step_thresholds = []
        rates = []
        for values in self.config.gradual_learning_rates:
            step_thresholds.append(values[0])
            rates.append(values[1])

        boolean_indeces = np.less(step_thresholds, step)
        try:
            first_false = np.where(boolean_indeces == False)[0]
            first_false = first_false[0]
        except IndexError:
            # For the steps larger than the last step in the list
            pass
        lr = rates[np.max(first_false - 1, 0)]

        # Return last lr if step is above the set threshold
        lr = rates[-1] if step > step_thresholds[-1] else lr
        # Return first lr if step is below the second threshold - first is initial lr
        lr = rates[0] if step < step_thresholds[1] else lr

        return np.tile(lr, len(self.base_lrs)) # hack?

def gradual_training_scheduler(global_step, config):
    """Setup the gradual training schedule wrt number
    of active GPUs"""
    num_gpus = torch.cuda.device_count()
    if num_gpus == 0:
        num_gpus = 1
    new_values = None
    # we set the scheduling wrt num_gpus
    for values in config.gradual_training:
        if global_step * num_gpus >= values[0]:
            new_values = values
    return new_values[1], new_values[2]
