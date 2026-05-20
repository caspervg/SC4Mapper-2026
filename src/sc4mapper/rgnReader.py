"""Reading and writing SimCity 4 regions and DBPF city save files."""

import math
import os
import os.path
import struct
import time
from math import sqrt

import numpy as Numeric
import wx
from PIL import Image, ImageDraw

from . import GradientReader
from . import qfs as QFS
from . import settings
from . import tools3d as tools3D
from .resources import asset_path
from .utils import encodeFilename

generic_saveValue = 3
COMPRESSED_SIG = 0xFB10


def Normalize(p1):
    dx = float(p1[0])
    dy = float(p1[1])
    dz = float(p1[2])
    norm = sqrt(dx * dx + dy * dy + dz * dz)
    try:
        return (p1[0] / norm, p1[1] / norm, p1[2] / norm)
    except ZeroDivisionError:
        return (0, 0, 0)


def ComputeOneRGB(bLight, height, waterLevel, region):
    lightDir = Normalize((1, -5, -1))
    rawRGB = tools3D.onePassColors(bLight, height.shape, waterLevel, height,
                                   GradientReader.paletteWater,
                                   GradientReader.paletteLand, lightDir)
    rgb = Numeric.frombuffer(rawRGB, Numeric.int8)
    rgb = Numeric.reshape(rgb, (height.shape[0], height.shape[1], 3))
    return rgb


class SC4Entry(object):
    def __init__(self, buffer, idx):
        self.compressed = False
        self.buffer = buffer
        t, g, i, self.fileLocation, self.filesize = struct.unpack("<3I2i", buffer)
        self.TGI = {'t': t, 'g': g, 'i': i}
        self.initialFileLocation = self.fileLocation
        self.order = idx

    def ReadFile(self, sc4, readWhole=True, decompress=False):
        self.rawContent = None
        if readWhole:
            sc4.seek(self.fileLocation)
            self.rawContent = sc4.read(self.filesize)
            if decompress:
                if len(self.rawContent) >= 8:
                    compress_sig = struct.unpack("<H", self.rawContent[0x04:0x04 + 2])[0]
                    if compress_sig == COMPRESSED_SIG:
                        self.compressed = True
            if self.compressed:
                uncompress = QFS.decode(self.rawContent[4:])
                self.content = uncompress
            else:
                self.content = self.rawContent

    def IsItThisTGI(self, tgi):
        return (tgi[0] == self.TGI['t'] and tgi[1] == self.TGI['g']
                and tgi[2] == self.TGI['i'])

    def GetDWORD(self, pos):
        return struct.unpack("<I", self.content[pos:pos + 4])[0]

    def GetString(self, pos, length):
        return self.content[pos:pos + length]


