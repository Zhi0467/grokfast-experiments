import math
from argparse import ArgumentParser
from itertools import permutations
import copy

import matplotlib.pyplot as plt
from tqdm import tqdm

import torch
import torch.nn as nn
import torch.nn.functional as F

from grokfast import *


class Block(nn.Module):
    """Causal transformer block
    """

    def __init__(self, dim, num_heads):
        super().__init__()
        self.ln_1 = nn.LayerNorm(dim)
        self.ln_2 = nn.LayerNorm(dim)
        self.attn = nn.MultiheadAttention(dim, num_heads)
        self.mlp = nn.Sequential(
            nn.Linear(dim, dim * 4),
            nn.GELU(),
            nn.Linear(dim * 4, dim),
        )

    def forward(self, x):
        attn_mask = torch.full(
            (len(x), len(x)), -float("Inf"), device=x.device, dtype=x.dtype
        )
        attn_mask = torch.triu(attn_mask, diagonal=1)
        attn_mask[torch.isnan(attn_mask)] = 0.0 # fixes all 'nan' on 'mps' device

        x = self.ln_1(x)
        a, _ = self.attn(x, x, x, attn_mask=attn_mask, need_weights=False)
        x = x + a
        m = self.mlp(self.ln_2(x))
        x = x + m
        return x


class Decoder(nn.Module):
    """Causal Transformer decoder
    """

    def __init__(self, dim=128, num_layers=2, num_heads=4, num_tokens=97, seq_len=5):
        super().__init__()
        self.token_embeddings = nn.Embedding(num_tokens, dim)
        self.position_embeddings = nn.Embedding(seq_len, dim)
        self.layers = nn.ModuleList()
        for _ in range(num_layers):
            self.layers.append(Block(dim, num_heads))

        self.ln_f = nn.LayerNorm(dim)
        self.head = nn.Linear(dim, num_tokens, bias=False)

    def forward(self, x):
        h = self.token_embeddings(x)
        positions = torch.arange(x.shape[0], device=x.device).unsqueeze(-1)
        h = h + self.position_embeddings(positions).expand_as(h)
        for layer in self.layers:
            h = layer(h)

        h = self.ln_f(h)
        logits = self.head(h)
        return logits


def multiplication_mod_p_data(p, eq_token, op_token):
    """x◦y = x/y (mod p) for 0 ≤ x < p, 0 < y < p
    """
    x = torch.arange(p)
    y = torch.arange(1, p)
    x, y = torch.cartesian_prod(x, y).T

    eq = torch.ones_like(x) * eq_token
    op = torch.ones_like(x) * op_token
    result = (x**2 + x * y + y**2 ) % p

    # "All of our experiments used a small transformer trained on datasets of
    # equations of the form a◦b = c, where each of “a”, “◦”, “b”, “=”, and “c”
    # is a seperate token"
    return torch.stack([x, op, y, eq, result])

def compute_sparsity(model):
    total_params = 0
    zero_params = 0
    for param in model.parameters():
        total_params += param.numel()
        zero_params += (param == 0).sum().item()
    sparsity = zero_params / total_params
    return sparsity

