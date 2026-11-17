"""Apply one deadline to response headers and body, including proxied requests."""

import http.client
import io
import time
import urllib.request


class DeadlineReader(io.RawIOBase):
    def __init__(self, channel, remaining):
        self.channel = channel
        self.remaining = remaining
        self.raw = channel.makefile("rb", buffering=0)

    def readable(self):
        return True

    def readinto(self, buffer):
        self.channel.settimeout(self.remaining())
        return self.raw.readinto(buffer)

    def close(self):
        try:
            self.raw.close()
        finally:
            super().close()


class ResponseSocket:
    def __init__(self, channel, remaining):
        self.channel, self.remaining = channel, remaining

    def makefile(self, mode):
        if mode != "rb":
            raise ValueError("HTTP response requires a binary reader")
        return io.BufferedReader(DeadlineReader(self.channel, self.remaining))


class DeadlineHandler(urllib.request.HTTPHandler, urllib.request.HTTPSHandler):
    def __init__(self, timeout):
        super().__init__()
        self.deadline = time.monotonic() + timeout

    def remaining(self):
        value = self.deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("HTTP deadline exceeded")
        return value

    def connection(self, kind, host, **kwargs):
        remaining = self.remaining

        class Response(http.client.HTTPResponse):
            def __init__(self, channel, *args, **options):
                super().__init__(ResponseSocket(channel, remaining), *args, **options)

        kwargs["timeout"] = remaining()
        connection = kind(host, **kwargs)
        connection.response_class = Response
        return connection

    def http_open(self, request):
        return self.do_open(
            lambda host, **kw: self.connection(http.client.HTTPConnection, host, **kw), request
        )

    def https_open(self, request):
        return self.do_open(
            lambda host, **kw: self.connection(http.client.HTTPSConnection, host, **kw), request
        )
