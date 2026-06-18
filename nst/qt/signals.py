"""Signal bridges.

The service layer invokes its callbacks from background threads. Qt widgets may
only be touched on the GUI thread, so every service callback is wired to emit a
signal here; because the receiver objects live on the main thread, Qt delivers
the slot call via a queued (thread-safe) connection automatically.
"""

from PyQt6.QtCore import QObject, pyqtSignal


class ChatSignals(QObject):
    roster_changed = pyqtSignal(object)                       # list[Peer]
    message = pyqtSignal(str, str, str, float, object)        # ip, name, text, ts, reply
    file_offer = pyqtSignal(str, str, object)                 # ip, name, msg
    file_accept = pyqtSignal(str, str, object)
    file_reject = pyqtSignal(str, str, object)
    chat_request = pyqtSignal(str, str, object)
    group_message = pyqtSignal(object, str, str, str, float, object)  # group, ip, name, text, ts, reply


class MainSignals(QObject):
    beacon = pyqtSignal(str, bool)        # host ip, has_internet
    internet = pyqtSignal(bool)           # this host's internet state
    client_auto_off = pyqtSignal(str)     # reason — client proxy disabled automatically
