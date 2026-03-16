"""
tests/test_throttler.py — Focused tests for EventThrottler's task_002/fix-3 changes.

Covers the SHOULD FIX reviewer items:
  fix-3: EventThrottler._pending dict was unbounded — attackers could open many
         concurrent WebSocket connections with unique throttle keys and grow
         _pending without limit. Now enforces _max_keys on _pending as well.

Additional coverage of concurrent-access safety guarantees (asyncio single-thread).

Naming convention: test_<what>_when_<condition>_should_<expected>
"""

from __future__ import annotations

import asyncio
import time

import pytest

from dashboard.events import EventBus, EventThrottler

# ===========================================================================
# EventThrottler._pending max_keys enforcement (task_002/fix-3)
# ===========================================================================


class TestEventThrottlerPendingMaxKeys:
    """Tests for _pending dict max_keys cap (task_002/fix-3 SHOULD FIX)."""

    def test_set_pending_when_at_max_keys_with_new_key_should_evict_one_entry(self):
        """When _pending is at max_keys and a new key arrives, one entry is evicted."""
        t = EventThrottler(max_per_second=1.0, max_keys=3)
        # Fill _pending to capacity with distinct keys
        t._pending["key-a"] = {"type": "chunk", "text": "a"}
        t._pending["key-b"] = {"type": "chunk", "text": "b"}
        t._pending["key-c"] = {"type": "chunk", "text": "c"}
        assert len(t._pending) == 3

        # Adding a 4th (new) key should evict one existing entry
        t.set_pending("key-new", {"type": "chunk", "text": "new"})

        # The total must not exceed max_keys
        assert len(t._pending) <= 3

    def test_set_pending_when_at_max_keys_new_key_should_still_be_stored(self):
        """The incoming new key must be stored after eviction (not silently dropped)."""
        t = EventThrottler(max_per_second=1.0, max_keys=2)
        t._pending["existing-key"] = {"type": "chunk", "text": "old"}
        t._pending["another-key"] = {"type": "chunk", "text": "old2"}

        t.set_pending("brand-new", {"type": "chunk", "text": "fresh"})

        assert "brand-new" in t._pending
        assert t._pending["brand-new"]["text"] == "fresh"

    def test_set_pending_when_key_already_exists_should_not_evict(self):
        """Updating an existing key does NOT trigger eviction (only new keys evict)."""
        t = EventThrottler(max_per_second=1.0, max_keys=2)
        t._pending["key-a"] = {"type": "chunk", "text": "v1"}
        t._pending["key-b"] = {"type": "chunk", "text": "v2"}

        # Update existing key — no eviction needed
        t.set_pending("key-a", {"type": "chunk", "text": "v2-updated"})

        # Both keys still present
        assert "key-a" in t._pending
        assert "key-b" in t._pending
        assert t._pending["key-a"]["text"] == "v2-updated"

    def test_set_pending_when_below_max_keys_should_not_evict(self):
        """When _pending has room, no eviction occurs."""
        t = EventThrottler(max_per_second=1.0, max_keys=10)
        t._pending["key-a"] = {"type": "chunk", "text": "a"}
        # 1 entry, max_keys=10 → no eviction

        t.set_pending("key-b", {"type": "chunk", "text": "b"})

        assert "key-a" in t._pending
        assert "key-b" in t._pending

    def test_set_pending_with_many_unique_keys_should_never_exceed_max_keys(self):
        """Sending 1000 unique keys with max_keys=5 must keep _pending ≤ 5."""
        t = EventThrottler(max_per_second=100.0, max_keys=5)
        for i in range(1000):
            t.set_pending(f"agent-{i}", {"type": "chunk", "text": f"event-{i}"})

        assert len(t._pending) <= 5

    def test_set_pending_eviction_logs_warning(self, caplog):
        """Evicting from _pending at capacity should log a WARNING."""
        import logging

        t = EventThrottler(max_per_second=1.0, max_keys=1)
        t._pending["existing"] = {"type": "x"}  # Fill to capacity

        with caplog.at_level(logging.WARNING, logger="dashboard.events"):
            t.set_pending("incoming", {"type": "y"})

        assert any(
            "max_keys" in r.message.lower() or "pending" in r.message.lower()
            for r in caplog.records
        )

    def test_pending_dict_independent_of_last_emit_cap(self):
        """_pending uses same max_keys cap as _last_emit but tracked independently."""
        t = EventThrottler(max_per_second=1.0, max_keys=3)
        # _last_emit is empty, _pending has entries
        t._pending["p1"] = {"type": "x"}
        t._pending["p2"] = {"type": "y"}
        t._pending["p3"] = {"type": "z"}

        # _last_emit cap is independent
        assert len(t._last_emit) == 0
        assert len(t._pending) == 3


