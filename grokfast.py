from collections import deque
from typing import Dict, Optional, Literal
import torch
import torch.nn as nn


def gradfilter_ma(
    m: nn.Module,
    grads: Optional[Dict[str, deque]] = None,
    window_size: int = 100,
    lamb: float = 5.0,
    filter_type: Literal['mean', 'sum'] = 'mean',
    warmup: bool = True,
    trigger: bool = False, # For ablation study.
) -> Dict[str, deque]:
    if grads is None:
        grads = {n: deque(maxlen=window_size) for n, p in m.named_parameters() if p.requires_grad and p.grad is not None}

    for n, p in m.named_parameters():
        if p.requires_grad and p.grad is not None:
            grads[n].append(p.grad.data.detach()) # .cpu())

            # Modify the gradients.
            if not warmup or len(grads[n]) == window_size and not trigger:
                if filter_type == "mean":
                    avg = sum(grads[n]) / len(grads[n])
                elif filter_type == "sum":
                    avg = sum(grads[n])
                else:
                    raise ValueError(f"Unrecognized filter_type {filter_type}")
                p.grad.data = p.grad.data + avg * lamb

    return grads


def gradfilter_ema(
    m: nn.Module,
    grads: Optional[Dict[str, torch.Tensor]] = None,
    alpha: float = 0.98,
    lamb: float = 2.0,
    trigger: bool = False
) -> Dict[str, torch.Tensor]:
    if grads is None:
        grads = {n: p.grad.data.detach() for n, p in m.named_parameters() if p.requires_grad and p.grad is not None}

    for n, p in m.named_parameters():
        if p.requires_grad and p.grad is not None:
            if not trigger:
                grads[n] = grads[n] * alpha + p.grad.data.detach() * (1 - alpha)
                p.grad.data = (p.grad.data + grads[n] * lamb) / (1 + lamb)

    return grads

def smoother(
    m: nn.Module,
    grads: Optional[Dict[str, torch.Tensor]] = None,
    beta: float = 0.98,
    pp: float = 0.01,
) -> Dict[str, torch.Tensor]:
    # Initialize grads if not provided
    if grads is None:
        grads = {n: p.grad.data.detach() for n, p in m.named_parameters() if p.requires_grad and p.grad is not None}
    
    # Initialize z with the same parameters as grads
    z = {n: p.data.clone() for n, p in m.named_parameters() if p.requires_grad and p.grad is not None}
    
    # Update gradients based on the smoother algorithm
    for n, p in m.named_parameters():
        if p.requires_grad and p.grad is not None:
            z[n] = z[n] + beta * (p.data - z[n])
            p.grad.data -= pp * (p.data - z[n])
    
    return grads

def gradfilter_kalman(
    m: nn.Module,
    grads: Optional[Dict[str, Dict[str, torch.Tensor]]] = None,
    process_noise: float = 1e-4,
    measurement_noise: float = 1e-2,
    lamb: float = 2.0,
) -> Dict[str, Dict[str, torch.Tensor]]:
    if grads is None:
        grads = {
            n: {
                "x": torch.zeros_like(p.grad.data),
                "P": torch.ones_like(p.grad.data) * measurement_noise,
            }
            for n, p in m.named_parameters()
            if p.requires_grad and p.grad is not None
        }

    for n, p in m.named_parameters():
        if p.requires_grad and p.grad is not None:
            # Prediction step
            x_pred = grads[n]["x"]
            P_pred = grads[n]["P"] + process_noise

            # Update step
            y = p.grad.data - x_pred
            S = P_pred + measurement_noise
            K = P_pred / S
            x = x_pred + K * y
            P = (1 - K) * P_pred

            # Store updated state
            grads[n]["x"] = x
            grads[n]["P"] = P

            # Apply the filtered gradient
            p.grad.data += x * lamb

    return grads


