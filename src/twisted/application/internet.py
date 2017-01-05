# -*- test-case-name: twisted.application.test.test_internet,twisted.test.test_application,twisted.test.test_cooperator -*-
# Copyright (c) Twisted Matrix Laboratories.
# See LICENSE for details.

"""
Reactor-based Services

Here are services to run clients, servers and periodic services using
the reactor.

If you want to run a server service, L{StreamServerEndpointService} defines a
service that can wrap an arbitrary L{IStreamServerEndpoint
<twisted.internet.interfaces.IStreamServerEndpoint>}
as an L{IService}. See also L{twisted.application.strports.service} for
constructing one of these directly from a descriptive string.

Additionally, this module (dynamically) defines various Service subclasses that
let you represent clients and servers in a Service hierarchy.  Endpoints APIs
should be preferred for stream server services, but since those APIs do not yet
exist for clients or datagram services, many of these are still useful.

They are as follows::

  TCPServer, TCPClient,
  UNIXServer, UNIXClient,
  SSLServer, SSLClient,
  UDPServer,
  UNIXDatagramServer, UNIXDatagramClient,
  MulticastServer

These classes take arbitrary arguments in their constructors and pass
them straight on to their respective reactor.listenXXX or
reactor.connectXXX calls.

For example, the following service starts a web server on port 8080:
C{TCPServer(8080, server.Site(r))}.  See the documentation for the
reactor.listen/connect* methods for more information.
"""

from __future__ import absolute_import, division

from random import random as _goodEnoughRandom

from twisted.python import log
from twisted.logger import Logger

from twisted.application import service
from twisted.internet import task
from twisted.python.failure import Failure
from twisted.internet.defer import (
    CancelledError, Deferred, succeed, fail
)

from automat import MethodicalMachine


def _maybeGlobalReactor(maybeReactor):
    """
    @return: the argument, or the global reactor if the argument is L{None}.
    """
    if maybeReactor is None:
        from twisted.internet import reactor
        return reactor
    else:
        return maybeReactor



class _VolatileDataService(service.Service):

    volatile = []

    def __getstate__(self):
        d = service.Service.__getstate__(self)
        for attr in self.volatile:
            if attr in d:
                del d[attr]
        return d



class _AbstractServer(_VolatileDataService):
    """
    @cvar volatile: list of attribute to remove from pickling.
    @type volatile: C{list}

    @ivar method: the type of method to call on the reactor, one of B{TCP},
        B{UDP}, B{SSL} or B{UNIX}.
    @type method: C{str}

    @ivar reactor: the current running reactor.
    @type reactor: a provider of C{IReactorTCP}, C{IReactorUDP},
        C{IReactorSSL} or C{IReactorUnix}.

    @ivar _port: instance of port set when the service is started.
    @type _port: a provider of L{twisted.internet.interfaces.IListeningPort}.
    """

    volatile = ['_port']
    method = None
    reactor = None

    _port = None

    def __init__(self, *args, **kwargs):
        self.args = args
        if 'reactor' in kwargs:
            self.reactor = kwargs.pop("reactor")
        self.kwargs = kwargs


    def privilegedStartService(self):
        service.Service.privilegedStartService(self)
        self._port = self._getPort()


    def startService(self):
        service.Service.startService(self)
        if self._port is None:
            self._port = self._getPort()


    def stopService(self):
        service.Service.stopService(self)
        # TODO: if startup failed, should shutdown skip stopListening?
        # _port won't exist
        if self._port is not None:
            d = self._port.stopListening()
            del self._port
            return d


    def _getPort(self):
        """
        Wrapper around the appropriate listen method of the reactor.

        @return: the port object returned by the listen method.
        @rtype: an object providing
            L{twisted.internet.interfaces.IListeningPort}.
        """
        return getattr(_maybeGlobalReactor(self.reactor),
                       'listen%s' % (self.method,))(*self.args, **self.kwargs)



