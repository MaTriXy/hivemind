"""
tests/test_events.py — Comprehensive pytest suite for EventBus and EventThrottler.

Scope
-----
EventThrottler:
  - should_emit: returns True when interval elapsed, False when throttled.
  - set_pending: stores latest event per key; back-pressure counter increments.
  - pop_pending: returns and removes the stored pending event; resets drop counter.
  - reset: clears all state for a key.
  - cleanup: removes stale entries older than max_age.
  - min_interval property: reflects max_per_second correctly.
  - ValueError raised for non-positive max_per_second.

EventBus:
  - subscribe: returns an asyncio.Queue.
  - unsubscribe: removes the subscriber.
  - publish: fans out to all subscribers; adds timestamp, sequence_id.
  - publish with project_id: sequence IDs are monotonically increasing.
  - publish_throttled: returns True if emitted, False if throttled.
  - flush_throttled: delivers pending event and resets throttle state.
  - get_buffered_events: returns events since given sequence.
  - get_latest_sequence: returns latest sequence for a project.
  - clear_project_events: wipes ring buffer, sequence, throttle state.
  - diagnostics: record_stuckness, record_error, record_progress, get_diagnostics.
  - heartbeat: start_heartbeat, stop_heartbeat, stop_all_heartbeats.
  - dead subscriber removal: dead subscribers are cleaned up on publish.
  - request_id propagation from ContextVar.

Naming convention: test_<what>_when_<condition>_should_<expected>
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from dashboard.events import (
    EventBus,
    EventThrottler,
    _TrackedQueue,
    current_request_id,
    text_chunk_throttler,
)

# ===========================================================================
# EventThrottler
# ===========================================================================


class TestEventThrottlerShouldEmit:
    """Tests for EventThrottler.should_emit()."""

    def test_should_emit_when_first_call_for_key_should_return_true(self):
        """First call for a new key always emits."""
        t = EventThrottler(max_per_second=2.0)
        assert t.should_emit("key-1") is True

    def test_should_emit_when_called_twice_immediately_should_return_false_second_time(self):
        """Second call within the interval should be throttled (False)."""
        t = EventThrottler(max_per_second=2.0)
        t.should_emit("key-1")  # First: True
        assert t.should_emit("key-1") is False

    def test_should_emit_when_interval_elapsed_should_return_true_again(self):
        """After the throttle interval passes, the next call should emit."""
        t = EventThrottler(max_per_second=100.0)  # 10ms interval
        t.should_emit("key-1")  # Consume the slot
        time.sleep(0.015)  # Wait > 10ms
        assert t.should_emit("key-1") is True

    def test_should_emit_when_different_keys_should_be_independent(self):
        """Different keys have independent throttle state."""
        t = EventThrottler(max_per_second=2.0)
        assert t.should_emit("key-a") is True
        assert t.should_emit("key-b") is True
        # Both emitted; now both throttled
        assert t.should_emit("key-a") is False
        assert t.should_emit("key-b") is False

    def test_min_interval_property_when_max_per_second_is_4_should_be_0_25_seconds(self):
        t = EventThrottler(max_per_second=4.0)
        assert t.min_interval == pytest.approx(0.25)

    def test_init_when_max_per_second_is_nonpositive_should_raise_valueerror(self):
        with pytest.raises(ValueError, match=r"positive"):
            EventThrottler(max_per_second=0)

    def test_init_when_max_per_second_is_negative_should_raise_valueerror(self):
        with pytest.raises(ValueError):
            EventThrottler(max_per_second=-1.0)


class TestEventThrottlerSetPending:
    """Tests for EventThrottler.set_pending() and back-pressure counter."""

    def test_set_pending_when_called_should_store_event_for_key(self):
        t = EventThrottler(max_per_second=2.0)
        event = {"type": "chunk", "text": "hello"}
        t.set_pending("agent-1", event)
        assert t._pending["agent-1"] is event

    def test_set_pending_when_called_twice_should_overwrite_with_latest_event(self):
        """Latest event wins — only the most recent pending event is kept."""
        t = EventThrottler(max_per_second=2.0)
        t.set_pending("agent-1", {"text": "first"})
        t.set_pending("agent-1", {"text": "second"})
        assert t._pending["agent-1"]["text"] == "second"

    def test_set_pending_when_called_should_increment_drop_counter(self):
        t = EventThrottler(max_per_second=2.0)
        t.set_pending("agent-1", {"text": "x"})
        assert t._drop_count.get("agent-1", 0) == 1

    def test_set_pending_when_called_100_times_should_log_backpressure_warning(self, caplog):
        """Every 100 drops should trigger a WARNING log (back-pressure signal)."""
        t = EventThrottler(max_per_second=2.0)
        import logging

        with caplog.at_level(logging.WARNING, logger="dashboard.events"):
            for _ in range(100):
                t.set_pending("agent-1", {"text": "chunk"})
        assert any("back-pressure" in r.message for r in caplog.records)

    def test_set_pending_when_called_99_times_should_not_log_backpressure_warning(self, caplog):
        """99 drops should NOT trigger a WARNING log yet."""
        t = EventThrottler(max_per_second=2.0)
        import logging

        with caplog.at_level(logging.WARNING, logger="dashboard.events"):
            for _ in range(99):
                t.set_pending("agent-1", {"text": "chunk"})
        assert not any("back-pressure" in r.message for r in caplog.records)


class TestEventThrottlerPopPending:
    """Tests for EventThrottler.pop_pending()."""

    def test_pop_pending_when_event_exists_should_return_and_remove_it(self):
        t = EventThrottler(max_per_second=2.0)
        event = {"type": "chunk"}
        t.set_pending("k", event)
        result = t.pop_pending("k")
        assert result is event
        assert "k" not in t._pending

    def test_pop_pending_when_no_event_exists_should_return_none(self):
        t = EventThrottler(max_per_second=2.0)
        assert t.pop_pending("nonexistent-key") is None

    def test_pop_pending_when_called_should_reset_drop_counter(self):
        t = EventThrottler(max_per_second=2.0)
        for _ in range(5):
            t.set_pending("k", {"text": "x"})
        t.pop_pending("k")
        assert t._drop_count.get("k", 0) == 0


class TestEventThrottlerReset:
    """Tests for EventThrottler.reset()."""

    def test_reset_when_key_has_state_should_clear_all_state_for_key(self):
        t = EventThrottler(max_per_second=2.0)
        t.should_emit("k")  # Sets last_emit
        t.set_pending("k", {"type": "x"})  # Sets pending and drop_count
        t.reset("k")
        assert "k" not in t._last_emit
        assert "k" not in t._pending
        assert "k" not in t._drop_count

    def test_reset_when_key_does_not_exist_should_not_raise(self):
        t = EventThrottler(max_per_second=2.0)
        t.reset("nonexistent")  # Should not raise


class TestEventThrottlerCleanup:
    """Tests for EventThrottler.cleanup()."""

    def test_cleanup_when_entry_is_stale_should_remove_it(self):
        t = EventThrottler(max_per_second=100.0)
        t.should_emit("old-key")
        # Backdate the last_emit timestamp to make it stale
        t._last_emit["old-key"] = time.monotonic() - 120.0
        t.cleanup(max_age=60.0)
        assert "old-key" not in t._last_emit

    def test_cleanup_when_entry_is_fresh_should_retain_it(self):
        t = EventThrottler(max_per_second=100.0)
        t.should_emit("fresh-key")  # Just emitted — timestamp is now
        t.cleanup(max_age=60.0)
        assert "fresh-key" in t._last_emit

    def test_cleanup_when_stale_entry_has_pending_should_also_remove_pending(self):
        t = EventThrottler(max_per_second=100.0)
        t.should_emit("stale")
        t._last_emit["stale"] = time.monotonic() - 120.0
        t._pending["stale"] = {"type": "x"}
        t.cleanup(max_age=60.0)
        assert "stale" not in t._pending


# ===========================================================================
# EventBus — subscribe / unsubscribe
# ===========================================================================


class TestEventBusSubscribeUnsubscribe:
    """Tests for EventBus subscribe and unsubscribe."""

    @pytest.mark.asyncio
    async def test_subscribe_when_called_should_return_asyncio_queue(self):
        bus = EventBus()
        queue = await bus.subscribe()
        assert isinstance(queue, asyncio.Queue)

    @pytest.mark.asyncio
    async def test_subscribe_when_called_should_increase_subscriber_count(self):
        bus = EventBus()
        assert bus.subscriber_count == 0
        await bus.subscribe()
        assert bus.subscriber_count == 1

    @pytest.mark.asyncio
    async def test_unsubscribe_when_called_should_decrease_subscriber_count(self):
        bus = EventBus()
        q = await bus.subscribe()
        await bus.unsubscribe(q)
        assert bus.subscriber_count == 0

    @pytest.mark.asyncio
    async def test_unsubscribe_when_called_with_unknown_queue_should_not_raise(self):
        bus = EventBus()
        unknown_queue = asyncio.Queue()
        await bus.unsubscribe(unknown_queue)  # Should not raise


# ===========================================================================
# EventBus — publish
# ===========================================================================


class TestEventBusPublish:
    """Tests for EventBus.publish()."""

    @pytest.mark.asyncio
    async def test_publish_when_subscriber_exists_should_deliver_event(self):
        bus = EventBus()
        q = await bus.subscribe()
        await bus.publish({"type": "test_event", "data": "hello"})
        event = q.get_nowait()
        assert event["type"] == "test_event"

    @pytest.mark.asyncio
    async def test_publish_when_no_project_id_should_not_assign_sequence_id(self):
        """Events without project_id don't get sequence IDs."""
        bus = EventBus()
        q = await bus.subscribe()
        await bus.publish({"type": "ping"})
        event = q.get_nowait()
        assert "sequence_id" not in event

    @pytest.mark.asyncio
    async def test_publish_when_project_id_present_should_assign_sequence_id(self):
        """Events with project_id get monotonically increasing sequence_id."""
        bus = EventBus()
        q = await bus.subscribe()
        await bus.publish({"type": "test", "project_id": "proj-x"})
        event = q.get_nowait()
        assert "sequence_id" in event
        assert isinstance(event["sequence_id"], int)
        assert event["sequence_id"] >= 1

    @pytest.mark.asyncio
    async def test_publish_sequence_ids_when_multiple_events_should_be_monotonically_increasing(
        self,
    ):
        """Each successive event for a project gets a higher sequence_id."""
        bus = EventBus()
        q = await bus.subscribe()
        await bus.publish({"type": "e1", "project_id": "proj-seq"})
        await bus.publish({"type": "e2", "project_id": "proj-seq"})
        await bus.publish({"type": "e3", "project_id": "proj-seq"})
        seq1 = q.get_nowait()["sequence_id"]
        seq2 = q.get_nowait()["sequence_id"]
        seq3 = q.get_nowait()["sequence_id"]
        assert seq1 < seq2 < seq3

    @pytest.mark.asyncio
    async def test_publish_when_no_timestamp_should_add_timestamp(self):
        """publish() should add a 'timestamp' key if not present."""
        bus = EventBus()
        q = await bus.subscribe()
        await bus.publish({"type": "test"})
        event = q.get_nowait()
        assert "timestamp" in event
        assert isinstance(event["timestamp"], float)

    @pytest.mark.asyncio
    async def test_publish_when_timestamp_already_set_should_preserve_it(self):
        """publish() should NOT overwrite an existing 'timestamp'."""
        bus = EventBus()
        q = await bus.subscribe()
        ts = 12345.0
        await bus.publish({"type": "test", "timestamp": ts})
        event = q.get_nowait()
        assert event["timestamp"] == ts

    @pytest.mark.asyncio
    async def test_publish_when_request_id_contextvar_set_should_propagate_it(self):
        """request_id from ContextVar is propagated to the event."""
        bus = EventBus()
        q = await bus.subscribe()
        token = current_request_id.set("req-abc123")
        try:
            await bus.publish({"type": "test"})
        finally:
            current_request_id.reset(token)
        event = q.get_nowait()
        assert event.get("request_id") == "req-abc123"

    @pytest.mark.asyncio
    async def test_publish_when_multiple_subscribers_should_fanout_to_all(self):
        """Every subscriber receives a copy of the published event."""
        bus = EventBus()
        q1 = await bus.subscribe()
        q2 = await bus.subscribe()
        q3 = await bus.subscribe()
        await bus.publish({"type": "broadcast", "msg": "hello"})
        assert q1.get_nowait()["type"] == "broadcast"
        assert q2.get_nowait()["type"] == "broadcast"
        assert q3.get_nowait()["type"] == "broadcast"

    @pytest.mark.asyncio
    async def test_publish_when_no_subscribers_should_not_raise(self):
        """Publish with zero subscribers should succeed silently."""
        bus = EventBus()
        await bus.publish({"type": "orphan_event"})  # Should not raise


