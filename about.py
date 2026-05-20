"""About box and the author-notes box shown for SC4M files."""

import webbrowser

import wx
import wx.html


class MyHtmlWindow(wx.html.HtmlWindow):
    """HtmlWindow subclass that opens links in the user's web browser."""

    def __init__(self, parent, id, size):
        wx.html.HtmlWindow.__init__(self, parent, id, size=size,
                                    style=wx.NO_FULL_REPAINT_ON_RESIZE)

    def OnLinkClicked(self, linkinfo):
        webbrowser.open_new(linkinfo.GetHref())


class AuthorBox(wx.Dialog):
    """Display the author notes embedded in an SC4M file."""

    def __init__(self, parent, htmlText):
        wx.Dialog.__init__(self, parent, -1, 'SC4M Author notes')
        if isinstance(htmlText, (bytes, bytearray)):
            htmlText = bytes(htmlText).decode('latin-1', 'replace')
        html = MyHtmlWindow(self, -1, size=(420, -1))
        html.SetPage(htmlText)

        ir = html.GetInternalRepresentation()
        html.SetSize((ir.GetWidth() + 25, ir.GetHeight() + 25))
        self.SetClientSize(html.GetSize())
        self.CentreOnParent(wx.BOTH)
