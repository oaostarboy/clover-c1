"""Loop-local elapsed-work injection; real setup time still counts."""
import asyncio


class DeadlineClock:
    def __init__(self, monkeypatch):
        self.loop = asyncio.get_running_loop()
        self.real_time = self.loop.time
        self.offset = 0.0
        self.started = self.real_time()
        self.events = []
        monkeypatch.setattr(self.loop, 'time', self.time)

    def time(self):
        return self.real_time() + self.offset

    def mark(self, stage):
        self.events.append({'stage': stage, 'real_elapsed': self.real_time() - self.started,
                            'injected_elapsed': self.offset})

    def advance(self, seconds):
        self.offset += seconds
