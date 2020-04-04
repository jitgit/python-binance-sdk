import asyncio
import json
import websockets as ws
from abc import ABC, abstractmethod
import logging

from binance.common.utils import json_stringify
from binance.common.exceptions import StreamDisconnectedException
from binance.common.constants import (
    DEFAULT_RETRY_POLICY,
    DEFAULT_STREAM_TIMEOUT,
    DEFAULT_STREAM_CLOSE_CODE,
    ERROR_PREFIX
)


logger = logging.getLogger(__name__)

KEY_ID = 'id'
KEY_RESULT = 'result'

# TODO: handle error code
# KEY_CODE = 'code'


class StreamBase(ABC):
    def __init__(self,
                 uri,
                 on_message,
                 # We redundant the default value here,
                 #   because `binance.Stream` is also a public class
                 retry_policy=DEFAULT_RETRY_POLICY,
                 timeout=DEFAULT_STREAM_TIMEOUT,
                 ):
        self._on_message = on_message
        self._retry_policy = retry_policy
        self._timeout = timeout

        self._socket = None
        self._conn_task = None
        self._retries = 0

        # message_id
        self._message_id = 0
        self._message_futures = {}

        self._open_future = None
        self._closing = False

        self._uri = uri

    def _set_socket(self, socket):
        if self._open_future:
            self._open_future.set_result(socket)
            self._open_future = None

        self._socket = socket

    def connect(self):
        self._before_connect()

        self._conn_task = asyncio.create_task(self._connect())
        return self

    async def _handle_message(self, msg):
        # > The id used in the JSON payloads is an unsigned INT used as
        # > an identifier to uniquely identify the messages going back and forth
        if KEY_ID in msg and msg[KEY_ID] in self._message_futures:
            message_id = msg[KEY_ID]
            future = self._message_futures[message_id]
            future.set_result(msg[KEY_RESULT])

            del self._message_futures[message_id]
            return

        await self._on_message(msg)

    def _before_connect(self):
        self._open_future = asyncio.Future()

    async def _receive(self):
        try:
            msg = await asyncio.wait_for(
                self._socket.recv(), timeout=self._timeout)
        except asyncio.TimeoutError:
            await self._socket.ping()
            return
        except asyncio.CancelledError:
            return
        else:
            try:
                parsed = json.loads(msg)
            except ValueError as e:
                logger.error(
                    '%sstream message "%s" is an invalid JSON: reason: %s',
                    ERROR_PREFIX,
                    msg,
                    e
                )

                return
            else:
                await self._handle_message(parsed)

    async def _connect(self):
        async with ws.connect(self._uri) as socket:
            self._set_socket(socket)
            self._retries = 0

            try:
                # Do not receive messages if the stream is closing
                while not self._closing:
                    await self._receive()

            except ws.ConnectionClosed:
                # We don't know whether `ws.ConnectionClosed(close_code)` or
                # `asyncio.CancelledError` comes first
                if self._closing:
                    # The socket is closed by `await self.close()`
                    return

                await self._reconnect()

            except asyncio.CancelledError:
                return

            except Exception:
                await self._reconnect()

    async def _reconnect(self):
        self._before_connect()

        # If the retries == 0, we will reconnect immediately
        retries = self._retries
        self._retries += 1

        abandon, delay, reset = self._retry_policy(retries)

        if abandon:
            self._open_future = None
            return

        if reset:
            self._retries = 0

        if delay:
            await asyncio.sleep(delay)

        await self._before_reconnect()
        await self._connect()

    @abstractmethod
    async def _before_reconnect(self):
        pass  # pragma: no-cover

    @abstractmethod
    def _after_close(self):
        pass  # pragma: no-cover

    async def close(self, code=DEFAULT_STREAM_CLOSE_CODE):
        if not self._conn_task:
            raise StreamDisconnectedException(self._uri)

        # A lot of incomming messages might prevent
        #   the socket from gracefully shutting down, which leads `websockets`
        #   to fail connection and result in a 1006 close code.
        # In that situation, we can not properly figure out whether the socket
        #   is closed by socket.close() or network connection error.
        # So just set up a flag to do the trick
        self._closing = True

        tasks = [self._conn_task]

        if self._socket:
            tasks.append(
                # make socket.close run in background
                asyncio.create_task(self._socket.close(code))
            )

        self._conn_task.cancel()

        try:
            # Make sure:
            # - conn_task is cancelled
            # - socket is closed
            await asyncio.wait(tasks)
        except Exception as e:
            logger.error(
                '%sclose tasks error: %s',
                ERROR_PREFIX,
                e
            )

        self._socket = None
        self._closing = False

        self._after_close()

    async def send(self, msg):
        socket = self._socket

        if not socket:
            if self._open_future:
                socket = await self._open_future
            else:
                raise StreamDisconnectedException(self._uri)

        future = asyncio.Future()

        message_id = self._message_id
        self._message_id += 1

        msg[KEY_ID] = message_id
        self._message_futures[message_id] = future

        await socket.send(json_stringify(msg))
        return await future
