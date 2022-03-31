from enum import Enum


class GoalSelectionStrategy(Enum):
    """
    The strategies for selecting new goals when
    creating artificial transitions.
    """

    # Select a goal that was achieved
    # after the current step, in the same episode
    FUTURE = 0
    # Select the goal that was achieved
    # at the end of the episode
    FINAL = 1
    # Select a goal that was achieved in the episode
    EPISODE = 2
    # Select a goal randomly that was DESIRED
    # (not necessarily achieved) at the end of a previous episode
    PAST_DESIRED = 3
    # Select a goal that was DESIRED
    # (not necessarily achieved) at the end of a previous episode
    # randomly but biased towards being successful
    PAST_DESIRED_SUCCESS = 4


# For convenience
# that way, we can use string to select a strategy
KEY_TO_GOAL_STRATEGY = {
    "future": GoalSelectionStrategy.FUTURE,
    "final": GoalSelectionStrategy.FINAL,
    "episode": GoalSelectionStrategy.EPISODE,
    "past_desired": GoalSelectionStrategy.PAST_DESIRED,
    "past_desired_success": GoalSelectionStrategy.PAST_DESIRED_SUCCESS
}
