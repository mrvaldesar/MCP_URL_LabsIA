from collections import OrderedDict, deque
from collections.abc import Hashable
from math import ceil
from threading import Lock
from time import monotonic


class SlidingWindow:
    def __init__(self, limit: int, seconds: int):
        self.limit = limit
        self.seconds = seconds
        self.requests: OrderedDict[Hashable, deque[float]] = OrderedDict()
        self.lock = Lock()

    def retry_after(self, key: Hashable, *, consume: bool = True) -> int | None:
        with self.lock:
            now = monotonic()
            cutoff = now - self.seconds
            # Buckets are ordered by most recent accepted request.
            while self.requests:
                oldest = next(iter(self.requests.values()))
                if oldest[-1] > cutoff:
                    break
                self.requests.popitem(last=False)

            timestamps = self.requests.get(key)
            if timestamps:
                while timestamps and timestamps[0] <= cutoff:
                    timestamps.popleft()
                if len(timestamps) >= self.limit:
                    return ceil(timestamps[0] + self.seconds - now)

            if consume:
                timestamps = self.requests.setdefault(key, deque())
                timestamps.append(now)
                self.requests.move_to_end(key)
        return None
