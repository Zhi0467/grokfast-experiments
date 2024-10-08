import math
from argparse import ArgumentParser
from itertools import permutations
import copy
import numpy as np

import matplotlib.pyplot as plt
from tqdm import tqdm
import seaborn as sns

import torch
import torch.nn as nn
import torch.nn.functional as F

from grokfast import *
from model import *
from optimizers import *
from arg_parser import *
from tools import *

def main(args):
    
    torch.manual_seed(args.seed)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    _ = torch.randn(1, device=device)
    update_rank_percentage = args.update_rank_percentage

    # tokens for <op> and <=>. It's not clear why <=> is needed at all since it
    # has no effect on the output, but we'll leave it in to best follow the
    # paper.
    eq_token = args.p
    op_token = args.p + 1

    # "We trained a standard decoder-only transformer (Vaswani et al., 2017)
    # with causal attention masking, and calculated loss and accuracy only on
    # the answer part of the equation. For all experiments we used a
    # transformer with 2 layers, width 128, and 4 attention heads"
    num_layers = 2
    model = Decoder(dim=128, num_layers = num_layers, num_heads=4, num_tokens=args.p + 2, seq_len=5, beta = args.beta, rank = args.init_rank, LoRA_rank = args.LoRA_rank, attn_freeze = True, first_block_freeze = True).to(device)
    nparams = sum([p.numel() for p in model.parameters() if p.requires_grad])
    print(model)
    print(f'Total number of parameters: {nparams}')

    data = multiplication_mod_p_data(args.p, eq_token, op_token)

    train_idx, valid_idx = torch.randperm(data.shape[1]).split(data.shape[1] // 2)
    train_data, valid_data = data[:, train_idx], data[:, valid_idx]

    # For most experiments we used AdamW optimizer with learning rate 10−3,
    # weight decay 1, β1 = 0.9, β2 = 0.98

    torch_optimizer = getattr(torch.optim, args.optimizer)(
        model.parameters(),
        lr=args.lr,
        weight_decay=args.weight_decay,
        betas=(args.beta1, args.beta2),
    )

    """
    optimizer = AdamWOptim(
      model = model, lr = args.lr, beta1 = args.beta1, beta2 = args.beta2, epsilon=1e-8, weight_decay = args.weight_decay
    )
    """

    # scheduler = LrScheduler(large_lr=args.large_lr, regular_lr=args.lr, warmup_steps = 20, cutoff_steps=args.cutoff_steps)
    # scheduler = LambdaWarmUpScheduler(initial_value = 0.0, final_value = args.lr, warmup_steps = 20)
    #  linear learning rate warmup over the first 10 updates
    scheduler = torch.optim.lr_scheduler.LambdaLR(
        torch_optimizer, lambda update: 1 if update > 10 else update / 10
    )

    # the layer to plot the normalized effective ranks
    layer_inspected = 2
    layer = model.layers[layer_inspected - 1] 

    layer_weights = extract_weight_matrices(layer)
    layer_matrix_ranks = {}
    layer_matrix_entropy = {}

    for name, weight_matrix in layer_weights.items():
        layer_matrix_ranks[name] = []
        layer_matrix_entropy[name] = []

    steps_per_epoch = math.ceil(train_data.shape[1] / args.batch_size)

    its, train_acc, val_acc, train_loss, val_loss = [], [], [], [], []
    grads = None
    i = 0
    # set the interval for logging cosine similarity 
    cos_sim_interval = [10]

    # For logging network weights.
    net_its, nets = [], []

    count = 0
    grads_similarity_log = [None for _ in range(len(cos_sim_interval))]
    current_grad_vector = [None for _ in range(len(cos_sim_interval))]
    
    for interval in cos_sim_interval:
        grads_similarity_log[count] = []
        count = count + 1

    
    save_epochs_for_attention_maps = [0, 1, 10, 100, 1000, 10000, 50000, (int(args.budget) // steps_per_epoch - 1) * steps_per_epoch]
    save_filenames_layer1 = []
    save_filenames_layer2 = []

    for e in tqdm(range(int(args.budget) // steps_per_epoch)):

        # randomly shuffle train data
        train_data = train_data[:, torch.randperm(train_data.shape[1])]

        for data, is_train in [(train_data, True), (valid_data, False)]:

            model.train(is_train)
            total_loss = 0
            total_acc = 0
            total_jacobian_norm_change = 0

            # torch.split faster than dataloader with tensor
            dl = torch.split(data, args.batch_size, dim=1)
            for input in dl:
                input = input.to(device)

                with torch.set_grad_enabled(is_train):
                    need_attn_weights = False
                    if e * steps_per_epoch in save_epochs_for_attention_maps and i % steps_per_epoch == 0:
                        need_attn_weights = True

                    logits, attention_maps = model(input[:-1], need_attn_weights)

                    if need_attn_weights:
                        filenames = plot_attention_maps(attention_maps, e * steps_per_epoch)
                        if filenames[0] not in save_filenames_layer1:
                            save_filenames_layer1.append(filenames[0])
                        if filenames[1] not in save_filenames_layer2:
                            save_filenames_layer2.append(filenames[1])
                    # calculate loss only on the answer part of the equation (last element
                    loss = F.cross_entropy(logits[-1], input[-1])
                    total_loss += loss.item() * input.shape[-1]

                if is_train:
                    model.zero_grad()
                    loss.backward()
                    """
                    if i == 1:
                        pre_grad_vector = [None for _ in range(len(cos_sim_interval))]
                        count = 0
                        for interval in cos_sim_interval:
                            pre_grad_vector[count] = torch.cat([param.grad.view(-1) for param in model.parameters() if param.grad is not None])
                            count = count + 1
                    """


                    trigger = i < args.starting_point if args.two_stage else False
                    if args.filter == "none":
                        pass
                    elif args.filter == "ma":
                        grads = gradfilter_ma(model, grads=grads, window_size=args.window_size, lamb=args.lamb, trigger=trigger)
                    elif args.filter == "ema":
                        grads = gradfilter_ema(model, grads=grads, alpha=args.alpha, lamb=args.lamb, trigger=trigger)
                    elif args.filter == "smoother":
                        grads = smoother(model, grads=grads, beta=args.beta, pp=args.pp)
                    elif args.filter == "kalman":
                        grads = gradfilter_kalman(model, grads=grads, process_noise=args.process_noise, measurement_noise=args.measurement_noise, lamb=args.lamb)
                    else:
                        raise ValueError(f"Invalid update filter type `{args.filter}`")
                    
                    # perform low-rank projection on grads
                    if args.enable_lr_update:
                        with torch.no_grad():
                            for name, param in model.named_parameters():
                                if param.grad is not None and len(param.grad.shape) == 2:
                                    grad = param.grad
                                    max_rank = max(grad.shape)
                                    rank = int(update_rank_percentage * max_rank)
                                    param.grad = low_rank_approximation(grad, rank)


                    # Update gradients using AdamW
                    # optimizer.lr = scheduler.step()
                    # optimizer.update(i + 1)

                    # Compute and log gradient angles every cos_sim_interval steps
                    """
                    count = 0
                    for interval in cos_sim_interval:
                        if i % interval == 0 and i >= interval:
                            current_grad_vector[count] = torch.cat([param.grad.view(-1) for param in model.parameters() if param.grad is not None])
                            cosine_similarity = F.cosine_similarity(current_grad_vector[count], pre_grad_vector[count], dim=0).item()
                            pre_grad_vector[count] = current_grad_vector[count]
                            grads_similarity_log[count].append(cosine_similarity)
                        count = count + 1
                                
                    # print(f"the cos similarity at step {i} is {cosine_similarity:.2f}\n")
                    """
    

                    torch_optimizer.step()
                    scheduler.step()
   
                    # Zero gradients after update
                    i += 1

                acc = (logits[-1].argmax(-1) == input[-1]).float().mean()
                total_acc += acc.item() * input.shape[-1]
                

            if is_train:
                train_acc.append(total_acc / train_data.shape[-1])
                train_loss.append(total_loss / train_data.shape[-1])
                its.append(i)
                # print(f"\n Training: Epoch {e}, Iteration {i}, Loss: {total_loss / train_data.shape[-1]}, Accuracy: {total_acc / train_data.shape[-1]}")

            else:
                val_acc.append(total_acc / valid_data.shape[-1])
                val_loss.append(total_loss / valid_data.shape[-1])
                # print(f"\n Test: Epoch {e}, Iteration {i}, Loss: {total_loss / valid_data.shape[-1]}, Accuracy: {total_acc / valid_data.shape[-1]} \n")


        """
        with torch.no_grad():
            param_vector = torch.cat([p.view(-1) for p in model.parameters() if p.requires_grad])
            # init_vector = torch.cat([p.view(-1) for p in initial_state.values()])
            l2_norm = torch.norm(param_vector, p=2).item()
            l2_distance = torch.norm(param_vector - init_vector, p=2).item()
            l1_norm = torch.norm(param_vector, p=1).item()
            l1_distance = torch.norm(param_vector - init_vector, p=1).item()

            param_norms_l2.append(l2_norm)
            param_distances_l2.append(l2_distance)
            param_norms_l1.append(l1_norm)
            param_distances_l1.append(l1_distance)
        """
        

        layer_weights = extract_weight_matrices(layer)
        for name, weight_matrix in layer_weights.items():
            layer_matrix_ranks[name].append(compute_norm_effective_rank(weight_matrix))
            layer_matrix_entropy[name].append(compute_norm_shannon_entropy(weight_matrix))
        

        if args.save_weights:
            do_save = e <= 100 or (e > 100 and (e + 1) % 100 == 0) or e == int(args.budget) // steps_per_epoch - 1
        else:
            do_save = (e + 10) % 100 == 0
        if do_save:
            print(f"epoch {e}: training acc: {train_acc[-1]}\n")
            print(f"epoch {e}: test acc: {val_acc[-1]}\n")
            steps = torch.arange(len(train_acc)).numpy() * steps_per_epoch
            
            plt.plot(steps, train_acc, label="train")
            plt.plot(steps, val_acc, label="val")
            plt.legend()
            plt.title("Modular Multiplication (training on 50% of data)")
            plt.xlabel("Optimization Steps")
            plt.ylabel("Accuracy")
            plt.xscale("log", base=10)
            plt.grid()
            plt.savefig(f"results_transformer/acc_{args.label}.png", dpi=150)
            plt.close()
            

            plt.plot(steps, train_loss, label="train")
            plt.plot(steps, val_loss, label="val")
            plt.legend()
            plt.title("Modular Multiplication (training on 50% of data)")
            plt.xlabel("Optimization Steps")
            plt.ylabel("Loss")
            plt.xscale("log", base=10)
            plt.grid()
            plt.savefig(f"results_transformer/loss_{args.label}.png", dpi=150)
            plt.close()

            # plot grads changes
            """
            count = 0
            for interval in cos_sim_interval:
                plt.scatter(range(len(grads_similarity_log[count])), grads_similarity_log[count], label="similarity")
                plt.xlabel(f'Steps (x{interval})')
                plt.ylabel('updates cosine similarity')
                plt.xscale("log", base=10)
                plt.title(f'Update Cos Similarity, Every {interval} Steps')
                plt.grid(True)
                plt.savefig(f"results_transformer/grads_similarity_interval_{interval}_{args.label}.png", dpi=150)
                plt.close()
                count = count + 1
            """

            
            for name, value in layer_matrix_ranks.items():
                plt.plot(steps, value, label=f"matrix_{name}")
            plt.legend()
            plt.title(f"normalized effective ranks on layer_{layer_inspected}")
            plt.xlabel("Optimization Steps")
            plt.ylabel("normalized ranks")
            plt.xscale("log", base=10)
            plt.grid()
            plt.savefig(f"results_transformer/layer_{layer_inspected}_ranks_{args.label}.png", dpi=150)
            plt.close()

            """
            for name, value in layer_matrix_entropy.items():
                plt.plot(steps, value, label=f"matrix_{name}")
            plt.legend()
            plt.title(f"Shannon entropy of layer_{layer_inspected}")
            plt.xlabel("Optimization Steps")
            plt.ylabel("entropy")
            plt.xscale("log", base=10)
            plt.grid()
            plt.savefig(f"results_post_AdamW/layer_{layer_inspected}_entropy_{args.label}.png", dpi=150)
            plt.close()
            """
            

            if args.save_weights:
                net_its.append(e)
                nets.append(copy.deepcopy(model.state_dict()))           

    concatenate_images(save_filenames_layer1, f'results_transformer/attention_maps_layer1_{args.label}.png')
    concatenate_images(save_filenames_layer2, f'results_transformer/attention_maps_layer2_{args.label}.png')  

if __name__ == "__main__":
    parser = Arg_parser()
    args = parser.return_args()
    main(args)