class SaveFile(object):
    """Create a SC4 save file holding city information, from a blank city."""

    def __init__(self, fileName):
        """Load filename, which should be a blank city."""
        self.fileName = fileName
        self.sc4 = open(self.fileName, "rb")
        self.ReadHeader()
        self.ReadEntries()

    def ReadHeader(self):
        """Read the SC4 DBPF header."""
        self.header = self.sc4.read(96)
        self.header = self.header[0:0x30] + b'\0' * 12 + self.header[0x30 + 12:96]
        raw = struct.unpack("<4s17I24s", self.header)
        self.indexRecordEntryCount = raw[9]
        self.indexRecordPosition = raw[10]
        self.indexRecordLength = raw[11]
        self.holeRecordEntryCount = raw[12]
        self.holeRecordPosition = raw[13]
        self.holeRecordLength = raw[14]
        self.dateCreated = raw[3]
        self.dateUpdated = raw[4]
        self.fileVersionMajor = raw[1]
        self.fileVersionMinor = raw[2]
        self.indexRecordType = raw[8]

    def ReadEntries(self):
        """Create entries so they can be written later."""
        self.entries = []
        self.sc4.seek(self.indexRecordPosition)
        header = self.sc4.read(self.indexRecordLength)
        for idx in range(self.indexRecordEntryCount):
            entry = SC4Entry(header[idx * 20:idx * 20 + 20], idx)
            if (entry.IsItThisTGI((0xA9DD6FF4, 0xE98F9525, 0x00000001))
                    or entry.IsItThisTGI((0xCA027EDB, 0xCA027EE1, 0x00000000))):
                entry.ReadFile(self.sc4, True, True)
            else:
                entry.ReadFile(self.sc4)
            self.entries.append(entry)
        self.sc4.close()

    def Save(self, cityXPos, cityYPos, heightMap, saveName):
        """Save a city: read all entries, create a save file, replace the
        height / city info / region-picture entries, write everything back.
        """
        global generic_saveValue
        self.heightMap = heightMap
        xSize = self.heightMap.shape[0]
        ySize = self.heightMap.shape[1]
        newData = QFS.encode(struct.pack('<H', 0x2) + self.heightMap.tobytes())
        newData = struct.pack("<i", len(newData)) + newData
        self.indexRecordPosition = 96
        self.dateUpdated = int(time.time()) + generic_saveValue * 65535
        generic_saveValue += 1
        self.header = (self.header[0:0x1C] + struct.pack("<I", self.dateUpdated)
                       + self.header[0x1C + 4:0x28]
                       + struct.pack("<i", self.indexRecordPosition)
                       + self.header[0x28 + 4:96])
        self.sc4 = open(self.fileName, "rb")
        for entry in self.entries:
            if (entry.IsItThisTGI((0xA9DD6FF4, 0xE98F9525, 0x00000001))
                    or entry.IsItThisTGI((0xCA027EDB, 0xCA027EE1, 0x00000000))):
                entry.ReadFile(self.sc4, True, True)
            if entry.rawContent is None:
                entry.ReadFile(self.sc4, True)
        self.sc4.close()
        while True:
            try:
                self.sc4 = open(saveName, "wb")
                break
            except IOError:
                dlg = wx.MessageDialog(
                    None, "file %s seems to be ReadOnly\nDo you want to "
                    "skip?(Yes)\nOr retry ?(No)" % (saveName),
                    "Warning", wx.YES_NO | wx.ICON_QUESTION)
                result = dlg.ShowModal()
                dlg.Destroy()
                if result == wx.ID_YES:
                    return False
        self.sc4.write(self.header)
        self.sc4.truncate(self.indexRecordPosition)
        self.sc4.seek(self.indexRecordPosition)
        pos = self.indexRecordPosition + self.indexRecordLength
        n = os.path.splitext(saveName)[0]
        with open(n + ".PNG", "rb") as png:
            regionPngData = png.read()
        with open(n + "_alpha.PNG", "rb") as png:
            alphaPngData = png.read()
        for entry in self.entries:
            entry.fileLocation = pos
            newbuffer = (entry.buffer[0:0x0C] + struct.pack("<i", entry.fileLocation)
                         + entry.buffer[0x0C + 4:])
            if entry.IsItThisTGI((0xA9DD6FF4, 0xE98F9525, 0x00000001)):  # heights
                newbuffer = (entry.buffer[0:0x0C]
                             + struct.pack("<i", entry.fileLocation)
                             + struct.pack("<i", len(newData))
                             + entry.buffer[0x10 + 4:])
                entry.rawContent = newData
                entry.compressed = 1
                entry.filesize = len(newData)
            if entry.IsItThisTGI((0xCA027EDB, 0xCA027EE1, 0x00000000)):  # city info
                v = self.dateUpdated
                entry.content = (entry.content[0:0x04]
                                 + struct.pack("<I", cityXPos)
                                 + struct.pack("<I", cityYPos)
                                 + entry.content[0x0C:39]
                                 + struct.pack("<I", v)
                                 + entry.content[39 + 4:])
                newDataCity = QFS.encode(entry.content)
                newDataCity = struct.pack("<i", len(newDataCity)) + newDataCity
                newbuffer = (entry.buffer[0:0x0C]
                             + struct.pack("<i", entry.fileLocation)
                             + struct.pack("<i", len(newDataCity))
                             + entry.buffer[0x10 + 4:])
                entry.rawContent = newDataCity
                entry.compressed = 1
                entry.filesize = len(newDataCity)
            if entry.IsItThisTGI((0x8a2482b9, 0x4a2482bb, 0x00000000)):  # region view
                pngData = regionPngData
                newbuffer = (entry.buffer[0:0x0C]
                             + struct.pack("<I", entry.fileLocation)
                             + struct.pack("<I", len(pngData))
                             + entry.buffer[0x10 + 4:])
                entry.rawContent = pngData
                entry.compressed = 0
                entry.filesize = len(pngData)
            if entry.IsItThisTGI((0x8a2482b9, 0x4a2482bb, 0x00000002)):  # alpha view
                pngData = alphaPngData
                newbuffer = (entry.buffer[0:0x0C]
                             + struct.pack("<I", entry.fileLocation)
                             + struct.pack("<I", len(pngData))
                             + entry.buffer[0x10 + 4:])
                entry.rawContent = pngData
                entry.compressed = 0
                entry.filesize = len(pngData)
            if entry.IsItThisTGI((0x8a2482b9, 0x4a2482bb, 0x00000004)):  # transport view
                pngData = regionPngData
                newbuffer = (entry.buffer[0:0x0C]
                             + struct.pack("<I", entry.fileLocation)
                             + struct.pack("<I", len(pngData))
                             + entry.buffer[0x10 + 4:])
                entry.rawContent = pngData
                entry.compressed = 0
                entry.filesize = len(pngData)
            if entry.IsItThisTGI((0x8a2482b9, 0x4a2482bb, 0x00000006)):  # transport alpha view
                pngData = alphaPngData
                newbuffer = (entry.buffer[0:0x0C]
                             + struct.pack("<I", entry.fileLocation)
                             + struct.pack("<I", len(pngData))
                             + entry.buffer[0x10 + 4:])
                entry.rawContent = pngData
                entry.compressed = 0
                entry.filesize = len(pngData)
            self.sc4.write(newbuffer)
            pos += entry.filesize
        for entry in self.entries:
            self.sc4.write(entry.rawContent)
        self.sc4.close()
        os.unlink(n + ".PNG")
        os.unlink(n + "_alpha.PNG")
        return True


