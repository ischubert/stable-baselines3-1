"""
Exploriaton Policies: Abstract base classes
"""

class ExplorationPolicy():
    """
    Abstract ExplorationPolicy class
    """
    def predict(self, observation, deterministic=False):
        """
        Return action based on observation. Action can be None, in which case the
        decision goes back to the RL algorithm
        """
        raise NotImplementedError

    def propose_reset(self):
        """
        Return a proposed reset position
        """
        raise NotImplementedError
