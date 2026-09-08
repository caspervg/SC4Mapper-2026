#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SC4Mapper - SimCity 4 region import/export tool (main wxPython application)."""

import os
import os.path
import queue
import struct
import sys
import threading
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
from . import png16
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


class ImportCancelled(Exception):
    """Raised when the user cancels a geographic import."""


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
    """Compact location picker with one canonical request and async preview."""

    PREVIEW_SIZE = (560, 420)
    SIZE_CHOICES = [("2 × 2 large-city areas", (8, 8)),
                    ("4 × 4 large-city areas", (16, 16)),
                    ("8 × 8 large-city areas", (32, 32)),
                    ("Custom small-tile dimensions", None)]
    CITY_CHOICES = [("Large cities", 4), ("Medium cities", 2),
                    ("Small cities", 1)]
    VERTICAL_CHOICES = [("Keep natural proportions", "match"),
                        ("Keep real elevation differences", "true"),
                        ("Custom height multiplier", "manual")]
    DATUM_CHOICES = [("Sea level", "sea"), ("Dry land", "lowest"),
                     ("Custom level", "manual")]
    WATER_SOURCE_CHOICES = [("Elevation", "elevation"),
                            ("Mapped water + elevation", "both"),
                            ("Mapped water only", "mask")]
    WATER_CHOICES = [("Sea level", "sea", "elevation"),
                     ("Dry land", "lowest", "elevation"),
                     ("Lake / custom level", "manual", "elevation"),
                     ("Mapped water only", "manual", "mask"),
                     ("Custom…", None, None)]

    def __init__(self, parent, settings, state=None):
        wx.Dialog.__init__(self, parent, -1, "Import real-world region",
                           style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.settings = settings
        self.places = []
        self._closed = False
        self._syncing = False
        self._location_pending = False
        self._committed_name = "Amsterdam, Netherlands"
        self._search_revision = 0
        self._preview_revision = 0
        self._preview_active = None
        self._preview_pending = None
        self._preview_timer = None
        self._previewReady = False
        self._preview_request = None
        self._preview_extent = None
        self._preview_image_size = self.PREVIEW_SIZE

        settingsPanel = wx.ScrolledWindow(self, style=wx.VSCROLL)
        settingsPanel.SetScrollRate(0, 10)
        left = wx.BoxSizer(wx.VERTICAL)

        searchRow = wx.BoxSizer(wx.HORIZONTAL)
        self.search = wx.TextCtrl(settingsPanel, -1, "Amsterdam, Netherlands",
                                  style=wx.TE_PROCESS_ENTER)
        self.btnSearch = wx.Button(settingsPanel, -1, "Find")
        searchRow.Add(self.search, 1, wx.EXPAND | wx.ALL, 3)
        searchRow.Add(self.btnSearch, 0, wx.ALL, 3)
        left.Add(wx.StaticText(settingsPanel, label="Place or coordinates"),
                 0, wx.LEFT | wx.TOP, 5)
        left.Add(searchRow, 0, wx.EXPAND)
        left.Add(wx.StaticText(
            settingsPanel, label="Paste a coordinate pair or supported map link; "
            "place names use OpenStreetMap search."), 0, wx.LEFT | wx.BOTTOM, 5)
        self.results = wx.ListBox(settingsPanel, -1)
        self.results.Hide()
        left.Add(self.results, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 3)
        self.placeDetails = wx.StaticText(
            settingsPanel, label="Amsterdam, Netherlands · 52.367600, 4.904100")
        left.Add(self.placeDetails, 0, wx.EXPAND | wx.ALL, 5)
        self.editCoordinates = wx.Button(settingsPanel, -1, "Edit coordinates")
        left.Add(self.editCoordinates, 0, wx.LEFT | wx.BOTTOM, 3)
        self.coordinatePanel = wx.Panel(settingsPanel)
        coords = wx.BoxSizer(wx.HORIZONTAL)
        self.lat = wx.TextCtrl(self.coordinatePanel, -1, "52.367600")
        self.lon = wx.TextCtrl(self.coordinatePanel, -1, "4.904100")
        coords.Add(wx.StaticText(self.coordinatePanel, label="Latitude"), 0,
                   wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        coords.Add(self.lat, 1, wx.RIGHT, 8)
        coords.Add(wx.StaticText(self.coordinatePanel, label="Longitude"), 0,
                   wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        coords.Add(self.lon, 1)
        self.coordinatePanel.SetSizer(coords)
        self.coordinatePanel.Hide()
        left.Add(self.coordinatePanel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 3)

        left.Add(wx.StaticLine(settingsPanel), 0, wx.EXPAND | wx.ALL, 5)
        left.Add(wx.StaticText(settingsPanel, label="Region size"), 0,
                 wx.LEFT | wx.TOP, 5)
        self.sizePreset = wx.Choice(settingsPanel, choices=[label for label, _ in self.SIZE_CHOICES])
        self.sizePreset.SetSelection(0)
        left.Add(self.sizePreset, 0, wx.EXPAND | wx.ALL, 3)
        self.customSizePanel = wx.Panel(settingsPanel)
        custom = wx.BoxSizer(wx.HORIZONTAL)
        self.sizeX = wx.TextCtrl(self.customSizePanel, -1, "8")
        self.sizeY = wx.TextCtrl(self.customSizePanel, -1, "8")
        custom.Add(wx.StaticText(self.customSizePanel, label="Small tiles"), 0,
                   wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        custom.Add(self.sizeX, 1, wx.RIGHT, 4)
        custom.Add(wx.StaticText(self.customSizePanel, label="×"), 0,
                   wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        custom.Add(self.sizeY, 1)
        self.customSizePanel.SetSizer(custom)
        self.customSizePanel.Hide()
        left.Add(self.customSizePanel, 0, wx.EXPAND | wx.ALL, 3)
        widthRow = wx.BoxSizer(wx.HORIZONTAL)
        self.areaWidth = wx.TextCtrl(settingsPanel, -1, "8.192")
        self.btnActual = wx.Button(settingsPanel, -1, "Actual size")
        widthRow.Add(wx.StaticText(settingsPanel, label="Area width"), 0,
                     wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        widthRow.Add(self.areaWidth, 1, wx.RIGHT, 4)
        widthRow.Add(wx.StaticText(settingsPanel, label="km"), 0,
                     wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        widthRow.Add(self.btnActual, 0)
        left.Add(widthRow, 0, wx.EXPAND | wx.ALL, 3)
        self.footprint = wx.StaticText(settingsPanel, label=" ")
        left.Add(self.footprint, 0, wx.EXPAND | wx.ALL, 5)

        left.Add(wx.StaticText(settingsPanel, label="Water"), 0,
                 wx.LEFT | wx.TOP, 5)
        self.waterChoice = wx.Choice(settingsPanel,
                                     choices=[label for label, _, _ in self.WATER_CHOICES])
        self.waterChoice.SetSelection(0)
        left.Add(self.waterChoice, 0, wx.EXPAND | wx.ALL, 3)
        self.waterNote = wx.StaticText(settingsPanel, label=" ")
        left.Add(self.waterNote, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)
        self.waterLevelPanel = wx.Panel(settingsPanel)
        waterLevel = wx.BoxSizer(wx.HORIZONTAL)
        waterLevel.Add(wx.StaticText(self.waterLevelPanel, label="Water level (m)"),
                       0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self.datum = wx.TextCtrl(self.waterLevelPanel, -1, "0")
        waterLevel.Add(self.datum, 1)
        self.waterLevelPanel.SetSizer(waterLevel)
        self.waterLevelPanel.Hide()
        left.Add(self.waterLevelPanel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 3)

        self.advanced = wx.CollapsiblePane(settingsPanel, label="Advanced settings")
        advancedPane = self.advanced.GetPane()
        advanced = wx.BoxSizer(wx.VERTICAL)
        self.rotation = wx.TextCtrl(advancedPane, -1, "0")
        self.citySize = wx.Choice(advancedPane,
                                   choices=[label for label, _ in self.CITY_CHOICES])
        self.citySize.SetSelection(0)
        self.metres = wx.TextCtrl(advancedPane, -1, "16")
        self.verticalMode = wx.Choice(advancedPane,
                                      choices=[label for label, _ in self.VERTICAL_CHOICES])
        self.verticalMode.SetSelection(0)
        self.vertical = wx.TextCtrl(advancedPane, -1, "1.0")
        self.datumMode = wx.Choice(advancedPane,
                                   choices=[label for label, _ in self.DATUM_CHOICES])
        self.datumMode.SetSelection(0)
        self.waterSource = wx.Choice(advancedPane,
                                     choices=[label for label, _ in self.WATER_SOURCE_CHOICES])
        self.waterSource.SetSelection(0)
        self.minWaterArea = wx.TextCtrl(advancedPane, -1, "64")
        self.maxWaterRise = wx.TextCtrl(advancedPane, -1, "30")
        self.waterDepth = wx.TextCtrl(advancedPane, -1, "3")
        self.flatten = wx.CheckBox(advancedPane, label="Flatten underwater terrain")
        self.flatten.SetValue(True)
        self.underlay = wx.CheckBox(advancedPane, label="Download a map underlay for the region view")
        hasBasemap = bool(getattr(settings, "basemap_url", ""))
        self.underlay.SetValue(hasBasemap)
        self.underlay.Enable(hasBasemap)
        for label, control in (("Rotation (degrees)", self.rotation),
                               ("Initial city layout", self.citySize),
                               ("Metres per cell", self.metres),
                               ("Heights", self.verticalMode),
                               ("Custom multiplier", self.vertical),
                               ("Shoreline datum", self.datumMode),
                               ("Mapped water", self.waterSource),
                               ("Minimum mapped area (cells)", self.minWaterArea),
                               ("Maximum rise (m)", self.maxWaterRise),
                               ("Water depth (m)", self.waterDepth)):
            row = wx.BoxSizer(wx.HORIZONTAL)
            row.Add(wx.StaticText(advancedPane, label=label), 0,
                    wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
            row.Add(control, 1)
            advanced.Add(row, 0, wx.EXPAND | wx.BOTTOM, 4)
        advanced.Add(self.flatten, 0, wx.BOTTOM, 4)
        advanced.Add(self.underlay, 0, wx.BOTTOM, 4)
        self.advancedSummary = wx.StaticText(advancedPane, label=" ")
        advanced.Add(self.advancedSummary, 0, wx.EXPAND | wx.BOTTOM, 4)
        advancedPane.SetSizer(advanced)
        left.Add(self.advanced, 0, wx.EXPAND | wx.ALL, 5)

        settingsPanel.SetSizer(left)

        blank = wx.Image(*self.PREVIEW_SIZE)
        blank.SetRGB(wx.Rect(0, 0, *self.PREVIEW_SIZE), 235, 238, 240)
        self.previewBitmap = wx.StaticBitmap(self, bitmap=wx.Bitmap(blank))
        self.btnPreview = wx.Button(self, -1, "Refresh / Retry")
        self.previewNote = wx.StaticText(
            self, label="Footprint only; water and height settings apply during import.")
        previewBox = wx.StaticBox(self, -1, "Footprint preview")
        previewSizer = wx.StaticBoxSizer(previewBox, wx.VERTICAL)
        previewSizer.Add(self.previewBitmap, 1, wx.EXPAND | wx.ALL, 5)
        previewSizer.Add(self.btnPreview, 0, wx.ALIGN_LEFT | wx.ALL, 5)
        previewSizer.Add(self.previewNote, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        body = wx.BoxSizer(wx.HORIZONTAL)
        body.Add(settingsPanel, 0, wx.EXPAND | wx.ALL, 5)
        body.Add(previewSizer, 1, wx.EXPAND | wx.ALL, 5)

        buttons = wx.StdDialogButtonSizer()
        self.btnOk = wx.Button(self, wx.ID_OK, "Import region")
        self.btnOk.SetDefault()
        buttons.AddButton(self.btnOk)
        buttons.AddButton(wx.Button(self, wx.ID_CANCEL, "Cancel"))
        buttons.Realize()
        root = wx.BoxSizer(wx.VERTICAL)
        root.Add(body, 1, wx.EXPAND)
        root.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 5)
        root.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, 7)
        self.SetSizer(root)
        self.SetSize((1080, 720))
        self.SetMinSize((760, 520))

        self.btnSearch.Bind(wx.EVT_BUTTON, self.OnSearch)
        self.search.Bind(wx.EVT_TEXT_ENTER, self.OnSearch)
        self.search.Bind(wx.EVT_TEXT, self.OnSearchText)
        self.results.Bind(wx.EVT_LISTBOX, self.OnPickPlace)
        self.editCoordinates.Bind(wx.EVT_BUTTON, self.OnEditCoordinates)
        self.sizePreset.Bind(wx.EVT_CHOICE, self.OnSizePreset)
        self.sizeX.Bind(wx.EVT_TEXT, self.OnCustomSize)
        self.sizeY.Bind(wx.EVT_TEXT, self.OnCustomSize)
        self.areaWidth.Bind(wx.EVT_TEXT, self.OnAreaWidth)
        self.btnActual.Bind(wx.EVT_BUTTON, self.OnActualSize)
        self.metres.Bind(wx.EVT_TEXT, self.OnMetres)
        self.rotation.Bind(wx.EVT_TEXT, self.OnFootprintEdited)
        self.lat.Bind(wx.EVT_TEXT, self.OnFootprintEdited)
        self.lon.Bind(wx.EVT_TEXT, self.OnFootprintEdited)
        self.citySize.Bind(wx.EVT_CHOICE, self.OnLayoutChanged)
        self.verticalMode.Bind(wx.EVT_CHOICE, self.OnAdvancedChanged)
        self.datumMode.Bind(wx.EVT_CHOICE, self.OnAdvancedChanged)
        self.waterSource.Bind(wx.EVT_CHOICE, self.OnAdvancedChanged)
        self.advanced.Bind(wx.EVT_COLLAPSIBLEPANE_CHANGED, self.OnAdvancedPane)
        self.waterChoice.Bind(wx.EVT_CHOICE, self.OnWaterPreset)
        self.btnPreview.Bind(wx.EVT_BUTTON, self.OnPreview)
        self.previewBitmap.Bind(wx.EVT_LEFT_UP, self.OnPreviewClick)
        self.btnOk.Bind(wx.EVT_BUTTON, self.OnImport)
        self.Bind(wx.EVT_CLOSE, self.OnCloseWindow)

        if state:
            self._load_state(state)
        self._update_controls()
        self._update_footprint()
        self.Centre()
        wx.CallAfter(self._schedule_preview, 0)
        self.search.SetFocus()

    def _float(self, control, label, default=None):
        text = control.GetValue().strip().replace(",", ".")
        if not text and default is not None:
            return default
        try:
            value = float(text)
        except (TypeError, ValueError):
            raise ValueError("%s must be a number" % label)
        if not __import__("math").isfinite(value):
            raise ValueError("%s must be finite" % label)
        return value

    def _set_choice(self, control, values, value):
        for index, item in enumerate(values):
            if item[1] == value:
                control.SetSelection(index)
                return

    def _load_state(self, state):
        request = state["request"]
        self.search.SetValue(state.get("name", "Custom centre"))
        self._committed_name = state.get("name", "Custom centre")
        self.SetLatLon(request.center_lat, request.center_lon)
        self._set_choice(self.citySize, self.CITY_CHOICES, state.get("city_size", 4))
        self._set_choice(self.waterSource, self.WATER_SOURCE_CHOICES, request.water_source)
        self._set_choice(self.datumMode, self.DATUM_CHOICES, request.water_datum_mode)
        self._set_choice(self.verticalMode, self.VERTICAL_CHOICES, request.vertical_mode)
        self.rotation.SetValue(str(request.rotation_deg))
        self.metres.SetValue(str(request.metres_per_cell))
        self.vertical.SetValue(str(request.vertical_scale))
        self.datum.SetValue(str(request.sea_reference_m))
        self.minWaterArea.SetValue(str(request.min_water_area_cells))
        self.maxWaterRise.SetValue(str(request.max_water_rise_m))
        self.waterDepth.SetValue(str(request.water_depth_m))
        self.flatten.SetValue(not request.keep_bathymetry)
        self.underlay.SetValue(bool(state.get("underlay", False)))
        self.sizePreset.SetSelection(3)
        self.sizeX.SetValue(str(request.tiles_x))
        self.sizeY.SetValue(str(request.tiles_y))
        self._set_area_from_metres()
        self._location_pending = False

    def GetState(self):
        return {"request": self.GetRequest(), "city_size": self.GetCitySize(),
                "underlay": self.WantsUnderlay(), "name": self.GetLocationName()}

    def _set_area_from_metres(self):
        try:
            metres = self._float(self.metres, "Metres per cell")
            x = int(self.sizeX.GetValue())
        except (ValueError, TypeError):
            return
        self._syncing = True
        try:
            self.areaWidth.SetValue("%.12g" % (x * geo.CELLS_PER_TILE * metres / 1000.0))
        finally:
            self._syncing = False

    def _set_metres_from_area(self):
        try:
            width = self._float(self.areaWidth, "Area width")
            x = int(self.sizeX.GetValue())
            if width <= 0 or x < 1:
                return
        except (ValueError, TypeError):
            return
        self._syncing = True
        try:
            self.metres.SetValue("%.12g" % (width * 1000.0 / (x * geo.CELLS_PER_TILE)))
        finally:
            self._syncing = False

    def _dimensions(self):
        if self.sizePreset.GetSelection() < 3:
            return self.SIZE_CHOICES[self.sizePreset.GetSelection()][1]
        try:
            return int(self.sizeX.GetValue()), int(self.sizeY.GetValue())
        except (ValueError, TypeError):
            return None

    def _update_footprint(self):
        dimensions = self._dimensions()
        try:
            metres = self._float(self.metres, "Metres per cell")
        except ValueError:
            self.footprint.SetLabel("Enter a valid area width or spacing.")
            return
        if not dimensions or min(dimensions) < 1 or metres <= 0:
            self.footprint.SetLabel("Enter positive small-tile dimensions.")
            return
        width = dimensions[0] * geo.CELLS_PER_TILE * metres / 1000.0
        height = dimensions[1] * geo.CELLS_PER_TILE * metres / 1000.0
        try:
            counts = geo.describe_layout(dimensions, self.GetCitySize())
            cities = ", ".join("%d %s" % (counts[size], geo.CITY_SIZE_NAMES[size].lower())
                                for size in (4, 2, 1) if counts.get(size))
        except geo.GeoImportError:
            cities = "layout unavailable"
        self.footprint.SetLabel("%.3f × %.3f km · %s" % (width, height, cities))
        self._update_controls()
        self.advancedSummary.SetLabel(self._advanced_summary())
        self.Layout()

    def _advanced_summary(self):
        parts = []
        try:
            rotation = self._float(self.rotation, "Rotation")
            if rotation:
                parts.append("Rotation %.3g°" % rotation)
            if self.GetVerticalMode() == "manual":
                parts.append("Custom heights %sx" % self._float(self.vertical, "Multiplier"))
            if self.GetWaterSource() != "elevation":
                parts.append("Mapped water")
            if self.WantsUnderlay():
                parts.append("Map underlay")
        except ValueError:
            parts.append("Check advanced values")
        return " · ".join(parts) or "Defaults"

    def _update_controls(self):
        self.customSizePanel.Show(self.sizePreset.GetSelection() == 3)
        self.datum.Enable(self.GetDatumMode() == "manual")
        self.waterLevelPanel.Show(self.GetDatumMode() == "manual")
        self.vertical.Enable(self.GetVerticalMode() == "manual")
        mapped = self.GetWaterSource() != "elevation"
        for control in (self.minWaterArea, self.maxWaterRise, self.waterDepth):
            control.Enable(mapped)
        notes = {
            "elevation": "Sea level uses elevation; inland lakes need a custom level.",
            "both": "Mapped water is added to elevation flooding.",
            "mask": "Only mapped water is wet; low unmapped ground is raised.",
        }
        self.waterNote.SetLabel(notes[self.GetWaterSource()])

    def _schedule_preview(self, delay=500):
        if self._closed:
            return
        self._preview_revision += 1
        if self._preview_timer:
            self._preview_timer.Stop()
        # wx's macOS timer rejects a zero-millisecond timeout.  A one-ms
        # delay still gives the event loop a chance to coalesce edits.
        self._preview_timer = wx.CallLater(max(1, int(delay)), self._queue_preview)

    def _queue_preview(self):
        if self._closed:
            return
        try:
            request = self.GetRequest()
        except ValueError:
            return
        snapshot = (self._preview_revision, request, self.GetCitySize())
        self.MarkPreviewStale()
        if self._preview_active:
            self._preview_pending = snapshot
            self._preview_active[1].set()
            return
        self._start_preview(snapshot)

    def _make_preview_fetcher(self):
        cacheDir = getattr(self.settings, "tile_cache_dir", "") or None
        basemap = getattr(self.settings, "basemap_url", "").strip()
        if basemap:
            return (geo.HttpTileFetcher(
                url_template=basemap,
                cache_dir=os.path.join(cacheDir, "basemap") if cacheDir else None,
                timeout=12,
                attribution=getattr(self.settings, "basemap_attribution", "") or None),
                    True, getattr(self.settings, "basemap_attribution", "")
                    or "Map preview: %s" % basemap)
        elevation = getattr(self.settings, "elevation_url", "") or geo.DEFAULT_TILE_URL
        return (geo.HttpTileFetcher(
            url_template=elevation,
            cache_dir=os.path.join(cacheDir, "elevation") if cacheDir else None,
            timeout=12,
            attribution=getattr(self.settings, "elevation_attribution", "") or None),
                False, "Elevation hillshade preview")

    def _start_preview(self, snapshot):
        revision, request, city_size = snapshot
        cancel = threading.Event()
        self._preview_active = (revision, cancel)
        fetcher, imagery, attribution = self._make_preview_fetcher()
        self.previewNote.SetLabel("Updating preview…")

        def progress(done, total, message):
            if cancel.is_set():
                raise ImportCancelled()

        def worker():
            try:
                image, zoom, fetched, missing = geo.build_footprint_preview(
                    request, fetcher, imagery=imagery, size=self.PREVIEW_SIZE,
                    city_size=city_size, progress=progress)
                result = (image, zoom, fetched, missing, attribution)
                error = None
            except Exception as exc:
                result, error = None, exc
            wx.CallAfter(self._finish_preview, revision, result, error)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_preview(self, revision, result, error):
        if self._closed:
            return
        active = self._preview_active
        if not active:
            return
        if active[0] != revision:
            self._preview_active = None
            pending = self._preview_pending
            self._preview_pending = None
            if pending and not self._closed:
                self._start_preview(pending)
            return
        self._preview_active = None
        if error is None and result is not None:
            image, zoom, fetched, missing, attribution = result
            wxImage = wx.Image(image.width, image.height)
            wxImage.SetData(image.tobytes())
            self.previewBitmap.SetBitmap(wx.Bitmap(wxImage))
            note = "%s · zoom %d · %d tile(s)" % (attribution, zoom, fetched)
            if missing:
                note += ", %d missing" % missing
            self.previewNote.SetLabel(note)
            self.previewNote.Wrap(self.PREVIEW_SIZE[0])
            self._previewReady = True
            self._preview_request = self.GetRequest()
            self._preview_extent = geo.footprint_preview_extent(
                self._preview_request, self.PREVIEW_SIZE)
            self.Layout()
        elif not isinstance(error, ImportCancelled):
            self.previewNote.SetLabel("Preview unavailable: %s" % error)
        pending = self._preview_pending
        self._preview_pending = None
        if pending and not self._closed:
            self._start_preview(pending)

    def MarkPreviewStale(self):
        self._previewReady = False
        if self._preview_request:
            self.previewNote.SetLabel("Updating preview…")

    def OnPreview(self, event):
        self._schedule_preview(0)

    def OnPreviewClick(self, event):
        if not self._previewReady or not self._preview_extent:
            return
        width, height = self.previewBitmap.GetClientSize()
        image_width, image_height = self._preview_image_size
        pad_x = max(0, (width - image_width) / 2.0)
        pad_y = max(0, (height - image_height) / 2.0)
        x = (event.GetX() - pad_x) / max(1.0, image_width - 1)
        y = (event.GetY() - pad_y) / max(1.0, image_height - 1)
        if not 0 <= x <= 1 or not 0 <= y <= 1:
            return
        west, east, south, north = self._preview_extent[:4]
        east_m = west + x * (east - west)
        north_m = north - y * (north - south)
        lat, lon = geo.local_offsets_to_lonlat(
            self._preview_request.center_lat, self._preview_request.center_lon,
            east_m, north_m)
        self.SetLatLon(float(lat), float(lon), "Custom centre")
        self._schedule_preview()

    def OnSearchText(self, event):
        if self._syncing:
            return
        self._search_revision += 1
        text = self.search.GetValue().strip()
        located = geo.parse_location(text)
        if located:
            self._commit_location(*located, name="Coordinates entered directly")
            self.btnSearch.Enable(True)
            self.results.Clear()
            self.results.Hide()
        elif text and text != self._committed_name:
            self._location_pending = True
            self.placeDetails.SetLabel("Location not yet found — press Enter or Find.")
        self.Layout()

    def OnSearch(self, event):
        text = self.search.GetValue().strip()
        if not text:
            self._show_error(self.search, "Enter a place or coordinates.")
            return
        located = geo.parse_location(text)
        if located:
            self._commit_location(*located, name="Coordinates entered directly")
            self._schedule_preview()
            return
        revision = self._search_revision
        self.btnSearch.Enable(False)
        wx.BeginBusyCursor()

        def worker():
            try:
                places = geo.geocode(text)
                error = None
            except Exception as exc:
                places, error = [], exc
            wx.CallAfter(self._finish_search, revision, text, places, error)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_search(self, revision, text, places, error):
        try:
            if self._closed or revision != self._search_revision:
                return
            self.btnSearch.Enable(True)
            self.results.Clear()
            self.places = places
            if error:
                self.placeDetails.SetLabel("Search failed: %s" % error)
            elif not places:
                self.results.Hide()
                self.placeDetails.SetLabel("No results for %r." % text)
            else:
                for place in places:
                    self.results.Append(place.label)
                self.results.Show()
                self.results.SetSelection(0)
                self.SelectPlace(0)
            self.Layout()
        finally:
            wx.EndBusyCursor()

    def OnPickPlace(self, event):
        index = self.results.GetSelection()
        if 0 <= index < len(self.places):
            self.SelectPlace(index)

    def SelectPlace(self, index):
        place = self.places[index]
        self._commit_location(place.lat, place.lon, place.name,
                              place.details() or "Location point")
        self._schedule_preview()

    def _commit_location(self, lat, lon, name, details=None):
        self.SetLatLon(lat, lon, name)
        self._location_pending = False
        self._committed_name = name
        self.placeDetails.SetLabel(
            "%s · %.6f, %.6f" % (details or name, lat, lon))

    def SetLatLon(self, lat, lon, name=None):
        self._syncing = True
        try:
            self.lat.SetValue("%.6f" % lat)
            self.lon.SetValue("%.6f" % lon)
        finally:
            self._syncing = False
        if name:
            self._committed_name = name

    def OnEditCoordinates(self, event):
        self.coordinatePanel.Show(not self.coordinatePanel.IsShown())
        self.Layout()

    def OnSizePreset(self, event):
        try:
            old = self._float(self.metres, "Metres per cell", 16.0)
        except ValueError:
            old = 16.0
        if self.sizePreset.GetSelection() < 3:
            x, y = self.SIZE_CHOICES[self.sizePreset.GetSelection()][1]
            self.sizeX.SetValue(str(x))
            self.sizeY.SetValue(str(y))
        self._syncing = True
        try:
            self.metres.SetValue("%.12g" % old)
        finally:
            self._syncing = False
        self._set_area_from_metres()
        self._update_footprint()
        self._schedule_preview()

    def OnCustomSize(self, event):
        if not self._syncing and self.sizePreset.GetSelection() == 3:
            self._set_area_from_metres()
            self._update_footprint()
            self._schedule_preview()

    def OnAreaWidth(self, event):
        if not self._syncing:
            self._set_metres_from_area()
            self._update_footprint()
            self._schedule_preview()

    def OnActualSize(self, event):
        self.metres.SetValue(str(geo.CELL_SIZE_M))
        self._set_area_from_metres()
        self._update_footprint()
        self._schedule_preview()

    def OnMetres(self, event):
        if not self._syncing:
            self._set_area_from_metres()
            self._update_footprint()
            self._schedule_preview()

    def OnFootprintEdited(self, event):
        if not self._syncing:
            self.MarkPreviewStale()
            self._schedule_preview()

    def OnLayoutChanged(self, event):
        self._update_footprint()
        self._schedule_preview()

    def OnAdvancedPane(self, event):
        self.Layout()

    def OnAdvancedChanged(self, event):
        current = (self.GetDatumMode(), self.GetWaterSource())
        for index, (_, datum, source) in enumerate(self.WATER_CHOICES):
            if (datum, source) == current:
                self.waterChoice.SetSelection(index)
                break
        else:
            self.waterChoice.SetSelection(4)
        self._update_controls()
        self._update_footprint()

    def OnWaterPreset(self, event):
        preset = self.WATER_CHOICES[self.waterChoice.GetSelection()]
        if preset[1] is not None:
            self._set_choice(self.datumMode, self.DATUM_CHOICES, preset[1])
            self._set_choice(self.waterSource, self.WATER_SOURCE_CHOICES, preset[2])
        else:
            self.advanced.Expand()
        self._update_controls()
        self.Layout()

    def GetWaterSource(self):
        return self.WATER_SOURCE_CHOICES[max(0, self.waterSource.GetSelection())][1]

    def GetDatumMode(self):
        return self.DATUM_CHOICES[max(0, self.datumMode.GetSelection())][1]

    def GetVerticalMode(self):
        return self.VERTICAL_CHOICES[max(0, self.verticalMode.GetSelection())][1]

    def GetCitySize(self):
        return self.CITY_CHOICES[max(0, self.citySize.GetSelection())][1]

    def _show_error(self, control, message):
        self.previewNote.SetLabel(message)
        control.SetFocus()
        self.Layout()

    def GetRequest(self):
        if self._location_pending:
            raise ValueError("Press Find to locate the edited place name first.")
        try:
            lat = self._float(self.lat, "Latitude")
            lon = self._float(self.lon, "Longitude")
            dimensions = self._dimensions()
            if not dimensions or min(dimensions) < 1:
                raise ValueError("Small-tile dimensions must be positive whole numbers")
            metres = self._float(self.metres, "Metres per cell")
            rotation = self._float(self.rotation, "Rotation", 0.0)
            vertical_mode = self.GetVerticalMode()
            vertical = self._float(self.vertical, "Height multiplier", 1.0) if vertical_mode == "manual" else 1.0
            datum_mode = self.GetDatumMode()
            datum = self._float(self.datum, "Water level", 0.0) if datum_mode == "manual" else 0.0
            water_source = self.GetWaterSource()
            min_area = int(self._float(self.minWaterArea, "Minimum mapped area", 64.0)) if water_source != "elevation" else 64
            max_rise = self._float(self.maxWaterRise, "Maximum water rise", 30.0) if water_source != "elevation" else 30.0
            depth = self._float(self.waterDepth, "Water depth", 3.0) if water_source != "elevation" else 3.0
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError(str(exc))
        request = geo.GeoImportRequest(
            center_lat=lat, center_lon=lon, tiles_x=dimensions[0], tiles_y=dimensions[1],
            metres_per_cell=metres, rotation_deg=rotation,
            vertical_mode=vertical_mode, vertical_scale=vertical,
            water_datum_mode=datum_mode, sea_reference_m=datum,
            water_source=water_source, min_water_area_cells=min_area,
            max_water_rise_m=max_rise, water_depth_m=depth,
            keep_bathymetry=not self.flatten.GetValue())
        try:
            request.validate()
        except geo.GeoImportError as exc:
            raise ValueError(str(exc))
        return request

    def OnImport(self, event):
        try:
            self.GetRequest()
        except ValueError as exc:
            self._show_error(self.search if self._location_pending else self.metres,
                             str(exc))
            return
        self.EndModal(wx.ID_OK)

    def GetLocationName(self):
        if self._committed_name and self._committed_name != "Coordinates entered directly":
            return self._committed_name
        try:
            return "%.4f, %.4f" % (self._float(self.lat, "Latitude"),
                                    self._float(self.lon, "Longitude"))
        except ValueError:
            return "Custom centre"

    def WantsUnderlay(self):
        return self.underlay.IsEnabled() and self.underlay.GetValue()

    def OnCloseWindow(self, event):
        self._closed = True
        self._search_revision += 1
        if self._preview_timer:
            self._preview_timer.Stop()
        if self._preview_active:
            self._preview_active[1].set()
        if self.IsModal():
            self.EndModal(wx.ID_CANCEL)
        else:
            event.Skip()


class PreferencesDialog(wx.Dialog):
    """Edit default folders and geographic data providers."""

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

        self.elevationUrl = wx.TextCtrl(
            self, value=getattr(settings, "elevation_url", ""))
        self.elevationAttribution = wx.TextCtrl(
            self, value=getattr(settings, "elevation_attribution", ""))
        self.basemapUrl = wx.TextCtrl(
            self, value=getattr(settings, "basemap_url", ""))
        self.basemapAttribution = wx.TextCtrl(
            self, value=getattr(settings, "basemap_attribution", ""))
        self.overpassUrls = wx.TextCtrl(
            self, value=getattr(settings, "overpass_urls", ""),
            size=(-1, 70), style=wx.TE_MULTILINE)
        providerGrid = wx.FlexGridSizer(cols=2, vgap=8, hgap=8)
        providerGrid.AddGrowableCol(1, 1)
        providerFields = [
            ("Elevation tile URL", self.elevationUrl),
            ("Elevation attribution", self.elevationAttribution),
            ("Basemap tile URL", self.basemapUrl),
            ("Basemap attribution", self.basemapAttribution),
            ("Overpass URLs", self.overpassUrls),
        ]
        for label, control in providerFields:
            providerGrid.Add(wx.StaticText(self, label=label),
                             0, wx.ALIGN_CENTER_VERTICAL)
            providerGrid.Add(control, 1, wx.EXPAND)
        providers = wx.StaticBoxSizer(wx.VERTICAL, self, "Map providers")
        providers.Add(providerGrid, 1, wx.EXPAND | wx.ALL, 8)

        buttons = wx.StdDialogButtonSizer()
        ok = wx.Button(self, wx.ID_OK)
        ok.SetDefault()
        buttons.AddButton(ok)
        buttons.AddButton(wx.Button(self, wx.ID_CANCEL))
        buttons.Realize()

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(grid, 1, wx.EXPAND | wx.ALL, 12)
        sizer.Add(providers, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        sizer.Add(buttons, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        self.SetSizerAndFit(sizer)
        height = self.GetSize().height
        self.SetMinSize((720, height))
        self.SetSize((720, height))

    def Apply(self):
        self.settings.import_dir = self.importDir.GetPath()
        self.settings.region_dir = self.regionDir.GetPath()
        self.settings.export_dir = self.exportDir.GetPath()
        self.settings.image_save_dir = self.imageSaveDir.GetPath()
        self.settings.elevation_url = self.elevationUrl.GetValue().strip()
        self.settings.elevation_attribution = (
            self.elevationAttribution.GetValue().strip())
        self.settings.basemap_url = self.basemapUrl.GetValue().strip()
        self.settings.basemap_attribution = (
            self.basemapAttribution.GetValue().strip())
        self.settings.overpass_urls = self.overpassUrls.GetValue().strip()
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

        self.geoSummary = wx.StaticText(self, label=" ")
        self.geoSummary.Wrap(700)
        self.btnGeoDetails = wx.Button(self, label="Details…")
        self.btnGeoAdjust = wx.Button(self, label="Adjust import…")
        self.btnGeoDetails.Hide()
        self.btnGeoAdjust.Hide()
        self.Bind(wx.EVT_BUTTON, self.OnGeoDetails, self.btnGeoDetails)
        self.Bind(wx.EVT_BUTTON, self.OnAdjustGeoImport, self.btnGeoAdjust)

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
        summaryRow = wx.BoxSizer(wx.HORIZONTAL)
        summaryRow.Add(self.geoSummary, 1, wx.EXPAND | wx.ALL, 5)
        summaryRow.Add(self.btnGeoDetails, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 3)
        summaryRow.Add(self.btnGeoAdjust, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 3)
        self.geoSummaryRow = summaryRow
        self.box.Add(summaryRow, 0, wx.EXPAND)
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
        self.geoImportState = None
        self.geoImportResult = None
        self.geoSummaryRow.ShowItems(False)

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

    def _show_geo_summary(self, result):
        text = "Imported %s · %.1f × %.1f km · %.1f m/cell" % (
            self.regionName, result.georeference.width_m / 1000.0,
            result.georeference.height_m / 1000.0,
            result.georeference.metres_per_cell)
        warnings = []
        if result.clamped_vertices or result.tiles_missing or result.dropped_water_bodies:
            warnings.append("See Details for import warnings.")
        if warnings:
            text += " " + " ".join(warnings)
        self.geoSummary.SetLabel(text)
        self.geoSummary.Wrap(700)
        self.geoSummaryRow.ShowItems(True)
        self.Layout()

    def OnGeoDetails(self, event):
        if self.geoImportResult is None:
            return
        wx.MessageBox(self.geoImportResult.summary() + "\n\n" +
                      self.geoImportResult.attribution,
                      "Real-world import details",
                      wx.OK | wx.ICON_INFORMATION, self)

    def OnAdjustGeoImport(self, event):
        if not self.geoImportState:
            return
        original = getattr(self.region, "originalConfig", None)
        current = getattr(self.region, "config", None)
        if (original is not None and current is not None
                and original.tobytes() != current.tobytes()):
            answer = wx.MessageBox(
                "Adjusting the import regenerates terrain and the initial city "
                "layout. Replace the layout edits made since import?",
                "Replace imported region", wx.YES_NO | wx.ICON_WARNING, self)
            if answer != wx.YES:
                return
        self.CreateRgnFromLocation(self.geoImportState)

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
        if result == 'Real-world location':
            return self.CreateRgnFromLocation()
        self.btnEditMode.Enable(False)
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
        self.geoImportState = None
        self.geoImportResult = None
        self.geoSummaryRow.ShowItems(False)

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

    def CreateRgnFromLocation(self, state=None):
        dlg = CreateRgnFromLocationDialog(self, self.settings, state=state)
        while dlg.ShowModal() == wx.ID_OK:
            if self._run_location_import(dlg):
                dlg.Destroy()
                return
        dlg.Destroy()

    def _run_location_import(self, dlg):
        """Run one attempt, returning False to reopen the populated form."""
        try:
            request = dlg.GetRequest()
        except ValueError as exc:
            dlg.Show()
            dlg._show_error(dlg.metres, str(exc))
            return False
        citySize = dlg.GetCitySize()
        wantUnderlay = dlg.WantsUnderlay()
        placeName = dlg.GetLocationName()
        dlg.Hide()

        cacheDir = getattr(self.settings, "tile_cache_dir", "") or None
        elevationUrl = (getattr(self.settings, "elevation_url", "")
                        or geo.DEFAULT_TILE_URL)

        progress = wx.ProgressDialog(
            "Importing terrain", "Contacting the elevation server",
            maximum=100, parent=self,
            style=(wx.PD_APP_MODAL | wx.PD_AUTO_HIDE | wx.PD_CAN_ABORT
                   | wx.PD_ELAPSED_TIME))

        updates = queue.SimpleQueue()
        cancelled = threading.Event()
        outcome = {}

        def report(done, total, message):
            if cancelled.is_set():
                raise ImportCancelled()
            percent = int(done * 100 / total) if total else 0
            updates.put((min(percent, 99), message))

        def import_worker():
            try:
                waterMask = None
                waterWarning = None
                if request.water_source != "elevation":
                    water = geo.OverpassClient(
                        cache_dir=(os.path.join(cacheDir, "osm")
                                   if cacheDir else None),
                        timeout=105,
                        mirrors=self.settings.overpass_endpoints() or None)
                    waterMask = geo.fetch_water_mask(request, water, report)
                    waterWarning = getattr(water, "last_warning", None)
                fetcher = geo.HttpTileFetcher(
                    url_template=elevationUrl,
                    cache_dir=(os.path.join(cacheDir, "elevation")
                               if cacheDir else None),
                    attribution=(getattr(
                        self.settings, "elevation_attribution", "") or None))
                result = geo.build_region_grid(
                    request, fetcher, report, water_mask=waterMask)
                basemap = None
                if wantUnderlay:
                    report(0, 100, "Downloading the map underlay")
                    mapFetcher = geo.HttpTileFetcher(
                        url_template=self.settings.basemap_url,
                        cache_dir=(os.path.join(cacheDir, "basemap")
                                   if cacheDir else None),
                        attribution=(getattr(
                            self.settings, "basemap_attribution", "") or None))
                    basemap, _, _, _ = geo.sample_basemap(
                        request, mapFetcher, report)
                outcome["result"] = result
                outcome["basemap"] = basemap
                outcome["water_warning"] = waterWarning
            except Exception as exc:
                outcome["error"] = exc

        worker = threading.Thread(target=import_worker, daemon=True)
        worker.start()
        latest = (0, "Contacting the elevation server")
        while worker.is_alive():
            changed = False
            try:
                while True:
                    latest = updates.get_nowait()
                    changed = True
            except queue.Empty:
                pass
            if changed:
                response = progress.Update(latest[0], latest[1])
            else:
                response = progress.Pulse(latest[1])
            keepGoing = response[0] if isinstance(response, tuple) else response
            if not keepGoing:
                cancelled.set()
                progress.Destroy()
                worker.join(0.2)
                dlg.Show()
                dlg.previewNote.SetLabel("Import cancelled. Your settings are still here.")
                return False
            wx.YieldIfNeeded()
            worker.join(0.05)
        progress.Destroy()

        error = outcome.get("error")
        if isinstance(error, ImportCancelled):
            dlg.Show()
            dlg.previewNote.SetLabel("Import cancelled. Your settings are still here.")
            return False
        if isinstance(error, geo.GeoImportError):
            dlg.Show()
            dlg._show_error(dlg.metres, str(error))
            return False
        if error is not None:
            dlg.Show()
            dlg._show_error(dlg.metres, "Import failed: %s" % error)
            return False
        if outcome.get("water_warning") and request.water_source == "mask":
            dlg.Show()
            answer = wx.MessageBox(
                outcome["water_warning"] + "\n\nContinue with the empty "
                "mapped-water mask?", "Mapped water coverage is ambiguous",
                wx.YES_NO | wx.ICON_WARNING, self)
            if answer != wx.YES:
                return False
            dlg.Hide()
        result = outcome["result"]
        basemap = outcome["basemap"]

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
        except AssertionError as exc:
            wx.EndBusyCursor()
            dlg.Show()
            dlg._show_error(dlg.metres,
                            "The generated city layout was rejected: %s" % exc)
            return False

        if tuple(newRegion.shape) != result.height_dm.shape:
            wx.EndBusyCursor()
            dlg.Show()
            dlg._show_error(dlg.metres,
                "The imported terrain is %dx%d but the region wants %dx%d."
                % (result.height_dm.shape[1], result.height_dm.shape[0],
                   newRegion.shape[1], newRegion.shape[0]))
            return False

        # Keep the existing region available throughout preview/download and
        # replace it only after the new one has been built successfully.
        self.CreateRgnInit()
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

        self.geoImportState = {"request": request, "city_size": citySize,
                               "underlay": wantUnderlay, "name": placeName}
        self.geoImportResult = result
        self._show_geo_summary(result)
        return True

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
        im.load()
        # Pillow 10+ keeps 16-bit grayscale PNGs as I;16, not I.
        if not png16.is_16bit_grayscale(im):
            dlg1 = wx.MessageDialog(
                self, paths + ' is not a valid 16-bit grayscale PNG\n'
                '(Pillow mode %r). Use "Grayscale image" for 8-bit files.'
                % (im.mode,),
                'Region creation error', wx.OK | wx.ICON_ERROR)
            dlg1.ShowModal()
            dlg1.Destroy()
            return
        im = png16.as_mode_i(im)
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
                im = png16.clamp_to_16bit(im)
            else:
                return

        dlgProg = wx.ProgressDialog(
            "Loading PNG", "Please wait while loading the region",
            maximum=configSize[1] * configSize[0] + 10, parent=self, style=0)

        wx.BeginBusyCursor()
        heights = png16.tiles_to_heightmap(
            im, configSize,
            lambda i: dlgProg.Update(
                i, "Please wait while loading the region"))

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

        im = Image.new("I;16", (config.size[0] * 64 + 1, config.size[1] * 64 + 1))
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
            imCity = Image.frombytes("I;16", (heightMap.shape[1],
                                              heightMap.shape[0]),
                                     heightMap.astype("<u2").tobytes())
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