def Save(city, folder, color, waterLevel):
    """Save a city file and build the thumbnail for the region view."""
    if city.cityXSize == 1:
        name = 'City - Small.sc4'
    if city.cityXSize == 2:
        name = 'City - Medium.sc4'
    if city.cityXSize == 4:
        name = 'City - Large.sc4'
    city.fileName = folder + "/" + "City - New city(%03d-%03d).sc4" % (
        city.cityXPos, city.cityYPos)
    BuildThumbnail(city, color, waterLevel)
    with asset_path(name) as path:
        saved = SaveFile(str(path))
        return saved.Save(city.cityXPos, city.cityYPos, city.heightMap,
                          city.fileName)


def BuildThumbnail(city, colors, waterLevel):
    """Build the region-view images (normal and alpha)."""
    n = os.path.splitext(city.fileName)[0]
    minx, miny, maxx, maxy, r = tools3D.generateImage(
        waterLevel, city.heightMap.shape, city.heightMap.tobytes(), colors)
    maxx += 2
    offset = len(r) // 2
    im = Image.frombytes("RGB", (514, 428), r[:offset])
    im = im.crop([minx, miny, maxx, maxy])
    alpha = Image.frombytes("RGB", (514, 428), r[offset:])
    alpha = alpha.crop([minx, miny, maxx, maxy])

    coverage = alpha.getchannel("B")
    im.putalpha(coverage)
    im.save(n + ".PNG")
    alpha.putalpha(0)
    alpha.save(n + "_alpha.PNG")
    return


