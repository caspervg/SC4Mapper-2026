"""Dialog to ask a modal question with a coder-specified list of buttons.

Taken from http://wiki.wxpython.org/index.cgi/GenericMessageDialog
"""

import wx


class curry(object):
    """Tie up a function with some default parameters and call it later.

    See the Python Cookbook recipe 52549 for background.
    """

    def __init__(self, func, *args, **kwargs):
        self.func = func
        self.pending = args[:]
        self.kwargs = kwargs

    def __call__(self, *args, **kwargs):
        if kwargs and self.kwargs:
            kw = self.kwargs.copy()
            kw.update(kwargs)
        else:
            kw = kwargs or self.kwargs
        return self.func(*(self.pending + args), **kw)


class dropArgs(object):
    """Same as :class:`curry`, but further call-time args are ignored."""

    def __init__(self, func, *args, **kwargs):
        self.func = func
        self.args = args[:]
        self.kwargs = kwargs

    def __call__(self, *args, **kwargs):
        return self.func(*self.args, **self.kwargs)


class ModalQuestion(wx.Dialog):
    """Ask a question.

    The modal return value is the index into the list of buttons.  Buttons
    can be specified either as strings or as wx IDs.
    """

    def __init__(self, parent, message, buttons, **kw):
        wx.Dialog.__init__(self, parent, **kw)

        topSizer = wx.BoxSizer(orient=wx.VERTICAL)
        self.SetSizer(topSizer)

        topSizer.Add(wx.StaticText(self, label=message),
                     flag=wx.ALIGN_CENTRE | wx.ALL, border=5)

        line = wx.StaticLine(self, size=(20, -1), style=wx.LI_HORIZONTAL)
        topSizer.Add(line, flag=wx.EXPAND | wx.RIGHT | wx.TOP, border=5)

        buttonSizer = wx.BoxSizer(orient=wx.HORIZONTAL)
        topSizer.Add(buttonSizer, flag=wx.ALIGN_CENTRE)

        for i, button in enumerate(buttons):
            if isinstance(button, int):
                b = wx.Button(self, id=button)
            else:
                b = wx.Button(self, label=button)

            self.Bind(wx.EVT_BUTTON, dropArgs(curry(self.EndModal, i)), b)

            buttonSizer.Add(b, flag=wx.ALL, border=5)

        self.Fit()


def ask_question(message, buttons=[wx.ID_OK, wx.ID_CANCEL], caption=''):
    """Ask a question and return the button the user clicked.

    Allowable button specifications are strings or wx IDs of stock buttons.
    If the user clicks the 'x' close button, the return value is ``None``.
    """
    dlg = ModalQuestion(None, message, buttons, title=caption)
    try:
        x = buttons[dlg.ShowModal()]
        dlg.Destroy()
        return x
    except IndexError:
        dlg.Destroy()
        return None
