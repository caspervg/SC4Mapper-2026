#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Filesystem helpers.

On Python 3 paths are ``str`` and the OS layer handles encoding, so
:func:`encode_filename` is a no-op kept only for API compatibility.
"""


def encode_filename(s):
    """Return ``s`` unchanged (kept for backwards compatibility)."""
    return s


encodeFilename = encode_filename
