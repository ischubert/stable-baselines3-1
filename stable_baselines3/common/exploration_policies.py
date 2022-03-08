"""
Exploriaton Policies: Abstract base classes
"""

class ExplorationPolicy():
    """
    Abstract ExplorationPolicy class
    """
    def predict(self, observation, deterministic=False):
        """
        Return action based on observation
        """
        raise NotImplementedError

    def propose_reset(self):
        """
        Return a proposed reset position
        """
        raise NotImplementedError
