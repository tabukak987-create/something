"""Seeds __init__ — exposes run_seed for programmatic invocation."""
from .seed_indian_bonds import run_seed, all_bonds

__all__ = ["run_seed", "all_bonds"]