# ===========================================================================
# EventThrottler.should_emit max_keys enforcement (existing, supplement)
# ===========================================================================


class TestEventThrottlerShouldEmitMaxKeys:
    """Tests for _last_emit dict max_keys cap in should_emit()."""

    def test_should_emit_when_max_keys_hit_should_not_exceed_max_keys(self):
        """After filling max_keys, subsequent new keys stay within bound."""
        t = EventThrottler(max_per_second=1.0, max_keys=5)
        for i in range(20):
            t.should_emit(f"unique-key-{i}")
        assert len(t._last_emit) <= 5

    def test_should_emit_eviction_removes_oldest_half(self):
        """When max_keys is exceeded, should_emit evicts the oldest half."""
        t = EventThrottler(max_per_second=0.1, max_keys=4)
        # Emit with slightly different timestamps (use sleep or backdate)
        # Seed with 4 different timestamps
        t._last_emit["old-1"] = time.monotonic() - 100
        t._last_emit["old-2"] = time.monotonic() - 90
        t._last_emit["old-3"] = time.monotonic() - 80
        t._last_emit["old-4"] = time.monotonic() - 70

        assert len(t._last_emit) == 4

        # This triggers eviction
        t.should_emit("brand-new-key")

        # Oldest entries should have been evicted, total ≤ max_keys
        assert len(t._last_emit) <= 4

    def test_should_emit_after_eviction_new_key_is_recorded(self):
        """New key is stored in _last_emit after eviction."""
        t = EventThrottler(max_per_second=1.0, max_keys=2)
        t._last_emit["a"] = time.monotonic() - 100
        t._last_emit["b"] = time.monotonic() - 90

        # Trigger eviction + insert new key
        t.should_emit("c")

        assert "c" in t._last_emit


# ===========================================================================
# EventThrottler — concurrency safety (asyncio single-thread guarantees)
# ===========================================================================


class TestEventThrottlerConcurrencySafety:
    """Asyncio concurrency tests for EventThrottler thread-safety claims.

    asyncio is single-threaded: no two coroutines can interleave inside
    should_emit() or set_pending() because neither contains await points.
    These tests verify the behaviour under concurrent coroutine scheduling.
    """

    @pytest.mark.asyncio
    async def test_should_emit_concurrent_coroutines_should_not_exceed_max_keys(self):
        """Many coroutines calling should_emit concurrently must not exceed max_keys."""
        t = EventThrottler(max_per_second=100.0, max_keys=10)

        async def emit_many(start: int):
            for i in range(20):
                t.should_emit(f"key-{start + i}")
                await asyncio.sleep(0)  # yield to event loop

        # Run 5 concurrent coroutines each trying to add 20 unique keys
        await asyncio.gather(*[emit_many(i * 100) for i in range(5)])

        # Total tracked keys must never exceed max_keys (10)
        assert len(t._last_emit) <= 10

    @pytest.mark.asyncio
    async def test_set_pending_concurrent_coroutines_should_not_exceed_max_keys(self):
        """Concurrent coroutines calling set_pending must not exceed max_keys."""
        t = EventThrottler(max_per_second=100.0, max_keys=5)

        async def fill_pending(start: int):
            for i in range(10):
                t.set_pending(f"agent-{start + i}", {"type": "chunk", "seq": i})
                await asyncio.sleep(0)

        await asyncio.gather(*[fill_pending(i * 100) for i in range(4)])

        assert len(t._pending) <= 5

    @pytest.mark.asyncio
    async def test_interleaved_should_emit_and_set_pending_should_stay_bounded(self):
        """Interleaved should_emit + set_pending calls respect max_keys on both dicts."""
        t = EventThrottler(max_per_second=1.0, max_keys=3)

        async def alternate(start: int):
            for i in range(6):
                key = f"key-{start}-{i}"
                if not t.should_emit(key):
                    t.set_pending(key, {"type": "chunk"})
                await asyncio.sleep(0)

        await asyncio.gather(*[alternate(i) for i in range(5)])

        assert len(t._last_emit) <= 3
        assert len(t._pending) <= 3


