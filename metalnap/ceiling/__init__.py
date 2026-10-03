from .prometheus import PrometheusCeiling  # noqa: F401
from .static import StaticCeiling          # noqa: F401


def from_config(cfg, prom_url):
    """The ceiling an environment asks for, or None when it asks for none.

    Neither source set is no ceiling at all, and the controller then runs
    exactly as it did before the seam existed -- no query a minute for an
    answer that is always "none". Both set was refused by Config.validate().
    """
    if cfg.ceiling_static is not None:
        return StaticCeiling(cfg.ceiling_static)
    if cfg.ceiling_query:
        return PrometheusCeiling(prom_url, cfg.ceiling_query)
    return None