class _AbstractClient(_VolatileDataService):
    """
    @cvar volatile: list of attribute to remove from pickling.
    @type volatile: C{list}

    @ivar method: the type of method to call on the reactor, one of B{TCP},
        B{UDP}, B{SSL} or B{UNIX}.
    @type method: C{str}

    @ivar reactor: the current running reactor.
    @type reactor: a provider of C{IReactorTCP}, C{IReactorUDP},
        C{IReactorSSL} or C{IReactorUnix}.

    @ivar _connection: instance of connection set when the service is started.
    @type _connection: a provider of L{twisted.internet.interfaces.IConnector}.
    """

    volatile = ['_connection']
    method = None
    reactor = None

    _connection = None

    def __init__(self, *args, **kwargs):
        self.args = args
        if 'reactor' in kwargs:
            self.reactor = kwargs.pop("reactor")
        self.kwargs = kwargs


    def startService(self):
        service.Service.startService(self)
        self._connection = self._getConnection()


    def stopService(self):
        service.Service.stopService(self)
        if self._connection is not None:
            self._connection.disconnect()
            del self._connection


    def _getConnection(self):
        """
        Wrapper around the appropriate connect method of the reactor.

        @return: the port object returned by the connect method.
        @rtype: an object providing L{twisted.internet.interfaces.IConnector}.
        """
        return getattr(_maybeGlobalReactor(self.reactor),
                       'connect%s' % (self.method,))(*self.args, **self.kwargs)



_doc={
'Client':
"""Connect to %(tran)s

Call reactor.connect%(tran)s when the service starts, with the
arguments given to the constructor.
""",
'Server':
"""Serve %(tran)s clients

Call reactor.listen%(tran)s when the service starts, with the
arguments given to the constructor. When the service stops,
stop listening. See twisted.internet.interfaces for documentation
on arguments to the reactor method.
""",
}

for tran in 'TCP UNIX SSL UDP UNIXDatagram Multicast'.split():
    for side in 'Server Client'.split():
        if tran == "Multicast" and side == "Client":
            continue
        if tran == "UDP" and side == "Client":
            continue
        base = globals()['_Abstract'+side]
        doc = _doc[side] % vars()

        klass = type(tran+side, (base,), {'method': tran, '__doc__': doc})
        globals()[tran+side] = klass



class TimerService(_VolatileDataService):
    """
    Service to periodically call a function

    Every C{step} seconds call the given function with the given arguments.
    The service starts the calls when it starts, and cancels them
    when it stops.

    @ivar clock: Source of time. This defaults to L{None} which is
        causes L{twisted.internet.reactor} to be used.
        Feel free to set this to something else, but it probably ought to be
        set *before* calling L{startService}.
    @type clock: L{IReactorTime<twisted.internet.interfaces.IReactorTime>}

    @ivar call: Function and arguments to call periodically.
    @type call: L{tuple} of C{(callable, args, kwargs)}
    """

    volatile = ['_loop', '_loopFinished']

    def __init__(self, step, callable, *args, **kwargs):
        """
        @param step: The number of seconds between calls.
        @type step: L{float}

        @param callable: Function to call
        @type callable: L{callable}

        @param args: Positional arguments to pass to function
        @param kwargs: Keyword arguments to pass to function
        """
        self.step = step
        self.call = (callable, args, kwargs)
        self.clock = None

    def startService(self):
        service.Service.startService(self)
        callable, args, kwargs = self.call
        # we have to make a new LoopingCall each time we're started, because
        # an active LoopingCall remains active when serialized. If
        # LoopingCall were a _VolatileDataService, we wouldn't need to do
        # this.
        self._loop = task.LoopingCall(callable, *args, **kwargs)
        self._loop.clock = _maybeGlobalReactor(self.clock)
        self._loopFinished = self._loop.start(self.step, now=True)
        self._loopFinished.addErrback(self._failed)

    def _failed(self, why):
        # make a note that the LoopingCall is no longer looping, so we don't
        # try to shut it down a second time in stopService. I think this
        # should be in LoopingCall. -warner
        self._loop.running = False
        log.err(why)

    def stopService(self):
        """
        Stop the service.

        @rtype: L{Deferred<defer.Deferred>}
        @return: a L{Deferred<defer.Deferred>} which is fired when the
            currently running call (if any) is finished.
        """
        if self._loop.running:
            self._loop.stop()
        self._loopFinished.addCallback(lambda _:
                service.Service.stopService(self))
        return self._loopFinished



class CooperatorService(service.Service):
    """
    Simple L{service.IService} which starts and stops a L{twisted.internet.task.Cooperator}.
    """
    def __init__(self):
        self.coop = task.Cooperator(started=False)


    def coiterate(self, iterator):
        return self.coop.coiterate(iterator)


    def startService(self):
        self.coop.start()


    def stopService(self):
        self.coop.stop()