def main(args):
    torch.manual_seed(args.seed)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # tokens for <op> and <=>. It's not clear why <=> is needed at all since it
    # has no effect on the output, but we'll leave it in to best follow the
    # paper.
    eq_token = args.p
    op_token = args.p + 1

    # "We trained a standard decoder-only transformer (Vaswani et al., 2017)
    # with causal attention masking, and calculated loss and accuracy only on
    # the answer part of the equation. For all experiments we used a
    # transformer with 2 layers, width 128, and 4 attention heads"
    model = Decoder(
        dim=128, num_layers=2, num_heads=4, num_tokens=args.p + 2, seq_len=5
    ).to(device)
    nparams = sum([p.numel() for p in model.parameters() if p.requires_grad])
    print(model)
    print(f'Total number of parameters: {nparams}')

    data = multiplication_mod_p_data(args.p, eq_token, op_token)

    train_idx, valid_idx = torch.randperm(data.shape[1]).split(data.shape[1] // 2)
    train_data, valid_data = data[:, train_idx], data[:, valid_idx]

    # For most experiments we used AdamW optimizer with learning rate 10−3,
    # weight decay 1, β1 = 0.9, β2 = 0.98
    optimizer = getattr(torch.optim, args.optimizer)(
        model.parameters(),
        lr=args.lr,
        weight_decay=args.weight_decay,
        betas=(args.beta1, args.beta2),
    )

    #  linear learning rate warmup over the first 20 updates
    scheduler = torch.optim.lr_scheduler.LambdaLR(
        optimizer, lambda update: 1 if update > 10 else update / 10
    )

    steps_per_epoch = math.ceil(train_data.shape[1] / args.batch_size)

    its, train_acc, val_acc, train_loss, val_loss = [], [], [], [], []
    grads = None
    i = 0

    # For logging network weights.
    net_its, nets = [], []

    initial_state = copy.deepcopy(model.state_dict())
    init_vector = torch.cat([p.view(-1) for p in initial_state.values()])

    param_norms_l2, param_distances_l2 = [], []
    param_norms_l1, param_distances_l1 = [], []
    sparsity_log = []

    for e in tqdm(range(int(args.budget) // steps_per_epoch)):

        # randomly shuffle train data
        train_data = train_data[:, torch.randperm(train_data.shape[1])]

        for data, is_train in [(train_data, True), (valid_data, False)]:

            model.train(is_train)
            total_loss = 0
            total_acc = 0

            # torch.split faster than dataloader with tensor
            dl = torch.split(data, args.batch_size, dim=1)
            for input in dl:
                input = input.to(device)

                with torch.set_grad_enabled(is_train):
                    logits = model(input[:-1])
                    # calculate loss only on the answer part of the equation (last element
                    loss = F.cross_entropy(logits[-1], input[-1])
                    total_loss += loss.item() * input.shape[-1]

                if is_train:
                    model.zero_grad()
                    loss.backward()

                    #######

                    trigger = i < args.starting_point if args.two_stage else False

                    if args.filter == "none":
                        pass
                    elif args.filter == "ma":
                        grads = gradfilter_ma(model, grads=grads, window_size=args.window_size, lamb=args.lamb, trigger=trigger)
                    elif args.filter == "ema":
                        grads = gradfilter_ema(model, grads=grads, alpha=args.alpha, lamb=args.lamb, trigger = trigger)
                    elif args.filter == "smoother":
                        grads = smoother(model, grads=grads, beta=args.beta, pp=args.pp)
                    elif args.filter == "kalman":
                        grads = gradfilter_kalman(
                            model,
                            grads=grads,
                            process_noise=args.process_noise,
                            measurement_noise=args.measurement_noise,
                            lamb=args.lamb,
                        )
                    else:
                        raise ValueError(f"Invalid gradient filter type `{args.filter}`")

                    #######

                    optimizer.step()
                    scheduler.step()
                    i += 1

                acc = (logits[-1].argmax(-1) == input[-1]).float().mean()
                total_acc += acc.item() * input.shape[-1]

            if is_train:
                train_acc.append(total_acc / train_data.shape[-1])
                train_loss.append(total_loss / train_data.shape[-1])
                its.append(i)
            else:
                val_acc.append(total_acc / valid_data.shape[-1])
                val_loss.append(total_loss / valid_data.shape[-1])

        with torch.no_grad():
            param_vector = torch.cat([p.view(-1) for p in model.parameters()])
            # init_vector = torch.cat([p.view(-1) for p in initial_state.values()])
            l2_norm = torch.norm(param_vector, p=2).item()
            l2_distance = torch.norm(param_vector - init_vector, p=2).item()
            l1_norm = torch.norm(param_vector, p=1).item()
            l1_distance = torch.norm(param_vector - init_vector, p=1).item()

            param_norms_l2.append(l2_norm)
            param_distances_l2.append(l2_distance)
            param_norms_l1.append(l1_norm)
            param_distances_l1.append(l1_distance)
        sparsity = compute_sparsity(model)
        sparsity_log.append(sparsity)
        if args.save_weights:
            do_save = e <= 100 or (e > 100 and (e + 1) % 100 == 0) or e == int(args.budget) // steps_per_epoch - 1
        else:
            do_save = (e + 1) % 100 == 0
        if do_save:
            steps = torch.arange(len(train_acc)).numpy() * steps_per_epoch
            plt.plot(steps, train_acc, label="train")
            plt.plot(steps, val_acc, label="val")
            plt.legend()
            plt.title("Modular Multiplication (training on 50% of data)")
            plt.xlabel("Optimization Steps")
            plt.ylabel("Accuracy")
            plt.xscale("log", base=10)
            plt.grid()
            plt.savefig(f"results_old/acc_{args.label}.png", dpi=150)
            plt.close()

            plt.plot(steps, train_loss, label="train")
            plt.plot(steps, val_loss, label="val")
            plt.legend()
            plt.title("Modular Multiplication (training on 50% of data)")
            plt.xlabel("Optimization Steps")
            plt.ylabel("Loss")
            plt.xscale("log", base=10)
            plt.grid()
            plt.savefig(f"results_old/loss_{args.label}.png", dpi=150)
            plt.close()
            """
            plt.plot(steps, sparsity_log, label="sparsity")
            plt.legend()
            plt.title("Sparsity Over Training")
            plt.xlabel("Optimization Steps")
            plt.ylabel("Sparsity")
            plt.grid()
            plt.savefig(f"results/sparsity_{args.label}.png", dpi=150)
            plt.close()
            """


            if args.save_weights:
                net_its.append(e)
                nets.append(copy.deepcopy(model.state_dict()))           

    steps = torch.arange(len(param_norms_l1)).numpy() * steps_per_epoch
    results = {
        'its': its,
        'train_acc': train_acc,
        'train_loss': train_loss,
        'val_acc': val_acc,
        'val_loss': val_loss,
        'param_norms_l1': param_norms_l1,
        'param_distances_l1': param_distances_l1,
        'param_norms_l2': param_norms_l2,
        'param_distances_l2': param_distances_l2,
        'steps_per_epoch': steps_per_epoch,
    }

    if args.save_weights:
        results['net_its'] = net_its
        results['net'] = nets

    torch.save(results, f"results_old/res_{args.label}.pt")
    # results['steps'] = steps
    # torch.save(results, f"results/res_{args.label}.pt")
    # Plotting L2 norms and distances
    plt.figure()
    plt.plot(steps, param_norms_l2, label="L2 Norm")
    plt.plot(steps, param_distances_l2, label="L2 Distance from Initial")
    plt.xlabel("Optimization Steps")
    plt.ylabel("L2 Norm")
    plt.xscale("log", base=10)
    plt.yscale("log", base=10)
    plt.legend()
    plt.title("L2 Norm and Distance")
    plt.legend()
    plt.savefig(f"results_old/norms_distances_l2_{args.label}.png")
    plt.close()

    # Plotting L1 norms and distances
    plt.figure()
    plt.plot(steps, param_norms_l1, label="L1 Norm")
    plt.plot(steps, param_distances_l1, label="L1 Distance from Initial")
    plt.xlabel("Optimization Steps")
    plt.ylabel("L1 Norm")
    plt.xscale("log", base=10)
    plt.yscale("log", base=10)
    plt.legend()
    plt.title("L1 Norm and Distance")
    plt.legend()
    plt.savefig(f"results_old/norms_distances_l1_{args.label}.png")
    plt.close()
        


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("--label", default="")
    parser.add_argument("--seed", type=int, default= 0)
    parser.add_argument("--p", type=int, default=97)
    parser.add_argument("--budget", type=int, default= 150000)
    parser.add_argument("--batch_size", type=int, default= 512)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--beta1", type=float, default=0.9)
    parser.add_argument("--beta2", type=float, default=0.98)
    parser.add_argument("--weight_decay", type=float, default=0)
    parser.add_argument("--optimizer", default="Adam")
    parser.add_argument("--starting_point", type=int, default= 1)

    # Grokfast
    parser.add_argument("--filter", type=str, choices=["none", "ma", "ema", "fir", "smoother", "kalman"], default="none")
    parser.add_argument("--alpha", type=float, default=0.99)
    parser.add_argument("--window_size", type=int, default=100)
    parser.add_argument("--lamb", type=float, default=5.0)
    parser.add_argument("--process_noise", type=float, default=1e-4)
    parser.add_argument("--measurement_noise", type=float, default=1e-2)

    # Smoother
    parser.add_argument("--beta", type=float, default=0.98)
    parser.add_argument("--pp", type=float, default=0.01)

    # Ablation studies
    parser.add_argument("--two_stage", action='store_true')
    parser.add_argument("--save_weights", action='store_true')
    args = parser.parse_args()

    filter_str = ('_' if args.label != '' else '') + args.filter
    window_size_str = f'_w{args.window_size}'
    alpha_str = f'_a{args.alpha:.3f}'.replace('.', '')
    lamb_str = f'_l{int(args.lamb)}'
    optimizer_str = f'_optimizer{args.optimizer}'
    beta_str = f'_beta{args.beta}'
    pp_str = f'_pp{args.pp}'

    if args.filter == 'none':
        filter_suffix = ''
    elif args.filter == 'ma':
        filter_suffix = window_size_str + lamb_str
    elif args.filter == 'ema':
        filter_suffix = alpha_str + lamb_str
    elif args.filter == 'smoother':
        filter_suffix = beta_str + pp_str
    elif args.filter == "kalman":
        filter_suffix = (
            f"_p{args.process_noise:.1e}_m{args.measurement_noise:.1e}".replace(".", "")
            + lamb_str
        )
    else:
        raise ValueError(f"Unrecognized filter type {args.filter}")

    optim_suffix = ''
    if args.weight_decay != 0:
        optim_suffix = optim_suffix + f'_wd{args.weight_decay:.1e}'.replace('.', '')
    if args.lr != 1e-3:
        optim_suffix = optim_suffix + f'_lrx{int(args.lr / 1e-3)}'

    two_stage_suffix = '_two_stage' if args.two_stage else ''
    starting_point_suffix = f'_start_at{int(args.starting_point)}'

    args.label = args.label + filter_str + filter_suffix + optim_suffix +  optimizer_str + two_stage_suffix + starting_point_suffix
    print(f'Experiment results saved under name: {args.label}')

    main(args)
