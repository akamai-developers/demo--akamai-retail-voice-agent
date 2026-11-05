import asyncio
import unittest

from retail_voice.admission import State
from retail_voice.api import fixture_media
from retail_voice.models.mock import MockBackend
from retail_voice.pipeline import run_turn


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_backend_failure_has_one_terminal_failure(self):
        class Broken(MockBackend):
            async def transcribe(self, audio):
                raise RuntimeError("sensitive backend detail")

        s = State().reserve("retailer-a", "one")
        r = await run_turn(Broken(), s, *fixture_media(), "jacket-a", "store-a")
        self.assertEqual(r["reason"], "backend_failed")
        self.assertNotIn("sensitive", str(r))
        self.assertEqual([e["event_type"] for e in r["events"]], ["turn.started", "turn.failed"])

    async def test_deadline_waits_for_backend_cleanup(self):
        cleaned = asyncio.Event()

        class Slow(MockBackend):
            async def transcribe(self, audio):
                try:
                    await asyncio.sleep(60)
                finally:
                    await asyncio.sleep(0.01)
                    cleaned.set()

        s = State().reserve("retailer-a", "one")
        r = await run_turn(Slow(), s, *fixture_media(), "jacket-a", "store-a", deadline=1)
        self.assertTrue(cleaned.is_set())
        self.assertEqual(r["reason"], "deadline")

    async def test_cancel_waits_for_backend_cleanup(self):
        started, cleaned = asyncio.Event(), asyncio.Event()

        class Slow(MockBackend):
            async def transcribe(self, audio):
                started.set()
                try:
                    await asyncio.sleep(60)
                finally:
                    await asyncio.sleep(0.01)
                    cleaned.set()

        s = State().reserve("retailer-a", "one")
        task = asyncio.create_task(run_turn(Slow(), s, *fixture_media(), "jacket-a", "store-a"))
        await started.wait()
        task.cancel()
        r = await task
        self.assertTrue(cleaned.is_set())
        self.assertEqual(r["reason"], "cancelled")


class MessagePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_deadline_changes_no_memory(self):
        from retail_voice.catalog_repository import default_repository
        from retail_voice.conversation import Conversation
        from retail_voice.pipeline import Message, run_message

        class Slow(MockBackend):
            async def decide(self, messages, tools, tool_choice="auto"):
                await asyncio.sleep(60)

        state = State()
        s = state.reserve("retailer-a", "one")
        s.conversation = Conversation("retailer-a", "store-a")
        s, _ = state.begin(s.id, s.token, "turn")
        r = await run_message(
            Slow(),
            s,
            Message(text="rain jacket"),
            default_repository(),
            deadline=1,
            permit=s.ticket,
        )
        state.finish(s, r)
        self.assertEqual((r["status"], r["reason"]), ("failed", "deadline"))
        self.assertNotIn("commit", r)
        self.assertEqual(r["transcript"], "rain jacket")
        self.assertEqual((s.conversation.exchanges, s.conversation.display_revision), ([], 0))
        self.assertEqual(s.turn_count, 1)
