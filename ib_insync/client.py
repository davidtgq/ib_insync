import struct
import asyncio
import logging
import time
import io
from collections import deque
from typing import List

import ibapi
from ibapi.client import EClient
from ibapi.wrapper import EWrapper, iswrapper
from ibapi.common import UNSET_INTEGER, UNSET_DOUBLE

from ib_insync.objects import ConnectionStats
from ib_insync.contract import Contract
import ib_insync.util as util

__all__ = ['Client']


class Client(EClient):
    """
    Modification of ``ibapi.client.EClient`` that uses asyncio.
    
    The client is fully asynchronous and has its own
    event-driven networking code that replaces the
    networking code of the standard EClient.
    It also replaces the infinite loop of ``EClient.run()``
    with the asyncio event loop. It can be used as a drop-in
    replacement for the standard EClient as provided by IBAPI.
    
    Compared to the standard EClient this client has the following
    additional features:
    
    * ``client.connect()`` will block until the client is ready to
      serve requests; It is not necessary to wait for ``nextValidId``
      to start requests as the client has already done that.
      The reqId is directly available with :py:meth:`.getReqId()`.
      
    * ``client.connectAsync()`` is a coroutine for connecting asynchronously.
      
    * When blocking, ``client.connect()`` can be made to time out with
      the timeout parameter (default 2 seconds).
    
    * Optional ``wrapper.priceSizeTick(reqId, tickType, price, size)`` that
      combines price and size instead of the two wrapper methods
      priceTick and sizeTick.
    
    * Optional ``wrapper.tcpDataArrived()`` method;
      If the wrapper has this method it is invoked directly after
      a network packet has arrived.
      A possible use is to timestamp all data in the packet with
      the exact same time.
      
    * Optional ``wrapper.tcpDataProcessed()`` method;
      If the wrapper has this method it is invoked after the
      network packet's data has been handled.
      A possible use is to write or evaluate the newly arrived data in
      one batch instead of item by item.
      
    * Optional callbacks:
        - apiStart()
        - apiEnd()
        - apiError(errorMsg)
    """

    # throttle number of requests to MaxRequests per RequestsInterval seconds
    MaxRequests, RequestsInterval = 250, 5

    def __init__(self, wrapper):
        EClient.__init__(self, wrapper)
        self._logger = logging.getLogger('ib_insync.client')

        # extra optional wrapper methods
        self._priceSizeTick = getattr(wrapper, 'priceSizeTick', None)
        self._tcpDataArrived = getattr(wrapper, 'tcpDataArrived', None)
        self._tcpDataProcessed = getattr(wrapper, 'tcpDataProcessed', None)

        # optional callbacks
        self.apiStart = None
        self.apiEnd = None
        self.apiError = None

    def reset(self):
        EClient.reset(self)
        self._data = b''
        self._readyEvent = asyncio.Event()
        self._reqIdSeq = 0
        self._accounts = None
        self._startTime = time.time()
        self._numBytesRecv = 0
        self._numMsgRecv = 0
        self._isThrottling = False
        self._msgQ = deque()
        self._timeQ = deque()

    def run(self):
        loop = asyncio.get_event_loop()
        loop.run_forever()

    def isReady(self) -> bool:
        """
        Is the API connection up and running?
        """
        return self._readyEvent.is_set()

    def connectionStats(self) -> ConnectionStats:
        """
        Get statistics about the connection.
        """
        assert self.isReady(), 'Not connected'
        return ConnectionStats(
                self._startTime,
                time.time() - self._startTime,
                self._numBytesRecv, self.conn.numBytesSent,
                self._numMsgRecv, self.conn.numMsgSent)

    def getReqId(self) -> int:
        """
        Get new request ID.
        """
        assert self.isReady(), 'Not connected'
        newId = self._reqIdSeq
        self._reqIdSeq += 1
        return newId

    def getAccounts(self) -> List[str]:
        """
        Get the list of account names that are under management.
        """
        assert self.isReady(), 'Not connected'
        return self._accounts

    def connect(self, host, port, clientId, timeout=2):
        """
        Connect to TWS/IBG at given host and port and with a clientId
        that is not in use elsewhere.
        
        When timeout is not zero, asyncio.TimeoutError
        is raised if the connection is not established within the timeout period.
        """
        util.syncAwait(self.connectAsync(host, port, clientId, timeout))

    async def connectAsync(self, host, port, clientId, timeout=2):
        self._logger.info(
                f'Connecting to {host}:{port} with clientId {clientId}...')
        self.host = host
        self.port = port
        self.clientId = clientId
        self.setConnState(EClient.CONNECTING)
        self.conn = Connection(host, port)
        self.conn.connected = self._onSocketConnected
        self.conn.hasData = self._onSocketHasData
        self.conn.disconnected = self._onSocketDisconnected
        self.conn.hasError = self._onSocketHasError
        try:
            await asyncio.wait_for(asyncio.gather(
                    self.conn.connect(), self._readyEvent.wait()), timeout)
            self._logger.info('API connection ready')
            if self.apiStart:
                self.apiStart()
        except Exception as e:
            self.reset()
            msg = f'API connection failed: {e!r}'
            self._logger.error(msg)
            if self.apiError:
                self.apiError(msg)
            if isinstance(e, ConnectionRefusedError):
                msg = 'Make sure API port on TWS/IBG is open'
                self._logger.error(msg)
            raise

    def sendMsg(self, msg):
        loop = asyncio.get_event_loop()
        t = loop.time()
        times = self._timeQ
        msgs = self._msgQ
        while times and t - times[0] > Client.RequestsInterval:
            times.popleft()
        if msg:
            msgs.append(msg)
        while msgs and len(times) < Client.MaxRequests:
            msg = msgs.popleft()
            self.conn.sendMsg(self._prefix(msg.encode()))
            times.append(t)
        if msgs:
            if not self._isThrottling:
                self._isThrottling = True
                self._logger.warn('Started to throttle requests')
            loop.call_at(times[0] + Client.RequestsInterval, self.sendMsg, None)
        else:
            if self._isThrottling:
                self._isThrottling = False
                self._logger.warn('Stopped to throttle requests')

    def _prefix(self, msg):
        # prefix a message with its length
        return struct.pack('>I', len(msg)) + msg

    def _onSocketConnected(self):
        self._logger.info('Connected')
        # start handshake
        msg = b'API\0'
        msg += self._prefix(b'v%d..%d' % (
                ibapi.server_versions.MIN_CLIENT_VER,
                ibapi.server_versions.MAX_CLIENT_VER))
        self.conn.sendMsg(msg)
        self.decoder = ibapi.decoder.Decoder(self.wrapper, None)

    def _onSocketHasData(self, data):
        debug = self._logger.isEnabledFor(logging.DEBUG)
        if self._tcpDataArrived:
            self._tcpDataArrived()

        self._data += data
        self._numBytesRecv += len(data)

        while True:
            if len(self._data) <= 4:
                break
            # 4 byte prefix tells the message length
            msgEnd = 4 + struct.unpack('>I', self._data[:4])[0]
            if len(self._data) < msgEnd:
                # insufficient data for now
                break
            msg = self._data[4:msgEnd]
            self._data = self._data[msgEnd:]
            fields = msg.split(b'\0')
            fields.pop()  # pop off last empty element
            self._numMsgRecv += 1

            if debug:
                self._logger.debug('<<< %s', ','.join(f.decode() for f in fields))

            if not self.serverVersion_ and len(fields) == 2:
                # this concludes the handshake
                version, self.connTime = fields
                self.serverVersion_ = int(version)
                self.decoder.serverVersion = self.serverVersion_
                self.setConnState(EClient.CONNECTED)
                self.startApi()
                self.wrapper.connectAck()
                self._logger.info(
                        f'Logged on to server version {self.serverVersion_}')
            else:
                # decode and handle the message
                try:
                    self._decode(fields)
                except:
                    self._logger.exception('Decode failed')

        if self._tcpDataProcessed:
            self._tcpDataProcessed()

    def _onSocketDisconnected(self):
        if self.isConnected():
            msg = f'Peer closed connection'
            self._logger.error(msg)
            if not self.isReady():
                msg = f'clientId {self.clientId} already in use?'
                self._logger.error(msg)
            if self.apiError:
                self.apiError(msg)
        else:
            self._logger.info('Disconnected')
        self.reset()
        if self.apiEnd:
            self.apiEnd()

    def _onSocketHasError(self, msg):
        self._logger.error(msg)
        self.reset()
        if self.apiError:
            self.apiError(msg)

    def _encode(self, *fields):
        """
        Serialize the given fields to a string conforming to the
        IB socket protocol.
        """
        result = io.StringIO()
        for field in fields:
            if field in (None, UNSET_INTEGER, UNSET_DOUBLE):
                s = ''
            elif isinstance(field, Contract):
                c = field
                s = '\0'.join(str(f) for f in (
                        c.conId, c.symbol, c.secType,
                        c.lastTradeDateOrContractMonth, c.strike,
                        c.right, c.multiplier, c.exchange,
                        c.primaryExchange, c.currency,
                        c.localSymbol, c.tradingClass,
                        1 if c.includeExpired else 0))
            elif type(field) is list:
                # list of TagValue
                s = ''.join(f'{v.tag}={v.value};' for v in field)
            elif type(field) is bool:
                s = '1' if field else '0'
            else:
                s = str(field)

            result.write(s)
            result.write('\0')
        return result.getvalue()

    def _decode(self, fields):
        """
        Decode the fields of the single response and call the appropriate
        callback handler.
        """
        msgId = int(fields[0])

        # bypass the ibapi decoder for ticks for more efficiency
        if msgId == 1:
            if self._priceSizeTick:
                _, _, reqId, tickType, price, size, _ = fields
                self._priceSizeTick(int(reqId), int(tickType),
                        float(price), int(size))
                return
        elif msgId == 2:
            _, _, reqId, tickType, size = fields
            self.wrapper.tickSize(int(reqId), int(tickType), int(size))
            return
        elif msgId == 12:
            _, _, reqId, position, operation, side, price, size = fields
            self.wrapper.updateMktDepth(int(reqId), int(position),
                    int(operation), int(side), float(price), int(size))
            return
        elif msgId == 46:
            _, _, reqId, tickType, value = fields
            self.wrapper.tickString(int(reqId), int(tickType), value.decode())
            return

        # snoop for nextValidId and managedAccounts response,
        # when both are in then the client is ready
        elif msgId == 9:
            _, _, validId = fields
            self._reqIdSeq = int(validId)
            if self._accounts:
                self._readyEvent.set()
        elif msgId == 15:
            _, _, accts = fields
            self._accounts = accts.decode().split(',')
            if self._reqIdSeq:
                self._readyEvent.set()

        self.decoder.interpret(fields)


