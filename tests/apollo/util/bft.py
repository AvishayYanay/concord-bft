# Concord
#
# Copyright (c) 2019 VMware, Inc. All Rights Reserved.
#
# This product is licensed to you under the Apache 2.0 license (the "License").
# You may not use this product except in compliance with the Apache 2.0 License.
#
# This product may include a number of subcomponents with separate copyright
# notices and license terms. Your use of these subcomponents is subject to the
# terms and conditions of the subcomponent's license, as noted in the LICENSE
# file.

# Add the pyclient directory to $PYTHONPATH

import sys

import os
import os.path
import shutil
import random
import subprocess
from collections import namedtuple
import tempfile
from functools import wraps

import trio

sys.path.append(os.path.abspath("../../util/pyclient"))

import bft_config
import bft_client
import bft_metrics_client
from util import bft_metrics
from util.bft_test_exceptions import AlreadyRunningError, AlreadyStoppedError


TestConfig = namedtuple('TestConfig', [
    'n',
    'f',
    'c',
    'num_clients',
    'key_file_prefix',
    'start_replica_cmd',
    'stop_replica_cmd',
    'num_ro_replicas'
])

KEY_FILE_PREFIX = "replica_keys_"


def interesting_configs(selected=None):
    if selected is None:
        selected=lambda *config: True

    bft_configs = [{'n': 4, 'f': 1, 'c': 0, 'num_clients': 30},
                   {'n': 6, 'f': 1, 'c': 1, 'num_clients': 30},
                   {'n': 7, 'f': 2, 'c': 0, 'num_clients': 30},
                   # {'n': 9, 'f': 2, 'c': 1, 'num_clients': 30}
                   # {'n': 12, 'f': 3, 'c': 1, 'num_clients': 30}
                   ]

    selected_bft_configs = \
        [conf for conf in bft_configs
         if selected(conf['n'], conf['f'], conf['c'])]

    assert len(selected_bft_configs) > 0, "No eligible BFT configs"

    for config in selected_bft_configs:
        assert config['n'] == 3 * config['f'] + 2 * config['c'] + 1, \
            "Invariant breached. Expected: n = 3f + 2c + 1"

    return selected_bft_configs


def with_trio(async_fn):
    """ Decorator for running a coroutine (async_fn) with trio. """
    @wraps(async_fn)
    def trio_wrapper(*args, **kwargs):
        if "already_in_trio" in kwargs:
            kwargs.pop("already_in_trio")
            return async_fn(*args, **kwargs)
        else:
            return trio.run(async_fn, *args, **kwargs)

    return trio_wrapper


def with_bft_network(start_replica_cmd, selected_configs=None, num_clients=None, num_ro_replicas=0):
    """
    Runs the decorated async function for all selected BFT configs
    """
    def decorator(async_fn):
        @wraps(async_fn)
        async def wrapper(*args, **kwargs):
            if "bft_network" in kwargs:
                bft_network = kwargs.pop("bft_network")
                bft_network.is_existing = True
                await async_fn(*args, **kwargs, bft_network=bft_network)
            else:
                for bft_config in interesting_configs(selected_configs):

                    config = TestConfig(n=bft_config['n'],
                                        f=bft_config['f'],
                                        c=bft_config['c'],
                                        num_clients=bft_config['num_clients'] \
                                            if num_clients is None \
                                            else num_clients,
                                        key_file_prefix=KEY_FILE_PREFIX,
                                        start_replica_cmd=start_replica_cmd,
                                        stop_replica_cmd=None,
                                        num_ro_replicas=num_ro_replicas)
                    with BftTestNetwork.new(config) as bft_network:
                        print(f'Running {async_fn.__name__} '
                              f'with n={config.n}, f={config.f}, c={config.c}, '
                              f'num_clients={config.num_clients}, '
                              f'num_ro_replicas={config.num_ro_replicas}')
                        await async_fn(*args, **kwargs, bft_network=bft_network)
        return wrapper

    return decorator

