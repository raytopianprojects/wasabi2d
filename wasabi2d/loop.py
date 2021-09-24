"""The event loop for Wasabi2D.

This is now based on coroutines and tasks but adapted to call synchronous
functions, for backwards compatibility.
"""
from os import initgroups
import time
import types
from typing import Optional
from functools import total_ordering
import heapq

import pygame.event


@types.coroutine
def _block(reschedule=False):
    """An awaitable that yields obj."""
    return (yield reschedule)


def resume_callback():
    """Get a callback to resume the current task.

    The callback can be called with an optional single argument which is the
    value that will be returned from the await.
    """
    return current_task._resume


async def next_tick() -> float:
    """Await the next tick.

    Return the amount of time that has passed in seconds (dt).
    """
    return (await _block(True))


class Cancelled(Exception):
    """An exception raised indicating a coroutine has been cancelled."""
    def __init__(self, scope=None):
        super().__init__(scope or set())

    @property
    def scope(self):
        return self.args[0]

    def _handle(self, scope):
        """Handle the given scope.

        Return True if all scopes in this exception have now been handled,
        meaning flow should continue.
        """
        scopes = self.args[0]
        scopes.discard(scope)
        if not scopes:
            current_task.cancelled = None
            return True
        return False


def do(coro):
    """Schedule a task as runnable."""
    if isinstance(coro, types.CoroutineType):
        if coro.cr_running:
            raise RuntimeError(f"{coro!r} is already started")
    else:
        try:
            await_ = coro.__await__
        except AttributeError:
            if isinstance(coro, types.FunctionType):
                raise RuntimeError(
                    f"{coro.__qualname__} is a function object; "
                    "did you forget to call it?"
                ) from None
            raise RuntimeError(
                f"{coro!r} is not a coroutine or awaitable"
            ) from None
        coro = await_()

    task = Task(coro)
    task._resume()  # Start immediately, ie. this frame
    return task


class Event:
    """A concurrency primitive where many tasks can wait for one event.

    Events start in an "un-set" state. In this state a task waiting for the
    event will block. Calling .set() will release all tasks, and subsequent
    waits will not block.

    """
    __slots__ = ('_is_set', 'waiters')

    def __init__(self):
        """Construct an event. Events are initially in the un-set state."""
        self._is_set = False
        self.waiters = set()

    def __bool__(self):
        """Return True if the event is set."""
        return self._is_set

    is_set = __bool__

    def set(self):
        """Set the event.

        This will release all blocked tasks. Subsequent waits will not block.
        """
        for waiter in self.waiters:
            waiter()
        self.waiters.clear()
        self._is_set = True

    def reset(self):
        """Reset the event.

        Subsequent waits will block until the event is set again.
        """
        self._is_set = False

    def __await__(self):
        """Await this event object."""
        return self.wait().__await__()

    async def wait(self):
        """Wait for the event to be set."""
        if self._is_set:
            return
        resume = resume_callback()
        try:
            self.waiters.add(resume)
            await _block()
        except Cancelled:
            self.waiters.discard(resume)
            raise


