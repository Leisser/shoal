"""unfork - stop forking thirty-two processes.

Nothing here yet but the diagnosis. The runtime lands once `doctor` has told us
what the ecosystem can actually bear.
"""
from ._build import Build, detect, gil_enabled, is_freethreaded_build

__version__ = "0.0.1"
__all__ = ["Build", "detect", "gil_enabled", "is_freethreaded_build", "__version__"]