class SC4File(object):
    """A file representing a saved city in the regions folder."""

    def __init__(self, fileName):
        """The file is opened here and closed in :meth:`ReadEntries`."""
        self.fileName = fileName
        self.sc4 = open(self.fileName, "rb")

    def AtPos(self, x, y):
        """Check if the city is at a specific config.bmp coordinate."""
        return x == self.cityXPos and y == self.cityYPos

    def Split(self):
        """Split a medium or large city into four smaller cities."""
        if self.cityXSize == 1:
            return []
        half = self.cityXSize // 2
        halfY = self.cityYSize // 2
        return [
            CityProxy(250, self.cityXPos, self.cityYPos, half, halfY),
            CityProxy(250, self.cityXPos + half, self.cityYPos, half, halfY),
            CityProxy(250, self.cityXPos + half, self.cityYPos + halfY, half, halfY),
            CityProxy(250, self.cityXPos, self.cityYPos + halfY, half, halfY),
        ]

    def ReadHeader(self):
        self.header = self.sc4.read(96)
        self.header = self.header[0:0x30] + b'\0' * 12 + self.header[0x30 + 12:96]
        raw = struct.unpack("<4s17I24s", self.header)
        self.indexRecordEntryCount = raw[9]
        self.indexRecordPosition = raw[10]
        self.indexRecordLength = raw[11]
        self.holeRecordEntryCount = raw[12]
        self.holeRecordPosition = raw[13]
        self.holeRecordLength = raw[14]
        self.dateCreated = raw[3]
        self.dateUpdated = raw[4]
        self.fileVersionMajor = raw[1]
        self.fileVersionMinor = raw[2]
        self.indexRecordType = raw[8]

    def ReadEntries(self):
        """Read all entries; only a few are read deeply and only the height
        entry is kept.
        """
        self.entries = []
        self.sc4.seek(self.indexRecordPosition)
        header = self.sc4.read(self.indexRecordLength)
        for idx in range(self.indexRecordEntryCount):
            entry = SC4Entry(header[idx * 20:idx * 20 + 20], idx)
            if (entry.IsItThisTGI((0xA9DD6FF4, 0xE98F9525, 0x00000001))
                    or entry.IsItThisTGI((0xCA027EDB, 0xCA027EE1, 0x00000000))):
                entry.ReadFile(self.sc4, True, True)
            if entry.IsItThisTGI((0xA9DD6FF4, 0xE98F9525, 0x00000001)):
                self.heightMapEntry = entry
            if entry.IsItThisTGI((0xCA027EDB, 0xCA027EE1, 0x00000000)):
                version = entry.GetDWORD(0x00)
                self.cityXPos = entry.GetDWORD(0x04)
                self.cityYPos = entry.GetDWORD(0x08)
                self.cityXSize = entry.GetDWORD(0x0C)
                self.cityYSize = entry.GetDWORD(0x10)
                offsetLen = 64
                if version == 0xD0001:
                    offsetLen = 64
                if version == 0xA0001:
                    offsetLen = 63
                if version == 0x90001:
                    offsetLen = 59
                sizeName = entry.GetDWORD(offsetLen)
                if sizeName < 100:
                    self.cityName = entry.GetString(offsetLen + 4, sizeName)
                else:
                    self.cityName = "weird name"
        self.ySize = self.cityYSize * 64 + 1
        self.xSize = self.cityXSize * 64 + 1
        self.xPos = self.cityXPos * 64
        self.yPos = self.cityYPos * 64
        self.sc4.close()


class CityProxy(object):
    """A proxy for an empty (not-yet-created) city."""

    def __init__(self, waterLevel, xPos, yPos, xSize, ySize):
        self.cityXPos = xPos
        self.cityYPos = yPos
        self.cityXSize = xSize
        self.cityYSize = ySize
        self.cityName = 'Not created yet'
        self.ySize = self.cityYSize * 64 + 1
        self.xSize = self.cityXSize * 64 + 1
        self.xPos = self.cityXPos * 64
        self.yPos = self.cityYPos * 64
        self.fileName = None

    def AtPos(self, x, y):
        """Check if the city is at a specific config.bmp coordinate."""
        return x == self.cityXPos and y == self.cityYPos

    def Split(self):
        """Split a medium or large city into four smaller cities."""
        if self.cityXSize == 1:
            return []
        half = self.cityXSize // 2
        halfY = self.cityYSize // 2
        return [
            CityProxy(250, self.cityXPos, self.cityYPos, half, halfY),
            CityProxy(250, self.cityXPos + half, self.cityYPos, half, halfY),
            CityProxy(250, self.cityXPos + half, self.cityYPos + halfY, half, halfY),
            CityProxy(250, self.cityXPos, self.cityYPos + halfY, half, halfY),
        ]