class StreamServerEndpointService(service.Service, object):
    """
    A L{StreamServerEndpointService} is an L{IService} which runs a server on a
    listening port described by an L{IStreamServerEndpoint
    <twisted.internet.interfaces.IStreamServerEndpoint>}.

    @ivar factory: A server factory which will be used to listen on the
        endpoint.

    @ivar endpoint: An L{IStreamServerEndpoint
        <twisted.internet.interfaces.IStreamServerEndpoint>} provider
        which will be used to listen when the service starts.

    @ivar _waitingForPort: a Deferred, if C{listen} has yet been invoked on the
        endpoint, otherwise None.

    @ivar _raiseSynchronously: Defines error-handling behavior for the case
        where C{listen(...)} raises an exception before C{startService} or
        C{privilegedStartService} have completed.

    @type _raiseSynchronously: C{bool}

    @since: 10.2
    """

    _raiseSynchronously = False

    def __init__(self, endpoint, factory):
        self.endpoint = endpoint
        self.factory = factory
        self._waitingForPort = None


    def privilegedStartService(self):
        """
        Start listening on the endpoint.
        """
        service.Service.privilegedStartService(self)
        self._waitingForPort = self.endpoint.listen(self.factory)
        raisedNow = []
        def handleIt(err):
            if self._raiseSynchronously:
                raisedNow.append(err)
            elif not err.check(CancelledError):
                log.err(err)
        self._waitingForPort.addErrback(handleIt)
        if raisedNow:
            raisedNow[0].raiseException()
        self._raiseSynchronously = False


    def startService(self):
        """
        Start listening on the endpoint, unless L{privilegedStartService} got
        around to it already.
        """
        service.Service.startService(self)
        if self._waitingForPort is None:
            self.privilegedStartService()


    def stopService(self):
        """
        Stop listening on the port if it is already listening, otherwise,
        cancel the attempt to listen.

        @return: a L{Deferred<twisted.internet.defer.Deferred>} which fires
            with L{None} when the port has stopped listening.
        """
        self._waitingForPort.cancel()
        def stopIt(port):
            if port is not None:
                return port.stopListening()
        d = self._waitingForPort.addCallback(stopIt)
        def stop(passthrough):
            self.running = False
            return passthrough
        d.addBoth(stop)
        return d



class _ReconnectingProtocolProxy(object):
    """
    A proxy for a Protocol to provide connectionLost notification to a client
    connection service, in support of reconnecting when connections are lost.
    """

    def __init__(self, protocol, lostNotification):
        """
        Create a L{_ReconnectingProtocolProxy}.

        @param protocol: the application-provided L{interfaces.IProtocol}
            provider.
        @type protocol: provider of L{interfaces.IProtocol} which may
            additionally provide L{interfaces.IHalfCloseableProtocol} and
            L{interfaces.IFileDescriptorReceiver}.

        @param lostNotification: a 1-argument callable to invoke with the
            C{reason} when the connection is lost.
        """
        self._protocol = protocol
        self._lostNotification = lostNotification


    def connectionLost(self, reason):
        """
        The connection was lost.  Relay this information.

        @param reason: The reason the connection was lost.

        @return: the underlying protocol's result
        """
        try:
            return self._protocol.connectionLost(reason)
        finally:
            self._lostNotification(reason)


    def __getattr__(self, item):
        return getattr(self._protocol, item)


    def __repr__(self):
        return '<%s wrapping %r>' % (
            self.__class__.__name__, self._protocol)



class _DisconnectFactory(object):
    """
    A L{_DisconnectFactory} is a proxy for L{IProtocolFactory} that catches
    C{connectionLost} notifications and relays them.
    """

    def __init__(self, protocolFactory, protocolDisconnected):
        self._protocolFactory = protocolFactory
        self._protocolDisconnected = protocolDisconnected


    def buildProtocol(self, addr):
        """
        Create a L{_ReconnectingProtocolProxy} with the disconnect-notification
        callback we were called with.

        @param addr: The address the connection is coming from.

        @return: a L{_ReconnectingProtocolProxy} for a protocol produced by
            C{self._protocolFactory}
        """
        return _ReconnectingProtocolProxy(
            self._protocolFactory.buildProtocol(addr),
            self._protocolDisconnected
        )


    def __getattr__(self, item):
        return getattr(self._protocolFactory, item)


    def __repr__(self):
        return '<%s wrapping %r>' % (
            self.__class__.__name__, self._protocolFactory)



