"""Pure-Python QFS / RefPack codec used by SimCity 4 DBPF subfiles.

This module replaces the former C extension ``QFS`` (``Modules/qfs.c``),
which implemented Denis Auroux's QFS decompressor/compressor (v1.22,
copyright (c) 1998-2002 Denis Auroux).  The on-disk format and the public
API (:func:`decode` / :func:`encode`) are byte-for-byte compatible with the
original extension, so no caller changes are required beyond the import
name.
"""

QFS_MAXITER = 50          # compression quality factor (candidates per position)
_WINDOW_LEN = 1 << 17
_WINDOW_MASK = _WINDOW_LEN - 1


def _refcopy(buf, pos, offset, length):
    """LZ-style (possibly overlapping) copy inside ``buf``."""
    src = pos - offset
    if offset >= length:
        buf[pos:pos + length] = buf[src:src + length]
    else:
        for i in range(length):
            buf[pos + i] = buf[src + i]


def decode(data):
    """Decompress a QFS byte stream and return the raw bytes."""
    inbuf = data if isinstance(data, (bytes, bytearray)) else bytes(data)
    inlen = len(inbuf)
    if inlen < 5:
        return b""
    outlen = (inbuf[2] << 16) + (inbuf[3] << 8) + inbuf[4]
    outbuf = bytearray(outlen)
    inpos = 8 if (inbuf[0] & 0x01) else 5
    outpos = 0

    while inpos < inlen and inbuf[inpos] < 0xFC:
        packcode = inbuf[inpos]
        a = inbuf[inpos + 1] if inpos + 1 < inlen else 0
        b = inbuf[inpos + 2] if inpos + 2 < inlen else 0

        if not (packcode & 0x80):
            nlit = packcode & 3
            outbuf[outpos:outpos + nlit] = inbuf[inpos + 2:inpos + 2 + nlit]
            inpos += nlit + 2
            outpos += nlit
            length = ((packcode & 0x1C) >> 2) + 3
            offset = ((packcode >> 5) << 8) + a + 1
            _refcopy(outbuf, outpos, offset, length)
            outpos += length
        elif not (packcode & 0x40):
            nlit = (a >> 6) & 3
            outbuf[outpos:outpos + nlit] = inbuf[inpos + 3:inpos + 3 + nlit]
            inpos += nlit + 3
            outpos += nlit
            length = (packcode & 0x3F) + 4
            offset = (a & 0x3F) * 256 + b + 1
            _refcopy(outbuf, outpos, offset, length)
            outpos += length
        elif not (packcode & 0x20):
            c = inbuf[inpos + 3]
            nlit = packcode & 3
            outbuf[outpos:outpos + nlit] = inbuf[inpos + 4:inpos + 4 + nlit]
            inpos += nlit + 4
            outpos += nlit
            length = ((packcode >> 2) & 3) * 256 + c + 5
            offset = ((packcode & 0x10) << 12) + 256 * a + b + 1
            _refcopy(outbuf, outpos, offset, length)
            outpos += length
        else:
            nlit = (packcode & 0x1F) * 4 + 4
            outbuf[outpos:outpos + nlit] = inbuf[inpos + 1:inpos + 1 + nlit]
            inpos += nlit + 1
            outpos += nlit

    # trailing literal bytes
    if inpos < inlen and outpos < outlen:
        nlit = inbuf[inpos] & 3
        outbuf[outpos:outpos + nlit] = inbuf[inpos + 1:inpos + 1 + nlit]
        outpos += nlit

    return bytes(outbuf)


def _match_len(buf, p1, p2, maxlen):
    """Length of the common byte run ``buf[p1:]`` vs ``buf[p2:]``, capped."""
    n = 0
    step = 64
    while n + step <= maxlen and buf[p1 + n:p1 + n + step] == buf[p2 + n:p2 + n + step]:
        n += step
    while n < maxlen and buf[p1 + n] == buf[p2 + n]:
        n += 1
    return n