class Connection:
    """
    Replacement for ibapi.connection.Connection that uses asyncio.
    """
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.socket = None
        self.numBytesSent = 0
        self.numMsgSent = 0
        self._logger = logging.getLogger('ib_insync.connection')

        # the following are callbacks for socket events
        self.connected = None
        self.disconnected = None
        self.hasError = None
        self.hasData = None

    def _onConnectionCreated(self, future):
        if not future.exception():
            _, self.socket = future.result()
            self.connected()

    def connect(self):
        loop = asyncio.get_event_loop()
        coro = loop.create_connection(lambda: Socket(self),
                self.host, self.port)
        future = asyncio.ensure_future(coro)
        future.add_done_callback(self._onConnectionCreated)
        return future

    def disconnect(self):
        if self.socket:
            self.socket.transport.close()
            self.socket = None

    def isConnected(self):
        return self.socket is not None

    def sendMsg(self, msg):
        self.socket.transport.write(msg)
        self.numBytesSent += len(msg)
        self.numMsgSent += 1
        if self._logger.isEnabledFor(logging.DEBUG):
            self._logger.debug(
                '>>> %s', ','.join(f.decode() for f in msg[4:].split(b'\0')))


class Socket(asyncio.Protocol):

    def __init__(self, connection):
        self.transport = None
        self.connection = connection

    def connection_made(self, transport):
        self.transport = transport

    def connection_lost(self, exc):
        if exc:
            self.connection.hasError(exc.strerror)
        else:
            self.connection.disconnected()

    def data_received(self, data):
        self.connection.hasData(data)


class TestClient(Client, EWrapper):
    """
    Test to connect to a running TWS or gateway server.
    """
    def __init__(self):
        Client.__init__(self, wrapper=self)

    @iswrapper
    def managedAccounts(self, accountsList):
        print(self.__class__.__name__, accountsList)


if __name__ == '__main__':
    util.logToConsole(logging.DEBUG)
    client = TestClient()
    client.connect(host='127.0.0.1', port=7497, clientId=1)
    client.disconnect()