def backoffPolicy(initialDelay=1.0, maxDelay=60.0, factor=1.5,
                  jitter=_goodEnoughRandom):
    """
    A timeout policy for L{ClientService} which computes an exponential backoff
    interval with configurable parameters.

    @since: 16.1.0

    @param initialDelay: Delay for the first reconnection attempt (default
        1.0s).
    @type initialDelay: L{float}

    @param maxDelay: Maximum number of seconds between connection attempts
        (default 60 seconds, or one minute).  Note that this value is before
        jitter is applied, so the actual maximum possible delay is this value
        plus the maximum possible result of C{jitter()}.
    @type maxDelay: L{float}

    @param factor: A multiplicative factor by which the delay grows on each
        failed reattempt.  Default: 1.5.
    @type factor: L{float}

    @param jitter: A 0-argument callable that introduces noise into the delay.
        By default, C{random.random}, i.e. a pseudorandom floating-point value
        between zero and one.
    @type jitter: 0-argument callable returning L{float}

    @return: a 1-argument callable that, given an attempt count, returns a
        floating point number; the number of seconds to delay.
    @rtype: see L{ClientService.__init__}'s C{retryPolicy} argument.
    """
    def policy(attempt):
        return min(initialDelay * (factor ** attempt), maxDelay) + jitter()
    return policy

_defaultPolicy = backoffPolicy()


def _firstResult(gen):
    """
    Return the first element of a generator and exhaust it.

    C{MethodicalMachine.upon}'s C{collector} argument takes a generator of
    output results. If the generator is exhausted, the later outputs aren't
    actually run.

    @param gen: Generator to extract values from

    @return: The first element of the generator.
    """
    return list(gen)[0]



