"""Thread-safe ring buffer with O(1) mean and std-dev computation.

Tracks a running sum and sum-of-squares alongside a fixed-capacity deque,
so mean() and std() are always O(1) regardless of capacity.

Used by enrichment processors that need rolling statistics over loop packets
(e.g., 10-minute average wind, trailing pressure trend).

Ported from weewx-clearskies-realtime (ADR-058).
"""

from __future__ import annotations

import math
import threading
from collections import deque


class RingBuffer:
    """Fixed-capacity circular buffer with O(1) running statistics.

    Thread-safe: all public methods acquire a lock before touching state.
    """

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {capacity!r}")
        self._capacity = capacity
        self._data: deque[float] = deque(maxlen=capacity)
        self._sum: float = 0.0
        self._sum_sq: float = 0.0  # sum of squares
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def add(self, value: float) -> None:
        """Append a value, evicting the oldest entry when at capacity.

        Running sums are adjusted BEFORE the deque appends (and potentially
        evicts) so the evicted value is subtracted first.
        """
        with self._lock:
            if len(self._data) == self._capacity:
                # The deque is full; appending will evict the leftmost item.
                # Subtract it from the running sums now, before eviction.
                evicted = self._data[0]
                self._sum -= evicted
                self._sum_sq -= evicted * evicted
            self._data.append(value)
            self._sum += value
            self._sum_sq += value * value

    def clear(self) -> None:
        """Remove all entries and reset running sums."""
        with self._lock:
            self._data.clear()
            self._sum = 0.0
            self._sum_sq = 0.0

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def mean(self) -> float:
        """Return the arithmetic mean of buffered values.

        Raises:
            ValueError: if the buffer is empty.
        """
        with self._lock:
            n = len(self._data)
            if n == 0:
                raise ValueError("Buffer is empty")
            return self._sum / n

    def std(self) -> float:
        """Return the population standard deviation of buffered values.

        Uses Var(X) = E[X^2] - E[X]^2.  The result is clamped to 0.0 to
        guard against small negative values from floating-point rounding.

        Raises:
            ValueError: if the buffer is empty.
        """
        with self._lock:
            n = len(self._data)
            if n == 0:
                raise ValueError("Buffer is empty")
            mean_sq = (self._sum / n) ** 2
            variance = max(0.0, self._sum_sq / n - mean_sq)
            return math.sqrt(variance)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def count(self) -> int:
        """Number of values currently stored (0 <= count <= capacity)."""
        with self._lock:
            return len(self._data)
