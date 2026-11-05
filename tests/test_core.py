import unittest
from concurrent.futures import ThreadPoolExecutor

from retail_voice.admission import State
from retail_voice.catalog import execute_tool, lookup_inventory
from retail_voice.errors import Conflict, Denied


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.state = State(clock=lambda: self.now)

    def test_atomic_single_slot(self):
        def reserve(i):
            try:
                return self.state.reserve("retailer-a", str(i))
            except Conflict:
                return None

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(reserve, range(40)))
        self.assertEqual(sum(s is not None for s in results), 1)

    def test_setup_retry_returns_same_session(self):
        a = self.state.reserve("retailer-a", "one")
        self.assertIs(self.state.reserve("retailer-a", "one"), a)

    def test_unused_reservation_expires_at_30_seconds(self):
        a = self.state.reserve("retailer-a", "one")
        self.now = 30
        with self.assertRaises(Denied):
            self.state.authorize(a.id, a.token)
        with self.assertRaises(Conflict):
            self.state.reserve("retailer-a", "one")
        self.state.reserve("retailer-a", "two")

    def test_active_idle_timer_refreshes_at_turn_completion(self):
        a = self.state.reserve("retailer-a", "one")
        self.state.begin(a.id, a.token, "turn")
        self.now = 400  # Execution holds capacity even past idle timeout.
        self.assertIs(self.state.authorize(a.id, a.token), a)
        self.state.finish(a, {"turn_id": "t"})
        self.now = 699
        self.assertIs(self.state.authorize(a.id, a.token), a)
        self.now = 700
        with self.assertRaises(Denied):
            self.state.authorize(a.id, a.token)

    def test_drain_keeps_existing_session(self):
        a = self.state.reserve("retailer-a", "one")
        self.state.drain = True
        self.assertIs(self.state.authorize(a.id, a.token), a)
        with self.assertRaises(Conflict):
            self.state.reserve("retailer-b", "two")

    def test_end_holds_capacity_until_execution_stops(self):
        a = self.state.reserve("retailer-a", "one")
        self.state.begin(a.id, a.token, "turn")
        self.assertTrue(self.state.end(a.id, a.token))
        # The closing session still holds its tenant slot and the execution permit.
        with self.assertRaises(Conflict) as caught:
            self.state.reserve("retailer-a", "two")
        self.assertEqual(str(caught.exception), "tenant_session_active")
        b = self.state.reserve("retailer-b", "three")
        self.state.begin(b.id, b.token, "b-turn")
        self.assertTrue(b.ticket.waiting)
        self.state.finish(a, {"turn_id": "t"})
        self.assertFalse(a.results)
        self.assertFalse(b.ticket.waiting)
        self.assertIs(self.state.executing, b.ticket)
        self.state.reserve("retailer-a", "two")

    def test_history_bounded_and_pruned(self):
        self.state.max_keys = 2
        for key in ["one", "two"]:
            a = self.state.reserve("retailer-a", key)
            self.state.end(a.id, a.token)
        with self.assertRaises(Conflict):
            self.state.reserve("retailer-a", "three")
        self.now = 601
        self.state.reserve("retailer-a", "three")
        self.assertEqual(len(self.state.requests), 1)

    def test_turn_retry_and_payload_binding(self):
        a = self.state.reserve("retailer-a", "one")
        self.state.begin(a.id, a.token, "turn", "hash-a")
        with self.assertRaises(Conflict):
            self.state.begin(a.id, a.token, "another")
        result = {"turn_id": "t"}
        self.state.finish(a, result)
        self.assertIs(self.state.begin(a.id, a.token, "turn", "hash-a")[1], result)
        with self.assertRaises(Conflict):
            self.state.begin(a.id, a.token, "turn", "hash-b")

    def test_catalog_enforces_scope_and_schema(self):
        self.assertEqual(lookup_inventory("retailer-a", "jacket-a", "store-a")["navy_medium"], 3)
        for name, arguments in [
            ("lookup_inventory", {"sku": "jacket-b", "store_id": "store-b"}),
            (
                "lookup_inventory",
                {"sku": "jacket-a", "store_id": "store-a", "tenant": "retailer-b"},
            ),
            ("shell", {"sku": "jacket-a", "store_id": "store-a"}),
        ]:
            with self.assertRaises(Denied):
                execute_tool("retailer-a", name, arguments)


