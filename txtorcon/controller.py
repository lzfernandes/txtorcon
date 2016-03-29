# -*- coding: utf-8 -*-

from __future__ import absolute_import
from __future__ import print_function
from __future__ import with_statement

import os
import sys
import shlex
import tempfile
import functools
from io import StringIO
if sys.platform in ('linux2', 'darwin'):
    import pwd

from twisted.python import log
from twisted.internet.defer import inlineCallbacks, returnValue, Deferred
from twisted.internet import protocol, error
from twisted.internet.endpoints import TCP4ClientEndpoint
from twisted.internet.interfaces import IReactorTime, IReactorCore
from twisted.internet.interfaces import IStreamClientEndpoint

from txtorcon.util import delete_file_or_tree, find_keywords
from txtorcon.util import find_tor_binary, available_tcp_port
from txtorcon.log import txtorlog
from txtorcon.torcontrolprotocol import TorProtocolFactory
from txtorcon.torstate import TorState
from txtorcon.torconfig import TorConfig


@inlineCallbacks
def launch(reactor,
           progress_updates=None,
           data_directory=None,
           socks_port=None,
           stdout=None,
           stderr=None,
           timeout=None,
           tor_binary=None,
           # 'users' probably never need these:
           connection_creator=None,
           kill_on_stderr=True,
           ):
    """
    launches a new Tor process, and returns a Deferred that fires with
    a new :class:`txtorcon.Tor` instance. From this instance, you can
    create or get any "interesting" instances you need: the
    :class:`txtorcon.TorConfig` instance, create endpoints, create
    :class:`txtorcon.TorState` instance(s), etc.

    Note that there is NO way to pass in a config; we only expost a
    couple of basic Tor options. If you need anything beyond these,
    you can access the ``TorConfig`` instance (via ``.get_config()``)
    and make any changes there, reflecting them in tor with
    ``.save()``.

    You can igore all the options and safe defaults will be
    provided. However, **it is recommended to pass data_directory**
    especially if you will be starting up Tor frequently, as it saves
    a bunch of time (and bandwidth for the directory
    authorities). "Safe defaults" means:

      - a tempdir for a ``DataDirectory`` is used (respecting ``TMP``)
        and is deleted when this tor is shut down (you therefore
        *probably* want to supply the ``data_directory=`` kwarg);
      - a random, currently-unused local TCP port is used as the
        ``SocksPort`` (specify ``socks_port=`` if you want your
        own). If you want no SOCKS listener at all, pass
        ``socks_port=0``
      - we set ``__OwningControllerProcess`` and call
        ``TAKEOWNERSHIP`` so that if our control connection goes away,
        tor shuts down (see `control-spec
        <https://gitweb.torproject.org/torspec.git/blob/HEAD:/control-spec.txt>`_
        3.23).
      - the launched Tor will use ``COOKIE`` authentication.

    :param reactor: a Twisted IReactorCore implementation (usually
        twisted.internet.reactor)

    :param progress_updates: a callback which gets progress updates; gets 3
         args: percent, tag, summary (FIXME make an interface for this).

    :param data_directory: set as the ``DataDirectory`` option to Tor,
        this is where tor keeps its state information (cached relays,
        etc); starting with an already-populated state directory is a lot
        faster. If ``None`` (the default), we create a tempdir for this
        **and delete it on exit**. It is recommended you pass something here.

    :param stdout: a file-like object to which we write anything that
        Tor prints on stdout (just needs to support write()).

    :param stderr: a file-like object to which we write anything that
        Tor prints on stderr (just needs .write()). Note that we kill
        Tor off by default if anything appears on stderr; pass
        "kill_on_stderr=False" if you don't want this behavior.

    :param tor_binary: path to the Tor binary to run. If None (the
        default), we try to find the tor binary.

    :param kill_on_stderr:
        When True (the default), if Tor prints anything on stderr we
        kill off the process, close the TorControlProtocol and raise
        an exception.

    :param connection_creator: is mostly available to ease testing, so
        you probably don't want to supply this. If supplied, it is a
        callable that should return a Deferred that delivers an
        :api:`twisted.internet.interfaces.IProtocol <IProtocol>` or
        ConnectError.
        See :api:`twisted.internet.interfaces.IStreamClientEndpoint`.connect
        Note that this parameter is ignored if config.ControlPort == 0

    :return: a Deferred which callbacks with :class:`txtorcon.Tor`
        instance, from which you can retrieve the TorControlProtocol
        instance via the ``.protocol`` property.

    HACKS:

     1. It's hard to know when Tor has both (completely!) written its
        authentication cookie file AND is listening on the control
        port. It seems that waiting for the first 'bootstrap' message on
        stdout is sufficient. Seems fragile...and doesn't work 100% of
        the time, so FIXME look at Tor source.



    XXX this "User" thing was, IIRC, a feature for root-using scripts
    (!!) that were going to launch tor, but where tor would drop to a
    different user. Do we still want to support this?

    ``User``: if this exists, we attempt to set ownership of the tempdir
    to this user (but only if our effective UID is 0).
    """

    # We have a slight problem with the approach: we need to pass a
    # few minimum values to a torrc file so that Tor will start up
    # enough that we may connect to it. Ideally, we'd be able to
    # start a Tor up which doesn't really do anything except provide
    # "AUTHENTICATE" and "GETINFO config/names" so we can do our
    # config validation.

    if not IReactorCore.providedBy(reactor):
        raise ValueError("'reactor' argument must provide IReactorCore")

    if tor_binary is None:
        tor_binary = find_tor_binary()
    if tor_binary is None:
        # We fail right here instead of waiting for the reactor to start
        raise TorNotFound('Tor binary could not be found')

    # make sure we got things that have write() for stderr, stdout
    # kwargs
    for arg in [stderr, stdout]:
        if arg and not getattr(arg, "write", None):
            raise RuntimeError(
                'File-like object needed for stdout or stderr args.'
            )

    config = TorConfig()
    if data_directory is not None:
        user_set_data_directory = True
        config.DataDirectory = data_directory
        try:
            os.mkdir(data_directory, 0700)
        except OSError:
            pass
    else:
        user_set_data_directory = False
        data_directory = tempfile.mkdtemp(prefix='tortmp')
        config.DataDirectory = data_directory
        # note: we also set up the ProcessProtocol to delete this when
        # Tor exits, this is "just in case" fallback:
        reactor.addSystemEventTrigger(
            'before', 'shutdown',
            functools.partial(delete_file_or_tree, data_directory)
        )

    if socks_port is None:
        socks_port = yield available_tcp_port(reactor)
    config.SOCKSPort = socks_port

    if False: # XXX see note in docstring
        # Set ownership on the temp-dir to the user tor will drop privileges to
        # when executing as root.
        try:
            user = config.User
        except KeyError:
            pass
        else:
            if sys.platform in ('linux2', 'darwin') and os.geteuid() == 0:
                os.chown(data_directory, pwd.getpwnam(user).pw_uid, -1)

    # XXX would be better, on supported platforms, to use a
    # unix-socket inside the data-directory?
    control_port = yield available_tcp_port(reactor)
    config.ControlPort = control_port
    print("CONTROL PORT", control_port)

    config.CookieAuthentication = 1
    config.__OwningControllerProcess = os.getpid()
    if connection_creator is None:
        connection_creator = functools.partial(
            TCP4ClientEndpoint(reactor, 'localhost', control_port).connect,
            TorProtocolFactory()
        )

    # NOTE well, that if we don't pass "-f" then Tor will merrily load
    # its default torrc, and apply our options over top... :/ should
    # file a bug probably?
    config_args = ['-f', '/dev/null/non-existant-on-purpose', '--ignore-missing-torrc']

    # ...now add all our config options on the command-line. This
    # avoids writing a temporary torrc.
    for (k, v) in config.config_args():
        config_args.append(k)
        config_args.append(v)

    process_protocol = TorProcessProtocol(
        connection_creator,
        progress_updates,
        config, reactor,
        timeout,
        kill_on_stderr,
        stdout,
        stderr
    )

    # we set both to_delete and the shutdown events because this
    # process might be shut down way before the reactor, but if the
    # reactor bombs out without the subprocess getting closed cleanly,
    # we'll want the system shutdown events triggered so the temporary
    # files get cleaned up either way

    # we don't want to delete the user's directories, just temporary
    # ones this method created.
    if not user_set_data_directory:
        process_protocol.to_delete = [data_directory]

    log.msg('Spawning tor process with DataDirectory', data_directory)
    args = [tor_binary] + config_args
    transport = reactor.spawnProcess(
        process_protocol,
        tor_binary,
        args=args,
        env={'HOME': data_directory},
        path=data_directory
    )
    # FIXME? don't need rest of the args: uid, gid, usePTY, childFDs)
    transport.closeStdin()

    if process_protocol.connected_cb:
        yield process_protocol.connected_cb
        print("OHAI")
    yield config.post_bootstrap
    # ^ should also wait for protocol to bootstrap; be more explicit?

    returnValue(
        Tor(
            reactor,
            config,
            _process_proto=process_protocol,
        )
    )