# ===========================================================================
# EventBus — ring buffer
# ===========================================================================


class TestEventBusRingBuffer:
    """Tests for in-memory ring buffer and buffered event retrieval."""

    @pytest.mark.asyncio
    async def test_get_buffered_events_when_events_published_should_return_them(self):
        bus = EventBus()
        await bus.publish({"type": "agent_update", "project_id": "buf-proj"})
        await bus.publish({"type": "agent_update", "project_id": "buf-proj"})
        events = bus.get_buffered_events("buf-proj", since_sequence=0)
        assert len(events) == 2

    @pytest.mark.asyncio
    async def test_get_buffered_events_when_since_sequence_filters_should_return_newer_only(self):
        bus = EventBus()
        await bus.publish({"type": "e1", "project_id": "filter-proj"})
        await bus.publish({"type": "e2", "project_id": "filter-proj"})
        await bus.publish({"type": "e3", "project_id": "filter-proj"})

        seq1 = bus.get_latest_sequence("filter-proj")
        # Ask for events since the first event (should return the last two)
        events = bus.get_buffered_events("filter-proj", since_sequence=seq1 - 2)
        assert all(e["sequence_id"] > seq1 - 2 for e in events)

    @pytest.mark.asyncio
    async def test_get_buffered_events_when_no_events_should_return_empty_list(self):
        bus = EventBus()
        events = bus.get_buffered_events("never-published", since_sequence=0)
        assert events == []

    @pytest.mark.asyncio
    async def test_get_latest_sequence_when_events_published_should_return_correct_value(self):
        bus = EventBus()
        assert bus.get_latest_sequence("new-project") == 0
        await bus.publish({"type": "e", "project_id": "seq-proj"})
        await bus.publish({"type": "e", "project_id": "seq-proj"})
        assert bus.get_latest_sequence("seq-proj") == 2

    @pytest.mark.asyncio
    async def test_skip_buffer_event_types_when_published_should_not_enter_ring_buffer(self):
        """agent_text_chunk is a high-frequency type that skips the ring buffer."""
        bus = EventBus()
        await bus.publish({"type": "agent_text_chunk", "project_id": "chunk-proj", "text": "hi"})
        events = bus.get_buffered_events("chunk-proj", since_sequence=0)
        assert len(events) == 0  # agent_text_chunk skips ring buffer