class _ClientMachine(object):
    """
    State machine for maintaining a single outgoing connection to an endpoint.

    @see: L{ClientService}
    """

    _machine = MethodicalMachine()

    def __init__(self, endpoint, factory, retryPolicy, clock, log):
        """
        @see: L{ClientService.__init__}

        @param log: The logger for the L{ClientService} instance this state
            machine is associated to.
        @type log: L{Logger}
        """
        self._endpoint = endpoint
        self._failedAttempts = 0
        self._stopped = False
        self._factory = factory
        self._timeoutForAttempt = retryPolicy
        self._clock = clock
        self._connectionInProgress = succeed(None)

        self._awaitingConnected = []

        self._stopWaiters = []
        self._log = log


    @_machine.state(initial=True)
    def _init(self):
        """
        The service has not been started.
        """

    @_machine.state()
    def _connecting(self):
        """
        The service has started connecting.
        """

    @_machine.state()
    def _waiting(self):
        """
        The service is waiting for the reconnection period
        before reconnecting.
        """

    @_machine.state()
    def _connected(self):
        """
        The service is connected.
        """

    @_machine.state()
    def _disconnecting(self):
        """
        The service is disconnecting after being asked to shutdown.
        """

    @_machine.state()
    def _restarting(self):
        """
        The service is disconnecting and has been asked to restart.
        """

    @_machine.state()
    def _stopped(self):
        """
        The service has been stopped an is disconnected.
        """

    @_machine.input()
    def start(self):
        """
        Start this L{ClientService}, initiating the connection retry loop.
        """

    @_machine.output()
    def _connect(self):
        """
        Start a connection attempt.
        """
        factoryProxy = _DisconnectFactory(self._factory,
                                          lambda _: self._clientDisconnected())

        self._connectionInProgress = (
            self._endpoint.connect(factoryProxy)
            .addCallback(self._connectionMade)
            .addErrback(lambda _: self._connectionFailed()))


    @_machine.output()
    def _resetFailedAttempts(self):
        """
        Reset the number of failed attempts.
        """
        self._failedAttempts = 0


    @_machine.input()
    def stop(self):
        """
        Stop trying to connect and disconnect any current connection.

        @return: a L{Deferred} that fires when all outstanding connections are
            closed and all in-progress connection attempts halted.
        """

    @_machine.output()
    def _waitForStop(self):
        """
        Return a deferred that will fire when the service has finished
        disconnecting.

        @return: L{Deferred} that fires when the service has finished
            disconnecting.
        """
        self._stopWaiters.append(Deferred())
        return self._stopWaiters[-1]


    @_machine.output()
    def _stopConnecting(self):
        """
        Stop pending connection attempt.
        """
        self._connectionInProgress.cancel()


    @_machine.output()
    def _stopRetrying(self):
        """
        Stop pending attempt to reconnect.
        """
        self._retryCall.cancel()
        del self._retryCall


    @_machine.output()
    def _disconnect(self):
        """
        Disconnect the current connection.
        """
        self._currentConnection.transport.loseConnection()


    @_machine.input()
    def _connectionMade(self, protocol):
        """
        A connection has been made.

        @param protocol: The protocol of the connection.
        @type protocol: L{IProtocol}
        """

    @_machine.output()
    def _notifyWaiters(self, protocol):
        """
        Notify all pending requests for a connection that a connection has been
        made.

        @param protocol: The protocol of the connection.
        @type protocol: L{IProtocol}
        """
        # This should be in _resetFailedAttempts but the signature doesn't
        # match.
        self._failedAttempts = 0

        self._currentConnection = protocol._protocol
        self._unawait(self._currentConnection)


    @_machine.input()
    def _connectionFailed(self):
        """
        The current connection attempt failed.
        """

    @_machine.output()
    def _wait(self):
        """
        Schedule a retry attempt.
        """
        self._failedAttempts += 1
        delay = self._timeoutForAttempt(self._failedAttempts)
        self._log.info("Scheduling retry {attempt} to connect {endpoint} "
                       "in {delay} seconds.", attempt=self._failedAttempts,
                       endpoint=self._endpoint, delay=delay)
        self._retryCall = self._clock.callLater(delay, self._reconnect)


    @_machine.input()
    def _reconnect(self):
        """
        The wait between connection attempts is done.
        """

    @_machine.input()
    def _clientDisconnected(self):
        """
        The current connection has been disconnected.
        """

    @_machine.output()
    def _forgetConnection(self):
        """
        Forget the current connection.
        """
        del self._currentConnection


    @_machine.output()
    def _cancelConnectWaiters(self):
        """
        Notify all pending requests for a connection that no more connections
        are expected.
        """
        self._unawait(Failure(CancelledError()))


    @_machine.output()
    def _finishStopping(self):
        """
        Notify all deferreds waiting on the service stopping.
        """
        self._stopWaiters, waiting = [], self._stopWaiters
        for w in waiting:
            w.callback(None)


    @_machine.input()
    def whenConnected(self):
        """
        Retrieve the currently-connected L{Protocol}, or the next one to
        connect.

        @return: a Deferred that fires with a protocol produced by the factory
            passed to C{__init__}
        @rtype: L{Deferred} firing with L{IProtocol} or failing with
            L{CancelledError} the service is stopped.
        """

    @_machine.output()
    def _currentConnection(self):
        """
        Return the currently connected protocol.

        @return: L{Deferred} that is fired with currently connected protocol.
        """
        return succeed(self._currentConnection)


    @_machine.output()
    def _noConnection(self):
        """
        Notify the caller that no connection is expected.

        @return: L{Deferred} that is fired with L{CancelledError}.
        """
        return fail(CancelledError())


    @_machine.output()
    def _awaitingConnection(self):
        """
        Return a deferred that will fire with the next connected protocol.

        @return: L{Deferred} that will fire with the next connected protocol.
        """
        result = Deferred()
        self._awaitingConnected.append(result)
        return result


    def _unawait(self, value):
        """
        Fire all outstanding L{ClientService.whenConnected} L{Deferred}s.

        @param value: the value to fire the L{Deferred}s with.
        """
        self._awaitingConnected, waiting = [], self._awaitingConnected
        for w in waiting:
            w.callback(value)

    # State Transitions

    _init.upon(start, enter=_connecting,
               outputs=[_connect])
    _init.upon(stop, enter=_init,
               outputs=[])

    _connecting.upon(start, enter=_connecting, outputs=[])
    # Note that this synchonously triggers _connectionFailed in the
    # _disconnecting state.
    _connecting.upon(stop, enter=_disconnecting,
                     outputs=[_waitForStop, _stopConnecting],
                     collector=_firstResult)
    _connecting.upon(_connectionMade, enter=_connected,
                     outputs=[_notifyWaiters])
    _connecting.upon(_connectionFailed, enter=_waiting,
                     outputs=[_wait])

    _waiting.upon(start, enter=_waiting,
                  outputs=[])
    _waiting.upon(stop, enter=_stopped,
                  outputs=[_waitForStop, _stopRetrying, _finishStopping],
                  collector=_firstResult)
    _waiting.upon(_reconnect, enter=_connecting,
                  outputs=[_connect])

    _connected.upon(start, enter=_connected,
                    outputs=[])
    _connected.upon(stop, enter=_disconnecting,
                    outputs=[_waitForStop, _disconnect],
                    collector=_firstResult)
    _connected.upon(_clientDisconnected, enter=_waiting,
                    outputs=[_forgetConnection, _wait])

    _disconnecting.upon(start, enter=_restarting,
                        outputs=[_resetFailedAttempts])
    _disconnecting.upon(stop, enter=_disconnecting,
                        outputs=[])
    _disconnecting.upon(_clientDisconnected, enter=_stopped,
                        outputs=[_cancelConnectWaiters,
                                 _finishStopping,
                                 _forgetConnection])
    # Note that this is triggered synchonously with the transition from
    # _connecting
    _disconnecting.upon(_connectionFailed, enter=_stopped,
                        outputs=[_cancelConnectWaiters, _finishStopping])

    _restarting.upon(start, enter=_restarting,
                     outputs=[])
    _restarting.upon(stop, enter=_disconnecting,
                     outputs=[])
    _restarting.upon(_clientDisconnected, enter=_connecting,
                     outputs=[_finishStopping, _connect])

    _stopped.upon(start, enter=_connecting,
                  outputs=[_connect])
    _stopped.upon(stop, enter=_stopped,
                  outputs=[])

    _init.upon(whenConnected, enter=_init,
               outputs=[_awaitingConnection],
               collector=_firstResult)
    _connecting.upon(whenConnected, enter=_connecting,
                     outputs=[_awaitingConnection],
                     collector=_firstResult)
    _waiting.upon(whenConnected, enter=_waiting,
                  outputs=[_awaitingConnection],
                  collector=_firstResult)
    _connected.upon(whenConnected, enter=_connected,
                    outputs=[_currentConnection],
                    collector=_firstResult)
    _disconnecting.upon(whenConnected, enter=_disconnecting,
                        outputs=[_awaitingConnection],
                        collector=_firstResult)
    _restarting.upon(whenConnected, enter=_restarting,
                     outputs=[_awaitingConnection],
                     collector=_firstResult)
    _stopped.upon(whenConnected, enter=_stopped,
                  outputs=[_noConnection],
                  collector=_firstResult)