def connect(control_endpoint, password_function=None):
    """
    XXX THINK: do we want/need a reactor arg?

    Creates a :class:`txtorcon.Tor` instance by connecting to an
    already-running tor's control port. For example, a common default
    tor uses is UNIXClientEndpoint(reactor, '/var/run/tor/control') or
    TCP4ClientEndpoint(reactor, 'localhost', 9051)

    If only password authentication is available in the tor we connect
    to, the ``password_function`` is called (if supplied) to retrieve
    a valid password. This function can return a Deferred.

    For example::

        import txtorcon
        from twisted.internet.task import react
        from twisted.internet.defer import inlineCallbacks

        @inlineCallbacks
        def main(reactor):
            tor = yield txtorcon.connect(
                TCP4ClientEndpoint(reactor, "localhost", 9051)
            )
            state = yield tor.create_state()
            for circuit in state.circuits:
                print(circuit)

    :param password_function:
        See :class:`txtorcon.TorControlProtocol`

    :return:
        a Deferred that fires with a :class:`txtorcon.Tor` instance
    """

    if not IStreamClientEndpoint.providedBy(control_endpoint):
        raise ValueError("control_endpoint must provide IStreamClientEndpoint")

    proto = yield control_endpoint.connect(
        TorProtocolFactory(
            password_function=password_function
        )
    )
    yield proto.post_bootstrap
    returnValue(proto)