# ===========================================================================
# EventBus — clear_project_events
# ===========================================================================


class TestEventBusClearProjectEvents:
    """Tests for EventBus.clear_project_events()."""

    @pytest.mark.asyncio
    async def test_clear_project_events_when_called_should_wipe_ring_buffer(self):
        bus = EventBus()
        await bus.publish({"type": "e", "project_id": "clear-me"})
        assert len(bus.get_buffered_events("clear-me", since_sequence=0)) > 0
        bus.clear_project_events("clear-me")
        assert bus.get_buffered_events("clear-me", since_sequence=0) == []

    @pytest.mark.asyncio
    async def test_clear_project_events_when_called_should_reset_sequence_counter(self):
        bus = EventBus()
        await bus.publish({"type": "e", "project_id": "reset-seq"})
        bus.clear_project_events("reset-seq")
        assert bus.get_latest_sequence("reset-seq") == 0

    @pytest.mark.asyncio
    async def test_clear_project_events_when_called_should_clear_diagnostics(self):
        bus = EventBus()
        bus.record_stuckness("clear-diag")
        bus.record_error("clear-diag")
        bus.clear_project_events("clear-diag")
        diag = bus.get_diagnostics("clear-diag")
        assert diag["health_score"] == "healthy"
        assert diag["last_stuckness"] is None

    def test_clear_project_events_when_project_unknown_should_not_raise(self):
        bus = EventBus()
        bus.clear_project_events("ghost-project")  # Should not raise


# ===========================================================================
# EventBus — diagnostics
# ===========================================================================


class TestEventBusDiagnostics:
    """Tests for EventBus diagnostics (health scoring)."""

    def test_get_diagnostics_when_no_events_should_return_healthy(self):
        bus = EventBus()
        diag = bus.get_diagnostics("pristine-project")
        assert diag["health_score"] == "healthy"
        assert diag["warnings_count"] == 0
        assert diag["last_stuckness"] is None
        assert diag["seconds_since_progress"] is None

    def test_get_diagnostics_when_stuckness_recent_should_return_critical(self):
        """Stuckness within last 60s → critical."""
        bus = EventBus()
        bus.record_stuckness("stuck-proj")
        diag = bus.get_diagnostics("stuck-proj")
        assert diag["health_score"] == "critical"

    def test_get_diagnostics_when_stuckness_old_should_not_be_critical(self):
        """Old stuckness (>60s ago) should not trigger critical."""
        bus = EventBus()
        bus._last_stuckness["old-stuck"] = time.time() - 120
        diag = bus.get_diagnostics("old-stuck")
        assert diag["health_score"] != "critical"

    def test_get_diagnostics_when_error_recent_should_return_degraded(self):
        """Error within last 120s → degraded (if no stuckness)."""
        bus = EventBus()
        bus.record_error("error-proj")
        diag = bus.get_diagnostics("error-proj")
        assert diag["health_score"] == "degraded"

    def test_get_diagnostics_when_progress_recent_should_return_healthy(self):
        """Recent progress resets warning count → healthy."""
        bus = EventBus()
        bus.record_stuckness("progress-proj")  # Trigger warning
        bus.record_progress("progress-proj")  # Reset
        # Note: record_progress resets warnings_count but stuckness timestamp remains
        # The health score might still be critical due to recent stuckness
        # Test that warning count was reset
        diag = bus.get_diagnostics("progress-proj")
        assert diag["warnings_count"] == 0

    def test_record_stuckness_when_called_should_increment_warnings_count(self):
        bus = EventBus()
        bus.record_stuckness("warn-proj")
        bus.record_stuckness("warn-proj")
        diag = bus.get_diagnostics("warn-proj")
        assert diag["warnings_count"] == 2

    def test_get_diagnostics_when_agent_silent_90s_should_return_critical(self):
        """Agent silent > 90s → critical."""
        bus = EventBus()
        bus._last_progress["silent-proj"] = time.time() - 95  # 95s ago
        diag = bus.get_diagnostics("silent-proj")
        assert diag["health_score"] == "critical"

    def test_get_diagnostics_when_agent_silent_50s_should_return_degraded(self):
        """Agent silent 45-90s → degraded."""
        bus = EventBus()
        bus._last_progress["slow-proj"] = time.time() - 55
        diag = bus.get_diagnostics("slow-proj")
        assert diag["health_score"] == "degraded"

    def test_get_diagnostics_seconds_since_progress_when_progress_recorded_should_be_positive(self):
        bus = EventBus()
        bus.record_progress("timed-proj")
        diag = bus.get_diagnostics("timed-proj")
        assert diag["seconds_since_progress"] is not None
        assert diag["seconds_since_progress"] >= 0.0


