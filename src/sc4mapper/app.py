#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SC4Mapper - SimCity 4 region import/export tool (main wxPython application)."""

import os
import os.path
import struct
import sys
import uuid
import zlib

import numpy as Numeric
import wx
import wx.adv
import wx.lib.masked as masked
from PIL import Image, ImageDraw

from . import about_dialog
from . import dialogs
from . import geo
from . import gradient
from . import region
from . import settings as appsettings
from . import terrain
from . import zip_utils
from .region import Normalize
from .resources import asset_path
from .version import get_version

# Sanity check: make sure the (now pure-Python) terrain backend is the one we
# expect.  The original guarded against a stale compiled DLL; terrain is now a
# pure-Python module, so a mismatch means a broken or outdated install.
try:
    version = terrain.GetVersion()
    if version != "v1.0d":
        raise ValueError
except Exception:
    class ErrApp(wx.App):
        def OnInit(self):
            dlg = wx.MessageDialog(
                None, "The terrain backend module is missing or out of date.\n"
                "Please reinstall SC4Mapper.",
                'Error', wx.OK | wx.ICON_ERROR)
            dlg.ShowModal()
            dlg.Destroy()
            return False

    app = ErrApp(False)
    app.MainLoop()
    sys.exit()


MAPPER_VERSION = get_version()
SCROLL_RATE = 1


class CreateRgnFromFile(wx.Dialog):
    """Dialog for entering region settings (file, size, name, config.bmp)."""

    def __init__(self, parent, title, wildCard, bAllowScale=False,
                 default_dir=None, config_default_dir=None):
        self.wildCard = wildCard
        self.default_dir = default_dir or os.getcwd()
        self.config_default_dir = config_default_dir or self.default_dir
        wx.Dialog.__init__(self, parent, -1, "Create region from " + title,
                           pos=wx.DefaultPosition, size=wx.DefaultSize,
                           style=wx.DEFAULT_DIALOG_STYLE)
        labelFileName = wx.StaticText(self, -1, "Filename")
        self.fileName = wx.TextCtrl(self, -1, "", style=wx.TE_READONLY)
        browseFile = wx.Button(self, -1, "...", size=(20, -1))
        if bAllowScale:
            label = wx.StaticText(self, -1, "Scale factor:")
            self.imageFactor = wx.ComboBox(self, -1, "Default factor",
                                           style=wx.CB_DROPDOWN)
            scaleTable = ["100m", "250m", "500m", "Default factor", "1000m",
                          "1500m", "2000m", "import.dat", "2500m", "3000m",
                          "3500m", "4000m", "4500m", "5000m"]
            for s in scaleTable:
                self.imageFactor.Append(s)
        self.fromConfig = wx.RadioButton(self, -1, "Config.bmp",
                                         style=wx.RB_GROUP)
        self.configFileName = wx.TextCtrl(self, -1, "", style=wx.TE_READONLY)
        browseConfig = wx.Button(self, -1, "...", size=(20, -1))
        self.fromSize = wx.RadioButton(self, -1, "Specify size")
        self.sizeX = masked.NumCtrl(self, value=8, integerWidth=3,
                                    allowNegative=False, min=2)
        self.sizeY = masked.NumCtrl(self, value=8, integerWidth=3,
                                    allowNegative=False, min=2)
        sizer = wx.BoxSizer(wx.VERTICAL)
        box = wx.BoxSizer(wx.HORIZONTAL)
        box.Add(labelFileName, 0, wx.ALIGN_CENTRE | wx.ALL, 5)
        box.Add(self.fileName, 0, wx.EXPAND | wx.ALL, 5)
        box.Add(browseFile, 0, wx.ALIGN_CENTRE | wx.ALL, 5)
        sizer.Add(box, 0, wx.GROW | wx.ALL, 5)
        if bAllowScale:
            box = wx.BoxSizer(wx.HORIZONTAL)
            box.Add(label, 0, wx.ALIGN_CENTRE | wx.ALL, 5)
            box.Add(self.imageFactor, 0,
                    wx.EXPAND | wx.ALL, 5)
            sizer.Add(box, 0, wx.GROW | wx.ALL, 5)
        box = wx.BoxSizer(wx.HORIZONTAL)
        box.Add(self.fromConfig, 0, wx.ALIGN_CENTRE | wx.ALL, 5)
        box.Add(self.configFileName, 0,
                wx.EXPAND | wx.ALL, 5)
        box.Add(browseConfig, 0, wx.ALIGN_CENTRE | wx.ALL, 5)
        sizer.Add(box, 0, wx.GROW | wx.ALL, 5)
        box = wx.BoxSizer(wx.HORIZONTAL)
        box.Add(self.fromSize, 0, wx.ALIGN_CENTRE | wx.ALL, 5)
        box.Add(self.sizeX, 0, wx.ALIGN_CENTRE | wx.ALL, 5)
        box.Add(self.sizeY, 0, wx.ALIGN_CENTRE | wx.ALL, 5)
        sizer.Add(box, 0, wx.GROW | wx.ALL, 5)
        line = wx.StaticLine(self, -1, size=(20, -1), style=wx.LI_HORIZONTAL)
        sizer.Add(line, 0, wx.GROW | wx.ALL, 5)
        btnsizer = wx.StdDialogButtonSizer()
        self.btnOk = wx.Button(self, wx.ID_OK)
        self.btnOk.SetDefault()
        btnsizer.AddButton(self.btnOk)
        btn = wx.Button(self, wx.ID_CANCEL)
        btnsizer.AddButton(btn)
        btnsizer.Realize()
        sizer.Add(btnsizer, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.ALL, 5)
        self.SetSizer(sizer)
        sizer.Fit(self)
        self.Bind(wx.EVT_BUTTON, self.OnBrowseFile, browseFile)
        self.Bind(wx.EVT_BUTTON, self.OnBrowseConfig, browseConfig)
        self.Bind(wx.EVT_RADIOBUTTON, self.OnSelectSize, self.fromSize)
        self.Bind(wx.EVT_RADIOBUTTON, self.OnSelectConfig, self.fromConfig)
        self.sizeX.Enable(True)
        self.sizeY.Enable(True)
        self.configFileName.Enable(False)
        self.fromConfig.SetValue(False)
        self.fromSize.SetValue(True)
        self.btnOk.Enable(False)

    def GetImageFactor(self):
        """Return the factor for a standard terrain mod or a real value."""
        s = self.imageFactor.GetValue()
        scales = {"100m": 1.3725, "250m": 1.9608, "500m": 2.9412,
                  "Default factor": 3., "1000m": 4.9020, "1500m": 6.8627,
                  "2000m": 8.8235, "import.dat": 9.7832, "2500m": 10.7843,
                  "3000m": 12.7451, "3500m": 14.7059, "4000m": 16.6667,
                  "4500m": 18.6275, "5000m": 20.5882}
        if s in scales:
            return scales[s]
        try:
            return float(s)
        except ValueError:
            return 3.

    def OnSelectConfig(self, event):
        self.sizeX.Enable(False)
        self.sizeY.Enable(False)
        self.configFileName.Enable(True)

    def OnSelectSize(self, event):
        self.sizeX.Enable(True)
        self.sizeY.Enable(True)
        self.configFileName.Enable(False)

    def OnBrowseFile(self, event):
        dlg = wx.FileDialog(self, message="Choose a file",
                            defaultDir=self.default_dir, defaultFile="",
                            wildcard=self.wildCard, style=wx.FD_OPEN)
        if dlg.ShowModal() == wx.ID_OK:
            paths = dlg.GetPaths()
            dlg.Destroy()
            try:
                im = Image.open(paths[0])
            except Exception:
                dlg1 = wx.MessageDialog(self, "This is not a valid file",
                                        "Error", wx.OK | wx.ICON_ERROR)
                dlg1.ShowModal()
                dlg1.Destroy()
                return
            x = (im.size[0] - 1) // 64
            y = (im.size[1] - 1) // 64
            self.fileName.SetValue(paths[0])
            self.sizeX.SetValue(x)
            self.sizeY.SetValue(y)
            del im
            self.btnOk.Enable(True)
        dlg.Destroy()

    def OnBrowseConfig(self, event):
        dlg = wx.FileDialog(self, message="Choose a config.bmp",
                            defaultDir=self.config_default_dir, defaultFile="",
                            wildcard="config (*.bmp)|*config.bmp",
                            style=wx.FD_OPEN)
        if dlg.ShowModal() == wx.ID_OK:
            paths = dlg.GetPaths()
            dlg.Destroy()
            self.configFileName.SetValue(paths[0])
            try:
                im = Image.open(paths[0])
            except Exception:
                dlg1 = wx.MessageDialog(self, "This is not a valid config",
                                        "Error", wx.OK | wx.ICON_ERROR)
                dlg1.ShowModal()
                dlg1.Destroy()
                return
            x = im.size[0]
            y = im.size[1]
            self.sizeX.SetValue(x)
            self.sizeY.SetValue(y)
            del im
            self.fromConfig.SetValue(True)
            self.fromSize.SetValue(False)
            self.sizeX.Enable(False)
            self.sizeY.Enable(False)
            self.configFileName.Enable(True)
        dlg.Destroy()