class Tor(object):
    """
    I represent a single instance of Tor and act as a Builder/Factory
    for several useful objects you will probably want. There are two
    ways to create a Tor instance:

       - :func:`txtorcon.connect`` to connect to a tor that is already
         running (e.g. Tor Browser Bundle, a system Tor, ...).
       - :func:`txtorcon.launch`` to launch a fresh tor instance

    If you desire more control, there are "lower level" APIs which are
    the very ones used by this class. However, this "highest level"
    API should cover many use-cases::

        import txtorcon

        @inlineCallbacks
        def main(reactor):
            # tor = yield txtorcon.connect(UNIXClientEndpoint(reactor, "/var/run/tor/control"))
            tor = yield txtorcon.launch(reactor)

            onion_ep = tor.create_onion_endpoint(port=80)
            port = yield onion_ep.listen(Site())
            print(port.getHost())
    """

    def __init__(self, reactor, tor_config, _process_proto=None):
        """
        don't instantiate this class yourself -- instead use the factory
        methods :func:`txtorcon.launch` or :func:`txtorcon.connect`
        """
        self._config = tor_config
        self._protocol = tor_config.protocol
        self._reactor = reactor
        # this only passed/set when we launch()
        self._process_protocol = _process_proto

    # XXX this shold probasbly include access to the "process
    # protocol" instance, too...bikeshed on this name?
    @property
    def process(self):
        if self._process_protocol:
            return self._process_protocol
        raise RuntimeError(
            "This Tor instance was not launched by us; no process to return"
        )

    @property
    def protocol(self):
        """
        The TorControlProtocol instance that is communicating with this
        Tor instance.
        """
        return self._protocol

    @property
    def config(self):
        """
        The TorConfig instance associated with the tor instance we
        launched. This instance represents up-to-date configuration of
        the tor instance (even if another controller is connected).
        """
        return self._config

    # XXX One Onion Method To Rule Them All, or
    # create_disk_onion_endpoint vs. create_ephemeral_onion_endpoint,
    # or ...?
    def create_onion_endpoint(self, port, private_key=None, hs_dir=None, ):
        """
        for "real" args, see onion.py in the hidden-services API branch
        """
        raise NotImplemented(__name__)

    def create_client_endpoint(self, host, port):
        """
        returns an IStreamClientEndpoint instance that will connect via
        SOCKS over this Tor instance. Error if this Tor has no SOCKS
        ports.
        """
        # probably takes args similar to TorClientEndpoint on master
        raise NotImplemented(__name__)

    @inlineCallbacks
    def create_state(self):
        """
        returns a Deferred that fires with a ready-to-go
        :class:`txtorcon.TorState` instance.
        """
        state = TorState(self.protocol)
        yield state.post_bootstrap
        returnValue(state)

    def shutdown(self):
        # shuts down the Tor instance; nothing else will work after this
        pass


