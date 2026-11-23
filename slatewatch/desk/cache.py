"""Bound the memory retained by temporary browser previews."""

from collections import OrderedDict
from threading import Lock
from uuid import uuid4

from ..clock import InputError, Missing

PREVIEW_LIMIT = 24 * 1024 * 1024
CACHE_LIMIT = 48 * 1024 * 1024


class PreviewCache:
    def __init__(self):
        self.entries = OrderedDict()
        self.total = 0
        self.lock = Lock()

    def put(self, mapping, assets):
        size = sum(len(data) for data, _mime in assets.values())
        if size > PREVIEW_LIMIT:
            raise InputError('摘片超过浏览器预览容量，请下载完整证据包。')
        key = uuid4().hex
        with self.lock:
            while self.entries and (len(self.entries) >= 12 or self.total + size > CACHE_LIMIT):
                _old, (_mapping, _assets, old_size) = self.entries.popitem(last=False)
                self.total -= old_size
            self.entries[key] = (mapping, assets, size)
            self.total += size
        return key

    def get(self, key):
        with self.lock:
            if key not in self.entries:
                raise Missing('此预览已过期，请从审片记录重新摘片。')
            self.entries.move_to_end(key)
            mapping, assets, _size = self.entries[key]
            return mapping, assets