class CreateRgnFromLocationDialog(wx.Dialog):
    """Pick a place on Earth and the shape of the region to cut out of it."""

    CITY_CHOICES = [("Large cities (4x4)", 4), ("Medium cities (2x2)", 2),
                    ("Small cities (1x1)", 1)]

    VERTICAL_CHOICES = [("Match the horizontal scale", "match"),
                        ("Keep true elevations", "true"),
                        ("Exaggerate by", "manual")]

    DATUM_CHOICES = [("Real sea level", "sea"),
                     ("Lowest ground in the area", "lowest"),
                     ("Elevation (m)", "manual")]

    WATER_CHOICES = [("From elevation only", "elevation"),
                     ("Add mapped water", "both"),
                     ("Mapped water only", "mask")]

    def __init__(self, parent, settings):
        wx.Dialog.__init__(self, parent, -1,
                           "Create region from a real-world location",
                           style=wx.DEFAULT_DIALOG_STYLE)
        self.settings = settings
        self.places = []

        find = wx.StaticBox(self, -1, "Where")
        findSizer = wx.StaticBoxSizer(find, wx.VERTICAL)
        self.search = wx.TextCtrl(self, -1, "", size=(340, -1),
                                  style=wx.TE_PROCESS_ENTER)
        self.btnSearch = wx.Button(self, -1, "Find")
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(self.search, 1, wx.EXPAND | wx.ALL, 3)
        row.Add(self.btnSearch, 0, wx.ALL, 3)
        findSizer.Add(row, 0, wx.EXPAND)
        findSizer.Add(wx.StaticText(
            self, -1, "Type a place name, or paste coordinates or an "
                      "OpenStreetMap / Google Maps link."),
            0, wx.LEFT | wx.BOTTOM, 5)
        self.results = wx.ListBox(self, -1, size=(340, 90))
        findSizer.Add(self.results, 0, wx.EXPAND | wx.ALL, 3)

        self.lat = wx.TextCtrl(self, -1, "52.3676", size=(100, -1))
        self.lon = wx.TextCtrl(self, -1, "4.9041", size=(100, -1))
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, -1, "Latitude"), 0,
                wx.ALIGN_CENTRE_VERTICAL | wx.ALL, 3)
        row.Add(self.lat, 0, wx.ALL, 3)
        row.Add(wx.StaticText(self, -1, "Longitude"), 0,
                wx.ALIGN_CENTRE_VERTICAL | wx.ALL, 3)
        row.Add(self.lon, 0, wx.ALL, 3)
        findSizer.Add(row, 0, wx.EXPAND)

        shape = wx.StaticBox(self, -1, "Region")
        shapeSizer = wx.StaticBoxSizer(shape, wx.VERTICAL)
        self.sizeX = masked.NumCtrl(self, value=8, integerWidth=3,
                                    allowNegative=False, min=1)
        self.sizeY = masked.NumCtrl(self, value=8, integerWidth=3,
                                    allowNegative=False, min=1)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, -1, "Size in small tiles"), 0,
                wx.ALIGN_CENTRE_VERTICAL | wx.ALL, 3)
        row.Add(self.sizeX, 0, wx.ALL, 3)
        row.Add(wx.StaticText(self, -1, "x"), 0,
                wx.ALIGN_CENTRE_VERTICAL | wx.ALL, 3)
        row.Add(self.sizeY, 0, wx.ALL, 3)
        shapeSizer.Add(row, 0, wx.EXPAND)

        self.metres = wx.TextCtrl(self, -1, "16", size=(60, -1))
        self.rotation = wx.TextCtrl(self, -1, "0", size=(60, -1))
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, -1, "Metres per cell"), 0,
                wx.ALIGN_CENTRE_VERTICAL | wx.ALL, 3)
        row.Add(self.metres, 0, wx.ALL, 3)
        row.Add(wx.StaticText(self, -1, "Rotation"), 0,
                wx.ALIGN_CENTRE_VERTICAL | wx.ALL, 3)
        row.Add(self.rotation, 0, wx.ALL, 3)
        row.Add(wx.StaticText(self, -1, "degrees"), 0,
                wx.ALIGN_CENTRE_VERTICAL | wx.ALL, 3)
        shapeSizer.Add(row, 0, wx.EXPAND)

        self.citySize = wx.Choice(
            self, -1, choices=[label for label, _ in self.CITY_CHOICES])
        self.citySize.SetSelection(0)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, -1, "Start layout with"), 0,
                wx.ALIGN_CENTRE_VERTICAL | wx.ALL, 3)
        row.Add(self.citySize, 0, wx.ALL, 3)
        shapeSizer.Add(row, 0, wx.EXPAND)
        shapeSizer.Add(wx.StaticText(
            self, -1, "You can repaint individual tiles, or erase them to "
                      "leave holes, after the import."),
            0, wx.LEFT | wx.BOTTOM, 5)

        self.footprint = wx.StaticText(self, -1, " ")
        shapeSizer.Add(self.footprint, 0, wx.ALL, 5)

        terrainBox = wx.StaticBox(self, -1, "Terrain")
        terrainSizer = wx.StaticBoxSizer(terrainBox, wx.VERTICAL)
        self.verticalMode = wx.Choice(
            self, -1, choices=[label for label, _ in self.VERTICAL_CHOICES])
        self.verticalMode.SetSelection(0)
        self.vertical = wx.TextCtrl(self, -1, "1.0", size=(60, -1))
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, -1, "Heights"), 0,
                wx.ALIGN_CENTRE_VERTICAL | wx.ALL, 3)
        row.Add(self.verticalMode, 0, wx.ALL, 3)
        row.Add(self.vertical, 0, wx.ALL, 3)
        terrainSizer.Add(row, 0, wx.EXPAND)
        self.verticalNote = wx.StaticText(self, -1, " ")
        terrainSizer.Add(self.verticalNote, 0, wx.LEFT | wx.BOTTOM, 5)
        self.datumMode = wx.Choice(
            self, -1, choices=[label for label, _ in self.DATUM_CHOICES])
        self.datumMode.SetSelection(0)
        self.datum = wx.TextCtrl(self, -1, "0", size=(70, -1))
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, -1, "Shoreline at"), 0,
                wx.ALIGN_CENTRE_VERTICAL | wx.ALL, 3)
        row.Add(self.datumMode, 0, wx.ALL, 3)
        row.Add(self.datum, 0, wx.ALL, 3)
        terrainSizer.Add(row, 0, wx.EXPAND)
        self.datumNote = wx.StaticText(self, -1, " ")
        terrainSizer.Add(self.datumNote, 0, wx.LEFT | wx.BOTTOM, 5)

        self.flatten = wx.CheckBox(
            self, -1, "Flood everything below the shoreline")
        self.flatten.SetValue(True)
        terrainSizer.Add(self.flatten, 0, wx.ALL, 3)

        waterBox = wx.StaticBox(self, -1, "Water from OpenStreetMap")
        waterSizer = wx.StaticBoxSizer(waterBox, wx.VERTICAL)
        self.waterSource = wx.Choice(
            self, -1, choices=[label for label, _ in self.WATER_CHOICES])
        self.waterSource.SetSelection(0)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, -1, "Lakes and rivers"), 0,
                wx.ALIGN_CENTRE_VERTICAL | wx.ALL, 3)
        row.Add(self.waterSource, 0, wx.ALL, 3)
        waterSizer.Add(row, 0, wx.EXPAND)

        self.minWaterArea = wx.TextCtrl(self, -1, "64", size=(60, -1))
        self.maxWaterRise = wx.TextCtrl(self, -1, "30", size=(60, -1))
        self.waterDepth = wx.TextCtrl(self, -1, "3", size=(60, -1))
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, -1, "Ignore smaller than"), 0,
                wx.ALIGN_CENTRE_VERTICAL | wx.ALL, 3)
        row.Add(self.minWaterArea, 0, wx.ALL, 3)
        row.Add(wx.StaticText(self, -1, "cells, or more than"), 0,
                wx.ALIGN_CENTRE_VERTICAL | wx.ALL, 3)
        row.Add(self.maxWaterRise, 0, wx.ALL, 3)
        row.Add(wx.StaticText(self, -1, "m above the shoreline"), 0,
                wx.ALIGN_CENTRE_VERTICAL | wx.ALL, 3)
        waterSizer.Add(row, 0, wx.EXPAND)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, -1, "Water depth"), 0,
                wx.ALIGN_CENTRE_VERTICAL | wx.ALL, 3)
        row.Add(self.waterDepth, 0, wx.ALL, 3)
        row.Add(wx.StaticText(self, -1, "m"), 0,
                wx.ALIGN_CENTRE_VERTICAL | wx.ALL, 3)
        waterSizer.Add(row, 0, wx.EXPAND)
        self.waterNote = wx.StaticText(self, -1, " ")
        waterSizer.Add(self.waterNote, 0, wx.LEFT | wx.BOTTOM, 5)
        self.waterSizer = waterSizer
        self.underlay = wx.CheckBox(
            self, -1, "Download a map underlay for the region view")
        hasBasemap = bool(getattr(settings, "basemap_url", ""))
        self.underlay.SetValue(hasBasemap)
        self.underlay.Enable(hasBasemap)
        terrainSizer.Add(self.underlay, 0, wx.ALL, 3)
        if not hasBasemap:
            terrainSizer.Add(wx.StaticText(
                self, -1, "Set basemap_url in SC4Mapper.ini to enable the "
                          "underlay."), 0, wx.LEFT | wx.BOTTOM, 5)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(findSizer, 0, wx.EXPAND | wx.ALL, 5)
        sizer.Add(shapeSizer, 0, wx.EXPAND | wx.ALL, 5)
        sizer.Add(terrainSizer, 0, wx.EXPAND | wx.ALL, 5)
        sizer.Add(waterSizer, 0, wx.EXPAND | wx.ALL, 5)
        sizer.Add(wx.StaticLine(self, -1, size=(20, -1),
                                style=wx.LI_HORIZONTAL), 0, wx.GROW | wx.ALL, 5)
        btnsizer = wx.StdDialogButtonSizer()
        self.btnOk = wx.Button(self, wx.ID_OK)
        self.btnOk.SetDefault()
        btnsizer.AddButton(self.btnOk)
        btnsizer.AddButton(wx.Button(self, wx.ID_CANCEL))
        btnsizer.Realize()
        sizer.Add(btnsizer, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.ALL, 5)
        self.SetSizer(sizer)
        sizer.Fit(self)

        self.Bind(wx.EVT_BUTTON, self.OnSearch, self.btnSearch)
        self.search.Bind(wx.EVT_TEXT_ENTER, self.OnSearch)
        self.results.Bind(wx.EVT_LISTBOX, self.OnPickPlace)
        for control in (self.sizeX, self.sizeY, self.metres):
            control.Bind(wx.EVT_TEXT, self.OnShapeChanged)
        self.citySize.Bind(wx.EVT_CHOICE, self.OnShapeChanged)
        self.verticalMode.Bind(wx.EVT_CHOICE, self.OnShapeChanged)
        self.datumMode.Bind(wx.EVT_CHOICE, self.OnShapeChanged)
        self.waterSource.Bind(wx.EVT_CHOICE, self.OnShapeChanged)
        self.OnShapeChanged(None)

    # -- helpers ----------------------------------------------------------

    def _float(self, control, label, default=None):
        text = control.GetValue().strip().replace(",", ".")
        if not text and default is not None:
            return default
        try:
            return float(text)
        except ValueError:
            raise ValueError("%s must be a number" % label)

    def OnShapeChanged(self, event):
        """Keep the footprint readout in step with the inputs."""
        try:
            tiles_x = int(self.sizeX.GetValue())
            tiles_y = int(self.sizeY.GetValue())
            metres = self._float(self.metres, "Metres per cell")
        except (ValueError, TypeError):
            self.footprint.SetLabel(" ")
            return
        if tiles_x < 1 or tiles_y < 1 or metres <= 0:
            self.footprint.SetLabel(" ")
            return
        width = tiles_x * geo.CELLS_PER_TILE * metres / 1000.0
        height = tiles_y * geo.CELLS_PER_TILE * metres / 1000.0
        try:
            counts = geo.describe_layout((tiles_x, tiles_y), self.GetCitySize())
        except geo.GeoImportError:
            counts = {}
        parts = ["%d %s" % (counts[size], geo.CITY_SIZE_NAMES[size].lower())
                 for size in (4, 2, 1) if counts.get(size)]
        self.footprint.SetLabel(
            "Covers %.1f x %.1f km of real ground - %s"
            % (width, height, ", ".join(parts) if parts else "no cities"))
        self.UpdateVerticalNote(metres)
        self.UpdateDatumNote()
        self.UpdateWaterNote()

    def UpdateWaterNote(self):
        """Explain the OpenStreetMap water options."""
        mode = self.GetWaterSource()
        for control in (self.minWaterArea, self.maxWaterRise, self.waterDepth):
            control.Enable(mode != "elevation")
        notes = {
            "elevation": "Water is decided by the shoreline alone. No lookup "
                         "is made.",
            "both": "Lakes and rivers from OpenStreetMap are added to what "
                    "the shoreline already floods. Best for the coast.",
            "mask": "Only mapped water is wet; everything else is raised to "
                    "dry land however low it sits. Use for polders.",
        }
        self.waterNote.SetLabel(notes[mode])

    def GetWaterSource(self):
        index = max(0, self.waterSource.GetSelection())
        return self.WATER_CHOICES[index][1]

    def UpdateDatumNote(self):
        """Explain which real elevation becomes SimCity 4's shoreline."""
        mode = self.GetDatumMode()
        self.datum.Enable(mode == "manual")
        notes = {
            "sea": "Real sea level becomes SimCity 4's shoreline. Right for "
                   "the coast; inland, it leaves the whole area on high ground.",
            "lowest": "The shoreline is dropped below the lowest ground here, "
                      "so nothing floods. Use for land below sea level, such "
                      "as polders.",
            "manual": "The elevation entered becomes the shoreline. For a "
                      "mountain lake, use its surface height.",
        }
        self.datumNote.SetLabel(notes[mode])

    def GetDatumMode(self):
        index = max(0, self.datumMode.GetSelection())
        return self.DATUM_CHOICES[index][1]

    def UpdateVerticalNote(self, metres_per_cell):
        """Explain what the chosen height mode will do at this scale."""
        mode = self.GetVerticalMode()
        self.vertical.Enable(mode == "manual")
        if mode == "manual":
            self.verticalNote.SetLabel(
                "Heights are multiplied by the factor above.")
            return
        if mode == "true":
            note = ("Real metres, kept as they are. A cell is always 16 m "
                    "in game, so at %.0f m per cell slopes come out %.1fx "
                    "steeper than life."
                    % (metres_per_cell, metres_per_cell / geo.CELL_SIZE_M))
        else:
            scale = geo.isotropic_vertical_scale(metres_per_cell)
            note = ("Heights scaled %.2fx so hills keep their real profile."
                    % scale)
        self.verticalNote.SetLabel(note)

    def GetVerticalMode(self):
        index = max(0, self.verticalMode.GetSelection())
        return self.VERTICAL_CHOICES[index][1]

    def GetCitySize(self):
        index = max(0, self.citySize.GetSelection())
        return self.CITY_CHOICES[index][1]

    def OnSearch(self, event):
        text = self.search.GetValue().strip()
        if not text:
            return
        # A pasted coordinate or link needs no network round trip.
        located = geo.parse_location(text)
        if located:
            self.SetLatLon(*located)
            self.results.Clear()
            self.places = []
            return
        wx.BeginBusyCursor()
        try:
            self.places = geo.geocode(text)
        except geo.GeoImportError as exc:
            wx.EndBusyCursor()
            wx.MessageBox(str(exc), "Search failed", wx.OK | wx.ICON_ERROR, self)
            return
        wx.EndBusyCursor()
        self.results.Clear()
        if not self.places:
            wx.MessageBox("Nothing found for %r." % text, "No results",
                          wx.OK | wx.ICON_INFORMATION, self)
            return
        for place in self.places:
            self.results.Append(place.name)
        self.results.SetSelection(0)
        self.SetLatLon(self.places[0].lat, self.places[0].lon)

    def OnPickPlace(self, event):
        index = self.results.GetSelection()
        if 0 <= index < len(self.places):
            place = self.places[index]
            self.SetLatLon(place.lat, place.lon)

    def SetLatLon(self, lat, lon):
        self.lat.SetValue("%.6f" % lat)
        self.lon.SetValue("%.6f" % lon)

    def GetRequest(self):
        """Build the import request, raising ValueError on bad input."""
        lat = self._float(self.lat, "Latitude")
        lon = self._float(self.lon, "Longitude")
        metres = self._float(self.metres, "Metres per cell")
        rotation = self._float(self.rotation, "Rotation", 0.0)
        vertical = self._float(self.vertical, "Vertical exaggeration", 1.0)
        datum = self._float(self.datum, "Shoreline elevation", 0.0)
        minArea = self._float(self.minWaterArea, "Minimum water size", 64.0)
        maxRise = self._float(self.maxWaterRise, "Maximum water rise", 30.0)
        depth = self._float(self.waterDepth, "Water depth", 3.0)
        request = geo.GeoImportRequest(
            center_lat=lat,
            center_lon=lon,
            tiles_x=int(self.sizeX.GetValue()),
            tiles_y=int(self.sizeY.GetValue()),
            metres_per_cell=metres,
            rotation_deg=rotation,
            vertical_mode=self.GetVerticalMode(),
            vertical_scale=vertical,
            water_datum_mode=self.GetDatumMode(),
            sea_reference_m=datum,
            water_source=self.GetWaterSource(),
            min_water_area_cells=int(max(0.0, minArea)),
            max_water_rise_m=max(0.0, maxRise),
            water_depth_m=max(0.1, depth),
            keep_bathymetry=not self.flatten.GetValue(),
        )
        try:
            request.validate()
        except geo.GeoImportError as exc:
            raise ValueError(str(exc))
        return request

    def WantsUnderlay(self):
        return self.underlay.IsEnabled() and self.underlay.GetValue()


