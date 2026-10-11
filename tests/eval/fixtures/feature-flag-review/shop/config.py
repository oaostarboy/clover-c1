"""Feature flags. Every flag is off unless it is set here."""

FLAGS = {}


def is_enabled(name):
    return bool(FLAGS.get(name, False))