def WorkTheconfig(config, waterLevel):
    """Read the config.bmp, verify it, and create city proxies for it."""
    verified = Numeric.zeros(config.size, Numeric.int8)

    def Redish(value):
        """True for a small city."""
        (r, g, b) = value
        return r > g and r > b and r > 250

    def Greenish(value):
        """True for a medium city."""
        (r, g, b) = value
        return g > r and g > b and g > 250

    def Blueish(value):
        """True for a big city."""
        (r, g, b) = value
        return b > r and b > g and b > 250

    def VerifyMedium(x, y):
        """Verify the 2x2 pixels from x,y are green."""
        rgbs = (config.getpixel((x + 1, y)), config.getpixel((x, y + 1)),
                config.getpixel((x + 1, y + 1)))
        for rgb in rgbs:
            if not Greenish(rgb):
                assert 0
        verified[x, y] = 1
        verified[x + 1, y] = 1
        verified[x, y + 1] = 1
        verified[x + 1, y + 1] = 1

    def VerifyLarge(x, y):
        """Verify the 4x4 pixels from x,y are blue."""
        rgbs = (
            config.getpixel((x + 1, y)), config.getpixel((x + 2, y)),
            config.getpixel((x + 3, y)),
            config.getpixel((x, y + 1)), config.getpixel((x + 1, y + 1)),
            config.getpixel((x + 2, y + 1)), config.getpixel((x + 3, y + 1)),
            config.getpixel((x, y + 2)), config.getpixel((x + 1, y + 2)),
            config.getpixel((x + 2, y + 2)), config.getpixel((x + 3, y + 2)),
            config.getpixel((x, y + 3)), config.getpixel((x + 1, y + 3)),
            config.getpixel((x + 2, y + 3)), config.getpixel((x + 3, y + 3)),
        )
        for rgb in rgbs:
            if not Blueish(rgb):
                assert 0
        for j in range(4):
            for i in range(4):
                verified[x + i, y + j] = 1

    big = 0
    bigs = []
    small = 0
    smalls = []
    medium = 0
    mediums = []
    for y in range(config.size[1]):
        for x in range(config.size[0]):
            if verified[x, y] == 0:
                rgb = config.getpixel((x, y))
                if Blueish(rgb):
                    try:
                        VerifyLarge(x, y)
                        bigs.append((x, y))
                        big += 1
                    except Exception:
                        raise
                if Greenish(rgb):
                    try:
                        VerifyMedium(x, y)
                        mediums.append((x, y))
                        medium += 1
                    except Exception:
                        raise
                if Redish(rgb):
                    smalls.append((x, y))
                    small += 1
    cities =([CityProxy(waterLevel, c[0], c[1], 1, 1) for c in smalls]
              + [CityProxy(waterLevel, c[0], c[1], 2, 2) for c in mediums]
              + [CityProxy(waterLevel, c[0], c[1], 4, 4) for c in bigs])
    return cities


def BuildBestConfig(configSize):
    """Create a config.bmp packed with as many big cities as possible, then
    medium, then small.
    """
    im = Image.new("RGB", configSize, "#0000FF")
    nbBigX = configSize[0] // 4
    nbMediumX = 0
    nbSmallX = 0
    rX = configSize[0] % 4
    if rX == 1 or rX == 3:
        nbSmallX = 1
    if rX == 3 or rX == 2:
        nbMediumX = 1
    nbBigY = configSize[1] // 4
    nbSmallY = 0
    nbMediumY = 0
    rY = configSize[1] % 4
    if rY == 1 or rY == 3:
        nbSmallY = 1
    if rY == 3 or rY == 2:
        nbMediumY = 1
    im.paste("#00FF00", (nbBigX * 4, 0, configSize[0], configSize[1]))
    im.paste("#00FF00", (0, nbBigY * 4, configSize[0], configSize[1]))
    im.paste("#FF0000", (nbBigX * 4 + nbMediumX * 2, 0, configSize[0], configSize[1]))
    im.paste("#FF0000", (0, nbBigY * 4 + nbMediumY * 2, configSize[0], configSize[1]))
    return im


