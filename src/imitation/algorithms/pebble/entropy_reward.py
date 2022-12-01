from enum import Enum, auto
from typing import Tuple

import numpy as np
import torch as th

from imitation.policies.replay_buffer_wrapper import (
    ReplayBufferView,
    ReplayBufferRewardWrapper,
)
from imitation.rewards.reward_function import ReplayBufferAwareRewardFn, RewardFn
from imitation.util import util
from imitation.util.networks import RunningNorm


class PebbleRewardPhase(Enum):
    """States representing different behaviors for PebbleStateEntropyReward"""

    # Collecting samples so that we have something for entropy calculation
    LEARNING_START = auto()
    # Entropy based reward
    UNSUPERVISED_EXPLORATION = auto()
    # Learned reward
    POLICY_AND_REWARD_LEARNING = auto()


class PebbleStateEntropyReward(ReplayBufferAwareRewardFn):
    """
    Reward function for implementation of the PEBBLE learning algorithm
    (https://arxiv.org/pdf/2106.05091.pdf).

    The rewards returned by this function go through the three phases
    defined in PebbleRewardPhase. To transition between these phases,
    unsupervised_exploration_start() and unsupervised_exploration_finish()
    need to be called.

    The second phase (UNSUPERVISED_EXPLORATION) also requires that a buffer
    with observations to compare against is supplied with set_replay_buffer()
    or on_replay_buffer_initialized().

    Args:
        learned_reward_fn: The learned reward function used after unsupervised
            exploration is finished
        nearest_neighbor_k: Parameter for entropy computation (see
            compute_state_entropy())
    """

    # TODO #625: parametrize nearest_neighbor_k
    def __init__(
        self,
        learned_reward_fn: RewardFn,
        nearest_neighbor_k: int = 5,
    ):
        self.trained_reward_fn = learned_reward_fn
        self.nearest_neighbor_k = nearest_neighbor_k
        # TODO support n_envs > 1
        self.entropy_stats = RunningNorm(1)
        self.state = PebbleRewardPhase.LEARNING_START

        # These two need to be set with set_replay_buffer():
        self.replay_buffer_view = None
        self.obs_shape = None

    def on_replay_buffer_initialized(self, replay_buffer: ReplayBufferRewardWrapper):
        self.set_replay_buffer(replay_buffer.buffer_view, replay_buffer.obs_shape)

    def set_replay_buffer(self, replay_buffer: ReplayBufferView, obs_shape: Tuple):
        self.replay_buffer_view = replay_buffer
        self.obs_shape = obs_shape

    def unsupervised_exploration_start(self):
        assert self.state == PebbleRewardPhase.LEARNING_START
        self.state = PebbleRewardPhase.UNSUPERVISED_EXPLORATION

    def unsupervised_exploration_finish(self):
        assert self.state == PebbleRewardPhase.UNSUPERVISED_EXPLORATION
        self.state = PebbleRewardPhase.POLICY_AND_REWARD_LEARNING

    def __call__(
        self,
        state: np.ndarray,
        action: np.ndarray,
        next_state: np.ndarray,
        done: np.ndarray,
    ) -> np.ndarray:
        if self.state == PebbleRewardPhase.UNSUPERVISED_EXPLORATION:
            return self._entropy_reward(state)
        else:
            return self.trained_reward_fn(state, action, next_state, done)

    def _entropy_reward(self, state):
        if self.replay_buffer_view is None:
            raise ValueError(
                "Replay buffer must be supplied before entropy reward can be used"
            )

        all_observations = self.replay_buffer_view.observations
        # ReplayBuffer sampling flattens the venv dimension, let's adapt to that
        all_observations = all_observations.reshape((-1, *self.obs_shape))
        # TODO #625: deal with the conversion back and forth between np and torch
        entropies = util.compute_state_entropy(
            th.tensor(state),
            th.tensor(all_observations),
            self.nearest_neighbor_k,
        )
        normalized_entropies = self.entropy_stats.forward(entropies)
        return normalized_entropies.numpy()

    def __getstate__(self):
        state = self.__dict__.copy()
        del state["replay_buffer_view"]
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.replay_buffer_view = None