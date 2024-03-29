import matplotlib.pyplot as plt
import numpy as np
import torch as th
import torch.nn as nn
import itertools

import envs.multi_cart.constants as constants

from reward_decomposition.decomposer import *

from mpl_toolkits.axes_grid1 import make_axes_locatable

import time

# wierd bug where matplotlib spits out a ton of debug messages for no apparent reason
import logging

logging.getLogger('matplotlib').setLevel(logging.ERROR)


# Shape Conventions:

# Observations: [batch_size, ep_length, num_agents, obs_length]
# Raw Outputs (Regression): List of List of [batch_size, ep_length, 1]
# Raw Outputs (Regression): List of List of [batch_size, ep_length, num_classes (for that func)]
# Flat Raw Outputs: Same as Raw outputs, only with a flattened list
# Pred (Regression): [batch_size, ep_length, 1]
# Pred (Classification): [batch_size, ep_length, num_global_classes]
# Classes (Only Classification): [batch_size, ep_length, num_reward_functions]
# Local Rewards (Both): [batch_size, ep_length, num_reward_functions, 1]
# Agent Rewards (Both): [batch_size, ep_length, num_agents, 1]
# Global Rewards: [batch_size, ep_length, 1]


def train_decomposer(decomposer, batch, reward_optimizer):
    # organize the data
    reward_inputs, global_rewards, mask, _ = build_reward_data(decomposer, batch)
    # th.set_printoptions(profile="full") # reset
    #
    # print(reward_inputs)
    # print(global_rewards)

    raw_outputs = decomposer.forward(reward_inputs)

    reward_pred = decomposer.convert_raw_outputs(raw_outputs, output_type=PRED)
    local_rewards = decomposer.convert_raw_outputs(raw_outputs, output_type=LOCAL_REWARDS)

    if decomposer.args.assume_binary_reward:
        # First, create to global probabilities
        reward_pred = th.log(reward_pred + 1e-8)
        reward_pred = th.reshape(reward_pred, shape=(-1, reward_pred.shape[-1]))

        # Next ready the global targets
        global_rewards = global_rewards.long()
        global_rewards = global_rewards.flatten()

        loss = nn.NLLLoss()
        output = loss(reward_pred, global_rewards)
    else:
        # compute the loss
        output = compute_loss(local_rewards, global_rewards, mask)

    # Now add the regularizing loss
    output += decomposer.compute_regularization(raw_outputs)

    reward_optimizer.zero_grad()
    output.backward()
    reward_optimizer.step()

    # visualize_decomposer_1d(decomposer, batch)
    # visualize_data(reward_inputs, global_rewards)


# decomposes the global rewards into local rewards
# Returns (status, reward_mask, local_rewards) where
# status - refers to the total reliability of the local rewards
# reward_mask - if status is True, which rewards should be used
def decompose(decomposer, batch):
    # decompose the reward
    reward_inputs, global_rewards, mask, _ = build_reward_data(decomposer, batch, include_last=False)
    raw_outputs = decomposer.forward(reward_inputs)

    agent_rewards = decomposer.convert_raw_outputs(raw_outputs, output_type=AGENT_REWARDS)

    if decomposer.args.assume_binary_reward:
        global_rewards = global_rewards.long()

    # now we assume that the local rewards
    status, reward_mask = build_reward_mask(decomposer, agent_rewards, global_rewards, mask)

    # return the rewards and the status. Just in case, return None as local rewards to make sure they arent used even
    # if status is False
    if not status:
        agent_rewards = None
    return status, reward_mask, agent_rewards


# Function recvs the true global reward and the outputs generated by the decomposer, and checks how similar they are
# This will determine if the QLearner should use each modified sample
def build_reward_mask(decomposer, local_rewards, global_rewards, mask):
    diff = compute_diff(local_rewards, global_rewards, mask)

    # Determine the reward mask and the status
    delta = decomposer.args.reward_diff_threshold * decomposer.n_agents
    reward_mask = th.where(th.logical_and(mask, (th.abs(diff) < delta)), 1., 0.)
    reward_decomposition_acc = th.sum(reward_mask) / th.sum(mask)
    status = reward_decomposition_acc > decomposer.args.reward_acc

    # Visualize inverse histogram if necessary
    print(f"Decomposition Accuracy: {reward_decomposition_acc}")
    # visualize_diff(diff, mask, horiz_line=decomposer.args.reward_diff_threshold)

    return status, reward_mask


def build_reward_data(decomposer, batch, include_last=True):
    # For now, define the input for the reward decomposition network as just the observations
    # note that some of these aren't relevant, so we additionally supply a mask for pairs that shouldn't be learnt
    inputs = batch["obs"][:, :, :, :]
    outputs = local_to_global(batch["reward"])
    truth = batch["reward"]
    mask = batch["filled"].float()

    obs_index = decomposer.args.reward_index_in_obs
    if obs_index != -1:
        inputs = inputs[:, :, :, obs_index: obs_index + 1]

    if not include_last:
        inputs = inputs[:, :-1]
        outputs = outputs[:, :-1]
        truth = truth[:, :-1]
        mask = mask[:, :-1].float()

    return inputs, outputs, mask, truth


# huber loss
def huber(diff, delta=0.1):
    loss = th.where(th.abs(diff) < delta, 0.5 * (diff ** 2),
                    delta * th.abs(diff) - 0.5 * (delta ** 2))
    return th.sum(loss) / len(diff)


# log cosh loss
def logcosh(diff):
    loss = th.log(th.cosh(diff))
    return th.sum(loss) / len(diff)


def mse(diff):
    return th.sum(diff ** 2) / len(diff)


def mae(diff):
    return th.sum(th.abs(diff)) / len(diff)


def compute_loss(local_rewards, global_rewards, mask):
    diff = compute_diff(local_rewards, global_rewards, mask).flatten()
    loss = logcosh(diff)
    return loss


def compute_diff(local_rewards, global_rewards, mask, use_mask=True):
    # reshape local rewards
    local_rewards = th.reshape(local_rewards, shape=(*local_rewards.shape[:2], -1))
    summed_local_rewards = local_to_global(local_rewards)
    global_rewards = th.reshape(global_rewards, summed_local_rewards.shape)

    diff = summed_local_rewards - global_rewards
    if use_mask:
        diff = th.mul(diff, mask)
    return diff


def almost_flatten(arr):
    return arr.reshape(-1, arr.shape[-1])


def local_to_global(arr):
    return th.sum(arr, dim=-1, keepdims=True)