class SC4Region(object):
    """A SC4 region: cities, layout and height map."""

    def __init__(self, folder, waterLevel, dlg, config=None):
        self.waterLevel = waterLevel
        if config is not None:
            self.folder = None
            allCityFileNames = []
            self.config = config
        else:
            self.folder = folder
            allfiles = sorted(os.listdir(folder))
            allCityFileNames = [x for x in allfiles
                                if os.path.splitext(x)[1] == ".sc4"]
            try:
                self.config = Image.open(
                    encodeFilename(os.path.join(folder, "config.bmp")))
            except Exception:
                self.config = None
        self.allCities = []

        if self.config:
            self.config = self.config.convert('RGB')
            self.originalConfig = self.config.copy()
            self.allCities = WorkTheconfig(self.config, waterLevel)
        else:
            self.originalConfig = None

        for save in allCityFileNames:
            if dlg is not None:
                dlg.Update(1, "Please wait while loading the region"
                              "\nReading " + save)
            sc4 = SC4File(os.path.join(folder, save))
            sc4.ReadHeader()
            sc4.ReadEntries()
            for i, city in enumerate(self.allCities):
                if city.AtPos(sc4.cityXPos, sc4.cityYPos):
                    if (city.__class__ == CityProxy
                            and city.cityXPos == sc4.cityXPos
                            and city.cityYPos == sc4.cityYPos
                            and city.cityXSize == sc4.cityXSize
                            and city.cityYSize == sc4.cityYSize):
                        self.allCities = self.allCities[:i] + self.allCities[i + 1:]
                    else:
                        dlg1 = wx.MessageDialog(
                            None, 'It seems that the config.bmp does not match '
                            'the savegames present in the region folder',
                            'error', wx.OK | wx.ICON_ERROR)
                        dlg1.ShowModal()
                        dlg1.Destroy()
                        self.allCities = None
                        return
            self.allCities.append(sc4)
        self.config = self.BuildConfig()
        self.originalConfig = self.BuildConfig()
        if dlg is not None:
            dlg.Update(1, "Please wait while loading the region")

    def CropConfig(self):
        """Find the bbox of valid cities and return the resized config."""
        sizeX = sizeY = 0
        minX = minY = maxX = maxY = None
        for city in self.allCities:
            if minX is None or city.cityXPos < minX:
                minX = city.cityXPos
            if minY is None or city.cityYPos < minY:
                minY = city.cityYPos
            if maxX is None or city.cityXPos + city.cityXSize > maxX:
                maxX = city.cityXPos + city.cityXSize
            if maxY is None or city.cityYPos + city.cityYSize > maxY:
                maxY = city.cityYPos + city.cityYSize
        sizeX = maxX - minX
        sizeY = maxY - minY
        config = self.config.crop((minX, minY, maxX, maxY))
        return minX, minY, maxX, maxY, sizeX, sizeY, config

    def BuildConfig(self):
        """Build a config.bmp with slight colour changes; also fill the
        list of missing cities.
        """
        sizeX = sizeY = 0
        bigs = []
        smalls = []
        mediums = []
        for city in self.allCities:
            if city.cityXSize == 4:
                bigs.append((city.cityXPos, city.cityYPos))
            if city.cityXSize == 2:
                mediums.append((city.cityXPos, city.cityYPos))
            if city.cityXSize == 1:
                smalls.append((city.cityXPos, city.cityYPos))
            if city.cityXPos + city.cityXSize > sizeX:
                sizeX = city.cityXPos + city.cityXSize
            if city.cityYPos + city.cityYSize > sizeY:
                sizeY = city.cityYPos + city.cityYSize
        if self.originalConfig:
            sizeX = self.originalConfig.size[0]
            sizeY = self.originalConfig.size[1]
        config = Image.new("RGB", (sizeX, sizeY))
        draw = ImageDraw.Draw(config)
        for c in smalls:
            reds = ("#FF7777", "#FF0000")
            color = c[0] + c[1]
            draw.rectangle([c, (c[0], c[1])], fill=reds[color % 2])
        for c in mediums:
            colors = ("#00FF00", "#99FF00", "#00FF99", "#55FF55")
            color = c[0] + c[1]
            draw.rectangle([c, (c[0] + 1, c[1] + 1)], fill=colors[color % 4])
        for c in bigs:
            colors = ("#0000FF", "#4000FF", "#8000FF", "#C000FF",
                      "#0040FF", "#4040FF", "#8040FF", "#C040FF",
                      "#0080FF", "#4080FF", "#8080FF", "#C080FF",
                      "#00C0FF", "#40C0FF", "#80C0FF", "#C0C0FF")
            color = c[0] + c[1]
            draw.rectangle([c, (c[0] + 3, c[1] + 3)], fill=colors[color % 16])
        # Mark every tile covered by a city once, then collect the gaps.
        # This avoids an O(sizeX * sizeY * nCities) scan via GetCityUnder.
        occupied = set()
        for city in self.allCities:
            for cy in range(city.cityYPos, city.cityYPos + city.cityYSize):
                for cx in range(city.cityXPos, city.cityXPos + city.cityXSize):
                    occupied.add((cx, cy))
        self.missingCities = [(x, y)
                              for y in range(sizeY)
                              for x in range(sizeX)
                              if (x, y) not in occupied]
        return config

    def DeleteCityAt(self, pos):
        """Find the city at a certain x,y and remove it."""
        for i, city in enumerate(self.allCities):
            if (pos[0] >= city.cityXPos
                    and pos[0] < city.cityXPos + city.cityXSize
                    and pos[1] >= city.cityYPos
                    and pos[1] < city.cityYPos + city.cityYSize):
                self.allCities = self.allCities[:i] + self.allCities[i + 1:]
                break

    def GetCityUnder(self, pos):
        """Find the city at a certain x,y."""
        for city in self.allCities:
            if (pos[0] >= city.cityXPos
                    and pos[0] < city.cityXPos + city.cityXSize
                    and pos[1] >= city.cityYPos
                    and pos[1] < city.cityYPos + city.cityYSize):
                return city
        return None

    def GetCitiesUnder(self, pos, size):
        """Find all cities under a rectangle."""
        cities = []
        for city in self.allCities:
            def collide(x1, y1, w1, h1, x2, y2, w2, h2):
                return not (x1 >= x2 + w2 or x1 + w1 <= x2
                            or y1 >= y2 + h2 or y1 + h1 <= y2)
            if collide(pos[0], pos[1], size, size, city.cityXPos,
                       city.cityYPos, city.cityXSize, city.cityYSize):
                cities.append(city)
        return cities

    def IsValid(self):
        """The region is valid with at least one city or a valid config.bmp."""
        return len(self.allCities) > 0 or self.config is not None

    def Save(self, dlg, minX, minY, subRgn):
        """Save the region to SC4 files."""
        saved = True
        for i, city in enumerate(self.allCities):
            dlg.Update(i, "Please wait while saving the region\nSaving "
                       " City - New city(%03d-%03d).sc4" % (city.cityXPos,
                                                            city.cityYPos))
            citySave = CityProxy(self.waterLevel, city.cityXPos - minX,
                                 city.cityYPos - minY, city.cityXSize,
                                 city.cityYSize)
            citySave.heightMap = Numeric.zeros((citySave.ySize, citySave.xSize),
                                               Numeric.uint16)
            citySave.heightMap[::, ::] = self.height[
                citySave.yPos + subRgn[1]:citySave.yPos + subRgn[1] + citySave.ySize,
                citySave.xPos + subRgn[0]:citySave.xPos + subRgn[0] + citySave.xSize]
            citySave.heightMap = (citySave.heightMap.astype(Numeric.float32)
                                  / Numeric.asarray(10, Numeric.float32))
            lightDir = Normalize((1, -5, -1))
            rawRGB = tools3D.onePassColors(False, citySave.heightMap.shape,
                                           self.waterLevel, citySave.heightMap,
                                           GradientReader.paletteWater,
                                           GradientReader.paletteLand, lightDir)
            try:
                if not Save(citySave, self.folder, rawRGB, self.waterLevel):
                    saved = False
            except Exception:
                saved = False
            citySave.heightMap = None
        return saved

    def show(self, dlg, readFiles=False):
        """Compute size/shape and load the height map if readFiles is True."""
        imgSize = [0, 0]
        if self.config:
            imgSize[0] = self.config.size[0]
            imgSize[1] = self.config.size[1]
        for city in self.allCities:
            x = city.cityXPos + city.cityXSize
            y = city.cityYPos + city.cityYSize
            if imgSize[0] < x:
                imgSize[0] = x
            if imgSize[1] < y:
                imgSize[1] = y
        self.imgSize = [a * 64 + 1 for a in imgSize]
        self.shape = [self.imgSize[1], self.imgSize[0]]
        if readFiles is False:
            return
        dlg.Update(2, "Please wait while loading the region\nBuilding textures")
        self.height = Numeric.zeros(self.shape, Numeric.uint16)
        for city in self.allCities:
            if hasattr(city, "heightMapEntry"):
                self.height[city.yPos:city.yPos + city.ySize,
                            city.xPos:city.xPos + city.xSize] = Numeric.reshape(
                    (Numeric.frombuffer(city.heightMapEntry.content[2:],
                                        Numeric.float32)
                     * Numeric.array(10, Numeric.float32)).astype(Numeric.uint16),
                    (city.ySize, city.xSize))
                del city.heightMapEntry
            else:
                self.height[city.yPos:city.yPos + city.ySize,
                            city.xPos:city.xPos + city.xSize] = (
                    Numeric.ones((city.ySize, city.xSize), Numeric.uint16)
                    * Numeric.array(self.waterLevel - 50).astype(Numeric.uint16))
            city.height = None
        dlg.Update(2, "Please wait while loading the region\nBuilding textures")
        return


def LoadGradient(path=None):
    if path is None:
        path = settings.load().config_file
    GradientReader.Init(str(path))


LoadGradient()