@total_ordering
class Task:
    """The individual unit of concurrency.

    Each task has its own call stack of async functions. Tasks run until they
    complete or block. Tasks will only block at `await` statements (but not
    all `await` statements will cause the task to block).

    Additionally, each task can be cancelled.

    """
    next_id = 0

    __slots__ = (
        'id',
        '_scheduled',
        'coro',
        'failed',
        'finished',
        'result',
        'cancelled',
        'joiners',
        '_resume_value',
    )

    def __init__(self, coro):
        self.id = Task.next_id
        Task.next_id += 1

        self._scheduled = False
        self.coro = coro
        self.failed = False
        self.finished = False
        self.result = None
        self.cancelled = False  # Have we been cancelled?
        self.joiners = set()  # Other tasks waiting for this one
        self._resume_value = None  # The next value to send to the task

    def __repr__(self):
        return f"Task({self.coro!r})"

    def __lt__(self, other):
        return self.next_id < other.next_id

    def _step(self):
        global current_task
        assert not self.finished, \
            f"{self!r} was rescheduled when already finished"
        self._scheduled = False
        try:
            current_task = self
            if self.cancelled:
                result = self.coro.throw(Cancelled(scope=self.cancelled.scope))
            else:
                v = self._resume_value
                self._resume_value = None
                result = self.coro.send(v)
        except Cancelled as e:
            e.scope.discard(None)
            assert not e.scope, \
                f"Task {self.coro} cancelled with uncaught scopes {e.scope!r}"
            self._finish()
        except StopIteration as e:
            self.result = e.value
            self._finish()
        except BaseException:
            self.failed = True
            self._finish()
            raise
        else:
            if self.cancelled:
                self._resume()
            elif result:
                self._park()
        finally:
            current_task = None

    def _park(self):
        """Block until the next frame."""
        assert not self.finished
        if not self._scheduled:
            heapq.heappush(runnable_next, self)
            self._scheduled = True

    def _resume(self, v=None):
        assert not self.finished
        if not self._scheduled:
            heapq.heappush(runnable, self)
            self._resume_value = v
            self._scheduled = True

    def _finish(self):
        self.finished = True
        for j in self.joiners:
            j(self.result)
        self.joiners.clear()

    def cancel(self, scope=None):
        """Cancel this task."""
        if self.finished:
            return

        if not self.cancelled:
            self.cancelled = Cancelled()
        self.cancelled.scope.add(scope)

        if current_task is not self:
            self._resume()

    async def join(self):
        """Wait until this task completes."""
        if self.finished:
            return self.result

        resume = resume_callback()
        try:
            self.joiners.add(resume)
            return (await _block())
        except Cancelled:
            self.joiners.discard(resume)
            raise


runnable = []
runnable_next = []

#: This is the currently executing task, if any.
current_task: Optional[Task] = None


class PygameEvents:
    def __init__(self, evmapper, get_events=pygame.event.get):
        self.evmapper = evmapper
        self.get_events = get_events
        self.waiters = {}

    async def wait(self, *event_types):
        handler = current_task._resume

        for event_type in event_types:
            if event_type in self.waiters:
                self.waiters[event_type].add(handler)
            else:
                self.waiters[event_type] = {handler}

        try:
            return await _block()
        finally:
            for event_type in event_types:
                self.waiters[event_type].discard(handler)

    async def run(self):
        from .game import UpdateEvent, DrawEvent
        from .clock import default_clock
        t = dt = 0.0
        updated = True
        while True:
            events = self.get_events()
            update = UpdateEvent(UpdateEvent, t, dt, None)

            # Because the current draw strategy a single draw is
            # only flipped at the next draw. Therefore we must always issue
            # a draw event. However we pass the "updated" flag and hope the
            # renderer can deal with this.

            draw = DrawEvent(DrawEvent, t, dt, updated)
            events.extend([update, draw])

            updated = False
            for event in events:
                updated |= self.evmapper.dispatch_event(event)
                handlers = self.waiters.get(event.type, ())
                for h in handlers:
                    h(event)
                if handlers:
                    updated = True

            updated |= default_clock.tick(dt)

            dt = await next_tick()
            t += dt


async def frames_dt():
    """Iterate over frames."""
    while True:
        yield (await next_tick())


async def gather(*coros):
    """Wait for all of the given coroutines/tasks to finish."""
    tasks = []
    for coro in coros:
        tasks.append(do(coro))
    for t in tasks:
        await t.join()


class CancelScope:
    __slots__ = ('task',)

    def __init__(self):
        self.task = None

    def __enter__(self):
        assert self.task is None or self.task is current_task
        self.task = current_task
        return self

    def cancel(self):
        if self.task:
            self.task.cancel(scope=self)

    def __exit__(self, cls, inst, tb):
        # If we're being cancelled by this scope then we absorb the
        # cancellation and flow can continue.
        #
        # Otherwise we let the cancellation propagate to an outer scope.
        self.task = None

        handled = False
        if isinstance(inst, Cancelled):
            handled = inst._handle(self)

        if current_task.cancelled and current_task.cancelled._handle(self):
            current_task.cancelled = None

        return handled


