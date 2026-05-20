#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Filesystem helpers.

On Python 3 paths are ``str`` and the OS layer handles encoding, so
:func:`encodeFilename` is a no-op kept only for API compatibility.
"""


def encodeFilename(s):
    """Return ``s`` unchanged (kept for backwards compatibility)."""
    return s