MAX_MSG_SIZE = 64*1024 # 64k
REQ_TIMEOUT_MILLI = 5000
RETRY_TIMEOUT_MILLI = 250
METRICS_TIMEOUT_SEC = 5


# TODO: This is not generic, but is required for use by SimpleKVBC. In the
# future we will likely want to change how we determine the lengths of keys and
# values, make them parameterizable, or generate keys in the protocols rather
# than tester. For now, all keys and values must be 21 bytes.
KV_LEN = 21


class BftTestNetwork:
    """Encapsulates a BFT network instance for testing purposes"""

    def __enter__(self):
        """context manager method for 'with' statements"""
        return self

    def __exit__(self, *args):
        """context manager method for 'with' statements"""
        if not self.is_existing:
            for client in self.clients.values():
                client.__exit__()
            self.metrics.__exit__()
            self.stop_all_replicas()
            os.chdir(self.origdir)
            shutil.rmtree(self.testdir, ignore_errors=True)

    def __init__(self, is_existing, origdir,
                 config, testdir, builddir, toolsdir,
                 procs, replicas, clients, metrics):
        self.is_existing = is_existing
        self.origdir = origdir
        self.config = config
        self.testdir = testdir
        self.builddir = builddir
        self.toolsdir = toolsdir
        self.procs = procs
        self.replicas = replicas
        self.clients = clients
        self.metrics = metrics

    @classmethod
    def new(cls, config):
        builddir = os.path.abspath("../../build")
        toolsdir = os.path.join(builddir, "tools")
        testdir = tempfile.mkdtemp()
        bft_network = cls(
            is_existing=False,
            origdir=os.getcwd(),
            config=config,
            testdir=testdir,
            builddir=builddir,
            toolsdir=toolsdir,
            procs={},
            replicas=[bft_config.Replica(i, "127.0.0.1", 3710 + 2*i, 4710 + 2*i)
                for i in range(0, config.n + config.num_ro_replicas)],
            clients = {},
            metrics = None
        )
        print("Running test in {}".format(bft_network.testdir))

        os.chdir(bft_network.testdir)
        bft_network._generate_crypto_keys()

        bft_network._init_metrics()
        bft_network._create_clients()

        return bft_network

    @classmethod
    def existing(cls, config, replicas, clients):
        bft_network = cls(
            is_existing=True,
            origdir=None,
            config=config,
            testdir=None,
            builddir=None,
            toolsdir=None,
            procs={r.id: r for r in replicas},
            replicas=replicas,
            clients={i: clients[i] for i in range(len(clients))},
            metrics=None
        )

        bft_network._init_metrics()
        return bft_network

    def _generate_crypto_keys(self):
        keygen = os.path.join(self.toolsdir, "GenerateConcordKeys")
        args = [keygen, "-n", str(self.config.n), "-f", str(self.config.f)]
        if self.config.num_ro_replicas > 0:
            args.extend(["-r", str(self.config.num_ro_replicas)])
        args.extend(["-o", self.config.key_file_prefix])
        subprocess.run(args, check=True)

    def _create_clients(self):
        for client_id in range(self.config.n + self.config.num_ro_replicas,
                               self.config.num_clients+self.config.n + self.config.num_ro_replicas):
            config = self._bft_config(client_id)
            self.clients[client_id] = bft_client.UdpClient(config, self.replicas)

    async def new_client(self):
        client_id = max(self.clients.keys()) + 1
        config = self._bft_config(client_id)
        client = bft_client.UdpClient(config, self.replicas)
        self.clients[client_id] = client
        return client

    def _bft_config(self, client_id):
        return bft_config.Config(client_id,
                                 self.config.f,
                                 self.config.c,
                                 MAX_MSG_SIZE,
                                 REQ_TIMEOUT_MILLI,
                                 RETRY_TIMEOUT_MILLI)

    def _init_metrics(self):
        metric_clients = {}
        for r in self.replicas:
            metric_clients[r.id] = bft_metrics_client.MetricsClient(r)
        self.metrics = bft_metrics.BftMetrics(metric_clients)

    def random_client(self):
        return random.choice(list(self.clients.values()))

    def random_clients(self, max_clients):
        return set(random.choices(list(self.clients.values()), k=max_clients))

    def start_replica_cmd(self, replica_id):
        """
        Returns command line to start replica with the given id
        """
        return self.config.start_replica_cmd(self.builddir, replica_id)

    def stop_replica_cmd(self, replica_id):
        """
        Returns command line to stop a replica with the given id
        """
        return self.config.stop_replica_cmd(replica_id)

    def start_all_replicas(self):
        for i in range(0, self.config.n):
            try:
                self.start_replica(i)
            except AlreadyRunningError:
                if not self.is_existing:
                    raise

        assert len(self.procs) == self.config.n

    def stop_all_replicas(self):
        """ Stop all running replicas"""
        [self.stop_replica(i) for i in self.get_live_replicas()]
        assert len(self.procs) == 0

    def start_replicas(self, replicas):
        """
        Start from list "replicas"
        """
        [self.start_replica(r) for r in replicas]

    def stop_replicas(self, replicas):
        """
        Start from list "replicas"
        """
        for r in replicas:
            self.stop_replica(r)

    def start_replica(self, replica_id):
        """
        Start a replica if it isn't already started.
        Otherwise raise an AlreadyStoppedError.
        """
        if replica_id in self.procs:
            raise AlreadyRunningError(replica_id)

        if self.is_existing and self.config.stop_replica_cmd is not None:
            self.procs[replica_id] = self._start_external_replica(replica_id)
        else:
            self.procs[replica_id] = subprocess.Popen(
                                        self.start_replica_cmd(replica_id),
                                        close_fds=True)

    def _start_external_replica(self, replica_id):
        subprocess.run(
            self.start_replica_cmd(replica_id),
            check=True
        )

        return self.replicas[replica_id]


    def stop_replica(self, replica_id):
        """
        Stop a replica if it is running.
        Otherwise raise an AlreadyStoppedError.
        """
        if replica_id not in self.procs.keys():
            raise AlreadyStoppedError(replica_id)

        if self.is_existing and self.config.stop_replica_cmd is not None:
            self._stop_external_replica(replica_id)
        else:
            p = self.procs[replica_id]
            p.kill()
            p.wait()

        del self.procs[replica_id]

    def _stop_external_replica(self, replica_id):
        subprocess.run(
            self.stop_replica_cmd(replica_id),
            check=True
        )

    def all_replicas(self, without=None):
        """
        Returns a list of all replicas excluding the "without" set
        """
        if without is None:
            without = set()

        return list(set(range(0, self.config.n)) - without)

    def get_live_replicas(self):
        """
        Returns the id-s of all live replicas
        """
        return list(self.procs.keys())

    async def get_current_primary(self):
        """
        Returns the current primary replica id
        """
        current_primary = await self.get_current_view()
        return current_primary % self.config.n

    async def get_current_view(self):
        """
        Returns the current view number
        """
        live_replica = random.choice(self.get_live_replicas())
        current_view = await self.wait_for_view(
            replica_id=live_replica, expected=None)

        return current_view

    async def wait_for_view(self, replica_id, expected=None,
                            err_msg="Expected view not reached"):
        """
        Waits for a view that matches the "expected" predicate,
        and returns the corresponding view number.

        If the "expected" predicate is not provided,
        returns the current view number.

        In case of a timeout, fails with the provided err_msg
        """
        if expected is None:
            expected = lambda _: True

        matching_view = None
        nb_replicas_in_matching_view = 0
        try:
            matching_view = await self._wait_for_matching_agreed_view(replica_id, expected)
            print(f'Matching view #{matching_view} has been agreed among replicas.')

            nb_replicas_in_matching_view = await self._wait_for_active_view(matching_view)
            print(f'View #{matching_view} has been activated by '
                  f'{nb_replicas_in_matching_view} >= n-f = {self.config.n - self.config.f}')

            return matching_view
        except trio.TooSlowError:
            assert False, err_msg + \
                          f'(matchingView={matching_view} ' \
                          f'replicasInMatchingView={nb_replicas_in_matching_view})'

    async def _wait_for_matching_agreed_view(self, replica_id, expected):
        """
        Wait for the last agreed view to match the "expected" predicate
        """
        last_agreed_view = None
        with trio.fail_after(seconds=30):
            while True:
                try:
                    with trio.move_on_after(seconds=1):
                        key = ['replica', 'Gauges', 'lastAgreedView']
                        view = await self.metrics.get(replica_id, *key)
                        if expected(view):
                            last_agreed_view = view
                            break
                except KeyError:
                    # metrics not yet available, continue looping
                    continue
        return last_agreed_view

    async def _wait_for_active_view(self, view):
        """
        Wait for a view to become active on enough (n-f) replicas
        """
        with trio.fail_after(seconds=30):
            while True:
                nb_replicas_in_view = await self._count_replicas_in_view(view)

                # wait for n-f = 2f+2c+1 replicas to be in the expected view
                if nb_replicas_in_view >= 2 * self.config.f + 2 * self.config.c + 1:
                    break
        return nb_replicas_in_view

    async def _count_replicas_in_view(self, view):
        """
        Count the number of replicas that have activated a given view
        """
        nb_replicas_in_view = 0

        async def count_if_replica_in_view(r, expected_view):
            """
            A closure that allows concurrent counting of replicas
            that have activated a given view.
            """
            nonlocal nb_replicas_in_view

            key = ['replica', 'Gauges', 'currentActiveView']

            with trio.move_on_after(seconds=5):
                while True:
                    with trio.move_on_after(seconds=1):
                        try:
                            replica_view = await self.metrics.get(r, *key)
                            if replica_view == expected_view:
                                nb_replicas_in_view += 1
                        except KeyError:
                            # metrics not yet available, continue looping
                            continue
                        else:
                            break

        async with trio.open_nursery() as nursery:
            for r in self.get_live_replicas():
                nursery.start_soon(
                    count_if_replica_in_view, r, view)
        return nb_replicas_in_view

    def force_quorum_including_replica(self, replica_id, primary=0):
        """
        Bring down a sufficient number of replicas (excluding the primary),
        so that the remaining replicas form a quorum that includes replica_id
        """
        unstable_replicas = self.all_replicas(without={primary, replica_id})

        random.shuffle(unstable_replicas)

        for backup_replica_id in unstable_replicas:
            print(f'Stopping backup replica {backup_replica_id} in order '
                  f'to force a quorum including replica {replica_id}...')
            self.stop_replica(backup_replica_id)
            if len(self.procs) == 2 * self.config.f + self.config.c + 1:
                break

        assert len(self.procs) == 2 * self.config.f + self.config.c + 1

    async def wait_for_fetching_state(self, replica_id):
        """
        Check metrics on fetching replica to see if the replica is in a
        fetching state

        Returns the current source replica for state transfer.
        """
        with trio.fail_after(10): # seconds
            while True:
                with trio.move_on_after(.5): # seconds
                    is_fetching = await self.is_fetching(replica_id)
                    source_replica_id = await self.source_replica(replica_id)
                    if is_fetching:
                        return source_replica_id

    async def is_fetching(self, replica_id):
        """Return whether the current replica is fetching state"""
        key = ['bc_state_transfer', 'Statuses', 'fetching_state']
        state = await self.metrics.get(replica_id, *key)
        return state != "NotFetching"

    async def source_replica(self, replica_id):
        """Return whether the current replica has a source replica already set"""
        key = ['bc_state_transfer', 'Gauges', 'current_source_replica']
        source_replica_id = await self.metrics.get(replica_id, *key)

        return source_replica_id

    async def wait_for_state_transfer_to_start(self):
        """
        Retry checking every .5 seconds until state transfer starts at least one
        node. Stop trying, and fail the test after 30 seconds.
        """
        with trio.fail_after(30): # seconds
            async with trio.open_nursery() as nursery:
                for replica in self.replicas:
                    nursery.start_soon(self._wait_to_receive_st_msgs,
                                       replica,
                                       nursery.cancel_scope)

    async def _wait_to_receive_st_msgs(self, replica, cancel_scope):
        """
        Check metrics to see if state transfer started. If so cancel the
        concurrent coroutines in the request scope.
        """
        while True:
            with trio.move_on_after(.5): # seconds
                try:
                    key = ['replica', 'Counters', 'receivedStateTransferMsgs']
                    n = await self.metrics.get(replica.id, *key)
                    if n > 0:
                        cancel_scope.cancel()
                except KeyError:
                    continue # metrics not yet available, continue looping

    async def wait_for_state_transfer_to_stop(
            self,
            up_to_date_node,
            stale_node,
            stop_on_stable_seq_num=False):
        with trio.fail_after(30): # seconds
            # Get the lastExecutedSeqNumber from a started node
            if stop_on_stable_seq_num:
                key = ['replica', 'Gauges', 'lastStableSeqNum']
            else:
                key = ['replica', 'Gauges', 'lastExecutedSeqNum']
            expected_seq_num = await self.metrics.get(up_to_date_node, *key)
            last_n = -1
            while True:
                with trio.move_on_after(.5): # seconds
                    metrics = await self.metrics.get_all(stale_node)
                    try:
                        n = self.metrics.get_local(metrics, *key)
                    except KeyError:
                        # ignore - the metric will eventually become available
                        pass
                    else:
                        # Debugging
                        if n != last_n:
                            last_n = n
                            checkpoint = ['bc_state_transfer',
                                    'Gauges', 'last_stored_checkpoint']
                            on_transferring_complete = ['bc_state_transfer',
                                    'Counters', 'on_transferring_complete']
                            print("wait_for_st_to_stop: expected_seq_num={} "
                                  "last_stored_checkpoint={} "
                                  "on_transferring_complete_count={}".format(
                                        n,
                                        self.metrics.get_local(metrics, *checkpoint),
                                        self.metrics.get_local(metrics,
                                            *on_transferring_complete)))
                        # Exit condition
                        if n >= expected_seq_num:
                           return

    async def wait_for_replicas_to_checkpoint(self, replica_ids, checkpoint_num):
        """
        Wait for every replica in `replicas` to take a checkpoint.
        Check every .5 seconds and give fail after 30 seconds.
        """
        with trio.fail_after(30): # seconds
            async with trio.open_nursery() as nursery:
                for replica_id in replica_ids:
                    nursery.start_soon(self.wait_for_checkpoint, replica_id,
                            checkpoint_num)

    async def wait_for_checkpoint(self, replica_id, expected_checkpoint_num=None):
        """
        Wait for a single replica to reach the expected_checkpoint_num.
        If none is provided, return the last stored checkpoint.
        """
        key = ['bc_state_transfer', 'Gauges', 'last_stored_checkpoint']
        with trio.fail_after(30):
            while True:
                with trio.move_on_after(.5): # seconds
                    last_stored_checkpoint = await self.metrics.get(replica_id, *key)
                    if expected_checkpoint_num is None \
                            or last_stored_checkpoint == expected_checkpoint_num:
                        return last_stored_checkpoint

    async def wait_for_slow_path_to_be_prevalent(
            self, as_of_seq_num=1, nb_slow_paths_so_far=0, replica_id=0):
        with trio.fail_after(seconds=5):
            while True:
                with trio.move_on_after(seconds=.5):
                    try:
                        await self.assert_slow_path_prevalent(
                            as_of_seq_num, nb_slow_paths_so_far, replica_id)
                    except (KeyError, AssertionError):
                        # continue polling
                        continue
                    else:
                        # slow path prevalent - done.
                        break

    async def wait_for_fast_path_to_be_prevalent(
            self, nb_slow_paths_so_far=0, replica_id=0):
        with trio.fail_after(seconds=5):
            while True:
                with trio.move_on_after(seconds=.5):
                    try:
                        await self.assert_fast_path_prevalent(
                            nb_slow_paths_so_far, replica_id)
                    except (KeyError, AssertionError):
                        # continue polling
                        continue
                    else:
                        # fast path prevalent - done.
                        break

    async def wait_for_last_executed_seq_num(self, replica_id=0, expected=0):
        with trio.fail_after(seconds=30):
            while True:
                with trio.move_on_after(seconds=.5):
                    try:
                        key = ['replica', 'Gauges', 'lastExecutedSeqNum']
                        last_executed_seq_num = await self.metrics.get(replica_id, *key)
                    except KeyError:
                        continue
                    else:
                        # success!
                        if last_executed_seq_num >= expected:
                            return last_executed_seq_num

    async def assert_state_transfer_not_started_all_up_nodes(self, up_replica_ids):
        with trio.fail_after(METRICS_TIMEOUT_SEC):
            # Check metrics for all started nodes in parallel
            async with trio.open_nursery() as nursery:
                up_replicas = [self.replicas[i] for i in up_replica_ids]
                for r in up_replicas:
                    nursery.start_soon(self._assert_state_transfer_not_started,
                                       r)

    async def assert_fast_path_prevalent(self, nb_slow_paths_so_far=0, replica_id=0):
        """
        Asserts there is at most 1 sequence processed on the slow path,
        given the "nb_slow_paths_so_far".
        """
        metric_key = ['replica', 'Counters', 'slowPathCount']
        total_nb_slow_paths = await self.metrics.get(replica_id, *metric_key)
        assert total_nb_slow_paths >= nb_slow_paths_so_far

        assert total_nb_slow_paths - nb_slow_paths_so_far <= 1, \
            f'Fast path is not prevalent for n={self.config.n}, f={self.config.f}, c={self.config.c}.'

    async def assert_slow_path_prevalent(
            self, as_of_seq_num=1, nb_slow_paths_so_far=0, replica_id=0):
        """
        Asserts all executed sequences after "as_of_seq_num" have been processed on the slow path,
        given the "nb_slow_paths_so_far".
        """
        metric_key = ['replica', 'Gauges', 'lastExecutedSeqNum']
        total_nb_executed_sequences = await self.metrics.get(replica_id, *metric_key)

        metric_key = ['replica', 'Counters', 'slowPathCount']
        total_nb_slow_paths = await self.metrics.get(replica_id, *metric_key)
        assert total_nb_slow_paths >= nb_slow_paths_so_far

        assert total_nb_slow_paths - nb_slow_paths_so_far >= total_nb_executed_sequences - as_of_seq_num, \
            f'Slow path is not prevalent for n={self.config.n}, f={self.config.f}, c={self.config.c}.'

    async def _assert_state_transfer_not_started(self, replica):
        key = ['replica', 'Counters', 'receivedStateTransferMsgs']
        n = await self.metrics.get(replica.id, *key)
        assert n == 0

    async def wait_for(self, predicate, timeout, interval):
        """
        Wait for the given async predicate function to return true. Give up
        waiting for the async function to complete after interval (seconds) and retry
        until timeout (seconds) expires. Raise trio.TooSlowError when timeout expires.

        Important:
         * The given predicate function must be async
         * Retries may occur more frequently than interval if the predicate
           returns false before interval expires. This only matters in that it
           uses more CPU.
        """
        with trio.fail_after(timeout):
            while True:
                with trio.move_on_after(interval):
                    if await predicate():
                        return


    async def num_of_slow_path(self):
        """
        Returns the total number of requests processed on the slow commit path
        """
        with trio.fail_after(seconds=5):
            while True:
                with trio.move_on_after(seconds=.5):
                    try:
                        metric_key = ['replica', 'Counters', 'slowPathCount']
                        nb_slow_path = await self.metrics.get(0, *metric_key)
                        return nb_slow_path
                    except KeyError:
                        # metrics not yet available, continue looping
                        pass