def encode(data):
    """Compress ``data`` into a QFS byte stream."""
    src = data if isinstance(data, bytes) else bytes(data)
    inlen = len(src)
    # The matcher reads up to ~1028 bytes past the current position; pad so
    # those reads stay in bounds.  Padding content is irrelevant: any match
    # that would reach past the real input is discarded by the length clamp.
    inbuf = src + b"\x00" * 1064
    outbuf = bytearray(inlen * 2 + 1064)

    rev_similar = [-1] * _WINDOW_LEN
    rev_last = [[-1] * 256 for _ in range(256)]

    outbuf[0] = 0x10
    outbuf[1] = 0xFB
    outbuf[2] = (inlen >> 16) & 0xFF
    outbuf[3] = (inlen >> 8) & 0xFF
    outbuf[4] = inlen & 0xFF
    outpos = 5
    lastwrot = 0

    for inpos in range(inlen):
        cur = inbuf[inpos]
        nxt = inbuf[inpos + 1]
        offs = rev_last[cur][nxt]
        rev_similar[inpos & _WINDOW_MASK] = offs
        rev_last[cur][nxt] = inpos
        if inpos < lastwrot:
            continue

        bestlen = 0
        bestoffs = 0
        it = 0
        while offs >= 0 and (inpos - offs) < _WINDOW_LEN and it < QFS_MAXITER:
            it += 1
            # Quick reject: a candidate can only beat the current best if the
            # byte that would extend that best already matches.  This skips
            # the full _match_len scan for the vast majority of candidates
            # while choosing exactly the same match the naive loop would.
            if bestlen >= 2 and inbuf[inpos + bestlen] != inbuf[offs + bestlen]:
                offs = rev_similar[offs & _WINDOW_MASK]
                continue
            length = 2 + _match_len(inbuf, inpos + 2, offs + 2, 1026)
            if length > bestlen:
                bestlen = length
                bestoffs = inpos - offs
            offs = rev_similar[offs & _WINDOW_MASK]

        if bestlen > inlen - inpos:
            bestlen = inpos - inlen          # forces a discard below
        if bestlen <= 2:
            bestlen = 0
        if bestlen == 3 and bestoffs > 1024:
            bestlen = 0
        if bestlen == 4 and bestoffs > 16384:
            bestlen = 0

        if bestlen:
            # flush whole groups of 4 unwritten literal bytes
            while inpos - lastwrot >= 4:
                n = (inpos - lastwrot) // 4 - 1
                if n > 0x1B:
                    n = 0x1B
                outbuf[outpos] = 0xE0 + n
                outpos += 1
                n = 4 * n + 4
                outbuf[outpos:outpos + n] = inbuf[lastwrot:lastwrot + n]
                lastwrot += n
                outpos += n

            nlit = inpos - lastwrot
            if bestlen <= 10 and bestoffs <= 1024:
                outbuf[outpos] = (((bestoffs - 1) >> 8) << 5) + ((bestlen - 3) << 2) + nlit
                outbuf[outpos + 1] = (bestoffs - 1) & 0xFF
                outpos += 2
            elif bestlen <= 67 and bestoffs <= 16384:
                outbuf[outpos] = 0x80 + (bestlen - 4)
                outbuf[outpos + 1] = (nlit << 6) + ((bestoffs - 1) >> 8)
                outbuf[outpos + 2] = (bestoffs - 1) & 0xFF
                outpos += 3
            else:
                bo = bestoffs - 1
                outbuf[outpos] = 0xC0 + ((bo >> 16) << 4) + (((bestlen - 5) >> 8) << 2) + nlit
                outbuf[outpos + 1] = (bo >> 8) & 0xFF
                outbuf[outpos + 2] = bo & 0xFF
                outbuf[outpos + 3] = (bestlen - 5) & 0xFF
                outpos += 4
            outbuf[outpos:outpos + nlit] = inbuf[lastwrot:lastwrot + nlit]
            outpos += nlit
            lastwrot += nlit + bestlen

    # end-of-stream: flush remaining literals
    inpos = inlen
    while inpos - lastwrot >= 4:
        n = (inpos - lastwrot) // 4 - 1
        if n > 0x1B:
            n = 0x1B
        outbuf[outpos] = 0xE0 + n
        outpos += 1
        n = 4 * n + 4
        outbuf[outpos:outpos + n] = inbuf[lastwrot:lastwrot + n]
        lastwrot += n
        outpos += n

    nlit = inpos - lastwrot
    outbuf[outpos] = 0xFC + nlit
    outpos += 1
    outbuf[outpos:outpos + nlit] = inbuf[lastwrot:lastwrot + nlit]
    outpos += nlit

    return bytes(outbuf[:outpos])