class PreferencesDialog(wx.Dialog):
    """Edit default folders and the visible colour-gradient INI."""

    def __init__(self, parent, settings):
        wx.Dialog.__init__(self, parent, -1, "Options",
                           style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.settings = settings

        self.importDir = wx.DirPickerCtrl(self, path=settings.import_dir)
        self.regionDir = wx.DirPickerCtrl(self, path=settings.region_dir)
        self.exportDir = wx.DirPickerCtrl(self, path=settings.export_dir)
        self.imageSaveDir = wx.DirPickerCtrl(self,
                                             path=settings.image_save_dir)
        grid = wx.FlexGridSizer(cols=2, vgap=8, hgap=8)
        grid.AddGrowableCol(1, 1)
        fields = [
            ("Open/import files", self.importDir),
            ("Save regions", self.regionDir),
            ("Export regions", self.exportDir),
            ("Save images", self.imageSaveDir),
        ]
        for label, control in fields:
            grid.Add(wx.StaticText(self, label=label),
                     0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(control, 1, wx.EXPAND)

        buttons = wx.StdDialogButtonSizer()
        ok = wx.Button(self, wx.ID_OK)
        ok.SetDefault()
        buttons.AddButton(ok)
        buttons.AddButton(wx.Button(self, wx.ID_CANCEL))
        buttons.Realize()

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(grid, 1, wx.EXPAND | wx.ALL, 12)
        sizer.Add(buttons, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        self.SetSizerAndFit(sizer)
        self.SetMinSize((560, self.GetSize().height))

    def Apply(self):
        self.settings.import_dir = self.importDir.GetPath()
        self.settings.region_dir = self.regionDir.GetPath()
        self.settings.export_dir = self.exportDir.GetPath()
        self.settings.image_save_dir = self.imageSaveDir.GetPath()
        self.settings.save()


class OverViewCanvas(wx.ScrolledWindow):
    def __init__(self, parent, id=-1, size=wx.DefaultSize):
        wx.ScrolledWindow.__init__(self, parent, id, (0, 0), size=size,
                                   style=wx.SUNKEN_BORDER
                                   | wx.FULL_REPAINT_ON_RESIZE)
        self.parent = parent
        self.Bind(wx.EVT_PAINT, self.OnPaint)
        self.Bind(wx.EVT_SIZE, self.OnSize)
        self.Bind(wx.EVT_SCROLLWIN, self.OnScroll)
        self.Bind(wx.EVT_MOUSEWHEEL, self.OnMouseWheel)
        self.Bind(wx.EVT_ERASE_BACKGROUND, self.OnEraseBackground)
        self.Bind(wx.EVT_CHAR, self.OnKeyDown)
        self.bmp = None
        self.drag = False
        self.buffer = None
        self.wait = False
        self.crop = None
        self.offX = 0
        self.offY = 0
        # Cached, fully-coloured terrain bitmap.  The terrain colours depend
        # only on the height map / zoom, so we colour the whole region once
        # and let scrolling just blit a sub-rectangle out of it.
        self._terrainBmp = None
        self._terrainZoom = None
        self._terrainRegion = None
        self._terrainShowMap = False
        self.OnSize(None)

    def _EnsureTerrainCache(self, zoom):
        """Colour the whole region once per zoom level.  Subsequent scrolls
        only have to blit from this bitmap instead of recolouring each frame.
        """
        region = self.parent.region
        showMap = self.parent.ShowBasemap()
        if (self._terrainBmp is not None and self._terrainZoom == zoom
                and self._terrainRegion is region
                and self._terrainShowMap == showMap):
            return
        lightDir = Normalize((1, -5, -1))
        heightMap = region.height[::zoom, ::zoom].astype(Numeric.float32)
        heightMap /= Numeric.float32(10)
        rawRGB = terrain.onePassColors(
            False, heightMap.shape, region.waterLevel, heightMap,
            gradient.paletteWater,
            gradient.paletteLand, lightDir)
        if showMap:
            rawRGB = self._BlendBasemap(rawRGB, region, zoom, heightMap.shape)
        img = wx.Image(heightMap.shape[1], heightMap.shape[0])
        img.SetData(rawRGB)
        self._terrainBmp = wx.Bitmap(img)
        self._terrainZoom = zoom
        self._terrainRegion = region
        self._terrainShowMap = showMap

    def _BlendBasemap(self, rawRGB, region, zoom, shape):
        """Mix the downloaded map into the terrain colours.

        The basemap was sampled onto the same grid as the height map, so
        decimating it by the same zoom step keeps it registered: a pixel of
        the map is the patch of ground under that terrain vertex, and the
        city rectangles drawn on top land exactly where those cities will.
        """
        basemap = getattr(region, "basemap", None)
        if basemap is None:
            return rawRGB
        basemap = basemap[::zoom, ::zoom]
        if basemap.shape[:2] != tuple(shape):
            return rawRGB
        colours = Numeric.frombuffer(rawRGB, dtype=Numeric.uint8)
        colours = colours.reshape(shape[0], shape[1], 3).astype(Numeric.float32)
        alpha = float(getattr(self.parent.settings, "basemap_opacity", 0.55))
        alpha = min(1.0, max(0.0, alpha))
        blended = (basemap.astype(Numeric.float32) * alpha
                   + colours * (1.0 - alpha))
        return Numeric.clip(blended, 0, 255).astype(Numeric.uint8).tobytes()

    def OnKeyDown(self, event):
        if (self.parent.btnEditMode.GetValue()
                and self.parent.editMode == EDITMODE_NONE):
            if self.wait is True:
                return
            if event.GetModifiers() != wx.MOD_CONTROL:
                return
            if event.GetKeyCode() == wx.WXK_LEFT:
                for _ in range(self.parent.zoomLevel):
                    offX = self.offX - 1
                    deletes = []
                    for city in self.parent.region.allCities:
                        if city.xPos + offX < 0:
                            deletes.append((city.cityXPos, city.cityYPos))
                    if len(deletes) == 0:
                        self.offX = offX
                self.UpdateDrawing()
                self.wait = True
                self.Refresh(False)
            if event.GetKeyCode() == wx.WXK_RIGHT:
                for _ in range(self.parent.zoomLevel):
                    offX = self.offX + 1
                    deletes = []
                    for city in self.parent.region.allCities:
                        if (city.xPos + city.xSize + offX
                                > self.parent.region.imgSize[0]):
                            deletes.append((city.cityXPos, city.cityYPos))
                    if len(deletes) == 0:
                        self.offX = offX
                self.UpdateDrawing()
                self.wait = True
                self.Refresh(False)
            if event.GetKeyCode() == wx.WXK_UP:
                for _ in range(self.parent.zoomLevel):
                    offY = self.offY - 1
                    deletes = []
                    for city in self.parent.region.allCities:
                        if city.yPos + offY < 0:
                            deletes.append((city.cityXPos, city.cityYPos))
                    if len(deletes) == 0:
                        self.offY = offY
                self.UpdateDrawing()
                self.wait = True
                self.Refresh(False)
            if event.GetKeyCode() == wx.WXK_DOWN:
                for _ in range(self.parent.zoomLevel):
                    offY = self.offY + 1
                    deletes = []
                    for city in self.parent.region.allCities:
                        if (city.yPos + city.ySize + offY
                                > self.parent.region.imgSize[1]):
                            deletes.append((city.cityXPos, city.cityYPos))
                    if len(deletes) == 0:
                        self.offY = offY
                self.UpdateDrawing()
                self.wait = True
                self.Refresh(False)

    def OnEraseBackground(self, event):
        pass

    def OnSize(self, event):
        size = self.ClientSize
        if event:
            size = event.GetSize()
        if self.parent.region:
            if (self.buffer is None or self.buffer.GetWidth() != size[0]
                    or self.buffer.GetHeight() != size[1]):
                if size[0] > 0 and size[1] > 0:
                    self.buffer = wx.Bitmap(size[0], size[1])
            self.UpdateDrawing(newSize=size)
        else:
            self.buffer = None
        if event:
            event.Skip()

    def OnScroll(self, event):
        size = self.ClientSize
        x, y = self.GetViewStart()
        if self.parent.region:
            if (self.buffer is None or self.buffer.GetWidth() != size[0]
                    or self.buffer.GetHeight() != size[1]):
                if size[0] > 0 and size[1] > 0:
                    self.buffer = wx.Bitmap(size[0], size[1])

            if event.GetOrientation() == wx.HORIZONTAL:
                pos = (event.GetPosition(), y)
            else:
                pos = (x, event.GetPosition())
            wx.CallAfter(self.UpdateDrawing, pos)
        else:
            self.buffer = None
        event.Skip()

    def OnMouseWheel(self, event):
        # ScrolledWindow's default wheel handling physically scrolls the
        # window but never fires EVT_SCROLLWIN, so our buffer is never
        # redrawn for the new position.  Handle the wheel ourselves instead.
        if not self.parent.region:
            return
        rotation = event.GetWheelRotation()
        if rotation == 0:
            return
        delta = event.GetWheelDelta() or 120
        step = int(-(rotation / delta) * 60)   # ~60 px per wheel notch
        x, y = self.GetViewStart()
        if event.ShiftDown():
            self.Scroll(x + step, y)
        else:
            self.Scroll(x, y + step)
        self.UpdateDrawing(self.GetViewStart())

    def UpdateDrawing(self, pos=None, newSize=None, finish=True):
        size = self.ClientSize
        if newSize:
            size = newSize

        zoom = self.parent.zoomLevel
        self._EnsureTerrainCache(zoom)
        cacheW = self._terrainBmp.GetWidth()
        cacheH = self._terrainBmp.GetHeight()
        sizeDest = (min(size[0], cacheW), min(size[1], cacheH))
        if pos:
            x, y = pos
        else:
            x, y = self.GetViewStart()
        x *= SCROLL_RATE
        y *= SCROLL_RATE
        x *= zoom
        y *= zoom
        # Clamp the source rectangle so the blit always stays inside the cache.
        srcX = max(0, min(x // zoom, cacheW - sizeDest[0]))
        srcY = max(0, min(y // zoom, cacheH - sizeDest[1]))

        dc = wx.BufferedDC(None, self.buffer)
        dc.SetBackground(wx.Brush("Light Gray"))
        dc.Clear()
        memDC = wx.MemoryDC(self._terrainBmp)
        dc.Blit(0, 0, sizeDest[0], sizeDest[1], memDC, srcX, srcY)
        memDC.SelectObject(wx.NullBitmap)

        dc.SetPen(wx.TRANSPARENT_PEN)
        dc.SetBrush(wx.Brush("Light Gray"))
        dc.SetLogicalFunction(wx.OR)
        dc.DrawRectangle(0 - x // zoom, 0 - y // zoom, self.offX // zoom,
                         self.parent.region.imgSize[1] // zoom)
        dc.DrawRectangle(0 - x // zoom, 0 - y // zoom,
                         self.parent.region.imgSize[0] // zoom,
                         self.offY // zoom)
        dc.DrawRectangle(
            (self.parent.region.imgSize[0] + self.offX) // zoom - x // zoom,
            0 - y // zoom, -self.offX // zoom,
            self.parent.region.imgSize[1] // zoom)
        dc.DrawRectangle(
            0 - x // zoom,
            (self.parent.region.imgSize[1] + self.offY) // zoom - y // zoom,
            self.parent.region.imgSize[0] // zoom, -self.offY // zoom)
        dc.SetLogicalFunction(wx.COPY)

        if self.parent.overlayCbx.GetValue():
            self.AddMasked(dc, zoom, self.parent.region, x // zoom, y // zoom)
            self.AddGrid(dc, zoom, self.parent.region, x // zoom, y // zoom)
            self.AddOverlay(dc, zoom, self.parent.region, x // zoom, y // zoom)
        if self.crop is not None:
            dc.SetPen(wx.TRANSPARENT_PEN)
            dc.SetBrush(wx.Brush("Light Gray"))
            dc.SetLogicalFunction(wx.XOR)
            crop = [min(self.crop[0], self.crop[2]),
                    min(self.crop[1], self.crop[3]),
                    max(self.crop[0], self.crop[2]),
                    max(self.crop[1], self.crop[3])]
            self.DrawRectangle(dc, (crop[0] * 64 - x) // zoom,
                               (crop[1] * 64 - y) // zoom,
                               ((crop[2] - crop[0]) * 64 + 65) // zoom,
                               ((crop[3] - crop[1]) * 64 + 65) // zoom)
            dc.SetLogicalFunction(wx.COPY)
        self.wait = True
        wx.CallAfter(self.Refresh, False)
        if finish is False:
            return dc

    def AddGrid(self, dc, zoomLevel, region, xO, yO):
        lines = []
        s = (region.height.shape[1], region.height.shape[0])
        for y in range(s[1] // 64):
            lines.append([0 - xO, y * (64 // zoomLevel) - yO,
                          region.originalConfig.size[0] * (64 // zoomLevel) - xO,
                          y * (64 // zoomLevel) - yO])
        for x in range(s[0] // 64):
            lines.append([x * (64 // zoomLevel) - xO, 0 - yO,
                          x * (64 // zoomLevel) - xO,
                          region.originalConfig.size[1] * (64 // zoomLevel) - yO])
        dc.SetPen(wx.Pen("Light Gray"))
        dc.DrawLineList([(x1 + self.offX // zoomLevel, y1 + self.offY // zoomLevel,
                          x2 + self.offX // zoomLevel, y2 + self.offY // zoomLevel)
                         for x1, y1, x2, y2 in lines])

    def AddOverlay(self, dc, zoomLevel, region, xO, yO):
        dc.SetPen(wx.Pen("WHITE"))
        dc.SetBrush(wx.Brush("WHITE", wx.TRANSPARENT))
        colours = [0, wx.Colour(255, 0, 0), wx.Colour(0, 255, 0), 0,
                   wx.Colour(0, 0, 255)]
        sizes = [0, 64, 128, 0, 256]
        for city in region.allCities:
            x = int(city.xPos // zoomLevel)
            y = int(city.yPos // zoomLevel)
            width = sizes[city.cityXSize] // zoomLevel
            height = sizes[city.cityYSize] // zoomLevel
            dc.SetPen(wx.Pen("WHITE"))
            dc.SetBrush(wx.Brush("WHITE", wx.TRANSPARENT))
            dc.SetPen(wx.Pen(colours[city.cityXSize]))
            dc.SetBrush(wx.Brush(colours[city.cityXSize], wx.TRANSPARENT))
            self.DrawRectangle(dc, x - xO, y - yO, width, height)
            self.DrawRectangle(dc, x - xO + 1, y - yO + 1, width - 2, height - 2)

    def AddMasked(self, dc, zoomLevel, region, xO, yO):
        dc.SetPen(wx.Pen("LIGHT GRAY"))
        dc.SetBrush(wx.Brush("LIGHT GRAY", wx.CROSSDIAG_HATCH))
        width = 64 // zoomLevel
        height = 64 // zoomLevel
        for x, y in region.missingCities:
            x = int(x * 64 // zoomLevel)
            y = int(y * 64 // zoomLevel)
            self.DrawRectangle(dc, x - xO, y - yO, width, height)

    def HighlightCity(self, zoomLevel, region, pos):
        dc = self.UpdateDrawing(finish=False)
        xO, yO = self.GetViewStart()
        colours = [0, wx.Colour(255, 0, 0), wx.Colour(0, 255, 0), 0,
                   wx.Colour(0, 0, 255)]
        for city in region.allCities:
            if (pos[0] >= city.cityXPos
                    and pos[0] < city.cityXPos + city.cityXSize
                    and pos[1] >= city.cityYPos
                    and pos[1] < city.cityYPos + city.cityYSize):
                x = int(city.xPos // zoomLevel)
                y = int(city.yPos // zoomLevel)
                width = int(city.xSize // zoomLevel)
                height = int(city.ySize // zoomLevel)
                dc.SetPen(wx.Pen(colours[city.cityXSize]))
                dc.SetBrush(wx.Brush(colours[city.cityXSize],
                                     wx.CROSSDIAG_HATCH))
                self.DrawRectangle(dc, x + 1 - xO, y + 1 - yO,
                                   width - 2, height - 2)
                self.DrawRectangle(dc, x - xO, y - yO, width, height)
                self.DrawRectangle(dc, x - xO - 1, y - 1 - yO,
                                   width + 2, height + 2)
                break

    def HighlightNewCity(self, zoomLevel, region, pos, size):
        dc = self.UpdateDrawing(finish=False)
        xO, yO = self.GetViewStart()
        colours = [0, wx.Colour(255, 0, 0), wx.Colour(0, 255, 0), 0,
                   wx.Colour(0, 0, 255)]
        x = int(pos[0] * 64 // zoomLevel)
        y = int(pos[1] * 64 // zoomLevel)
        width = size * 64 // zoomLevel
        height = size * 64 // zoomLevel
        dc.SetPen(wx.Pen(colours[size]))
        dc.SetBrush(wx.Brush(colours[size], wx.TRANSPARENT))
        self.DrawRectangle(dc, x + 1 - xO, y + 1 - yO, width - 2, height - 2)
        self.DrawRectangle(dc, x - xO, y - yO, width, height)
        self.DrawRectangle(dc, x - 1 - xO, y - 1 - yO, width + 2, height + 2)

    def DrawRectangle(self, dc, x, y, width, height):
        dc.DrawRectangle(x + self.offX // self.parent.zoomLevel,
                         y + self.offY // self.parent.zoomLevel, width, height)

    def OnPaint(self, event):
        if self.buffer is None:
            self.clear = False
            self.wait = False
            dc = wx.PaintDC(self)
            self.DoPrepareDC(dc)
            dc.SetBackground(wx.Brush(self.GetBackgroundColour()))
            dc.Clear()
        if self.buffer is not None:
            self.wait = False
            dc = wx.BufferedPaintDC(self, self.buffer, wx.BUFFER_CLIENT_AREA)


EDITMODE_NONE = 0
EDITMODE_SMALL = 1
EDITMODE_MEDIUM = 2
EDITMODE_BIG = 3
EDITMODE_VOID = 4


class OverView(wx.Frame):
    def __init__(self, parent, title, virtualSize, pos=wx.DefaultPosition,
                 size=wx.DefaultSize,
                 style=wx.DEFAULT_FRAME_STYLE | wx.MINIMIZE_BOX
                 | wx.MAXIMIZE_BOX):
        wx.Frame.__init__(self, parent, -1, title, pos, size, style)
        self.region = None
        self.SetSizeHints(wx.Size(700, 400), wx.DefaultSize)
        self.SetBackgroundColour(
            wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW))
        self.Bind(wx.EVT_CLOSE, self.OnCloseWindow)
        self.editMode = EDITMODE_NONE
        self.btnSmall = wx.ToggleButton(self, -1, "Small\nCity")
        self.Bind(wx.EVT_TOGGLEBUTTON, self.SetEditModeSmall, self.btnSmall)
        self.btnMedium = wx.ToggleButton(self, -1, "Medium\nCity")
        self.Bind(wx.EVT_TOGGLEBUTTON, self.SetEditModeMedium, self.btnMedium)
        self.btnBig = wx.ToggleButton(self, -1, "Big\nCity")
        self.Bind(wx.EVT_TOGGLEBUTTON, self.SetEditModeBig, self.btnBig)
        self.btnVoid = wx.ToggleButton(self, -1, "Erase\nCity")
        self.Bind(wx.EVT_TOGGLEBUTTON, self.SetEditModeVoid, self.btnVoid)
        self.btnRevert = wx.Button(self, -1, "Revert\nConfig")
        self.Bind(wx.EVT_BUTTON, self.RevertConfig, self.btnRevert)

        self.btnSave = wx.Button(self, -1, "Save\nImage")
        self.Bind(wx.EVT_BUTTON, self.SaveBmp, self.btnSave)
        self.btnLoadRgn = wx.Button(self, -1, "Load\nRegion")
        self.Bind(wx.EVT_BUTTON, self.OpenRgn, self.btnLoadRgn)
        self.btnCreateRgn = wx.Button(self, -1, "Create\nRegion")
        self.Bind(wx.EVT_BUTTON, self.CreateRgn, self.btnCreateRgn)
        self.btnSaveRgn = wx.Button(self, -1, "Save\nRegion")
        self.Bind(wx.EVT_BUTTON, self.SaveRgn, self.btnSaveRgn)
        self.btnExportRgn = wx.Button(self, -1, "Export\nRegion")
        self.Bind(wx.EVT_BUTTON, self.ExportRgn, self.btnExportRgn)
        self.btnOptions = wx.Button(self, -1, "Options")
        self.Bind(wx.EVT_BUTTON, self.OnOptions, self.btnOptions)
        self.btnQuit = wx.Button(self, -1, "Quit")
        self.Bind(wx.EVT_BUTTON, self.OnCloseWindow, self.btnQuit)

        self.btnZoomIn = wx.Button(self, -1, "+", wx.DefaultPosition,
                                   wx.Size(24, -1))
        self.Bind(wx.EVT_BUTTON, self.OnZoomIn, self.btnZoomIn)
        self.btnZoomOut = wx.Button(self, -1, "-", wx.DefaultPosition,
                                    wx.Size(24, -1))
        self.Bind(wx.EVT_BUTTON, self.OnZoomOut, self.btnZoomOut)

        self.overlayCbx = wx.CheckBox(self, wx.ID_ANY, u"Cities\noverlay")
        self.overlayCbx.Bind(wx.EVT_CHECKBOX, self.OnOverlay)
        self.overlayCbx.SetValue(True)

        self.mapCbx = wx.CheckBox(self, wx.ID_ANY, u"Map\nunderlay")
        self.mapCbx.Bind(wx.EVT_CHECKBOX, self.OnBasemap)
        self.mapCbx.SetValue(True)
        self.mapCbx.Enable(False)

        self.btnEditMode = wx.ToggleButton(self, wx.ID_ANY, "Edit\nConfig.bmp")
        self.Bind(wx.EVT_TOGGLEBUTTON, self.OnToggleEditMode, self.btnEditMode)

        self.back = OverViewCanvas(self, -1, size=size)
        self.back.SetBackgroundColour("WHITE")
        self.back.SetVirtualSize(virtualSize)
        self.back.SetScrollRate(SCROLL_RATE, SCROLL_RATE)
        self.back.Bind(wx.EVT_MOTION, self.OnMouseMove)
        self.back.Bind(wx.EVT_LEFT_UP, self.OnLeftUp)
        self.back.Bind(wx.EVT_LEFT_DOWN, self.OnLeftDown)

        self.box = wx.BoxSizer(wx.VERTICAL)
        boxh = wx.BoxSizer(wx.HORIZONTAL)

        boxh.Add(self.btnSmall, 0)
        self.btnSmall.Hide()
        boxh.Add(self.btnMedium, 0)
        self.btnMedium.Hide()
        boxh.Add(self.btnBig, 0)
        self.btnBig.Hide()
        boxh.Add(self.btnVoid, 0)
        self.btnVoid.Hide()
        boxh.Add(self.btnRevert, 0)
        self.btnRevert.Hide()

        boxh.Add(self.btnLoadRgn, 0)
        boxh.Add(self.btnCreateRgn, 0)
        boxh.Add(self.btnSaveRgn, 0)
        boxh.Add(self.btnExportRgn, 0)
        boxh.Add(self.btnSave, 0)
        boxh.Add(wx.StaticLine(self, wx.ID_ANY, wx.DefaultPosition,
                               wx.DefaultSize, wx.LI_VERTICAL), 0,
                 wx.EXPAND | wx.RIGHT | wx.LEFT, 5)
        boxh.Add(self.btnEditMode, 0)
        boxh.Add(self.btnZoomIn, 0, wx.ALIGN_CENTER_VERTICAL)
        boxh.Add(self.btnZoomOut, 0, wx.ALIGN_CENTER_VERTICAL)
        boxh.Add(self.overlayCbx, 0, wx.ALIGN_CENTER_VERTICAL)
        boxh.Add(self.mapCbx, 0, wx.ALIGN_CENTER_VERTICAL)
        boxh.Add(wx.StaticLine(self, wx.ID_ANY, wx.DefaultPosition,
                               wx.DefaultSize, wx.LI_VERTICAL), 0,
                 wx.EXPAND | wx.RIGHT | wx.LEFT, 5)
        boxh.Add(self.btnOptions, 0, wx.EXPAND)

        boxh.AddStretchSpacer()

        boxh.Add(self.btnQuit, 0, wx.EXPAND)
        self.box.Add(boxh, 0, wx.EXPAND)
        self.box.Add(wx.StaticLine(self), 0, wx.EXPAND)
        self.box.Add(self.back, 1, wx.EXPAND)
        self.box.Fit(self)
        self.SetSizer(self.box)

        self.SetClientSize((800, 600))

        default_regions = wx.StandardPaths.Get().GetDocumentsDir()
        default_regions = os.path.join(default_regions, u'SimCity 4/Regions/')
        self.settings = appsettings.load(default_regions)
        self.mydocs = self.settings.region_dir

        self.originalColors = None
        self.zoomLevel = 1
        self.zoomLevelPow = 0
        self.region = None
        self.rebuilder = None

        self.btnZoomIn.Enable(False)
        self.btnZoomOut.Enable(False)
        self.btnSaveRgn.Enable(False)
        self.btnSave.Enable(False)
        self.overlayCbx.Enable(False)
        self.mapCbx.Enable(False)
        self.btnEditMode.Enable(False)
        self.btnExportRgn.Enable(False)
        self.Center()

    def _default_dir(self, path, fallback=None):
        if path and os.path.isdir(path):
            return path
        if fallback and os.path.isdir(fallback):
            return fallback
        return os.getcwd()

    def OnOptions(self, event):
        dlg = PreferencesDialog(self, self.settings)
        if dlg.ShowModal() == wx.ID_OK:
            dlg.Apply()
            self.mydocs = self.settings.region_dir
            region.LoadGradient()
        dlg.Destroy()

    def OnCloseWindow(self, event):
        dlg = wx.MessageDialog(self, "Are you sure you want to quit ?",
                               "SC4Mapper",
                               wx.YES_NO | wx.YES_DEFAULT | wx.ICON_INFORMATION)
        res = dlg.ShowModal()
        dlg.Destroy()
        if res == wx.ID_NO:
            return
        self.Destroy()
        sys.exit(0)

    def RevertConfig(self, event):
        self.region.allCities = region.WorkTheconfig(
            self.region.originalConfig, 250.0)
        self.region.config = self.region.BuildConfig()
        self.editMode = EDITMODE_NONE
        self.btnSmall.SetValue(False)
        self.btnMedium.SetValue(False)
        self.btnBig.SetValue(False)
        self.btnVoid.SetValue(False)
        self.back.offX = 0
        self.back.offY = 0
        self.back.UpdateDrawing()
        self.back.Refresh(False)
        self.back.SetFocus()

    def SetEditModeSmall(self, event):
        if self.btnSmall.GetValue():
            self.editMode = EDITMODE_SMALL
            self.btnMedium.SetValue(False)
            self.btnBig.SetValue(False)
            self.btnVoid.SetValue(False)
        else:
            self.editMode = EDITMODE_NONE
            self.back.UpdateDrawing()
        self.back.SetFocus()

    def SetEditModeMedium(self, event):
        if self.btnMedium.GetValue():
            self.editMode = EDITMODE_MEDIUM
            self.btnSmall.SetValue(False)
            self.btnBig.SetValue(False)
            self.btnVoid.SetValue(False)
        else:
            self.editMode = EDITMODE_NONE
            self.back.UpdateDrawing()
        self.back.SetFocus()

    def SetEditModeBig(self, event):
        if self.btnBig.GetValue():
            self.editMode = EDITMODE_BIG
            self.btnMedium.SetValue(False)
            self.btnSmall.SetValue(False)
            self.btnVoid.SetValue(False)
        else:
            self.editMode = EDITMODE_NONE
            self.back.UpdateDrawing()
        self.back.SetFocus()

    def SetEditModeVoid(self, event):
        if self.btnVoid.GetValue():
            self.editMode = EDITMODE_VOID
            self.btnMedium.SetValue(False)
            self.btnBig.SetValue(False)
            self.btnSmall.SetValue(False)
        else:
            self.editMode = EDITMODE_NONE
            self.back.UpdateDrawing()
        self.back.SetFocus()

    def OnToggleEditMode(self, event):
        self.Freeze()
        if self.btnEditMode.GetValue():
            self.btnSmall.SetValue(False)
            self.btnMedium.SetValue(False)
            self.btnBig.SetValue(False)
            self.btnVoid.SetValue(False)

            self.btnSmall.Show()
            self.btnLoadRgn.Hide()
            self.btnMedium.Show()
            self.btnCreateRgn.Hide()
            self.btnBig.Show()
            self.btnSaveRgn.Hide()
            self.btnVoid.Show()
            self.btnExportRgn.Hide()
            self.btnRevert.Show()
            self.btnSave.Hide()
            self.overlayCbx.SetValue(True)
            self.overlayCbx.Enable(False)
            self.editMode = EDITMODE_NONE
            self.back.SetFocus()
        else:
            self.btnSmall.Hide()
            self.btnLoadRgn.Show()
            self.btnMedium.Hide()
            self.btnCreateRgn.Show()
            self.btnBig.Hide()
            self.btnSaveRgn.Show()
            self.btnVoid.Hide()
            self.btnExportRgn.Show()
            self.btnRevert.Hide()
            self.btnSave.Show()
            self.overlayCbx.Enable(True)
        self.back.OnSize(None)
        self.Layout()
        self.Refresh()
        self.Thaw()

    def OnOverlay(self, event):
        self.Freeze()
        self.back.UpdateDrawing()
        self.back.Refresh()
        self.Thaw()

    def ShowBasemap(self):
        """True when a map underlay exists and the user wants to see it."""
        if self.region is None:
            return False
        if getattr(self.region, "basemap", None) is None:
            return False
        return self.mapCbx.IsEnabled() and self.mapCbx.GetValue()

    def OnBasemap(self, event):
        self.Freeze()
        self.back.UpdateDrawing()
        self.back.Refresh()
        self.Thaw()

    def OnZoomIn(self, event):
        if self.zoomLevelPow > 0:
            self.zoomLevelPow -= 1
            self.zoomLevel = 2 ** self.zoomLevelPow
            self.back.SetVirtualSize(
                (self.region.imgSize[0] // self.zoomLevel,
                 self.region.imgSize[1] // self.zoomLevel))
        if self.zoomLevelPow > 0:
            self.btnZoomIn.Enable(True)
        else:
            self.btnZoomIn.Enable(False)
        if self.zoomLevelPow < 4:
            self.btnZoomOut.Enable(True)
        else:
            self.btnZoomOut.Enable(False)
        self.back.OnSize(None)
        self.back.SetFocus()

    def OnZoomOut(self, event):
        if self.zoomLevelPow < 4:
            self.zoomLevelPow += 1
            self.zoomLevel = 2 ** self.zoomLevelPow
            self.back.SetVirtualSize(
                (self.region.imgSize[0] // self.zoomLevel,
                 self.region.imgSize[1] // self.zoomLevel))
        if self.zoomLevelPow > 0:
            self.btnZoomIn.Enable(True)
        else:
            self.btnZoomIn.Enable(False)
        if self.zoomLevelPow < 4:
            self.btnZoomOut.Enable(True)
        else:
            self.btnZoomOut.Enable(False)
        self.back.OnSize(None)
        self.back.SetFocus()

    def OnMouseMove(self, event):
        if self.btnEditMode.GetValue():
            if self.back.wait is True:
                pass
            elif self.editMode == EDITMODE_NONE:
                if event.Dragging() and self.back.crop is not None:
                    newpos = self.back.CalcUnscrolledPosition(event.GetX(),
                                                              event.GetY())
                    newpos = [newpos[0] * self.zoomLevel,
                              newpos[1] * self.zoomLevel]
                    newpos = [newpos[0] - self.back.offX,
                              newpos[1] - self.back.offY]
                    newpos = [newpos[0] // 64, newpos[1] // 64]
                    origin = [newpos[0] * 64 + self.back.offX,
                              newpos[1] * 64 + self.back.offY]
                    size = 64 + 1
                    if (origin[0] >= 0 and origin[1] >= 0
                            and origin[0] + size <= self.region.imgSize[0]
                            and origin[1] + size <= self.region.imgSize[1]):
                        self.back.crop = [self.back.crop[0], self.back.crop[1],
                                          newpos[0], newpos[1]]
                        self.back.UpdateDrawing()
                        self.back.wait = True
                        self.back.Refresh(False)

            elif self.editMode == EDITMODE_VOID:
                newpos = self.back.CalcUnscrolledPosition(event.GetX(),
                                                          event.GetY())
                newpos = [newpos[0] * self.zoomLevel,
                          newpos[1] * self.zoomLevel]
                newpos = [newpos[0] - self.back.offX,
                          newpos[1] - self.back.offY]
                newpos = [newpos[0] // 64, newpos[1] // 64]
                self.back.HighlightCity(self.zoomLevel, self.region, newpos)
                self.back.wait = True
                self.back.Refresh(False)
            else:
                sizes = [0, 1, 2, 4]
                newpos = self.back.CalcUnscrolledPosition(event.GetX(),
                                                          event.GetY())
                newpos = [newpos[0] * self.zoomLevel,
                          newpos[1] * self.zoomLevel]
                newpos = [newpos[0] - self.back.offX,
                          newpos[1] - self.back.offY]
                newpos = [newpos[0] // 64, newpos[1] // 64]

                origin = [newpos[0] * 64 + self.back.offX,
                          newpos[1] * 64 + self.back.offY]
                size = sizes[self.editMode] * 64 + 1
                if origin[0] + size > self.region.imgSize[0]:
                    origin[0] = self.region.imgSize[0] - size
                    newpos[0] = (origin[0] - self.back.offX) // 64
                if origin[1] + size > self.region.imgSize[1]:
                    origin[1] = self.region.imgSize[1] - size
                    newpos[1] = (origin[1] - self.back.offY) // 64

                if (origin[0] >= 0 and origin[1] >= 0
                        and origin[0] + size <= self.region.imgSize[0]
                        and origin[1] + size <= self.region.imgSize[1]):
                    self.back.HighlightNewCity(self.zoomLevel, self.region,
                                               newpos, sizes[self.editMode])

                self.back.wait = True
                self.back.Refresh(False)

    def OnLeftDown(self, event):
        if (self.btnEditMode.GetValue() and self.editMode == EDITMODE_NONE
                and event.ControlDown()):
            newpos = self.back.CalcUnscrolledPosition(event.GetX(),
                                                      event.GetY())
            newpos = [newpos[0] * self.zoomLevel, newpos[1] * self.zoomLevel]
            newpos = [newpos[0] - self.back.offX, newpos[1] - self.back.offY]
            newpos = [newpos[0] // 64, newpos[1] // 64]
            origin = [newpos[0] * 64 + self.back.offX,
                      newpos[1] * 64 + self.back.offY]
            size = 64 + 1
            if (origin[0] >= 0 and origin[1] >= 0
                    and origin[0] + size <= self.region.imgSize[0]
                    and origin[1] + size <= self.region.imgSize[1]):
                self.back.crop = [newpos[0], newpos[1], newpos[0], newpos[1]]

    def OnLeftUp(self, event):
        if self.btnEditMode.GetValue():
            newpos = self.back.CalcUnscrolledPosition(event.GetX(),
                                                      event.GetY())
            newpos = [newpos[0] * self.zoomLevel, newpos[1] * self.zoomLevel]
            newpos = [newpos[0] - self.back.offX, newpos[1] - self.back.offY]
            newpos = [newpos[0] // 64, newpos[1] // 64]

            if self.editMode == EDITMODE_NONE:
                if self.back.crop is not None:
                    crop = [min(self.back.crop[0], self.back.crop[2]),
                            min(self.back.crop[1], self.back.crop[3]),
                            max(self.back.crop[0], self.back.crop[2]),
                            max(self.back.crop[1], self.back.crop[3])]
                    configSize = (crop[2] - crop[0] + 1, crop[3] - crop[1] + 1)
                    config = region.BuildBestConfig(configSize)
                    self.region.config.paste(
                        '#000000', (0, 0, self.region.config.size[0],
                                    self.region.config.size[1]))
                    self.region.config.paste(config, (crop[0], crop[1]))
                    self.region.allCities = region.WorkTheconfig(
                        self.region.config, self.region.waterLevel)
                    self.region.config = self.region.BuildConfig()
                self.back.crop = None
            elif self.editMode == EDITMODE_VOID:
                self.region.DeleteCityAt(newpos)
            else:
                sizes = [0, 1, 2, 4]
                origin = [newpos[0] * 64 + self.back.offX,
                          newpos[1] * 64 + self.back.offY]
                size = sizes[self.editMode] * 64 + 1
                if origin[0] + size > self.region.imgSize[0]:
                    origin[0] = self.region.imgSize[0] - size
                    newpos[0] = (origin[0] - self.back.offX) // 64
                if origin[1] + size > self.region.imgSize[1]:
                    origin[1] = self.region.imgSize[1] - size
                    newpos[1] = (origin[1] - self.back.offY) // 64

                if (origin[0] >= 0 and origin[1] >= 0
                        and origin[0] + size <= self.region.imgSize[0]
                        and origin[1] + size <= self.region.imgSize[1]):
                    currentSize = sizes[self.editMode]
                    done = False
                    while not done:
                        done = True
                        cities = self.region.GetCitiesUnder(newpos, currentSize)
                        for city in cities:
                            if city.cityXSize == 1:
                                self.region.allCities.remove(city)
                            else:
                                done = False
                                newCities = city.Split()
                                self.region.allCities.remove(city)
                                for c in newCities:
                                    self.region.allCities.append(c)
                    self.region.allCities.append(
                        region.CityProxy(250.0, newpos[0], newpos[1],
                                            currentSize, currentSize))
            self.region.config = self.region.BuildConfig()
            self.back.UpdateDrawing()
            self.back.wait = True
            self.back.Refresh(False)

    def SaveBmp(self, event):
        dlg = wx.FileDialog(
            self, message="Save file as ...",
            defaultDir=self._default_dir(self.settings.image_save_dir),
            defaultFile="",
            wildcard="PNG file (*.png)|*.png|"
                     "Jpeg file (*.jpg)|*.jpg|"
                     "Bitmap file (*.bmp)|*.bmp", style=wx.FD_SAVE)
        if dlg.ShowModal() == wx.ID_OK:
            wx.BeginBusyCursor()
            path = dlg.GetPath()

            lightDir = Normalize((1, -5, -1))
            s = (self.region.height.shape[1], self.region.height.shape[0])
            xO = yO = 0
            colours = [0, "#FF0000", "#00FF00", 0, "#0000FF"]
            sizes = [0, 64, 128, 0, 256]

            dlgProg = wx.ProgressDialog(
                "Saving overview", "Please wait while saving overview",
                maximum=len(self.region.allCities)
                + len(self.region.missingCities) + 10,
                parent=self, style=0)

            im = Image.new("RGB", (self.region.imgSize[0],
                                   self.region.imgSize[1]))
            for i, city in enumerate(self.region.allCities):
                dlgProg.Update(i, "Please wait while saving overview")
                x = int(city.xPos)
                y = int(city.yPos)
                width = sizes[city.cityXSize] + 1
                height = sizes[city.cityYSize] + 1
                x1 = x - xO + self.back.offX
                y1 = y - yO + self.back.offY
                x2 = x1 + width
                y2 = y1 + height

                h = Numeric.zeros((height, width), Numeric.uint16)
                h[:, :] = Numeric.reshape(self.region.height[y1:y2, x1:x2],
                                          (height, width))
                h = h.astype(Numeric.float32)
                h /= Numeric.array(10).astype(Numeric.float32)
                rawRGB = terrain.onePassColors(
                    False, (height, width), self.region.waterLevel, h,
                    gradient.paletteWater,
                    gradient.paletteLand, lightDir)
                del h
                imCity = Image.frombytes("RGB", (width, height), rawRGB)
                del rawRGB
                im.paste(imCity, (x1, y1))
                del imCity

            if self.overlayCbx.GetValue():
                draw = ImageDraw.Draw(im)

                def DrawHided(x, y, width, height):
                    x1 = x
                    y1 = y
                    x2 = x1 + width
                    y2 = y1 + height
                    h = Numeric.zeros((height, width), Numeric.uint16)
                    h[:, :] = Numeric.reshape(self.region.height[y1:y2, x1:x2],
                                              (height, width))
                    h = h.astype(Numeric.float32)
                    h /= Numeric.array(10).astype(Numeric.float32)
                    rawRGB = terrain.onePassColors(
                        False, (height, width), self.region.waterLevel, h,
                        gradient.paletteWater,
                        gradient.paletteLand, lightDir)
                    del h
                    imCity = Image.frombytes(
                        "RGB", (width, height), rawRGB).convert("L").convert("RGB")
                    del rawRGB
                    im.paste(imCity, (x1, y1))
                    del imCity

                if self.back.offX > 0:
                    i += 1
                    dlgProg.Update(i, "Please wait while saving overview")
                    width = self.back.offX
                    height = self.region.imgSize[1]
                    x = 0
                    y = 0
                    DrawHided(x, y, width, height)
                    x1 = x
                    y1 = y
                    x2 = x1 + width
                    y2 = y1 + height
                    h = Numeric.zeros((height, width), Numeric.uint16)
                    h[:, :] = Numeric.reshape(self.region.height[y1:y2, x1:x2],
                                              (height, width))
                    h = h.astype(Numeric.float32)
                    h /= Numeric.array(10).astype(Numeric.float32)
                    rawRGB = terrain.onePassColors(
                        False, (height, width), self.region.waterLevel, h,
                        gradient.paletteWater,
                        gradient.paletteLand, lightDir)
                    del h
                    imCity = Image.frombytes(
                        "RGB", (width, height), rawRGB).convert("L").convert("RGB")
                    del rawRGB
                    im.paste(imCity, (x1, y1))
                    del imCity
                if self.back.offY > 0:
                    i += 1
                    dlgProg.Update(i, "Please wait while saving overview")
                    width = self.region.imgSize[0]
                    height = self.back.offY
                    x = 0
                    y = 0
                    DrawHided(x, y, width, height)
                if self.back.offX < 0:
                    i += 1
                    dlgProg.Update(i, "Please wait while saving overview")
                    width = -self.back.offX
                    height = self.region.imgSize[1]
                    x = self.region.imgSize[0] + self.back.offX
                    y = 0
                    DrawHided(x, y, width, height)
                if self.back.offY < 0:
                    i += 1
                    dlgProg.Update(i, "Please wait while saving overview")
                    width = self.region.imgSize[0]
                    height = -self.back.offY
                    x = 0
                    y = self.region.imgSize[1] + self.back.offY
                    DrawHided(x, y, width, height)
                lines = []

                for y in range(s[1] // 64):
                    lines.append([0 - xO, y * 64 - yO,
                                  self.region.originalConfig.size[0] * 64 - xO,
                                  y * 64 - yO])
                for x in range(s[0] // 64):
                    lines.append([x * 64 - xO, 0 - yO, x * 64 - xO,
                                  self.region.originalConfig.size[1] * 64 - yO])
                for x1, y1, x2, y2 in lines:
                    draw.line([x1 + self.back.offX, y1 + self.back.offY,
                               x2 + self.back.offX, y2 + self.back.offY],
                              fill="#222222")

                for city in self.region.allCities:
                    x = int(city.xPos)
                    y = int(city.yPos)
                    width = sizes[city.cityXSize]
                    height = sizes[city.cityYSize]
                    draw.rectangle(
                        [x - xO + 1 + self.back.offX,
                         y - yO + 1 + self.back.offY,
                         x - xO + width - 1 + self.back.offX,
                         y - yO + height - 1 + self.back.offY],
                        outline=colours[city.cityXSize])
                for x, y in self.region.missingCities:
                    i += 1
                    dlgProg.Update(i, "Please wait while saving overview")

                    width = 65
                    height = 65
                    x = int(x * 64)
                    y = int(y * 64)
                    x1 = x - xO + self.back.offX
                    y1 = y - yO + self.back.offY
                    x2 = x - xO + width + self.back.offX
                    y2 = y - yO + height + self.back.offY
                    if x1 < 0:
                        x1 = 0
                    if y1 < 0:
                        y1 = 0
                    if x2 < 0:
                        x2 = 0
                    if y2 < 0:
                        y2 = 0
                    if x1 > self.region.imgSize[0]:
                        x1 = self.region.imgSize[0]
                    if y1 > self.region.imgSize[1]:
                        y1 = self.region.imgSize[1]
                    if x2 > self.region.imgSize[0]:
                        x2 = self.region.imgSize[0]
                    if y2 > self.region.imgSize[1]:
                        y2 = self.region.imgSize[1]
                    width = x2 - x1
                    height = y2 - y1
                    if width <= 0 or height <= 0:
                        continue
                    h = Numeric.zeros((height, width), Numeric.uint16)
                    h[:, :] = Numeric.reshape(self.region.height[y1:y2, x1:x2],
                                              (height, width))
                    h = h.astype(Numeric.float32)
                    h /= Numeric.array(10).astype(Numeric.float32)
                    rawRGB = terrain.onePassColors(
                        False, (height, width), self.region.waterLevel, h,
                        gradient.paletteWater,
                        gradient.paletteLand, lightDir)
                    imCity = Image.frombytes(
                        "RGB", (width, height), rawRGB).convert("L").convert("RGB")
                    del rawRGB
                    im.paste(imCity, (x1, y1))
                    del imCity

            im.save(path)
            dlgProg.Close()
            dlgProg.Destroy()
            wx.EndBusyCursor()

    def CreateRgn(self, event):
        result = dialogs.ask_question(
            'Do you want to create a region from ?',
            buttons=["Real-world location", "SC4M", "Grayscale image",
                     "16 bit png", "RGB image", wx.ID_CANCEL])
        if result == wx.ID_CANCEL or result is None:
            return
        self.btnEditMode.Enable(False)
        if result == 'Real-world location':
            self.CreateRgnFromLocation()
        if result == 'SC4M':
            self.CreateRgnFromSC4M()
        if result == 'Grayscale image':
            self.CreateRgnFromGrey()
        if result == '16 bit png':
            self.CreateRgnFromPNG()
        if result == 'RGB image':
            self.CreateRgnFromRGB()

    def CreateRgnInit(self):
        self.btnSave.Enable(False)
        self.btnExportRgn.Enable(False)
        self.btnSaveRgn.Enable(False)
        self.region = None

        self.back.SetVirtualSize((100, 100))
        self.zoomLevel = 1
        self.zoomLevelPow = 0

        self.SetTitle("NHP SC4Mapper %s Version " % MAPPER_VERSION)

    def CreateRgnOk(self):
        self.btnSave.Enable(True)
        self.btnSaveRgn.Enable(True)
        self.btnExportRgn.Enable(True)
        self.SetTitle("NHP SC4Mapper %s Version - " % MAPPER_VERSION
                      + self.regionName)
        self.btnZoomIn.Enable(False)
        self.btnZoomOut.Enable(True)
        self.overlayCbx.Enable(True)
        self.back.offX = 0
        self.back.offY = 0
        self.back.OnSize(None)

    def CreateRgnFromLocation(self):
        """Build a region from real-world elevation data."""
        dlg = CreateRgnFromLocationDialog(self, self.settings)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        try:
            request = dlg.GetRequest()
        except ValueError as exc:
            dlg.Destroy()
            wx.MessageBox(str(exc), "Check the settings",
                          wx.OK | wx.ICON_ERROR, self)
            return
        citySize = dlg.GetCitySize()
        wantUnderlay = dlg.WantsUnderlay()
        placeName = dlg.search.GetValue().strip()
        dlg.Destroy()

        self.CreateRgnInit()

        cacheDir = getattr(self.settings, "tile_cache_dir", "") or None
        elevationUrl = (getattr(self.settings, "elevation_url", "")
                        or geo.DEFAULT_TILE_URL)

        progress = wx.ProgressDialog(
            "Importing terrain", "Contacting the elevation server",
            maximum=100, parent=self, style=wx.PD_APP_MODAL | wx.PD_AUTO_HIDE)

        def report(done, total, message):
            percent = int(done * 100 / total) if total else 0
            progress.Update(min(percent, 99), message)
            wx.Yield()

        basemap = None
        try:
            waterMask = None
            if request.water_source != "elevation":
                water = geo.OverpassClient(
                    cache_dir=os.path.join(cacheDir, "osm") if cacheDir else None)
                waterMask = geo.fetch_water_mask(request, water, report)
            fetcher = geo.HttpTileFetcher(
                url_template=elevationUrl,
                cache_dir=os.path.join(cacheDir, "elevation") if cacheDir else None)
            result = geo.build_region_grid(request, fetcher, report,
                                           water_mask=waterMask)
            if wantUnderlay:
                report(0, 100, "Downloading the map underlay")
                mapFetcher = geo.HttpTileFetcher(
                    url_template=self.settings.basemap_url,
                    cache_dir=os.path.join(cacheDir, "basemap") if cacheDir else None)
                basemap, _, _, _ = geo.sample_basemap(request, mapFetcher, report)
        except geo.GeoImportError as exc:
            progress.Destroy()
            wx.MessageBox(str(exc), "Import failed", wx.OK | wx.ICON_ERROR, self)
            return
        except Exception as exc:
            progress.Destroy()
            wx.MessageBox("Unexpected problem while importing: %s" % exc,
                          "Import failed", wx.OK | wx.ICON_ERROR, self)
            return
        progress.Destroy()

        config = geo.build_config_image((request.tiles_x, request.tiles_y),
                                        citySize)

        class dlgstub:
            def __init__(self):
                pass

            def Update(self, x, y):
                pass

        wx.BeginBusyCursor()
        try:
            newRegion = region.SC4Region(None, request.sea_level_m, dlgstub(),
                                         config)
            newRegion.show(dlgstub())
        except AssertionError:
            wx.EndBusyCursor()
            wx.MessageBox("The generated city layout was rejected. This is a "
                          "bug -- please report the region size you used.",
                          "Region creation error", wx.OK | wx.ICON_ERROR, self)
            return

        if tuple(newRegion.shape) != result.height_dm.shape:
            wx.EndBusyCursor()
            wx.MessageBox(
                "The imported terrain is %dx%d but the region wants %dx%d."
                % (result.height_dm.shape[1], result.height_dm.shape[0],
                   newRegion.shape[1], newRegion.shape[0]),
                "Region creation error", wx.OK | wx.ICON_ERROR, self)
            return

        self.regionName = placeName or ("%.4f,%.4f" % (request.center_lat,
                                                       request.center_lon))
        self.region = newRegion
        self.region.height = result.height_dm
        self.region.basemap = basemap
        self.region.georeference = result.georeference
        # Carried into the georeference record written into each city save.
        self.region.regionName = self.regionName
        self.region.importId = uuid.uuid4().hex
        self.region.oceanDepth = request.ocean_depth_m
        self.region.keepBathymetry = request.keep_bathymetry
        self.zoomLevel = 1
        self.zoomLevelPow = 0
        self.back.SetVirtualSize((self.region.height.shape[1],
                                  self.region.height.shape[0]))
        self.SetFocus()
        self.CreateRgnOk()
        self.mapCbx.Enable(basemap is not None)
        self.btnEditMode.Enable(True)
        wx.EndBusyCursor()

        summary = result.summary()
        summary += ("\n\nUse Edit Config.bmp to change which parts become "
                    "small, medium or large cities, or to erase tiles and "
                    "leave holes.")
        if basemap is not None:
            summary += "\nThe Map underlay checkbox shows the real map behind the region."
        summary += "\n\n" + result.attribution
        wx.MessageBox(summary, "Imported %s" % self.regionName,
                      wx.OK | wx.ICON_INFORMATION, self)

    def CreateRgnFromSC4M(self):
        self.CreateRgnInit()
        dlg = wx.FileDialog(
            self, message="Choose a SC4M file",
            defaultDir=self._default_dir(self.settings.import_dir),
            defaultFile="",
            wildcard="SC4Terraform exported (*.SC4M)|*.SC4M", style=wx.FD_OPEN)
        if dlg.ShowModal() == wx.ID_OK:
            paths = dlg.GetPaths()[0]
            dlg.Destroy()
        else:
            dlg.Destroy()
            return
        sc4mFile = paths
        name = os.path.split(sc4mFile)[1]
        name = os.path.splitext(name)[0]

        wx.BeginBusyCursor()

        try:
            raw = open(sc4mFile, "rb")
            zipped = zip_utils.ZipInputStream(raw)
            s = zipped.read(4)
            if s != b"SC4M":
                raise IOError("SC4M")
            version = struct.unpack("<I", zipped.read(4))[0]
            if version != 0x0200:
                raise IOError("Version")
            ySize = struct.unpack("<I", zipped.read(4))[0]
            xSize = struct.unpack("<I", zipped.read(4))[0]
            mini = struct.unpack("<f", zipped.read(4))[0]
            temp = zipped.read(4)
            config = None
            if temp == b"SC4N":
                lenHtml = struct.unpack("<I", zipped.read(4))[0]
                if lenHtml:
                    htmlText = zipped.read(lenHtml)
                    old_cwd = os.getcwd()
                    os.chdir(os.path.split(sc4mFile)[0])
                    try:
                        authorNotes = about_dialog.AuthorBox(self, htmlText)
                        wx.EndBusyCursor()
                        authorNotes.ShowModal()
                        wx.BeginBusyCursor()
                        authorNotes.Destroy()
                    except Exception:
                        pass
                    os.chdir(old_cwd)
                temp = zipped.read(4)
            if temp == b'SC4C':
                configSize = struct.unpack("<2I", zipped.read(8))
                lenstring = struct.unpack("<I", zipped.read(4))[0]
                imString = zipped.read(lenstring)
                config = Image.frombytes("RGB", configSize, imString)
                temp = zipped.read(4)
            if temp != b"SC4D":
                raise IOError("SC4D")
            r = Numeric.frombuffer(zipped.read(xSize * ySize), Numeric.uint8)
            rH = Numeric.frombuffer(zipped.read(xSize * ySize), Numeric.uint8)
            raw.close()
            zipped = None
            r = r.astype(Numeric.uint16)
            rH = rH.astype(Numeric.uint16)
            rH = rH * Numeric.array(256).astype(Numeric.uint16)
            r = r + rH
            del rH

            class dlgstub:
                def __init__(self):
                    pass

                def Update(self, x, y):
                    pass

            NewRegion = region.SC4Region(None, 250, dlgstub(), config)
            NewRegion.show(dlgstub())
        except IOError:
            wx.EndBusyCursor()
            dlg1 = wx.MessageDialog(
                self, sc4mFile + ' seems not to be a valid image file',
                'Region creation error', wx.OK | wx.ICON_ERROR)
            dlg1.ShowModal()
            dlg1.Destroy()
            return

        self.regionName = name
        self.region = NewRegion
        self.region.height = Numeric.reshape(r, self.region.shape)
        del r
        self.zoomLevel = 1
        self.zoomLevelPow = 0
        self.back.SetVirtualSize((self.region.height.shape[1],
                                  self.region.height.shape[0]))
        self.SetFocus()
        self.CreateRgnOk()
        self.btnEditMode.Enable(True)
        wx.EndBusyCursor()

    def CreateRgnFromGrey(self):
        self.CreateRgnInit()
        dlg = CreateRgnFromFile(
            self, "8-bit Greyscale",
            "All graphics file |*.jpeg;*.jpg;*.png;*.bmp|"
            "Jpeg file (*.jpeg;*.jpg)|*.jpeg;*.jpg|"
            "Bitmap file (*.bmp)|*.bmp", True,
            default_dir=self._default_dir(self.settings.import_dir),
            config_default_dir=self._default_dir(self.settings.import_dir))
        ret = dlg.ShowModal()

        if ret == wx.ID_OK:
            paths = dlg.fileName.GetValue()
            configName = dlg.configFileName.GetValue()
            configSize = (dlg.sizeX.GetValue(), dlg.sizeY.GetValue())
            fromConfig = dlg.fromConfig.GetValue()
            scale = dlg.GetImageFactor()
            dlg.Destroy()
        else:
            dlg.Destroy()
            return

        wx.BeginBusyCursor()
        name = os.path.split(paths)[1]
        name = os.path.splitext(name)[0]

        im = Image.open(paths)
        if not (im.size[0] == configSize[0] * 64 + 1
                and im.size[1] == configSize[1] * 64 + 1):
            dlg1 = wx.MessageDialog(
                self, paths + ' has not correct dimensions\n'
                + 'It should be (%d by %d) but it is (%d by %d)\nDo you want '
                'to resize the image to fit region dimensions?'
                % (configSize[0] * 64 + 1, configSize[1] * 64 + 1,
                   im.size[0], im.size[1]),
                'Import warning',
                wx.YES_NO | wx.YES_DEFAULT | wx.ICON_INFORMATION)
            res = dlg1.ShowModal()
            dlg1.Destroy()
            if res == wx.ID_YES:
                im = im.resize((configSize[0] * 64 + 1, configSize[1] * 64 + 1),
                               Image.Resampling.BICUBIC)
            else:
                wx.EndBusyCursor()
                return
        if im.mode != "L":
            im = im.convert("L")

        r = Numeric.frombuffer(im.tobytes(), Numeric.uint8)
        r = Numeric.asarray(r, Numeric.float32)
        r = r * Numeric.array(10 * scale).astype(Numeric.float32)
        r = Numeric.asarray(r, Numeric.uint16)

        if fromConfig:
            config = Image.open(configName)
        else:
            config = region.BuildBestConfig(configSize)

        class dlgstub:
            def __init__(self):
                pass

            def Update(self, x, y):
                pass

        try:
            NewRegion = region.SC4Region(None, 250, dlgstub(), config)
            NewRegion.show(dlgstub())
        except AssertionError:
            wx.EndBusyCursor()
            dlg1 = wx.MessageDialog(
                self, configName + ' seems not to be a valid config.bmp',
                'Region creation error', wx.OK | wx.ICON_ERROR)
            dlg1.ShowModal()
            dlg1.Destroy()
            return

        self.regionName = name
        self.region = NewRegion
        self.region.height = Numeric.reshape(r, self.region.shape)
        del r
        self.zoomLevel = 1
        self.zoomLevelPow = 0
        self.back.SetVirtualSize((self.region.height.shape[1],
                                  self.region.height.shape[0]))
        self.SetFocus()
        self.CreateRgnOk()
        self.btnEditMode.Enable(True)
        wx.EndBusyCursor()

    def CreateRgnFromPNG(self):
        self.CreateRgnInit()
        dlg = CreateRgnFromFile(
            self, "16-bit PNG", "PNG File |*.png",
            default_dir=self._default_dir(self.settings.import_dir),
            config_default_dir=self._default_dir(self.settings.import_dir))
        ret = dlg.ShowModal()
        paths = dlg.fileName.GetValue()
        configName = dlg.configFileName.GetValue()
        configSize = (dlg.sizeX.GetValue(), dlg.sizeY.GetValue())
        fromConfig = dlg.fromConfig.GetValue()
        dlg.Destroy()
        if ret == wx.ID_CANCEL:
            return

        name = os.path.split(paths)[1]
        name = os.path.splitext(name)[0]

        im = Image.open(paths)
        if not (im.size[0] == configSize[0] * 64 + 1
                and im.size[1] == configSize[1] * 64 + 1):
            dlg1 = wx.MessageDialog(
                self, paths + ' has not correct dimensions\n'
                + 'It should be (%d by %d) but it is (%d by %d)\nDo you want '
                'to resize the image to fit region dimensions?'
                % (configSize[0] * 64 + 1, configSize[1] * 64 + 1,
                   im.size[0], im.size[1]),
                'Import warning',
                wx.YES_NO | wx.YES_DEFAULT | wx.ICON_INFORMATION)
            res = dlg1.ShowModal()
            dlg1.Destroy()
            if res == wx.ID_YES:
                im = im.resize((configSize[0] * 64 + 1, configSize[1] * 64 + 1),
                               Image.Resampling.BICUBIC)
            else:
                return
        if im.mode != "I":
            dlg1 = wx.MessageDialog(
                self, configName + ' seems not to be a valid 16 bit grescale image',
                'Region creation error', wx.OK | wx.ICON_ERROR)
            dlg1.ShowModal()
            dlg1.Destroy()
            return

        dlgProg = wx.ProgressDialog(
            "Loading PNG", "Please wait while loading the region",
            maximum=configSize[1] * configSize[0] + 10, parent=self, style=0)

        wx.BeginBusyCursor()
        heights = Numeric.zeros((configSize[1] * 64 + 1, configSize[0] * 64 + 1),
                                Numeric.uint16)
        i = 0
        for y in range(configSize[1]):
            for x in range(configSize[0]):
                i += 1
                dlgProg.Update(i, "Please wait while loading the region")
                imSmall = im.crop((x * 64, y * 64, x * 64 + 65, y * 64 + 65))
                r = Numeric.frombuffer(imSmall.tobytes(), Numeric.int32)
                r = Numeric.reshape(r, (64 + 1, 64 + 1))
                r = r.astype(Numeric.uint16)
                heights[y * 64:y * 64 + 65, x * 64:x * 64 + 65] = r
                del r
                del imSmall

        dlgProg.Close()
        dlgProg.Destroy()
        self.Refresh()
        wx.Yield()

        if fromConfig:
            config = Image.open(configName)
        else:
            config = region.BuildBestConfig(configSize)

        class dlgstub:
            def __init__(self):
                pass

            def Update(self, x, y):
                pass

        try:
            NewRegion = region.SC4Region(None, 250, dlgstub(), config)
            NewRegion.show(dlgstub())
        except AssertionError:
            wx.EndBusyCursor()
            dlg1 = wx.MessageDialog(
                self, configName + ' seems not to be a valid config.bmp',
                'Region creation error', wx.OK | wx.ICON_ERROR)
            dlg1.ShowModal()
            dlg1.Destroy()
            return

        self.regionName = name
        self.region = NewRegion
        self.region.height = Numeric.reshape(heights, self.region.shape)
        del heights
        self.zoomLevel = 1
        self.zoomLevelPow = 0
        self.back.SetVirtualSize((self.region.height.shape[1],
                                  self.region.height.shape[0]))
        self.SetFocus()
        self.CreateRgnOk()
        self.btnEditMode.Enable(True)
        wx.EndBusyCursor()

    def CreateRgnFromRGB(self):
        self.CreateRgnInit()
        dlg = CreateRgnFromFile(
            self, "RGB", "RGB File |*.png;*.bmp;*.jpg",
            default_dir=self._default_dir(self.settings.import_dir),
            config_default_dir=self._default_dir(self.settings.import_dir))
        ret = dlg.ShowModal()
        paths = dlg.fileName.GetValue()
        configName = dlg.configFileName.GetValue()
        configSize = (dlg.sizeX.GetValue(), dlg.sizeY.GetValue())
        fromConfig = dlg.fromConfig.GetValue()
        dlg.Destroy()
        if ret == wx.ID_CANCEL:
            return

        name = os.path.split(paths)[1]
        name = os.path.splitext(name)[0]

        im = Image.open(paths)
        if not (im.size[0] == configSize[0] * 64 + 1
                and im.size[1] == configSize[1] * 64 + 1):
            dlg1 = wx.MessageDialog(
                self, paths + ' has not correct dimensions\n'
                + 'It should be (%d by %d) but it is (%d by %d)\nDo you want '
                'to resize the image to fit region dimensions?'
                % (configSize[0] * 64 + 1, configSize[1] * 64 + 1,
                   im.size[0], im.size[1]),
                'Import warning',
                wx.YES_NO | wx.YES_DEFAULT | wx.ICON_INFORMATION)
            res = dlg1.ShowModal()
            dlg1.Destroy()
            if res == wx.ID_YES:
                im = im.resize((configSize[0] * 64 + 1, configSize[1] * 64 + 1),
                               Image.Resampling.NEAREST)
            else:
                return
        if im.mode != "RGB":
            dlg1 = wx.MessageDialog(
                self, configName + ' seems not to be a valid RGB image',
                'Region creation error', wx.OK | wx.ICON_ERROR)
            dlg1.ShowModal()
            dlg1.Destroy()
            return

        dlgProg = wx.ProgressDialog(
            "Loading RGB", "Please wait while loading the region",
            maximum=configSize[1] * configSize[0] + 10, parent=self, style=0)

        wx.BeginBusyCursor()
        heights = Numeric.zeros((configSize[1] * 64 + 1, configSize[0] * 64 + 1),
                                Numeric.uint16)
        i = 0
        for y in range(configSize[1]):
            for x in range(configSize[0]):
                i += 1
                dlgProg.Update(i, "Please wait while loading the region")
                imSmall = im.crop((x * 64, y * 64, x * 64 + 65, y * 64 + 65))
                r = Numeric.frombuffer(imSmall.tobytes(), Numeric.uint8)
                r = Numeric.reshape(r, (64 + 1, 64 + 1, 3))
                red = (r[:, :, 0].astype(Numeric.uint16)
                       * Numeric.array(4096 // 16, Numeric.uint16))
                green = (r[:, :, 1].astype(Numeric.uint16)
                         * Numeric.array(256 // 16, Numeric.uint16))
                blue = r[:, :, 2].astype(Numeric.uint16)
                r = red + green + blue
                heights[y * 64:y * 64 + 65, x * 64:x * 64 + 65] = r
                del red
                del green
                del blue
                del r
                del imSmall

        dlgProg.Close()
        dlgProg.Destroy()
        self.Refresh()
        wx.Yield()

        if fromConfig:
            config = Image.open(configName)
        else:
            config = region.BuildBestConfig(configSize)

        class dlgstub:
            def __init__(self):
                pass

            def Update(self, x, y):
                pass

        try:
            NewRegion = region.SC4Region(None, 250, dlgstub(), config)
            NewRegion.show(dlgstub())
        except AssertionError:
            wx.EndBusyCursor()
            dlg1 = wx.MessageDialog(
                self, configName + ' seems not to be a valid config.bmp',
                'Region creation error', wx.OK | wx.ICON_ERROR)
            dlg1.ShowModal()
            dlg1.Destroy()
            return

        self.regionName = name
        self.region = NewRegion
        self.region.height = heights
        del heights
        self.zoomLevel = 1
        self.zoomLevelPow = 0
        self.back.SetVirtualSize((self.region.height.shape[1],
                                  self.region.height.shape[0]))
        self.SetFocus()
        self.CreateRgnOk()
        self.btnEditMode.Enable(True)
        wx.EndBusyCursor()

    def ExportAsRGB(self, path, config, minX, minY, subRgn):
        if os.path.isfile(path):
            dlg = wx.MessageDialog(
                self, path + " already exist\nOverwrite it ?", "SC4Mapper",
                wx.YES_NO | wx.YES_DEFAULT | wx.ICON_INFORMATION)
            ret = dlg.ShowModal()
            dlg.Destroy()
            if ret == wx.ID_NO:
                return

        wx.BeginBusyCursor()
        im = Image.new("RGB", (config.size[0] * 64 + 1,
                               config.size[1] * 64 + 1))
        dlgProg = wx.ProgressDialog(
            "Exporting as RGB", "Please wait while exporting the region",
            maximum=len(self.region.allCities), parent=self, style=0)
        for i, city in enumerate(self.region.allCities):
            dlgProg.Update(i, "Please wait while exporting the region")
            citySave = region.CityProxy(
                self.region.waterLevel, city.cityXPos - minX,
                city.cityYPos - minY, city.cityXSize, city.cityYSize)
            heightMap = Numeric.zeros((citySave.ySize, citySave.xSize),
                                      Numeric.uint16)
            heightMap[::, ::] = self.region.height[
                citySave.yPos + subRgn[1]:citySave.yPos + subRgn[1] + citySave.ySize,
                citySave.xPos + subRgn[0]:citySave.xPos + subRgn[0] + citySave.xSize]
            red = ((heightMap // Numeric.array(4096, Numeric.uint16))
                   % Numeric.array(16, Numeric.uint16)) * Numeric.array(
                16, Numeric.uint16)
            red = red.astype(Numeric.uint8)
            imRed = Image.frombytes("L", (heightMap.shape[1], heightMap.shape[0]),
                                    red.tobytes())
            green = ((heightMap // Numeric.array(256, Numeric.uint16))
                     % Numeric.array(16, Numeric.uint16)) * Numeric.array(
                16, Numeric.uint16)
            green = green.astype(Numeric.uint8)
            imGreen = Image.frombytes("L", (heightMap.shape[1], heightMap.shape[0]),
                                      green.tobytes())
            blue = heightMap % Numeric.array(256, Numeric.uint16)
            blue = blue.astype(Numeric.uint8)
            imBlue = Image.frombytes("L", (heightMap.shape[1], heightMap.shape[0]),
                                     blue.tobytes())
            imCity = Image.merge("RGB", (imRed, imGreen, imBlue))
            im.paste(imCity, (citySave.xPos, citySave.yPos))
        dlgProg.Close()
        dlgProg.Destroy()
        self.Refresh()
        wx.Yield()

        try:
            im.save(path)
            pathCfg = os.path.splitext(path)[0]
            pathCfg += u"-config.bmp"
            config.save(pathCfg)
        except Exception:
            wx.EndBusyCursor()
            dlg1 = wx.MessageDialog(self, path + " can't be saved",
                                    'Export error', wx.OK | wx.ICON_ERROR)
            dlg1.ShowModal()
            dlg1.Destroy()
            return
        wx.EndBusyCursor()
        wx.CallAfter(self.ShowSuccess, path)

    def ExportAsPNG(self, path, config, minX, minY, subRgn):
        if os.path.isfile(path):
            dlg = wx.MessageDialog(
                self, path + " already exist\nOverwrite it?", "SC4Mapper",
                wx.YES_NO | wx.YES_DEFAULT | wx.ICON_INFORMATION)
            ret = dlg.ShowModal()
            dlg.Destroy()
            if ret == wx.ID_NO:
                return
        wx.BeginBusyCursor()

        im = Image.new("I", (config.size[0] * 64 + 1, config.size[1] * 64 + 1))
        dlgProg = wx.ProgressDialog(
            "Exporting as PNG", "Please wait while exporting the region",
            maximum=len(self.region.allCities), parent=self, style=0)
        for i, city in enumerate(self.region.allCities):
            dlgProg.Update(i, "Please wait while exporting the region")
            citySave = region.CityProxy(
                self.region.waterLevel, city.cityXPos - minX,
                city.cityYPos - minY, city.cityXSize, city.cityYSize)
            heightMap = Numeric.zeros((citySave.ySize, citySave.xSize),
                                      Numeric.uint16)
            heightMap[::, ::] = self.region.height[
                citySave.yPos + subRgn[1]:citySave.yPos + subRgn[1] + citySave.ySize,
                citySave.xPos + subRgn[0]:citySave.xPos + subRgn[0] + citySave.xSize]
            heightMap = heightMap.astype(Numeric.int32)
            imCity = Image.frombytes("I", (heightMap.shape[1],
                                           heightMap.shape[0]),
                                     heightMap.tobytes())
            im.paste(imCity, (citySave.xPos, citySave.yPos))
        dlgProg.Close()
        dlgProg.Destroy()
        self.Refresh()
        wx.Yield()
        try:
            im.save(path)
            pathCfg = os.path.splitext(path)[0]
            pathCfg += u"-config.bmp"
            config.save(pathCfg)
        except Exception:
            wx.EndBusyCursor()
            dlg1 = wx.MessageDialog(self, path + " can't be saved",
                                    'Export error', wx.OK | wx.ICON_ERROR)
            dlg1.ShowModal()
            dlg1.Destroy()
            return
        del im
        wx.EndBusyCursor()
        wx.CallAfter(self.ShowSuccess, path)

    def ShowSuccess(self, path):
        dlg1 = wx.MessageDialog(self, path + ' as been exported',
                                'Export done', wx.OK | wx.ICON_INFORMATION)
        dlg1.ShowModal()
        dlg1.Destroy()

    def ExportAsSC4M(self, path, config, minX, minY, subRgn):
        if os.path.isfile(path):
            dlg = wx.MessageDialog(
                self, path + " already exist\nOverwrite it ?", "SC4Mapper",
                wx.YES_NO | wx.YES_DEFAULT | wx.ICON_INFORMATION)
            ret = dlg.ShowModal()
            dlg.Destroy()
            if ret == wx.ID_NO:
                return

        dlg1 = wx.FileDialog(
            self, message="Enter a valid hml file that will be displayed on "
            "import", defaultDir=self._default_dir(self.settings.import_dir),
            defaultFile="",
            wildcard="HTML files (*.HTML)|*.html", style=wx.FD_OPEN)
        if dlg1.ShowModal() == wx.ID_OK:
            htmlFileName = dlg1.GetPaths()[0]
        else:
            htmlFileName = None
        dlg1.Destroy()

        wx.BeginBusyCursor()

        dlgProg = wx.ProgressDialog(
            "Exporting as SC4M", "Please wait while exporting the region",
            maximum=len(self.region.allCities), parent=self, style=0)
        im1 = Image.new("L", (config.size[0] * 64 + 1, config.size[1] * 64 + 1))
        im2 = Image.new("L", (config.size[0] * 64 + 1, config.size[1] * 64 + 1))
        for i, city in enumerate(self.region.allCities):
            dlgProg.Update(i, "Please wait while exporting the region")
            citySave = region.CityProxy(
                self.region.waterLevel, city.cityXPos - minX,
                city.cityYPos - minY, city.cityXSize, city.cityYSize)
            heightMap = Numeric.zeros((citySave.ySize, citySave.xSize),
                                      Numeric.uint16)
            heightMap[::, ::] = self.region.height[
                citySave.yPos + subRgn[1]:citySave.yPos + subRgn[1] + citySave.ySize,
                citySave.xPos + subRgn[0]:citySave.xPos + subRgn[0] + citySave.xSize]
            heightMap = heightMap.astype(Numeric.int32)
            imCity = Image.frombytes("RGBA", (heightMap.shape[1],
                                              heightMap.shape[0]),
                                     heightMap.tobytes())
            imCity1, imCity2 = imCity.split()[:2]
            im1.paste(imCity1, (citySave.xPos, citySave.yPos))
            im2.paste(imCity2, (citySave.xPos, citySave.yPos))
        dlgProg.Close()
        dlgProg.Destroy()
        self.Refresh()
        wx.Yield()

        s = b"SC4M"
        s += struct.pack("<I", 0x0200)
        s += struct.pack("<I", im1.size[1])
        s += struct.pack("<I", im1.size[0])
        s += struct.pack("<f", 0)
        if htmlFileName is not None and os.path.isfile(htmlFileName):
            s += b"SC4N"   # author notes
            filehtml = open(htmlFileName, "rb")
            lines = filehtml.readlines()
            line = b"\n".join(lines)
            filehtml.close()
            s += struct.pack("<I", len(line))
            s += line
        s += b"SC4C"       # config.bmp included
        s += struct.pack("<I", config.size[0])
        s += struct.pack("<I", config.size[1])
        configStr = config.tobytes()
        s += struct.pack("<I", len(configStr))
        s += configStr
        s += b"SC4D"       # elevation data
        try:
            encoder = zlib.compressobj(9)
            raw = open(path, "wb")
            raw.write(encoder.compress(s))
            raw.write(encoder.compress(im1.tobytes()))
            del im1
            raw.write(encoder.compress(im2.tobytes()))
            del im2
            raw.write(encoder.flush())
            raw.close()
            pathCfg = os.path.splitext(path)[0]
            pathCfg += u"-config.bmp"
            config.save(pathCfg)
        except Exception:
            wx.EndBusyCursor()
            raise
        wx.EndBusyCursor()
        wx.CallAfter(self.ShowSuccess, path)

    def ExportRgn(self, event):
        dlg = wx.FileDialog(
            self, message="Export region as ...",
            defaultDir=self._default_dir(self.settings.export_dir),
            defaultFile=self.regionName,
            wildcard="SC4 Terrain files (*.SC4M)|*.SC4M"
                     "|16bit png files (*.png)|*.png|RGB files (*.bmp)|*.bmp",
            style=wx.FD_SAVE)
        if dlg.ShowModal() == wx.ID_OK:
            path = dlg.GetPath()
            dlg.Destroy()
            ext = os.path.splitext(path)[1].upper()
            minX, minY, maxX, maxY, sizeX, sizeY, config = self.region.CropConfig()
            subRgn = [minX * 64 + self.back.offX, minY * 64 + self.back.offY,
                      maxX * 64 + 1 + self.back.offX,
                      maxY * 64 + 1 + self.back.offY]

            if ext == ".SC4M":
                self.ExportAsSC4M(path, config, minX, minY, subRgn)
            if ext == ".BMP":
                self.ExportAsRGB(path, config, minX, minY, subRgn)
            if ext == ".PNG":
                self.ExportAsPNG(path, config, minX, minY, subRgn)
        else:
            dlg.Destroy()
        self.Refresh(False)

    def SaveRgn(self, event):
        dlg = wx.TextEntryDialog(self, 'Enter the name of the new region',
                                 'Region name', self.regionName)
        if dlg.ShowModal() == wx.ID_OK:
            name = dlg.GetValue()
            dlg.Destroy()
        else:
            dlg.Destroy()
            return None

        path = os.path.join(self.mydocs, name)

        try:
            os.makedirs(path)
        except FileExistsError:
            dlg = wx.MessageDialog(
                self, 'A region with this name already exists or at least '
                'the region folder already exists\nDo you want to save '
                'anyway (removing previous region)?',
                'Warning', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_INFORMATION)
            ret = dlg.ShowModal()
            dlg.Destroy()
            if ret == wx.ID_NO:
                return
            try:
                allfiles = sorted(os.listdir(path))
                valid = ['.SC4', '.INI', '.BMP', '.PNG']
                allfiles = [f for f in allfiles
                            if os.path.splitext(f)[1].upper() in valid]
                for fname in allfiles:
                    os.unlink(os.path.join(path, fname))
            except OSError:
                dlg = wx.MessageDialog(
                    self, 'A problem has occured while cleaning the region '
                    'folder\nYou may try to clean it yourself',
                    'Error while saving region', wx.OK | wx.ICON_ERROR)
                dlg.ShowModal()
                dlg.Destroy()
                return
        except OSError:
            dlg = wx.MessageDialog(
                self, 'A problem has occured while creating the region '
                'folder\nYou should enter a valid folder name as region name',
                'Error while saving region', wx.OK | wx.ICON_ERROR)
            dlg.ShowModal()
            dlg.Destroy()
            return

        wx.BeginBusyCursor()
        self.region.folder = path
        dlg1 = wx.ProgressDialog(
            "Saving region", "Please wait while saving the region",
            maximum=len(self.region.allCities), parent=self, style=0)
        minX, minY, maxX, maxY, sizeX, sizeY, config = self.region.CropConfig()
        subRgn = [minX * 64 + self.back.offX, minY * 64 + self.back.offY,
                  maxX * 64 + 1 + self.back.offX,
                  maxY * 64 + 1 + self.back.offY]
        config.save(os.path.join(path, "config.bmp"))
        try:
            saved = self.region.Save(dlg1, minX, minY, subRgn)
        except Exception:
            saved = False
        wx.EndBusyCursor()
        dlg1.Close()
        dlg1.Destroy()
        if saved is False:
            dlg = wx.MessageDialog(
                self, 'A problem has occured while saving the cities files\n'
                'Some or all of the cities might not have been saved correctly',
                'Error while saving region', wx.OK | wx.ICON_ERROR)
            dlg.ShowModal()
            dlg.Destroy()
            return
        self.regionName = name

    def OpenRgn(self, event):
        self.btnEditMode.Enable(False)
        try:
            r = self.LoadARegion()
        except Exception:
            r = None
            dlg = wx.MessageDialog(
                self, 'A problem has occured while reading the region\n'
                'Maybe it is too large for your RAM',
                'Error while loading region', wx.OK | wx.ICON_ERROR)
            dlg.ShowModal()
            dlg.Destroy()

        if r is None:
            return

        self.btnEditMode.Enable(False)
        self.btnSave.Enable(True)
        self.btnExportRgn.Enable(True)
        self.btnSaveRgn.Enable(True)
        self.btnZoomIn.Enable(False)
        self.btnZoomOut.Enable(True)
        self.overlayCbx.Enable(True)
        self.back.offX = 0
        self.back.offY = 0

        self.region = r
        self.zoomLevel = 1
        self.zoomLevelPow = 0
        self.btnEditMode.Enable(True)
        self.back.SetVirtualSize((self.region.height.shape[1],
                                  self.region.height.shape[0]))

        self.SetFocus()
        self.SetTitle("NHP SC4Mapper %s Version - " % MAPPER_VERSION
                      + self.regionName)
        self.back.OnSize(None)

    def LoadARegion(self):
        dlg = wx.DirDialog(self, "Choose a directory:",
                           defaultPath=self._default_dir(self.mydocs),
                           style=wx.DEFAULT_DIALOG_STYLE | wx.DD_DIR_MUST_EXIST)
        if dlg.ShowModal() == wx.ID_OK:
            self.regionPath = dlg.GetPath()
        else:
            dlg.Destroy()
            return None

        if not os.path.isdir(self.regionPath):
            self.regionPath = os.path.split(self.regionPath)[0]
        dlg.Destroy()
        self.waterLevel = 250

        wx.BeginBusyCursor()
        dlg = wx.ProgressDialog(
            "Loading region", "Please wait while loading the region",
            maximum=6, parent=self, style=0)

        try:
            dlg.Update(0)
            NewRegion = region.SC4Region(self.regionPath, self.waterLevel,
                                            dlg)
            if NewRegion.allCities is None:
                wx.EndBusyCursor()
                dlg.Close()
                dlg.Destroy()
                dlg = wx.MessageDialog(self, 'No cities found',
                                       'Error while loading region',
                                       wx.OK | wx.ICON_ERROR)
                dlg.ShowModal()
                dlg.Destroy()
                return None
            NewRegion.show(dlg, True)
            dlg.Close()
            dlg.Destroy()

            if not NewRegion.IsValid():
                wx.EndBusyCursor()
                dlg = wx.MessageDialog(
                    self, 'This folder seems not to be a valid region',
                    'Error while loading region', wx.OK | wx.ICON_ERROR)
                dlg.ShowModal()
                dlg.Destroy()
                return None

            if NewRegion.IsValid() and NewRegion.config is None:
                wx.EndBusyCursor()
                dlg = wx.MessageDialog(
                    self, "There isn't any config.bmp",
                    'Warning while loading region',
                    wx.OK | wx.ICON_INFORMATION)
                dlg.ShowModal()
                dlg.Destroy()
            wx.EndBusyCursor()
            self.regionName = os.path.splitext(
                os.path.split(self.regionPath)[1])[0]
            return NewRegion
        except Exception:
            wx.EndBusyCursor()
            dlg.Destroy()
            raise


class SplashScreen(wx.adv.SplashScreen):
    def __init__(self):
        with asset_path("splash.jpg") as path:
            bmp = wx.Image(str(path), wx.BITMAP_TYPE_JPEG).ConvertToBitmap()
        wx.adv.SplashScreen.__init__(
            self, bmp,
            wx.adv.SPLASH_CENTRE_ON_SCREEN | wx.adv.SPLASH_TIMEOUT,
            1000, None, -1)
        self.Bind(wx.EVT_CLOSE, self.OnClose)

    def OnClose(self, evt):
        evt.Skip()
        self.Hide()
        self.ShowMain()

    def ShowMain(self):
        frame = OverView(None, "NHP SC4Mapper %s Version" % MAPPER_VERSION,
                         (100, 100))
        frame.Show()


class SC4App(wx.App):
    def OnInit(self):
        splash = SplashScreen()
        splash.Show()
        return True


def main():
    app = SC4App(False)
    app.MainLoop()


if __name__ == "__main__":
    main()
