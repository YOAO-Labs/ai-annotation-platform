NOW = "2026-10-17T00:00:00Z"


class Socket:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            value = next(self.messages)
        except StopIteration:
            raise StopAsyncIteration from None
        if isinstance(value, Exception):
            raise value
        return value

    async def close(self):
        self.closed = True