class TorNotFound(RuntimeError):
    """
    Raised by launch_tor() in case the tor binary was unspecified and could
    not be found by consulting the shell.
    """


class TorProcessProtocol(protocol.ProcessProtocol):

    def __init__(self, connection_creator, progress_updates=None, config=None,
                 ireactortime=None, timeout=None, kill_on_stderr=True,
                 stdout=None, stderr=None):
        """
        This will read the output from a Tor process and attempt a
        connection to its control port when it sees any 'Bootstrapped'
        message on stdout. You probably don't need to use this
        directly except as the return value from the
        :func:`txtorcon.launch_tor` method. tor_protocol contains a
        valid :class:`txtorcon.TorControlProtocol` instance by that
        point.

        connection_creator is a callable that should return a Deferred
        that callbacks with a :class:`txtorcon.TorControlProtocol`;
        see :func:`txtorcon.launch_tor` for the default one which is a
        functools.partial that will call
        ``connect(TorProtocolFactory())`` on an appropriate
        :api:`twisted.internet.endpoints.TCP4ClientEndpoint`

        :param connection_creator: A no-parameter callable which
            returns a Deferred which promises a
            :api:`twisted.internet.interfaces.IStreamClientEndpoint
            <IStreamClientEndpoint>`. If this is None, we do NOT
            attempt to connect to the underlying Tor process.

        :param progress_updates: A callback which received progress
            updates with three args: percent, tag, summary

        :param config: a TorConfig object to connect to the
            TorControlProtocl from the launched tor (should it succeed)

        :param ireactortime:
            An object implementing IReactorTime (i.e. a reactor) which
            needs to be supplied if you pass a timeout.

        :param timeout:
            An int representing the timeout in seconds. If we are
            unable to reach 100% by this time we will consider the
            setting up of Tor to have failed. Must supply ireactortime
            if you supply this.

        :param kill_on_stderr:
            When True, kill subprocess if we receive anything on stderr

        :param stdout:
            Anything subprocess writes to stdout is sent to .write() on this

        :param stderr:
            Anything subprocess writes to stderr is sent to .write() on this

        :ivar tor_protocol: The TorControlProtocol instance connected
            to the Tor this :api:`twisted.internet.protocol.ProcessProtocol
            <ProcessProtocol>`` is speaking to. Will be valid
            when the `connected_cb` callback runs.

        :ivar connected_cb: Triggered when the Tor process we
            represent is fully bootstrapped

        """

        self.config = config
        self.tor_protocol = None
        self.progress_updates = progress_updates

        if connection_creator:
            self.connection_creator = connection_creator
            self.connected_cb = Deferred()
        else:
            self.connection_creator = None
            self.connected_cb = None

        self.attempted_connect = False
        self.to_delete = []
        self.kill_on_stderr = kill_on_stderr
        self.stderr = stderr
        self.stdout = stdout
        self.collected_stdout = StringIO()

        self._setup_complete = False
        self._did_timeout = False
        self._timeout_delayed_call = None
        if timeout:
            if not ireactortime:
                raise RuntimeError(
                    'Must supply an IReactorTime object when supplying a '
                    'timeout')
            ireactortime = IReactorTime(ireactortime)
            self._timeout_delayed_call = ireactortime.callLater(
                timeout, self.timeout_expired)

    def outReceived(self, data):
        """
        :api:`twisted.internet.protocol.ProcessProtocol <ProcessProtocol>` API
        """

        if self.stdout:
            self.stdout.write(data)

        # minor hack: we can't try this in connectionMade because
        # that's when the process first starts up so Tor hasn't
        # opened any ports properly yet. So, we presume that after
        # its first output we're good-to-go. If this fails, we'll
        # reset and try again at the next output (see this class'
        # tor_connection_failed)

        txtorlog.msg(data)
        if not self.attempted_connect and self.connection_creator \
                and 'Bootstrap' in data:
            self.attempted_connect = True
            d = self.connection_creator()
            d.addCallback(self.tor_connected)
            d.addErrback(self.tor_connection_failed)

    def timeout_expired(self):
        """
        A timeout was supplied during setup, and the time has run out.
        """

        try:
            self.transport.signalProcess('TERM')
        except error.ProcessExitedAlready:
            self.transport.loseConnection()
        self._did_timeout = True

    def errReceived(self, data):
        """
        :api:`twisted.internet.protocol.ProcessProtocol <ProcessProtocol>` API
        """

        if self.stderr:
            self.stderr.write(data)

        if self.kill_on_stderr:
            self.transport.loseConnection()
            raise RuntimeError(
                "Received stderr output from slave Tor process: " + data)

    def cleanup(self):
        """
        Clean up my temporary files.
        """

        all([delete_file_or_tree(f) for f in self.to_delete])
        self.to_delete = []

    def processEnded(self, status):
        """
        :api:`twisted.internet.protocol.ProcessProtocol <ProcessProtocol>` API
        """

        self.cleanup()

        if status.value.exitCode is None:
            if self._did_timeout:
                err = RuntimeError("Timeout waiting for Tor launch..")
            else:
                err = RuntimeError(
                    "Tor was killed (%s)." % status.value.signal)
        else:
            err = RuntimeError(
                "Tor exited with error-code %d" % status.value.exitCode)

        log.err(err)
        if self.connected_cb:
            self.connected_cb.errback(err)
            self.connected_cb = None

    def progress(self, percent, tag, summary):
        """
        Can be overridden or monkey-patched if you want to get
        progress updates yourself.
        """

        if self.progress_updates:
            self.progress_updates(percent, tag, summary)

    # the below are all callbacks

    def tor_connection_failed(self, failure):
        # FIXME more robust error-handling please, like a timeout so
        # we don't just wait forever after 100% bootstrapped (that
        # is, we're ignoring these errors, but shouldn't do so after
        # we'll stop trying)
        self.attempted_connect = False

    def status_client(self, arg):
        print("KERBLAMMO", arg)
        args = shlex.split(arg)
        if args[1] != 'BOOTSTRAP':
            return

        kw = find_keywords(args)
        prog = int(kw['PROGRESS'])
        tag = kw['TAG']
        summary = kw['SUMMARY']
        self.progress(prog, tag, summary)

        if prog == 100:
            if self._timeout_delayed_call:
                self._timeout_delayed_call.cancel()
                self._timeout_delayed_call = None
            if self.connected_cb:
                self.connected_cb.callback(self)
                self.connected_cb = None

    def tor_connected(self, proto):
        txtorlog.msg("tor_connected %s" % proto)

        self.tor_protocol = proto
        if self.config is not None:
            self.config._update_proto(proto)
        self.tor_protocol.is_owned = self.transport.pid
        self.tor_protocol.post_bootstrap.addCallback(
            self.protocol_bootstrapped).addErrback(
                self.tor_connection_failed)

    @inlineCallbacks
    def protocol_bootstrapped(self, proto):
        txtorlog.msg("Protocol is bootstrapped")

        self.tor_protocol.add_event_listener(
            'STATUS_CLIENT', self.status_client)

        # FIXME: should really listen for these to complete as well
        # as bootstrap etc. For now, we'll be optimistic.
        yield self.tor_protocol.queue_command('TAKEOWNERSHIP')
        yield self.tor_protocol.queue_command('RESETCONF __OwningControllerProcess')
