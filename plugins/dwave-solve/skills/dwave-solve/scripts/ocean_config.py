"""Where Ocean reads its configuration, shared by the scripts next to it."""

import configparser
import os

from dwave.cloud.config import get_configfile_paths, get_default_configfile_path

# A dwave.conf in the working directory is left out: a cloned repository
# could redirect the endpoint and receive the token.
def config_files():
    explicit = os.environ.get("DWAVE_CONFIG_FILE")
    return [explicit] if explicit else get_configfile_paths(local=False)


def user_config_file():
    return os.environ.get("DWAVE_CONFIG_FILE") or get_default_configfile_path()


def read_config(paths):
    parser = configparser.ConfigParser(default_section="defaults")
    parser.read(paths)
    return parser


def active_profile(parser):
    """The section Ocean reads, in its own order of precedence."""
    env = os.environ.get("DWAVE_PROFILE")
    if env:
        return env
    sections = parser.sections()
    return parser.defaults().get("profile") or (sections[0] if sections else "defaults")


def silence_debug_logging():
    """Ocean logs the loaded configuration, token included, at DEBUG level."""
    import logging
    logging.getLogger("dwave.cloud").setLevel(logging.WARNING)