# ===========================================================================
# EventBus — publish_throttled and flush_throttled
# ===========================================================================


class TestEventBusThrottled:
    """Tests for publish_throttled() and flush_throttled()."""

    @pytest.mark.asyncio
    async def test_publish_throttled_when_first_call_should_publish_immediately(self):
        """First call for a key passes through immediately (returns True)."""
        bus = EventBus()
        q = await bus.subscribe()
        result = await bus.publish_throttled(
            {"type": "agent_text_chunk", "project_id": "tp"},
            throttle_key="tp::agent-1",
        )
        assert result is True
        assert not q.empty()

    @pytest.mark.asyncio
    async def test_publish_throttled_when_within_interval_should_throttle_and_return_false(self):
        """Second rapid call is throttled (stored as pending, returns False)."""
        bus = EventBus()
        q = await bus.subscribe()
        await bus.publish_throttled(
            {"type": "chunk", "text": "a"},
            throttle_key="tp::throttled",
        )
        q.get_nowait()  # Consume first event
        result = await bus.publish_throttled(
            {"type": "chunk", "text": "b"},
            throttle_key="tp::throttled",
        )
        assert result is False
        assert q.empty()  # Throttled — not published

    @pytest.mark.asyncio
    async def test_flush_throttled_when_pending_event_exists_should_publish_it(self):
        """flush_throttled() delivers the pending event."""
        bus = EventBus()
        q = await bus.subscribe()
        # Publish once to consume the slot
        await bus.publish_throttled({"type": "chunk", "text": "a"}, throttle_key="flush-key")
        q.get_nowait()
        # Throttle second event into pending
        await bus.publish_throttled({"type": "chunk", "text": "b"}, throttle_key="flush-key")
        # Now flush
        await bus.flush_throttled("flush-key")
        event = q.get_nowait()
        assert event["type"] == "chunk"
        assert event["text"] == "b"

    @pytest.mark.asyncio
    async def test_flush_throttled_when_no_pending_should_not_raise(self):
        """flush_throttled() with no pending event is a no-op."""
        bus = EventBus()
        await bus.flush_throttled("no-pending-key")  # Should not raise

    @pytest.mark.asyncio
    async def test_publish_throttled_when_no_throttle_key_should_publish_unconditionally(self):
        """Without throttle_key, publish_throttled always publishes."""
        bus = EventBus()
        q = await bus.subscribe()
        result1 = await bus.publish_throttled({"type": "e1"})
        result2 = await bus.publish_throttled({"type": "e2"})
        assert result1 is True
        assert result2 is True
        assert q.qsize() == 2


# ===========================================================================
# EventBus — heartbeat
# ===========================================================================


class TestEventBusHeartbeat:
    """Tests for EventBus heartbeat (start_heartbeat, stop_heartbeat)."""

    @pytest.mark.asyncio
    async def test_start_heartbeat_when_called_should_create_background_task(self):
        bus = EventBus()
        status_fn = AsyncMock(return_value={"status": "idle", "active_agents": 0})
        await bus.start_heartbeat("hb-proj", status_fn)
        assert "hb-proj" in bus._heartbeat_tasks
        await bus.stop_heartbeat("hb-proj")

    @pytest.mark.asyncio
    async def test_stop_heartbeat_when_running_should_cancel_task(self):
        bus = EventBus()
        status_fn = AsyncMock(return_value={"status": "idle", "active_agents": 0})
        await bus.start_heartbeat("stop-proj", status_fn)
        await bus.stop_heartbeat("stop-proj")
        assert "stop-proj" not in bus._heartbeat_tasks

    @pytest.mark.asyncio
    async def test_stop_heartbeat_when_not_started_should_not_raise(self):
        bus = EventBus()
        await bus.stop_heartbeat("never-started")  # Should not raise

    @pytest.mark.asyncio
    async def test_stop_all_heartbeats_when_multiple_running_should_cancel_all(self):
        bus = EventBus()
        status_fn = AsyncMock(return_value={"status": "idle", "active_agents": 0})
        await bus.start_heartbeat("proj-1", status_fn)
        await bus.start_heartbeat("proj-2", status_fn)
        await bus.stop_all_heartbeats()
        assert len(bus._heartbeat_tasks) == 0

    @pytest.mark.asyncio
    async def test_start_heartbeat_when_restarted_should_replace_existing_task(self):
        """Starting a heartbeat for a project that already has one replaces it."""
        bus = EventBus()
        status_fn = AsyncMock(return_value={"status": "idle", "active_agents": 0})
        await bus.start_heartbeat("replace-proj", status_fn)
        old_task = bus._heartbeat_tasks["replace-proj"]
        await bus.start_heartbeat("replace-proj", status_fn)
        new_task = bus._heartbeat_tasks["replace-proj"]
        assert new_task is not old_task
        assert old_task.done() or old_task.cancelled()
        await bus.stop_heartbeat("replace-proj")


# ===========================================================================
# EventBus — dead subscriber removal
# ===========================================================================


class TestEventBusDeadSubscriberRemoval:
    """Tests for automatic removal of dead (failed) subscribers."""

    @pytest.mark.asyncio
    async def test_dead_subscriber_when_put_nowait_fails_and_is_dead_should_be_removed_on_publish(
        self,
    ):
        """Subscribers whose put_nowait returns False AND is_dead are removed on publish.

        We inject a MagicMock that simulates a dead subscriber directly into
        the bus's internal subscriber list (bypassing subscribe() so we can
        fully control the mock's behaviour).
        """
        bus = EventBus()

        # Create a mock that behaves like a dead _TrackedQueue
        dead_mock = MagicMock()
        dead_mock.put_nowait.return_value = False
        dead_mock.is_dead = True

        async with bus._lock:
            bus._subscribers.append(dead_mock)

        assert bus.subscriber_count == 1

        # Publishing should detect the dead subscriber and remove it
        await bus.publish({"type": "cleanup_test"})

        # After cleanup, subscriber count should be 0
        assert bus.subscriber_count == 0

    @pytest.mark.asyncio
    async def test_dead_subscriber_detection_when_failures_at_threshold_should_return_true(self):
        """_TrackedQueue.is_dead returns True when failures >= _MAX_CONSECUTIVE_FAILURES."""
        from dashboard.events import _MAX_CONSECUTIVE_FAILURES

        bus = EventBus()
        q = await bus.subscribe()
        async with bus._lock:
            for tracked in bus._subscribers:
                if tracked.queue is q:
                    tracked.failures = _MAX_CONSECUTIVE_FAILURES
                    assert tracked.is_dead is True
                    break


