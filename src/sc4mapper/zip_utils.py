"""A seekable, streaming reader over a zlib-compressed file object.

Operates entirely on ``bytes``.  Used to read zlib-compressed SC4M files.
"""

import zlib


class ZipInputStream:

    def __init__(self, file):
        self.file = file
        self.__rewind()

    def __rewind(self):
        self.zip = zlib.decompressobj()
        self.pos = 0       # position in the zipped stream
        self.offset = 0    # position in the unzipped stream
        self.data = b""

    def __fill(self, nbytes):
        if self.zip:
            # read until we have enough bytes buffered
            while not nbytes or len(self.data) < nbytes:
                self.file.seek(self.pos)
                data = self.file.read(1024 * 1024)
                if not data:
                    self.data = self.data + self.zip.flush()
                    self.zip = None   # no more data
                    break
                self.pos = self.pos + len(data)
                self.data = self.data + self.zip.decompress(data)

    def seek(self, offset, whence=0):
        if whence == 0:
            position = offset
        elif whence == 1:
            position = self.offset + offset
        else:
            raise IOError("Illegal argument")
        if position < self.offset:
            raise IOError("Cannot seek backwards")

        # skip forward, in 16k blocks
        while position > self.offset:
            if not self.read(min(position - self.offset, 16384)):
                break

    def tell(self):
        return self.offset

    def read(self, nbytes=0):
        self.__fill(nbytes)
        if nbytes:
            data = self.data[:nbytes]
            self.data = self.data[nbytes:]
        else:
            data = self.data
            self.data = b""
        self.offset = self.offset + len(data)
        return data

    def readline(self):
        # make sure we have an entire line
        while self.zip and b"\n" not in self.data:
            self.__fill(len(self.data) + 512)
        i = self.data.find(b"\n") + 1
        if i <= 0:
            return self.read()
        return self.read(i)

    def readlines(self):
        lines = []
        while True:
            s = self.readline()
            if not s:
                break
            lines.append(s)
        return lines
