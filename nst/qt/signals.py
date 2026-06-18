"""Signal bridges.

The service layer invokes its callbacks from background threads. Qt widgets may
only be touched on the GUI thread, so every service callback is wired to emit a
signal here; because the receiver objects live on the main thread, Qt delivers
the slot call via a queued (thread-safe) connection automatically.
"""

from PyQt6.QtCore import QObject, pyqtSignal


class ChatSignals(QObject):
    roster_changed = pyqtSignal(object)                       # list[Peer]
    message = pyqtSignal(str, str, str, float, object, str)   # ip, name, text, ts, reply, mid
    file_offer = pyqtSignal(str, str, object)                 # ip, name, msg
    file_accept = pyqtSignal(str, str, object)
    file_reject = pyqtSignal(str, str, object)
    chat_request = pyqtSignal(str, str, object)
    group_message = pyqtSignal(object, str, str, str, float, object, str)  # group, ip, name, text, ts, reply, mid
    receipt = pyqtSignal(str, str, str)        # ip, mid, state ("delivered"|"read")
    deleted = pyqtSignal(str, str)             # from_ip, mid (delete-for-everyone)
    typing = pyqtSignal(str, str, object, bool)  # ip, name, gid|None, is_typing
    reaction = pyqtSignal(str, str, str)       # from_ip, mid, emoji
    mobile_join = pyqtSignal(object)           # MobileSession — needs approval
    mobile_leave = pyqtSignal(object)          # MobileSession — disconnected
    mobile_message = pyqtSignal(object, str)   # session, text


class MainSignals(QObject):
    beacon = pyqtSignal(str, bool)        # host ip, has_internet
    internet = pyqtSignal(bool)           # this host's internet state
    client_auto_off = pyqtSignal(str)     # reason — client proxy disabled automatically