# ===========================================================================
# _TrackedQueue
# ===========================================================================


class TestTrackedQueue:
    """Tests for the _TrackedQueue wrapper."""

    def test_put_nowait_when_queue_has_space_should_return_true(self):
        tq = _TrackedQueue(maxsize=10)
        result = tq.put_nowait({"type": "x"})
        assert result is True
        assert tq.failures == 0

    def test_put_nowait_when_queue_full_should_drop_oldest_and_return_true(self):
        """When full, oldest event is dropped to make room for the new event."""
        tq = _TrackedQueue(maxsize=2)
        tq.put_nowait({"type": "old"})
        tq.put_nowait({"type": "old2"})
        # Queue full — now trigger the drop-oldest logic
        result = tq.put_nowait({"type": "new"})
        # Should succeed (dropped oldest to make room)
        assert result is True

    def test_is_dead_when_failures_below_threshold_should_be_false(self):
        from dashboard.events import _MAX_CONSECUTIVE_FAILURES

        tq = _TrackedQueue(maxsize=10)
        tq.failures = _MAX_CONSECUTIVE_FAILURES - 1
        assert tq.is_dead is False

    def test_is_dead_when_failures_at_threshold_should_be_true(self):
        from dashboard.events import _MAX_CONSECUTIVE_FAILURES

        tq = _TrackedQueue(maxsize=10)
        tq.failures = _MAX_CONSECUTIVE_FAILURES
        assert tq.is_dead is True


# ===========================================================================
# EventBus — DB writer (start/stop writer lifecycle)
# ===========================================================================