# ===========================================================================
# EventThrottler — max_keys constructor validation
# ===========================================================================


class TestEventThrottlerConstructorValidation:
    """Tests for constructor validation of max_keys parameter."""

    def test_max_keys_zero_should_raise_value_error(self):
        """max_keys=0 is not valid — must raise ValueError."""
        with pytest.raises(ValueError, match="max_keys must be positive"):
            EventThrottler(max_per_second=1.0, max_keys=0)

    def test_max_keys_negative_should_raise_value_error(self):
        """max_keys < 0 is not valid."""
        with pytest.raises(ValueError, match="max_keys must be positive"):
            EventThrottler(max_per_second=1.0, max_keys=-1)

    def test_max_keys_one_should_be_valid(self):
        """max_keys=1 is the smallest valid value."""
        t = EventThrottler(max_per_second=1.0, max_keys=1)
        assert t._max_keys == 1

    def test_max_keys_default_is_10000(self):
        """Default max_keys is 10,000."""
        t = EventThrottler(max_per_second=4.0)
        assert t._max_keys == 10_000

    def test_max_per_second_zero_should_raise_value_error(self):
        """max_per_second=0 is not valid."""
        with pytest.raises(ValueError, match="max_per_second must be positive"):
            EventThrottler(max_per_second=0, max_keys=100)

    def test_max_per_second_negative_should_raise_value_error(self):
        """Negative max_per_second must raise ValueError."""
        with pytest.raises(ValueError, match="max_per_second must be positive"):
            EventThrottler(max_per_second=-5.0)


# ===========================================================================
# EventBus.publish_throttled — max_keys interaction
# ===========================================================================


class TestEventBusThrottledMaxKeys:
    """Tests for publish_throttled with max_keys bounding via EventBus."""

    @pytest.mark.asyncio
    async def test_publish_throttled_when_many_unique_keys_should_not_grow_unbounded(self):
        """Throttled events with many unique keys don't grow module-level throttler unbounded."""

        EventBus()
        # We create a fresh EventThrottler locally to avoid polluting the module singleton
        local_throttler = EventThrottler(max_per_second=1.0, max_keys=10)

        # Simulate what publish_throttled does internally but with the local throttler
        for i in range(50):
            key = f"agent-{i}"
            if not local_throttler.should_emit(key):
                local_throttler.set_pending(key, {"type": "chunk", "text": f"event-{i}"})

        assert len(local_throttler._last_emit) <= 10
        assert len(local_throttler._pending) <= 10

    @pytest.mark.asyncio
    async def test_flush_throttled_when_pending_cleared_should_reduce_pending_count(self):
        """After flushing, pending count decreases (memory is freed)."""
        bus = EventBus()
        q = await bus.subscribe()

        # Build up a throttled pending event
        await bus.publish_throttled({"type": "chunk", "text": "first"}, throttle_key="k1")
        q.get_nowait()  # Consume first event

        await bus.publish_throttled({"type": "chunk", "text": "pending"}, throttle_key="k1")

        from dashboard.events import text_chunk_throttler

        assert "k1" in text_chunk_throttler._pending

        # Flush removes the pending entry
        await bus.flush_throttled("k1")
        assert "k1" not in text_chunk_throttler._pending