class ClientService(service.Service, object):
    """
    A L{ClientService} maintains a single outgoing connection to a client
    endpoint, reconnecting after a configurable timeout when a connection
    fails, either before or after connecting.

    @since: 16.1.0
    """

    _log = Logger()
    def __init__(self, endpoint, factory, retryPolicy=None, clock=None):
        """
        @param endpoint: A L{stream client endpoint
            <interfaces.IStreamClientEndpoint>} provider which will be used to
            connect when the service starts.

        @param factory: A L{protocol factory <interfaces.IProtocolFactory>}
            which will be used to create clients for the endpoint.

        @param retryPolicy: A policy configuring how long L{ClientService} will
            wait between attempts to connect to C{endpoint}.
        @type retryPolicy: callable taking (the number of failed connection
            attempts made in a row (L{int})) and returning the number of
            seconds to wait before making another attempt.

        @param clock: The clock used to schedule reconnection.  It's mainly
            useful to be parametrized in tests.  If the factory is serialized,
            this attribute will not be serialized, and the default value (the
            reactor) will be restored when deserialized.
        @type clock: L{IReactorTime}
        """
        clock = _maybeGlobalReactor(clock)
        retryPolicy = _defaultPolicy if retryPolicy is None else retryPolicy

        self._machine = _ClientMachine(
            endpoint, factory, retryPolicy, clock,
            log=self._log,
        )


    def whenConnected(self):
        """
        Retrieve the currently-connected L{Protocol}, or the next one to
        connect.

        @return: a Deferred that fires with a protocol produced by the factory
            passed to C{__init__}
        @rtype: L{Deferred} firing with L{IProtocol} or failing with
            L{CancelledError} the service is stopped.
        """
        return self._machine.whenConnected()


    def startService(self):
        """
        Start this L{ClientService}, initiating the connection retry loop.
        """
        if self.running:
            self._log.warn("Duplicate ClientService.startService {log_source}")
            return
        super(ClientService, self).startService()
        self._machine.start()


    def stopService(self):
        """
        Stop attempting to reconnect and close any existing connections.

        @return: a L{Deferred} that fires when all outstanding connections are
            closed and all in-progress connection attempts halted.
        """
        super(ClientService, self).stopService()
        return self._machine.stop()


__all__ = (['TimerService', 'CooperatorService', 'MulticastServer',
            'StreamServerEndpointService', 'UDPServer',
            'ClientService'] +
           [tran + side
            for tran in 'TCP UNIX SSL UNIXDatagram'.split()
            for side in 'Server Client'.split()])