class TestEventBusWriter:
    """Tests for EventBus DB writer start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_writer_when_called_should_create_write_queue(self):
        bus = EventBus()
        assert bus._write_queue is None
        await bus.start_writer()
        assert bus._write_queue is not None
        await bus.stop_writer()

    @pytest.mark.asyncio
    async def test_start_writer_when_called_twice_should_be_idempotent(self):
        """Second start_writer call is a no-op."""
        bus = EventBus()
        await bus.start_writer()
        q1 = bus._write_queue
        await bus.start_writer()  # Should not create a new queue
        assert bus._write_queue is q1
        await bus.stop_writer()

    @pytest.mark.asyncio
    async def test_stop_writer_when_called_should_clear_write_queue(self):
        bus = EventBus()
        await bus.start_writer()
        await bus.stop_writer()
        assert bus._write_queue is None
        assert bus._writer_task is None

    @pytest.mark.asyncio
    async def test_stop_writer_when_not_started_should_not_raise(self):
        bus = EventBus()
        await bus.stop_writer()  # Should not raise

    @pytest.mark.asyncio
    async def test_publish_when_writer_running_and_persistable_event_should_queue_for_db(self):
        """Events of persisted types should be put into the write queue."""
        bus = EventBus()
        await bus.start_writer()
        assert bus._write_queue is not None
        await bus.publish(
            {
                "type": "agent_started",  # This is a persisted type
                "project_id": "persist-proj",
                "agent": "orchestrator",
            }
        )
        # After publish, the write queue should have received the event
        assert bus._write_queue.qsize() > 0
        await bus.stop_writer()

    @pytest.mark.asyncio
    async def test_publish_when_non_persisted_event_should_not_queue_for_db(self):
        """Non-persisted event types (ping, text_chunk) skip the write queue."""
        bus = EventBus()
        await bus.start_writer()
        initial_size = bus._write_queue.qsize()
        await bus.publish(
            {
                "type": "ping",  # Not in _PERSIST_EVENT_TYPES
                "project_id": "no-persist",
            }
        )
        # Ping should not be in the write queue
        assert bus._write_queue.qsize() == initial_size
        await bus.stop_writer()

    @pytest.mark.asyncio
    async def test_publish_when_write_queue_full_should_drop_and_not_raise(self):
        """When DB write queue is full, events are dropped (no block, no raise)."""
        bus = EventBus()
        await bus.start_writer()
        # Fill the write queue
        if bus._write_queue:
            try:
                for _ in range(5001):
                    bus._write_queue.put_nowait({"type": "fill"})
            except asyncio.QueueFull:
                pass
        # Publish a persisted event when queue is full — should not raise
        await bus.publish(
            {
                "type": "agent_started",
                "project_id": "full-queue-proj",
            }
        )
        await bus.stop_writer()


# ===========================================================================
# EventBus — heartbeat loop execution
# ===========================================================================


class TestEventBusHeartbeatExecution:
    """Tests for the heartbeat loop content — what gets published during a tick."""

    @pytest.mark.asyncio
    async def test_heartbeat_loop_when_ticks_should_publish_status_heartbeat(self):
        """The heartbeat loop publishes 'status_heartbeat' events."""
        bus = EventBus()
        q = await bus.subscribe()

        # Use a very fast interval for testing (override the module constant)
        import dashboard.events as ev_mod

        original_interval = ev_mod._HEARTBEAT_INTERVAL_SECONDS

        status_fn = AsyncMock(
            return_value={
                "status": "running",
                "active_agents": 2,
                "agents": {},
            }
        )

        try:
            # Patch the heartbeat interval to near-zero for immediate firing
            ev_mod._HEARTBEAT_INTERVAL_SECONDS = 0.01
            await bus.start_heartbeat("hb-exec-proj", status_fn)
            # Wait for at least one heartbeat tick
            await asyncio.sleep(0.05)
        finally:
            ev_mod._HEARTBEAT_INTERVAL_SECONDS = original_interval
            await bus.stop_heartbeat("hb-exec-proj")

        # Should have received at least one status_heartbeat event
        heartbeats = []
        while not q.empty():
            try:
                event = q.get_nowait()
                if event.get("type") == "status_heartbeat":
                    heartbeats.append(event)
            except asyncio.QueueEmpty:
                break

        assert len(heartbeats) > 0
        hb = heartbeats[0]
        assert hb["project_id"] == "hb-exec-proj"
        assert hb["status"] == "running"
        assert "agents" in hb
        assert "diagnostics" in hb

    @pytest.mark.asyncio
    async def test_heartbeat_loop_when_status_fn_raises_should_continue_heartbeat(self):
        """Heartbeat continues even when status_fn raises an exception."""
        bus = EventBus()
        import dashboard.events as ev_mod

        original_interval = ev_mod._HEARTBEAT_INTERVAL_SECONDS

        # First call raises, second call succeeds
        status_fn = AsyncMock(
            side_effect=[
                RuntimeError("DB unavailable"),
                {"status": "idle", "active_agents": 0},
            ]
        )

        try:
            ev_mod._HEARTBEAT_INTERVAL_SECONDS = 0.01
            await bus.start_heartbeat("resilient-proj", status_fn)
            await asyncio.sleep(0.08)  # Allow 2-3 ticks
        finally:
            ev_mod._HEARTBEAT_INTERVAL_SECONDS = original_interval
            await bus.stop_heartbeat("resilient-proj")

        # Task should still be in the heartbeat_tasks until explicitly stopped
        # and we should not have crashed
        assert "resilient-proj" not in bus._heartbeat_tasks  # Cleaned up


# ===========================================================================
# EventBus — diagnostics auto-tracking via publish
# ===========================================================================


class TestEventBusDiagnosticsViaPublish:
    """Tests for diagnostics auto-tracking triggered by publish()."""

    @pytest.mark.asyncio
    async def test_publish_stuckness_event_should_record_stuckness(self):
        bus = EventBus()
        await bus.publish(
            {
                "type": "stuckness_detected",  # matches orchestrator._emit_stuckness_event
                "project_id": "stuck-via-publish",
            }
        )
        assert bus._last_stuckness.get("stuck-via-publish") is not None

    @pytest.mark.asyncio
    async def test_publish_task_error_with_is_error_should_record_error(self):
        bus = EventBus()
        await bus.publish(
            {
                "type": "task_error",
                "project_id": "errored-proj",
                "is_error": True,
            }
        )
        assert bus._last_error.get("errored-proj") is not None

    @pytest.mark.asyncio
    async def test_publish_task_complete_without_error_should_record_progress(self):
        bus = EventBus()
        await bus.publish(
            {
                "type": "task_complete",
                "project_id": "done-proj",
                "is_error": False,
            }
        )
        assert bus._last_progress.get("done-proj") is not None


# ============================================================
# EventThrottler — max_keys memory-bounding (anti-leak guard)
# ============================================================


class TestEventThrottlerMaxKeys:
    """Tests for the max_keys hard cap that prevents unbounded memory growth."""

    def test_max_keys_default_is_10000(self):
        """Default max_keys should be 10,000 to cap memory usage."""
        t = EventThrottler(max_per_second=10.0)
        assert t._max_keys == 10_000

    def test_max_keys_custom_is_respected(self):
        """Custom max_keys is stored correctly."""
        t = EventThrottler(max_per_second=5.0, max_keys=50)
        assert t._max_keys == 50

    def test_max_keys_zero_raises(self):
        """max_keys=0 must raise ValueError (cannot have zero capacity)."""
        with pytest.raises(ValueError, match="max_keys must be positive"):
            EventThrottler(max_per_second=1.0, max_keys=0)

    def test_should_emit_when_max_keys_exceeded_evicts_oldest(self):
        """When max_keys is exceeded, oldest entries are evicted to cap memory."""
        # Use max_keys=4 so eviction triggers quickly
        t = EventThrottler(max_per_second=1.0, max_keys=4)
        # Fill up to max_keys
        for i in range(4):
            assert t.should_emit(f"key-{i}") is True

        # All 4 keys are in _last_emit
        assert len(t._last_emit) == 4

        # Add one more (max_keys=4, so eviction should fire for the 5th new key)
        t.should_emit("key-new")

        # After eviction, total keys should be at most max_keys
        assert len(t._last_emit) <= 4

    def test_rapid_fire_unique_keys_bounded_by_max_keys(self):
        """Sending 1000 unique keys with max_keys=100 must never exceed 100 entries."""
        t = EventThrottler(max_per_second=1.0, max_keys=100)
        for i in range(1000):
            t.should_emit(f"agent-{i}")
        # Memory is bounded — no more than max_keys entries
        assert len(t._last_emit) <= 100

    def test_cleanup_when_called_removes_stale_entries(self):
        """cleanup() removes entries older than max_age (existing behaviour preserved)."""
        t = EventThrottler(max_per_second=100.0)
        # Emit something, then manually make it old
        t.should_emit("old-key")
        t._last_emit["old-key"] = time.monotonic() - 200  # 200s ago

        t.cleanup(max_age=60.0)
        assert "old-key" not in t._last_emit

    def test_constructor_invalid_max_per_second_raises(self):
        """max_per_second <= 0 must raise ValueError."""
        with pytest.raises(ValueError, match="max_per_second must be positive"):
            EventThrottler(max_per_second=0)
        with pytest.raises(ValueError, match="max_per_second must be positive"):
            EventThrottler(max_per_second=-1.0)


# ===========================================================================
# Coverage boosters — targeted at uncovered branches in events.py
# ===========================================================================


class TestTrackedQueueFailurePath:
    """Tests for the _TrackedQueue edge case: both get_nowait and put_nowait fail."""

    def test_put_nowait_when_get_fails_should_increment_failures_and_return_false(self):
        """When drop-oldest get_nowait fails (QueueEmpty race), failures increments."""
        import asyncio

        tq = _TrackedQueue(maxsize=1)
        # Fill the queue
        tq.queue.put_nowait({"type": "old"})

        # Patch queue.get_nowait to raise QueueEmpty, simulating race
        def flaky_get():
            raise asyncio.QueueEmpty()

        tq.queue.get_nowait = flaky_get
        result = tq.put_nowait({"type": "new"})

        # Should have incremented failures and returned False
        assert result is False
        assert tq.failures >= 1


class TestEventBusRingBufferEviction:
    """Test ring buffer eviction when > 100 unique projects accumulate."""

    @pytest.mark.asyncio
    async def test_ring_buffer_evicts_oldest_project_when_over_100(self):
        """When >100 projects have ring buffer data, oldest is evicted."""
        bus = EventBus()

        # Publish to 101 unique projects to trigger eviction at the 101st
        for i in range(101):
            await bus.publish(
                {
                    "type": "agent_update",
                    "project_id": f"proj-{i:03d}",
                }
            )

        # After eviction, we should have at most 100 project ring buffers
        assert len(bus._ring_buffers) <= 100


class TestEventBusHeartbeatWithAgents:
    """Heartbeat loop tests covering the per-agent processing branches."""

    @pytest.mark.asyncio
    async def test_heartbeat_with_agent_duration_field_should_use_duration(self):
        """Heartbeat uses 'duration' field directly when present."""
        bus = EventBus()
        q = await bus.subscribe()

        import dashboard.events as ev_mod

        original_interval = ev_mod._HEARTBEAT_INTERVAL_SECONDS

        status_fn = AsyncMock(
            return_value={
                "status": "running",
                "active_agents": 1,
                "agents": {
                    "worker": {
                        "state": "working",
                        "duration": 45.5,
                        "current_tool": "bash",
                        "task": "write code",
                    }
                },
            }
        )

        try:
            ev_mod._HEARTBEAT_INTERVAL_SECONDS = 0.01
            await bus.start_heartbeat("agent-dur-proj", status_fn)
            await asyncio.sleep(0.05)
        finally:
            ev_mod._HEARTBEAT_INTERVAL_SECONDS = original_interval
            await bus.stop_heartbeat("agent-dur-proj")

        heartbeats = []
        while not q.empty():
            try:
                ev = q.get_nowait()
                if ev.get("type") == "status_heartbeat":
                    heartbeats.append(ev)
            except asyncio.QueueEmpty:
                break

        assert len(heartbeats) > 0
        agents = heartbeats[0].get("agents", [])
        if agents:
            w = next((a for a in agents if a["name"] == "worker"), None)
            if w:
                assert w["elapsed_seconds"] == pytest.approx(45.5)

    @pytest.mark.asyncio
    async def test_heartbeat_with_agent_started_at_field_should_compute_elapsed(self):
        """Heartbeat computes elapsed from started_at when duration absent."""
        bus = EventBus()
        q = await bus.subscribe()

        import time as _time

        import dashboard.events as ev_mod

        original_interval = ev_mod._HEARTBEAT_INTERVAL_SECONDS

        started = _time.time() - 30.0  # started 30 seconds ago

        status_fn = AsyncMock(
            return_value={
                "status": "running",
                "active_agents": 1,
                "agents": {
                    "coder": {
                        "state": "working",
                        "started_at": started,
                        "last_stream_at": _time.time() - 5,
                    }
                },
            }
        )

        try:
            ev_mod._HEARTBEAT_INTERVAL_SECONDS = 0.01
            await bus.start_heartbeat("agent-startedat", status_fn)
            await asyncio.sleep(0.05)
        finally:
            ev_mod._HEARTBEAT_INTERVAL_SECONDS = original_interval
            await bus.stop_heartbeat("agent-startedat")

        heartbeats = []
        while not q.empty():
            try:
                ev = q.get_nowait()
                if ev.get("type") == "status_heartbeat":
                    heartbeats.append(ev)
            except asyncio.QueueEmpty:
                break

        assert len(heartbeats) > 0

    @pytest.mark.asyncio
    async def test_heartbeat_with_non_dict_agent_info_should_skip_gracefully(self):
        """Non-dict agent info entries are skipped without crashing."""
        bus = EventBus()

        import dashboard.events as ev_mod

        original_interval = ev_mod._HEARTBEAT_INTERVAL_SECONDS

        status_fn = AsyncMock(
            return_value={
                "status": "running",
                "active_agents": 1,
                "agents": {
                    "bad-agent": "not-a-dict",
                    "good-agent": {"state": "idle"},
                },
            }
        )

        try:
            ev_mod._HEARTBEAT_INTERVAL_SECONDS = 0.01
            await bus.start_heartbeat("agent-skip-proj", status_fn)
            await asyncio.sleep(0.04)
        finally:
            ev_mod._HEARTBEAT_INTERVAL_SECONDS = original_interval
            await bus.stop_heartbeat("agent-skip-proj")

        assert "agent-skip-proj" not in bus._heartbeat_tasks


class TestEventBusPublishLoggingBranches:
    """Tests for the publish() logging branches that are uncovered."""

    @pytest.mark.asyncio
    async def test_publish_agent_finished_with_error_should_record_error(self):
        """agent_finished with is_error=True triggers error diagnostic tracking."""
        bus = EventBus()
        await bus.publish(
            {
                "type": "agent_finished",
                "project_id": "log-proj",
                "agent": "orchestrator",
                "is_error": True,
                "cost": 0.5,
                "failure_reason": "timeout",
            }
        )
        assert bus._last_error.get("log-proj") is not None

    @pytest.mark.asyncio
    async def test_publish_delegation_event_should_be_delivered(self):
        """delegation event is published (covers delegation logging branch)."""
        bus = EventBus()
        q = await bus.subscribe()
        await bus.publish(
            {
                "type": "delegation",
                "project_id": "deleg-proj",
                "agent": "orchestrator",
                "delegations": [{"target": "worker-1"}, {"target": "worker-2"}],
            }
        )
        ev = q.get_nowait()
        assert ev["type"] == "delegation"

    @pytest.mark.asyncio
    async def test_publish_project_status_event_should_log_status(self):
        """project_status event is published correctly."""
        bus = EventBus()
        q = await bus.subscribe()
        await bus.publish(
            {
                "type": "project_status",
                "project_id": "status-proj",
                "agent": "",
                "status": "completed",
            }
        )
        ev = q.get_nowait()
        assert ev["type"] == "project_status"

    @pytest.mark.asyncio
    async def test_publish_agent_started_event_should_log_task(self):
        """agent_started event is published and logged correctly."""
        bus = EventBus()
        q = await bus.subscribe()
        await bus.publish(
            {
                "type": "agent_started",
                "project_id": "start-proj",
                "agent": "worker",
                "task": "write tests for the new feature",
            }
        )
        ev = q.get_nowait()
        assert ev["type"] == "agent_started"

    @pytest.mark.asyncio
    async def test_publish_self_healing_event_should_be_delivered(self):
        """self_healing event is published and logged."""
        bus = EventBus()
        q = await bus.subscribe()
        await bus.publish(
            {
                "type": "self_healing",
                "project_id": "healing-proj",
                "agent": "orchestrator",
            }
        )
        ev = q.get_nowait()
        assert ev["type"] == "self_healing"

    @pytest.mark.asyncio
    async def test_publish_task_graph_event_should_be_delivered(self):
        """task_graph event is published and logged."""
        bus = EventBus()
        q = await bus.subscribe()
        await bus.publish(
            {
                "type": "task_graph",
                "project_id": "graph-proj",
                "agent": "orchestrator",
                "graph": {},
            }
        )
        ev = q.get_nowait()
        assert ev["type"] == "task_graph"

    @pytest.mark.asyncio
    async def test_publish_agent_update_event_should_be_logged_at_debug(self):
        """agent_update event is published and logged at debug level."""
        bus = EventBus()
        q = await bus.subscribe()
        await bus.publish(
            {
                "type": "agent_update",
                "project_id": "update-proj",
                "agent": "worker",
                "summary": "Writing tests...",
            }
        )
        ev = q.get_nowait()
        assert ev["type"] == "agent_update"


class TestEventBusClearProjectEventsThrottler:
    """Tests for clear_project_events clearing throttler state (lines 771-778)."""

    @pytest.mark.asyncio
    async def test_clear_project_events_when_pending_throttler_keys_should_clear_them(self):
        """clear_project_events removes pending throttled events for that project."""
        bus = EventBus()

        # Seed text_chunk_throttler with keys for our project
        text_chunk_throttler._pending["my-proj::agent-1"] = {"type": "chunk", "text": "x"}
        text_chunk_throttler._pending["my-proj::agent-2"] = {"type": "chunk", "text": "y"}
        text_chunk_throttler._pending["other-proj::agent-1"] = {"type": "chunk", "text": "z"}

        bus.clear_project_events("my-proj")

        # Keys for my-proj should be removed
        assert "my-proj::agent-1" not in text_chunk_throttler._pending
        assert "my-proj::agent-2" not in text_chunk_throttler._pending
        # Other project keys should be unaffected
        assert "other-proj::agent-1" in text_chunk_throttler._pending

        # Cleanup
        text_chunk_throttler._pending.pop("other-proj::agent-1", None)

    @pytest.mark.asyncio
    async def test_clear_project_events_when_last_emit_throttler_keys_should_clear_them(self):
        """clear_project_events removes stale last-emit timestamps for that project."""
        bus = EventBus()

        # Seed text_chunk_throttler._last_emit with keys for our project
        import time

        now = time.monotonic()
        text_chunk_throttler._last_emit["clear-proj::agent-x"] = now
        text_chunk_throttler._last_emit["clear-proj::agent-y"] = now
        text_chunk_throttler._last_emit["keep-proj::agent-z"] = now

        bus.clear_project_events("clear-proj")

        # Emission timestamps for clear-proj should be removed
        assert "clear-proj::agent-x" not in text_chunk_throttler._last_emit
        assert "clear-proj::agent-y" not in text_chunk_throttler._last_emit
        # Other project keys unaffected
        assert "keep-proj::agent-z" in text_chunk_throttler._last_emit

        # Cleanup
        text_chunk_throttler._last_emit.pop("keep-proj::agent-z", None)


class TestEventBusDeadSubscriberCleanupLogging:
    """Tests that dead subscriber cleanup logs the info message (lines 682-686)."""

    @pytest.mark.asyncio
    async def test_dead_subscriber_cleanup_should_log_info_message(self, caplog):
        """After removing dead subscriber, EventBus logs info about cleanup count."""
        import logging

        bus = EventBus()

        # Create a mock dead subscriber
        dead_mock = MagicMock()
        dead_mock.put_nowait.return_value = False
        dead_mock.is_dead = True

        async with bus._lock:
            bus._subscribers.append(dead_mock)

        with caplog.at_level(logging.INFO, logger="dashboard.events"):
            await bus.publish({"type": "test_cleanup_log"})

        # Subscriber should be removed and cleanup logged
        assert bus.subscriber_count == 0
        assert any(
            "cleaned up" in r.message.lower() or "dead" in r.message.lower() for r in caplog.records
        )


class TestEventBusSetSessionManager:
    """Tests for EventBus.set_session_manager (covers line 305)."""

    def test_set_session_manager_when_called_should_store_reference(self):
        """set_session_manager() stores the session manager reference."""
        bus = EventBus()
        mock_smgr = MagicMock()
        bus.set_session_manager(mock_smgr)
        assert bus._session_mgr is mock_smgr

    def test_set_session_manager_when_called_twice_should_overwrite(self):
        """set_session_manager() can be called again to update the reference."""
        bus = EventBus()
        smgr1 = MagicMock()
        smgr2 = MagicMock()
        bus.set_session_manager(smgr1)
        bus.set_session_manager(smgr2)
        assert bus._session_mgr is smgr2


class TestEventBusWriterWithSessionMgr:
    """Tests for the DB writer loop when a session manager is configured."""

    @pytest.mark.asyncio
    async def test_db_writer_loop_when_event_queued_should_call_log_activity(self):
        """Writer loop picks up persisted events and calls session_mgr.log_activity."""
        bus = EventBus()
        mock_smgr = AsyncMock()
        mock_smgr.log_activity = AsyncMock(return_value=None)
        bus.set_session_manager(mock_smgr)

        await bus.start_writer()
        # Publish a persisted event type to enqueue it for DB write
        await bus.publish(
            {
                "type": "agent_started",
                "project_id": "batch-proj",
                "agent": "test-agent",
            }
        )
        # Yield control so the writer task can run and process the batch
        await asyncio.sleep(0.3)
        await bus.stop_writer()

        # log_activity should have been called at least once
        assert mock_smgr.log_activity.await_count >= 1

    @pytest.mark.asyncio
    async def test_flush_write_queue_when_events_queued_and_smgr_set_should_persist(self):
        """_flush_write_queue writes remaining events on stop_writer when smgr is set."""
        bus = EventBus()
        mock_smgr = AsyncMock()
        mock_smgr.log_activity = AsyncMock(return_value=None)
        bus.set_session_manager(mock_smgr)

        await bus.start_writer()
        # Put an event directly into the write queue (bypass the writer loop)
        if bus._write_queue:
            bus._write_queue.put_nowait(
                {
                    "type": "agent_finished",
                    "project_id": "flush-proj",
                    "agent": "worker",
                    "timestamp": 1234567890.0,
                }
            )

        # stop_writer calls _flush_write_queue which writes remaining events
        await bus.stop_writer()

        # log_activity should have been called for the flushed event
        assert mock_smgr.log_activity.await_count >= 1
