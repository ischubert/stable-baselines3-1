import time
import warnings
from collections import deque
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import torch as th

from stable_baselines3.common.buffers import ReplayBuffer, DictReplayBuffer
from stable_baselines3.common.preprocessing import get_obs_shape
from stable_baselines3.common.type_aliases import DictReplayBufferSamples
from stable_baselines3.common.vec_env import VecEnv, VecNormalize
from stable_baselines3.her.goal_selection_strategy import KEY_TO_GOAL_STRATEGY, GoalSelectionStrategy


def get_time_limit(env: VecEnv, current_max_episode_length: Optional[int]) -> int:
    """
    Get time limit from environment.

    :param env: Environment from which we want to get the time limit.
    :param current_max_episode_length: Current value for max_episode_length.
    :return: max episode length
    """
    # try to get the attribute from environment
    if current_max_episode_length is None:
        try:
            current_max_episode_length = env.get_attr("spec")[0].max_episode_steps
            # Raise the error because the attribute is present but is None
            if current_max_episode_length is None:
                raise AttributeError
        # if not available check if a valid value was passed as an argument
        except AttributeError:
            raise ValueError(
                "The max episode length could not be inferred.\n"
                "You must specify a `max_episode_steps` when registering the environment,\n"
                "use a `gym.wrappers.TimeLimit` wrapper "
                "or pass `max_episode_length` to the model constructor"
            )
    return current_max_episode_length


