"""Errors shared by original-content readers and transport."""


class ContentError(ValueError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)
