"""Pipecat stage lifecycle shared by mock and NVIDIA turns."""

import asyncio
from dataclasses import dataclass

from loguru import logger
from pipecat.frames.frames import DataFrame, EndFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.workers.runner import WorkerRunner

logger.disable("pipecat")


@dataclass
class TurnFrame(DataFrame):
    error: BaseException | None = None


class Stage(FrameProcessor):
    def __init__(self, operation, interrupted):
        super().__init__()
        self.operation, self.interrupted = operation, interrupted
        self.active = None

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, TurnFrame) and frame.error is None:
            try:
                if self.interrupted.is_set():
                    raise asyncio.CancelledError()
                self.active = asyncio.create_task(self.operation())
                await self.active
            except BaseException as exc:
                if not isinstance(exc, (Exception, asyncio.CancelledError)):
                    raise
                frame.error = exc
        await self.push_frame(frame, direction)


async def run_stages(operations):
    interrupted = asyncio.Event()
    frame = TurnFrame()
    stages = [Stage(operation, interrupted) for operation in operations]
    worker = PipelineWorker(
        Pipeline(stages),
        enable_rtvi=False,
        enable_turn_tracking=False,
        idle_timeout_secs=None,
        cancel_timeout_secs=0.5,
    )
    runner = WorkerRunner(handle_sigint=False, check_dangling_tasks=False)
    await worker.queue_frames([frame, EndFrame()])
    await runner.add_workers(worker)
    running = asyncio.create_task(runner.run())
    try:
        await asyncio.shield(running)
    finally:
        if not running.done():
            interrupted.set()
            for stage in stages:
                if stage.active and not stage.active.done():
                    stage.active.cancel()
            await running
    if frame.error is not None:
        raise frame.error