class RetainedSessionTests(unittest.TestCase):
    """two retained sessions, one executing turn, at most one queued turn."""

    def setUp(self):
        self.now = 0
        self.state = State(clock=lambda: self.now)

    def test_both_tenants_hold_sessions_and_a_third_is_refused(self):
        a = self.state.reserve("retailer-a", "a")
        b = self.state.reserve("retailer-b", "b")
        self.assertIsNot(a, b)
        for tenant in ("retailer-a", "retailer-b"):
            with self.assertRaises(Conflict) as caught:
                self.state.reserve(tenant, "third")
            self.assertEqual(str(caught.exception), "tenant_session_active")
        with self.assertRaises(Conflict) as caught:
            self.state.reserve("retailer-c", "fourth")
        self.assertEqual(str(caught.exception), "capacity_full")
        self.assertEqual(self.state.snapshot()["retained_sessions"]["used"], 2)

    def test_concurrent_reservations_admit_one_session_per_tenant(self):
        def reserve(i):
            try:
                return self.state.reserve(("retailer-a", "retailer-b")[i % 2], str(i))
            except Conflict:
                return None

        with ThreadPoolExecutor(max_workers=16) as pool:
            sessions = [s for s in pool.map(reserve, range(60)) if s is not None]
        self.assertEqual(sorted(s.tenant for s in sessions), ["retailer-a", "retailer-b"])

    def test_turns_serialize_and_the_queue_holds_one(self):
        self.state.max_sessions = 3
        a = self.state.reserve("retailer-a", "a")
        b = self.state.reserve("retailer-b", "b")
        c = self.state.reserve("retailer-c", "c")
        self.state.begin(a.id, a.token, "ta")
        self.state.begin(b.id, b.token, "tb")
        self.assertFalse(a.ticket.waiting)
        self.assertTrue(b.ticket.waiting)
        self.assertEqual(self.state.snapshot()["executing"], 1)
        self.assertEqual(self.state.snapshot()["queued"], 1)
        with self.assertRaises(Conflict) as caught:
            self.state.begin(c.id, c.token, "tc")
        self.assertEqual(str(caught.exception), "queue_full")
        self.assertIsNone(c.ticket)
        self.assertEqual(c.turn_count, 0)
        self.state.finish(a, {"turn_id": "t1"})
        self.assertIs(self.state.executing, b.ticket)
        self.assertIsNone(self.state.queued)
        self.state.begin(c.id, c.token, "tc")
        self.assertTrue(c.ticket.waiting)

    def test_one_active_or_queued_turn_per_session_and_retry_takes_no_second_position(self):
        a = self.state.reserve("retailer-a", "a")
        b = self.state.reserve("retailer-b", "b")
        self.state.begin(a.id, a.token, "ta", "fa")
        self.state.begin(b.id, b.token, "tb", "fb")
        for key in ("tb", "another"):
            with self.assertRaises(Conflict) as caught:
                self.state.begin(b.id, b.token, key, "fb")
            self.assertEqual(str(caught.exception), "turn_in_progress")
        with self.assertRaises(Conflict):
            self.state.begin(a.id, a.token, "ta", "fa")
        self.assertEqual((a.turn_count, b.turn_count), (0, 0))
        self.assertEqual(self.state.snapshot()["queued"], 1)

    def test_queued_cancel_removes_only_that_work(self):
        a = self.state.reserve("retailer-a", "a")
        b = self.state.reserve("retailer-b", "b")
        self.state.begin(a.id, a.token, "ta")
        self.state.begin(b.id, b.token, "tb")
        running = self.state.executing
        self.state.finish(b, {"turn_id": "tb", "status": "failed", "reason": "cancelled"})
        self.assertIsNone(self.state.queued)
        self.assertIs(self.state.executing, running)
        self.assertEqual(b.turn_count, 1)
        self.assertIsNone(b.ticket)

    def test_end_removes_queued_work_and_keeps_the_running_turn(self):
        a = self.state.reserve("retailer-a", "a")
        b = self.state.reserve("retailer-b", "b")
        self.state.begin(a.id, a.token, "ta")
        self.state.begin(b.id, b.token, "tb")
        self.assertTrue(self.state.end(b.id, b.token))
        self.state.finish(b, {"turn_id": "tb", "status": "failed", "reason": "cancelled"})
        self.assertNotIn(b.id, self.state.sessions)
        self.assertIsNone(self.state.queued)
        self.assertIs(self.state.executing, a.ticket)

    def test_sixteen_accepted_turns_count_failures_not_retries(self):
        a = self.state.reserve("retailer-a", "a")
        for i in range(16):
            s, cached = self.state.begin(a.id, a.token, f"k{i}", "fp")
            self.assertIsNone(cached)
            status = "failed" if i == 3 else "complete"
            self.state.finish(s, {"turn_id": f"t{i}", "status": status})
        self.assertEqual(a.turn_count, 16)
        _, cached = self.state.begin(a.id, a.token, "k3", "fp")
        self.assertEqual(cached["turn_id"], "t3")
        self.assertEqual(a.turn_count, 16)
        with self.assertRaises(Conflict) as caught:
            self.state.begin(a.id, a.token, "k16", "fp")
        self.assertEqual(str(caught.exception), "turn_limit_start_new_session")
        self.assertEqual(a.turn_count, 16)

    def test_waiting_session_does_not_expire_and_idle_expiry_is_300_seconds(self):
        a = self.state.reserve("retailer-a", "a")
        b = self.state.reserve("retailer-b", "b")
        self.state.begin(a.id, a.token, "ta")
        self.state.begin(b.id, b.token, "tb")
        self.now = 1000
        self.assertIs(self.state.authorize(b.id, b.token), b)
        self.state.finish(a, {"turn_id": "ta"})
        self.state.finish(b, {"turn_id": "tb"})
        self.now = 1299
        self.assertIs(self.state.authorize(b.id, b.token), b)
        self.now = 1300
        with self.assertRaises(Denied):
            self.state.authorize(b.id, b.token)
        self.assertEqual(self.state.snapshot()["retained_sessions"]["used"], 0)
