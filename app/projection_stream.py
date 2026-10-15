"""Seekable bounded upload stream on the reserved projection disk volume."""
from werkzeug.exceptions import RequestEntityTooLarge

class LimitedFile:
    def __init__(self, path, maximum):
        self.file = open(path, 'w+b')
        self.maximum = maximum
    def write(self, data):
        if self.file.tell() + len(data) > self.maximum:
            raise RequestEntityTooLarge()
        return self.file.write(data)
    def __getattr__(self, name):
        return getattr(self.file, name)
