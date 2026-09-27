"""Recursopoly configuration loader.

Reads ``config.txt`` (plain ``key=value`` lines, ``#`` comments, blank lines
ignored) and exposes the values as a :class:`Config` object. Any key that is
missing or malformed falls back to the default in :data:`DEFAULTS`, so the
server always starts with a usable configuration.

config.txt holds server settings only. Everything that changes how the game
plays comes from the rule set the host picks (see rulesets.py). New server
settings are added as entries in :data:`DEFAULTS`; values are converted to
the same type as their default.
"""

import logging
import os

log = logging.getLogger("recursopoly.config")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(BASE_DIR, "config.txt")

# Every known server setting and its default. The type of each default
# decides how the text value from config.txt is converted. Game values
# (money, building limits, house rules, ...) live in rule sets instead: see
# rulesets/ and rulesets.py.
DEFAULTS = {
    "host": "0.0.0.0",
    "port": 5000,
    "debug": False,
    "join_code_length": 6,
    "disconnect_grace_seconds": 5,
    "turn_timer_seconds": 0,
    "scores_file": "scores.csv",
    "rulesets_dir": "rulesets",
    "default_ruleset": "classic",
}

_TRUE_WORDS = {"1", "true", "yes", "on"}
_FALSE_WORDS = {"0", "false", "no", "off"}


def _convert(raw, default):
    """Convert the string ``raw`` to the type of ``default``.

    Raises ValueError if the text can't be converted.
    """
    if isinstance(default, bool):  # check bool before int: bool is an int
        word = raw.strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
        raise ValueError(f"not a boolean: {raw!r}")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw


def parse_config_text(text):
    """Parse config text into a dict of raw string values."""
    values = {}
    for line_no, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            log.warning("Recursopoly config line %d ignored (no '='): %r", line_no, line)
            continue
        key, value = line.split("=", 1)
        # Allow trailing comments: port=5000  # comment
        value = value.split(" #", 1)[0]
        values[key.strip().lower()] = value.strip()
    return values


class Config:
    """Typed, read-only view of the Recursopoly settings."""

    def __init__(self, values=None):
        self._values = dict(DEFAULTS)
        for key, raw in (values or {}).items():
            if key in DEFAULTS:
                try:
                    self._values[key] = _convert(raw, DEFAULTS[key])
                except ValueError:
                    log.warning(
                        "Recursopoly config: bad value %r for %s, using default %r",
                        raw, key, DEFAULTS[key],
                    )
            else:
                # Unknown keys are kept as strings so later phases (or
                # experiments) can read them without changing this loader.
                self._values[key] = raw
        self._sanity_check()

    def _sanity_check(self):
        """Clamp values that would make the server unusable."""
        v = self._values
        v["join_code_length"] = max(4, v["join_code_length"])
        v["disconnect_grace_seconds"] = max(0, v["disconnect_grace_seconds"])
        v["turn_timer_seconds"] = max(0, v["turn_timer_seconds"])

    def get(self, key, default=None):
        return self._values.get(key, default)

    def __getattr__(self, key):
        # Only called when normal attribute lookup fails.
        try:
            return self._values[key]
        except KeyError:
            raise AttributeError(key) from None

    def as_dict(self):
        return dict(self._values)

    def path(self, key):
        """Resolve a path-valued setting relative to the project folder."""
        value = self._values[key]
        return value if os.path.isabs(value) else os.path.join(BASE_DIR, value)


def load_config(path=DEFAULT_CONFIG_PATH):
    """Load config.txt, falling back to defaults if the file is missing."""
    try:
        with open(path, encoding="utf-8") as fh:
            values = parse_config_text(fh.read())
    except FileNotFoundError:
        log.warning("Recursopoly config file %s not found, using defaults", path)
        values = {}
    return Config(values)
