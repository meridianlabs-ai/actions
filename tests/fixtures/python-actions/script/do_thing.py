# /// script
# requires-python = ">=3.9"
# dependencies = ["six==1.16.0"]
# ///
"""Fixture PEP 723 script with an inline dependency and its own lock."""

import six

print("six version:", six.__version__)