class Nursery:
    """A group of coroutines."""
    __slots__ = (
        'tasks',
        'entered',
        'waiter',
        'cancel_scope',
    )

    def __init__(self):
        self.tasks = set()
        self.entered = False
        self.waiter = None
        self.cancel_scope = CancelScope()

    def do(self, coro):
        assert self.entered
        task = do(coro)
        self.tasks.add(task)

        def _result(v):
            self.tasks.discard(task)
            if task.failed:
                self.cancel()
            if not self.tasks and self.waiter:
                self.waiter()

        task.joiners.add(_result)
        return task

    def cancel(self):
        self.cancel_scope.cancel()

    def __enter__(self):
        raise TypeError(
            "Nurseries are async context managers, not sync ones. "
            "You need 'async with' not 'with'."
        )

    def __exit__(self, *_):
        """Needed for __enter__."""

    async def __aenter__(self):
        if self.entered:
            raise RuntimeError("Nursery cannot be entered more than once")
        self.cancel_scope.__enter__()
        self.entered = True
        return self

    def _cancel_all(self):
        """Cancel all tasks in this nursery.

        Also prevent any new tasks being created.
        """
        # Prohibit creating new tasks now that we're cancelled
        self.entered = False
        for t in self.tasks:
            t.cancel()

    async def __aexit__(self, cls, inst, tb):
        if self.waiter:
            raise RuntimeError("A coroutine is already waiting on nursery exit")

        abort = cls is not None or current_task.cancelled
        if abort:
            self._cancel_all()

        # Absorb existing cancellation, but allow new cancellation
        handled = self.cancel_scope.__exit__(cls, inst, tb)
        self.cancel_scope.__enter__()

        self.waiter = resume_callback()
        exc = None
        if not abort or handled:
            exc = None
        elif isinstance(inst, Cancelled):
            exc = Cancelled(scope=inst.scope)

        if current_task.cancelled:
            if exc:
                exc.scope.update(current_task.cancelled.scope)
            else:
                exc = current_task.cancelled
        try:
            while True:
                try:
                    if self.tasks:
                        # We need to block regardless of cancellation state
                        # of this task. So we clear that state and restore it
                        # later from exc.
                        current_task.cancelled = None
                        await _block()
                except Cancelled as e:
                    # If we got cancelled while waiting, cancel tasks
                    self._cancel_all()

                    # Merge this cancellation into exc
                    if exc is None:
                        exc = Cancelled(scope=e.scope)
                    else:
                        exc.scope.update(e.scope)
                else:
                    if exc:
                        if not exc._handle(self.cancel_scope):
                            current_task.cancelled = exc
                            raise exc
                    current_task.cancelled = None
                    return handled
        finally:
            self.waiter = None
            self.entered = False
            self.cancel_scope.__exit__(None, None, None)


# Override timing calculation when recording video
lock_fps = False


def run(main=None, timefunc=time.perf_counter):
    """Run the event loop."""
    global current_task, runnable, runnable_next
    if main:
        main = do(main)

    t = timefunc()
    while True:
        if main and main.finished:
            return main.result
        while runnable:
            task = heapq.heappop(runnable)
            task._step()

        now = timefunc()
        if lock_fps:
            dt = 1.0 / 60.0
        else:
            dt = now - t
        t = now

        for task in runnable_next:
            task._resume_value = dt
        runnable, runnable_next = runnable_next, runnable


def _clear_all():
    """Clear all tasks. For use in testing only."""
    for task in runnable + runnable_next:
        # Cancel them in case they get rescheduled somehow
        task.cancelled = True
    runnable.clear()
    runnable_next.clear()