class HerReplayBuffer(DictReplayBuffer):
    """
    Hindsight Experience Replay (HER) buffer.
    Paper: https://arxiv.org/abs/1707.01495

    .. warning::

      For performance reasons, the maximum number of steps per episodes must be specified.
      In most cases, it will be inferred if you specify ``max_episode_steps`` when registering the environment
      or if you use a ``gym.wrappers.TimeLimit`` (and ``env.spec`` is not None).
      Otherwise, you can directly pass ``max_episode_length`` to the replay buffer constructor.


    Replay buffer for sampling HER (Hindsight Experience Replay) transitions.
    In the online sampling case, these new transitions will not be saved in the replay buffer
    and will only be created at sampling time.

    :param env: The training environment
    :param buffer_size: The size of the buffer measured in transitions.
    :param max_episode_length: The maximum length of an episode. If not specified,
        it will be automatically inferred if the environment uses a ``gym.wrappers.TimeLimit`` wrapper.
    :param goal_selection_strategy: Strategy for sampling goals for replay.
        One of ['episode', 'final', 'future']
    :param device: PyTorch device
    :param n_sampled_goal: Number of virtual transitions to create per real transition,
        by sampling new goals.
    :param n_sampled_goal_preselection: Number of goals sampled for preselection
        if goal_selection_strategy is PAST_DESIRED_SUCCESS
    :param desired_goal_buffer_size: Size of the buffer storing desired goals
        if goal_selection_strategy is PAST_DESIRED or PAST_DESIRED_SUCCESS
    :param handle_timeout_termination: Handle timeout termination (due to timelimit)
        separately and treat the task as infinite horizon task.
        https://github.com/DLR-RM/stable-baselines3/issues/284
    :param modify_goal: In some cases, the replay goal is dependend on state.
        In these cases set modify_goal=True
    :param create_desired_goal_storage: Create a desired_goal_storage
    """

    def __init__(
        self,
        env: VecEnv,
        buffer_size: int,
        device: Union[th.device, str] = "cpu",
        replay_buffer: Optional[DictReplayBuffer] = None,
        max_episode_length: Optional[int] = None,
        n_sampled_goal: int = 4,
        n_sampled_goal_preselection: Optional[int] = None,
        desired_goal_buffer_size: int = int(1e5),
        goal_selection_strategy: Union[GoalSelectionStrategy, str] = "future",
        online_sampling: bool = True,
        handle_timeout_termination: bool = True,
        modify_goal: bool = False
    ):

        super(HerReplayBuffer, self).__init__(buffer_size, env.observation_space, env.action_space, device, env.num_envs)

        # convert goal_selection_strategy into GoalSelectionStrategy if string
        if isinstance(goal_selection_strategy, str):
            self.goal_selection_strategy = KEY_TO_GOAL_STRATEGY[goal_selection_strategy.lower()]
        else:
            self.goal_selection_strategy = goal_selection_strategy

        # check if goal_selection_strategy is valid
        assert isinstance(
            self.goal_selection_strategy, GoalSelectionStrategy
        ), f"Invalid goal selection strategy, please use one of {list(GoalSelectionStrategy)}"

        if self.goal_selection_strategy in [GoalSelectionStrategy.PAST_DESIRED, GoalSelectionStrategy.PAST_DESIRED_SUCCESS]:
            assert not online_sampling, "Selected GoalSelectionStrategy not implemented for online sampling"

        self.n_sampled_goal = n_sampled_goal
        self.n_sampled_goal_preselection = n_sampled_goal_preselection
        # if we sample her transitions online use custom replay buffer
        self.online_sampling = online_sampling
        # compute ratio between HER replays and regular replays in percent for online HER sampling
        self.her_ratio = 1 - (1.0 / (self.n_sampled_goal + 1))
        # maximum steps in episode
        self.max_episode_length = get_time_limit(env, max_episode_length)
        # storage for transitions of current episode for offline sampling
        # for online sampling, it replaces the "classic" replay buffer completely
        her_buffer_size = buffer_size if online_sampling else self.max_episode_length

        # For GoalSelectionStrategy.PAST_DESIRED, and GoalSelectionStrategy.PAST_DESIRED_SUCCESS,
        # add basic buffer to save the desired_goal of each episode
        if self.goal_selection_strategy in [
            GoalSelectionStrategy.PAST_DESIRED, GoalSelectionStrategy.PAST_DESIRED_SUCCESS
        ]:
            self.desired_goal_storage = ReplayBuffer(
                buffer_size=desired_goal_buffer_size,
                observation_space=env.observation_space["desired_goal"],
                action_space=env.action_space,
                device=device,
                n_envs=env.num_envs,
                handle_timeout_termination=False
            )

        self.env = env
        self.buffer_size = her_buffer_size

        if online_sampling:
            replay_buffer = None
        self.replay_buffer = replay_buffer
        self.online_sampling = online_sampling

        # Handle timeouts termination properly if needed
        # see https://github.com/DLR-RM/stable-baselines3/issues/284
        self.handle_timeout_termination = handle_timeout_termination

        self.modify_goal = modify_goal

        # buffer with episodes
        # number of episodes which can be stored until buffer size is reached
        self.max_episode_stored = max(self.buffer_size // self.max_episode_length, 1)
        self.current_idx = 0
        # Counter to prevent overflow
        self.episode_steps = 0

        # Get shape of observation and goal
        # This is the general case in which achieved_goal and desired_goal are elements of different spaces
        self.obs_shape = get_obs_shape(self.env.observation_space.spaces["observation"])
        self.achieved_goal_shape = get_obs_shape(self.env.observation_space.spaces["achieved_goal"])
        self.desired_goal_shape = get_obs_shape(self.env.observation_space.spaces["desired_goal"])


        # input dimensions for buffer initialization
        input_shape = {
            "observation": (1,) + self.obs_shape,
            "achieved_goal": (1,) + self.achieved_goal_shape,
            "desired_goal": (1,) + self.desired_goal_shape,
            "action": (self.action_dim,),
            "reward": (1,),
            "next_obs": (1,) + self.obs_shape,
            "next_achieved_goal": (1,) + self.achieved_goal_shape,
            "next_desired_goal": (1,) + self.desired_goal_shape,
            "done": (1,),
        }
        self._observation_keys = ["observation", "achieved_goal", "desired_goal"]
        self._buffer = {
            key: np.zeros((self.max_episode_stored, self.max_episode_length, *dim), dtype=np.float32)
            for key, dim in input_shape.items()
        }
        # Store info dicts are it can be used to compute the reward (e.g. continuity cost)
        self.info_buffer = [deque(maxlen=self.max_episode_length) for _ in range(self.max_episode_stored)]
        # episode length storage, needed for episodes which has less steps than the maximum length
        self.episode_lengths = np.zeros(self.max_episode_stored, dtype=np.int64)
        # self.total_time_spent_for_sample_transition = 0

    def __getstate__(self) -> Dict[str, Any]:
        """
        Gets state for pickling.

        Excludes self.env, as in general Env's may not be pickleable.
        Note: when using offline sampling, this will also save the offline replay buffer.
        """
        state = self.__dict__.copy()
        # these attributes are not pickleable
        del state["env"]
        return state

    def __setstate__(self, state: Dict[str, Any]) -> None:
        """
        Restores pickled state.

        User must call ``set_env()`` after unpickling before using.

        :param state:
        """
        self.__dict__.update(state)
        assert "env" not in state
        self.env = None

    def set_env(self, env: VecEnv) -> None:
        """
        Sets the environment.

        :param env:
        """
        if self.env is not None:
            raise ValueError("Trying to set env of already initialized environment.")

        self.env = env

    def _get_samples(self, batch_inds: np.ndarray, env: Optional[VecNormalize] = None) -> DictReplayBufferSamples:
        """
        Abstract method from base class.
        """
        raise NotImplementedError()

    def sample(
        self,
        batch_size: int,
        env: Optional[VecNormalize],
    ) -> DictReplayBufferSamples:
        """
        Sample function for online sampling of HER transition,
        this replaces the "regular" replay buffer ``sample()``
        method in the ``train()`` function.

        :param batch_size: Number of element to sample
        :param env: Associated gym VecEnv
            to normalize the observations/rewards when sampling
        :return: Samples.
        """
        if self.replay_buffer is not None:
            return self.replay_buffer.sample(batch_size, env)
        return self._sample_transitions(batch_size, maybe_vec_env=env, online_sampling=True)  # pytype: disable=bad-return-type

    def _sample_offline(
        self,
        n_sampled_goal: Optional[int] = None,
    ) -> Tuple[Dict[str, np.ndarray], Dict[str, np.ndarray], np.ndarray, np.ndarray]:
        """
        Sample function for offline sampling of HER transition,
        in that case, only one episode is used and transitions
        are added to the regular replay buffer.

        :param n_sampled_goal: Number of sampled goals for replay
        :return: at most(n_sampled_goal * episode_length) HER transitions.
        """
        # `maybe_vec_env=None` as we should store unnormalized transitions,
        # they will be normalized at sampling time
        return self._sample_transitions(
            batch_size=None,
            maybe_vec_env=None,
            online_sampling=False,
            n_sampled_goal=n_sampled_goal,
        )

    def sample_goals(
        self,
        episode_indices: np.ndarray,
        her_indices: np.ndarray,
        transitions_indices: np.ndarray,
    ) -> np.ndarray:
        """
        Sample goals based on goal_selection_strategy.
        This is a vectorized (fast) version.

        :param episode_indices: Episode indices to use.
        :param her_indices: HER indices.
        :param transitions_indices: Transition indices to use.
        :return: Return sampled goals.
        """
        her_episode_indices = episode_indices[her_indices]

        if self.goal_selection_strategy == GoalSelectionStrategy.FINAL:
            # replay with final state of current episode
            transitions_indices = self.episode_lengths[her_episode_indices] - 1

        elif self.goal_selection_strategy == GoalSelectionStrategy.FUTURE:
            # replay with random state which comes from the same episode and was observed after current transition
            transitions_indices = np.random.randint(
                transitions_indices[her_indices] + 1, self.episode_lengths[her_episode_indices]
            )

        elif self.goal_selection_strategy == GoalSelectionStrategy.EPISODE:
            # replay with random state which comes from the same episode as current transition
            transitions_indices = np.random.randint(self.episode_lengths[her_episode_indices])

        else:
            raise ValueError(f"Strategy {self.goal_selection_strategy} for sampling goals not supported!")

        return self._buffer["achieved_goal"][her_episode_indices, transitions_indices]

    def _sample_transitions(
        self,
        batch_size: Optional[int],
        maybe_vec_env: Optional[VecNormalize],
        online_sampling: bool,
        n_sampled_goal: Optional[int] = None,
    ) -> Union[DictReplayBufferSamples, Tuple[Dict[str, np.ndarray], Dict[str, np.ndarray], np.ndarray, np.ndarray]]:
        """
        :param batch_size: Number of element to sample (only used for online sampling)
        :param env: associated gym VecEnv to normalize the observations/rewards
            Only valid when using online sampling
        :param online_sampling: Using online_sampling for HER or not.
        :param n_sampled_goal: Number of sampled goals for replay. (offline sampling)
        :return: Samples.
        """
        # Select which episodes to use
        if online_sampling:
            assert batch_size is not None, "No batch_size specified for online sampling of HER transitions"
            # Do not sample the episode with index `self.pos` as the episode is invalid
            if self.full:
                episode_indices = (
                    np.random.randint(1, self.n_episodes_stored, batch_size) + self.pos
                ) % self.n_episodes_stored
            else:
                episode_indices = np.random.randint(0, self.n_episodes_stored, batch_size)
            # A subset of the transitions will be relabeled using HER algorithm
            her_indices = np.arange(batch_size)[: int(self.her_ratio * batch_size)]
        else:
            assert maybe_vec_env is None, "Transitions must be stored unnormalized in the replay buffer"
            assert n_sampled_goal is not None, "No n_sampled_goal specified for offline sampling of HER transitions"
            if self.goal_selection_strategy == GoalSelectionStrategy.PAST_DESIRED_SUCCESS:
                assert self.n_sampled_goal_preselection is not None, "Using PAST_DESIRED_SUCCESS strategy, but n_sampled_goal_preselection not given"
                n_sampled_goal_preselection = self.n_sampled_goal_preselection
            else:

                assert self.n_sampled_goal_preselection is None, "Not using PAST_DESIRED_SUCCESS strategy, but n_sampled_goal_preselection given"
                # In this case there is no preselection
                n_sampled_goal_preselection = n_sampled_goal

            # Offline sampling: there is only one episode stored
            episode_length = self.episode_lengths[0]
            # we sample n_sampled_goal_preselection per timestep in the episode (only one is stored).
            episode_indices = np.tile(0, (episode_length * n_sampled_goal_preselection))
            # we only sample virtual transitions
            # as real transitions are already stored in the replay buffer
            her_indices = np.arange(len(episode_indices))

        ep_lengths = self.episode_lengths[episode_indices]

        # Special case when using the "future" goal sampling strategy
        # we cannot sample all transitions, we have to remove the last timestep
        if self.goal_selection_strategy == GoalSelectionStrategy.FUTURE:
            # restrict the sampling domain when ep_lengths > 1
            # otherwise filter out the indices
            her_indices = her_indices[ep_lengths[her_indices] > 1]
            ep_lengths[her_indices] -= 1

        if online_sampling:
            # Select which transitions to use
            transitions_indices = np.random.randint(ep_lengths)
        else:
            if her_indices.size == 0:
                # Episode of one timestep, not enough for using the "future" strategy
                # no virtual transitions are created in that case
                return {}, {}, np.zeros(0), np.zeros(0)
            else:
                # Repeat every transition index n_sampled_goal_preselection times
                # to sample n_sampled_goal_preselection per timestep in the episode (only one is stored).
                # Now with the corrected episode length when using "future" strategy
                transitions_indices = np.tile(np.arange(ep_lengths[0]), n_sampled_goal_preselection)
                episode_indices = episode_indices[transitions_indices]
                her_indices = np.arange(len(episode_indices))

        # get selected transitions
        transitions = {key: self._buffer[key][episode_indices, transitions_indices].copy() for key in self._buffer.keys()}

        # sample new desired goals and relabel the transitions
        if self.goal_selection_strategy in [
            GoalSelectionStrategy.PAST_DESIRED,
            GoalSelectionStrategy.PAST_DESIRED_SUCCESS
        ]:
            # In this case, simply sample len(her_indices) goals (stored as observations)
            # from self.desired_goal_storage
            # TODO the expand_dims solution here won't generalize to multiple environments
            new_goals = np.expand_dims(
                self.desired_goal_storage.sample(len(her_indices)).observations.cpu(),
                axis=1
            )
        else:
            new_goals = self.sample_goals(episode_indices, her_indices, transitions_indices)

        transitions["desired_goal"][her_indices] = new_goals

        # Convert info buffer to numpy array
        transitions["info"] = np.array(
            [
                self.info_buffer[episode_idx][transition_idx]
                for episode_idx, transition_idx in zip(episode_indices, transitions_indices)
            ]
        )

        # # For illustration purposes: info is saved to the transition
        # # during which it is produced. If the state at the beginning
        # # of the transition is saved to info["observation"], the
        # # following relation holds:
        # assert np.all(
        #     transitions["observation"].reshape(-1) == np.array(
        #         [info[0]["observation"]["observation"] for info in transitions["info"]]
        #     ).reshape(-1)
        # )

        # Edge case: episode of one timesteps with the future strategy
        # no virtual transition can be created
        if len(her_indices) > 0:
            # Vectorized computation of the new reward
            compute_reward_returns = self.env.env_method(
                "compute_reward",
                # the new state depends on the previous state and action
                # s_{t+1} = f(s_t, a_t)
                # so the next_achieved_goal depends also on the previous state and action
                # because we are in a GoalEnv:
                # r_t = reward(s_t, a_t) = reward(next_achieved_goal, desired_goal)
                # therefore we have to use "next_achieved_goal" and not "achieved_goal"
                transitions["next_achieved_goal"][her_indices, 0],
                # here we use the new desired goal
                transitions["desired_goal"][her_indices, 0],
                transitions["info"][her_indices, 0],
                indices=0,  # only call method for one env
            )[0]
            if self.modify_goal:
                transitions["reward"][her_indices, 0] = compute_reward_returns[0]
                transitions["desired_goal"][her_indices, 0] = compute_reward_returns[1]
            else:
                transitions["reward"][her_indices, 0] = compute_reward_returns

        for key in transitions.keys():
            if self.goal_selection_strategy == GoalSelectionStrategy.FUTURE:
                assert len(transitions[key]) == (episode_length - 1)*n_sampled_goal_preselection
            else:
                assert len(transitions[key]) == episode_length*n_sampled_goal_preselection

        # When using GoalSelectionStrategy.PAST_DESIRED_SUCCESS, this selection is filtered again
        if self.goal_selection_strategy == GoalSelectionStrategy.PAST_DESIRED_SUCCESS:
            assert n_sampled_goal < n_sampled_goal_preselection, "n_sampled_goal must be smaller than preselection"
            assert transitions['reward'].shape == (episode_length*n_sampled_goal_preselection, 1)
            reward_per_episode = np.sum(
                transitions['reward'].reshape(n_sampled_goal_preselection, episode_length), axis=-1
            )
            # returns unique reward_per_episode (sorted in ascending order)
            uniques, unique_indices = np.unique(reward_per_episode, return_index=True)

            if len(uniques) >= n_sampled_goal:
                # preferably, select n_sampled_goal largest unique
                winner_episodes = unique_indices[-n_sampled_goal:]
            else:
                # if not possible, select n_sampled_goal largest
                winner_episodes = np.argsort(reward_per_episode)[-n_sampled_goal:]

            keep_mask = np.zeros(n_sampled_goal_preselection, dtype=bool)
            keep_mask[winner_episodes] = True
            keep_mask = np.repeat(keep_mask, episode_length)
            # TODO control using verbosity flag
            print(f'Unique episode rewards on preselection: {len(uniques)}')
            print(f'Mean replay episode reward before selection: {np.mean(reward_per_episode)}')
            
            for key in transitions.keys():
                transitions[key] = transitions[key][keep_mask]

            print(f'Mean replay episode reward after selection: {np.sum(transitions["reward"])/n_sampled_goal}')
        else:
            assert n_sampled_goal_preselection == n_sampled_goal
        
        for key in transitions.keys():
            if self.goal_selection_strategy == GoalSelectionStrategy.FUTURE:
                assert len(transitions[key]) == (episode_length-1)*n_sampled_goal
            else:
                assert len(transitions[key]) == episode_length*n_sampled_goal

        # concatenate observation with (desired) goal
        observations = self._normalize_obs(transitions, maybe_vec_env)

        # HACK to make normalize obs and `add()` work with the next observation
        next_observations = {
            "observation": transitions["next_obs"],
            "achieved_goal": transitions["next_achieved_goal"],
            # The desired goal for the next observation must be the same as the previous one
            "desired_goal": transitions["desired_goal"],
        }
        next_observations = self._normalize_obs(next_observations, maybe_vec_env)

        if online_sampling:
            next_obs = {key: self.to_torch(next_observations[key][:, 0, :]) for key in self._observation_keys}

            normalized_obs = {key: self.to_torch(observations[key][:, 0, :]) for key in self._observation_keys}

            return DictReplayBufferSamples(
                observations=normalized_obs,
                actions=self.to_torch(transitions["action"]),
                next_observations=next_obs,
                dones=self.to_torch(transitions["done"]),
                rewards=self.to_torch(self._normalize_reward(transitions["reward"], maybe_vec_env)),
            )
        else:
            return observations, next_observations, transitions["action"], transitions["reward"]

    def add(
        self,
        obs: Dict[str, np.ndarray],
        next_obs: Dict[str, np.ndarray],
        action: np.ndarray,
        reward: np.ndarray,
        done: np.ndarray,
        infos: List[Dict[str, Any]],
    ) -> None:

        if self.current_idx == 0 and self.full:
            # Clear info buffer
            self.info_buffer[self.pos] = deque(maxlen=self.max_episode_length)

        # Remove termination signals due to timeout
        if self.handle_timeout_termination:
            done_ = done * (1 - np.array([info.get("TimeLimit.truncated", False) for info in infos]))
        else:
            done_ = done

        self._buffer["observation"][self.pos][self.current_idx] = obs["observation"]
        self._buffer["achieved_goal"][self.pos][self.current_idx] = obs["achieved_goal"]
        self._buffer["desired_goal"][self.pos][self.current_idx] = obs["desired_goal"]
        self._buffer["action"][self.pos][self.current_idx] = action
        self._buffer["done"][self.pos][self.current_idx] = done_
        self._buffer["reward"][self.pos][self.current_idx] = reward
        self._buffer["next_obs"][self.pos][self.current_idx] = next_obs["observation"]
        self._buffer["next_achieved_goal"][self.pos][self.current_idx] = next_obs["achieved_goal"]
        self._buffer["next_desired_goal"][self.pos][self.current_idx] = next_obs["desired_goal"]

        # When doing offline sampling
        # Add real transition to normal replay buffer
        if self.replay_buffer is not None:
            self.replay_buffer.add(
                obs,
                next_obs,
                action,
                reward,
                done,
                infos,
            )

        self.info_buffer[self.pos].append(infos)

        # update current pointer
        self.current_idx += 1

        self.episode_steps += 1

        if done or self.episode_steps >= self.max_episode_length:
            self.store_episode()
            if not self.online_sampling:
                # If past_desired strategy is used:
                if self.goal_selection_strategy in [
                    GoalSelectionStrategy.PAST_DESIRED,
                    GoalSelectionStrategy.PAST_DESIRED_SUCCESS
                ]:
                    # Add latest desired_goal to self.desired_goal_storage
                    self.desired_goal_storage.add(
                        next_obs["desired_goal"],
                        None, None, None, None, None
                    )

                # sample virtual transitions and store them in replay buffer
                # time0 = time.time()
                self._sample_her_transitions()
                # self.total_time_spent_for_sample_transition += (time.time()-time0)
                # clear storage for current episode
                self.reset()

            self.episode_steps = 0

    def store_episode(self) -> None:
        """
        Increment episode counter
        and reset transition pointer.
        """
        # add episode length to length storage
        self.episode_lengths[self.pos] = self.current_idx

        # update current episode pointer
        # Note: in the OpenAI implementation
        # when the buffer is full, the episode replaced
        # is randomly chosen
        self.pos += 1
        if self.pos == self.max_episode_stored:
            self.full = True
            self.pos = 0
        # reset transition pointer
        self.current_idx = 0

    def _sample_her_transitions(self) -> None:
        """
        Sample additional goals and store new transitions in replay buffer
        when using offline sampling.
        """

        # Sample goals to create virtual transitions for the last episode.
        observations, next_observations, actions, rewards = self._sample_offline(n_sampled_goal=self.n_sampled_goal)

        # Store virtual transitions in the replay buffer, if available
        if len(observations) > 0:
            for i in range(len(observations["observation"])):
                self.replay_buffer.add(
                    {key: obs[i] for key, obs in observations.items()},
                    {key: next_obs[i] for key, next_obs in next_observations.items()},
                    actions[i],
                    rewards[i],
                    # We consider the transition as non-terminal
                    done=[False],
                    infos=[{}],
                )

    @property
    def n_episodes_stored(self) -> int:
        if self.full:
            return self.max_episode_stored
        return self.pos

    def size(self) -> int:
        """
        :return: The current number of transitions in the buffer.
        """
        return int(np.sum(self.episode_lengths))

    def reset(self) -> None:
        """
        Reset the buffer.
        """
        self.pos = 0
        self.current_idx = 0
        self.full = False
        self.episode_lengths = np.zeros(self.max_episode_stored, dtype=np.int64)

    def truncate_last_trajectory(self) -> None:
        """
        Only for online sampling, called when loading the replay buffer.
        If called, we assume that the last trajectory in the replay buffer was finished
        (and truncate it).
        If not called, we assume that we continue the same trajectory (same episode).
        """
        # If we are at the start of an episode, no need to truncate
        current_idx = self.current_idx

        # truncate interrupted episode
        if current_idx > 0:
            warnings.warn(
                "The last trajectory in the replay buffer will be truncated.\n"
                "If you are in the same episode as when the replay buffer was saved,\n"
                "you should use `truncate_last_trajectory=False` to avoid that issue."
            )
            # get current episode and transition index
            pos = self.pos
            # set episode length for current episode
            self.episode_lengths[pos] = current_idx
            # set done = True for current episode
            # current_idx was already incremented
            self._buffer["done"][pos][current_idx - 1] = np.array([True], dtype=np.float32)
            # reset current transition index
            self.current_idx = 0
            # increment episode counter
            self.pos = (self.pos + 1) % self.max_episode_stored
            # update "full" indicator
            self.full = self.full or self.pos == 0


class VecHerReplayBuffer(DictReplayBuffer):
    """
    A Vectorized version of the Hindsight Experience Replay (HER) buffer.
    It is made to handle multiple environments at the same time
    and keep different ``HerReplayBuffer`` to do so.

    :param env: The training environment
    :param buffer_size: The size of the buffer measured in transitions.
    :param max_episode_length: The maximum length of an episode. If not specified,
        it will be automatically inferred if the environment uses a ``gym.wrappers.TimeLimit`` wrapper.
    :param goal_selection_strategy: Strategy for sampling goals for replay.
        One of ['episode', 'final', 'future']
    :param device: PyTorch device
    :param n_sampled_goal: Number of virtual transitions to create per real transition,
        by sampling new goals.
    :param handle_timeout_termination: Handle timeout termination (due to timelimit)
        separately and treat the task as infinite horizon task.
        https://github.com/DLR-RM/stable-baselines3/issues/284
    """

    def __init__(
        self,
        env: VecEnv,
        buffer_size: int,
        device: Union[th.device, str] = "cpu",
        replay_buffer: Optional[DictReplayBuffer] = None,
        max_episode_length: Optional[int] = None,
        n_sampled_goal: int = 4,
        goal_selection_strategy: Union[GoalSelectionStrategy, str] = "future",
        online_sampling: bool = True,
        handle_timeout_termination: bool = True,
    ):
        super().__init__(buffer_size, env.observation_space, env.action_space, device, env.num_envs)

        self.n_envs = env.num_envs
        self.buffers = []
        # Divides buffer size as evenly as possible
        # Each HerReplayBuffer will store at least one episode anyway
        buffer_sizes = [(buffer_size + i) // self.n_envs for i in range(self.n_envs)]
        for i in range(env.num_envs):
            self.buffers.append(
                HerReplayBuffer(
                    env,
                    buffer_sizes[i],
                    device,
                    replay_buffer,
                    max_episode_length,
                    n_sampled_goal,
                    goal_selection_strategy,
                    online_sampling,
                    handle_timeout_termination,
                )
            )

    def add(
        self,
        obs: Dict[str, np.ndarray],
        next_obs: Dict[str, np.ndarray],
        action: np.ndarray,
        reward: np.ndarray,
        done: np.ndarray,
        infos: List[Dict[str, Any]],
    ) -> None:

        for i in range(len(obs["observation"])):
            self.buffers[i].add(
                {key: obs_[i] for key, obs_ in obs.items()},
                {key: next_obs_[i] for key, next_obs_ in next_obs.items()},
                action[i],
                reward[i],
                done=np.array([done[i]]),
                infos=[infos[i]],
            )

    def sample(
        self,
        batch_size: int,
        env: Optional[VecNormalize],
    ) -> DictReplayBufferSamples:
        """
        Sample function for online sampling of HER transition,
        this replaces the "regular" replay buffer ``sample()``
        method in the ``train()`` function.

        :param batch_size: Number of element to sample
        :param env: Associated gym VecEnv
            to normalize the observations/rewards when sampling
        :return: Samples.
        """
        samples = []
        # Divides samples as evenly as possible
        batch_sizes = [(batch_size + i) // self.n_envs for i in range(self.n_envs)]
        for i in range(self.n_envs):
            if batch_sizes[i] > 0:
                samples.append(self.buffers[i].sample(batch_sizes[i], env))

        keys = list(samples[0].observations.keys())

        return DictReplayBufferSamples(
            observations={key: th.cat([sample.observations[key] for sample in samples]) for key in keys},
            actions=th.cat([sample.actions for sample in samples]),
            next_observations={key: th.cat([sample.next_observations[key] for sample in samples]) for key in keys},
            dones=th.cat([sample.dones for sample in samples]),
            rewards=th.cat([sample.rewards for sample in samples]),
        )

        def truncate_last_trajectory(self) -> None:
            """
            See ``HerReplayBuffer`` doc.
            """
            for buffer in self.buffers:
                self.buffers.truncate_last_trajectory()

        def set_env(self, env: VecEnv) -> None:
            """
            See ``HerReplayBuffer`` doc.

            :param env:
            """
            for buffer in self.buffers:
                self.buffers.set_env(env)
