#
# Copyright (C) 2006-2010  Nexedi SA
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.

from thread import get_ident
from cPickle import dumps, loads
from zlib import compress as real_compress, decompress
from neo.locking import Queue, Empty
from random import shuffle
import time
import os

from ZODB.POSException import UndoError, StorageTransactionError, ConflictError
from ZODB.POSException import ReadConflictError
from ZODB.ConflictResolution import ResolvedSerial
from persistent.TimeStamp import TimeStamp

import neo
from neo.protocol import NodeTypes, Packets, INVALID_PARTITION, ZERO_TID
from neo.event import EventManager
from neo.util import makeChecksum as real_makeChecksum, dump
from neo.locking import Lock
from neo.connection import MTClientConnection, OnTimeout, ConnectionClosed
from neo.node import NodeManager
from neo.connector import getConnectorHandler
from neo.client.exception import NEOStorageError, NEOStorageCreationUndoneError
from neo.client.exception import NEOStorageNotFoundError
from neo.exception import NeoException
from neo.client.handlers import storage, master
from neo.dispatcher import Dispatcher, ForgottenPacket
from neo.client.poll import ThreadedPoll, psThreadedPoll
from neo.client.iterator import Iterator
from neo.client.mq import MQ, MQIndex
from neo.client.pool import ConnectionPool
from neo.util import u64, parseMasterList
from neo.profiling import profiler_decorator, PROFILING_ENABLED
from neo.live_debug import register as registerLiveDebugger

if PROFILING_ENABLED:
    # Those functions require a "real" python function wrapper before they can
    # be decorated.
    @profiler_decorator
    def compress(data):
        return real_compress(data)

    @profiler_decorator
    def makeChecksum(data):
        return real_makeChecksum(data)
else:
    # If profiling is disabled, directly use original functions.
    compress = real_compress
    makeChecksum = real_makeChecksum

class ThreadContext(object):

    def __init__(self):
        super(ThreadContext, self).__setattr__('_threads_dict', {})

    def __getThreadData(self):
        thread_id = get_ident()
        try:
            result = self._threads_dict[thread_id]
        except KeyError:
            self.clear(thread_id)
            result = self._threads_dict[thread_id]
        return result

    def __getattr__(self, name):
        thread_data = self.__getThreadData()
        try:
            return thread_data[name]
        except KeyError:
            raise AttributeError, name

    def __setattr__(self, name, value):
        thread_data = self.__getThreadData()
        thread_data[name] = value

    def clear(self, thread_id=None):
        if thread_id is None:
            thread_id = get_ident()
        thread_dict = self._threads_dict.get(thread_id)
        if thread_dict is None:
            queue = Queue(0)
        else:
            queue = thread_dict['queue']
        self._threads_dict[thread_id] = {
            'tid': None,
            'txn': None,
            'data_dict': {},
            'data_list': [],
            'object_serial_dict': {},
            'object_stored_counter_dict': {},
            'conflict_serial_dict': {},
            'resolved_conflict_serial_dict': {},
            'txn_voted': False,
            'queue': queue,
            'txn_info': 0,
            'history': None,
            'node_tids': {},
            'node_ready': False,
            'asked_object': 0,
            'undo_object_tid_dict': {},
            'involved_nodes': set(),
            'barrier_done': False,
            'last_transaction': None,
        }

class RevisionIndex(MQIndex):
    """
    This cache index allows accessing a specifig revision of a cached object.
    It requires cache key to be a 2-tuple, composed of oid and revision.

    Note: it is expected that rather few revisions are held in cache, with few
    lookups for old revisions, so they are held in a simple sorted list
    Note2: all methods here must be called with cache lock acquired.
    """
    def __init__(self):
        # key: oid
        # value: tid list, from highest to lowest
        self._oid_dict = {}
        # key: oid
        # value: tid list, from lowest to highest
        self._invalidated = {}

    def clear(self):
        self._oid_dict.clear()
        self._invalidated.clear()

    def remove(self, key):
        oid_dict = self._oid_dict
        oid, tid = key
        tid_list = oid_dict[oid]
        tid_list.remove(tid)
        if not tid_list:
            # No more serial known for this object, drop entirely
            del oid_dict[oid]
            self._invalidated.pop(oid, None)

    def add(self, key):
        oid_dict = self._oid_dict
        oid, tid = key
        try:
            serial_list = oid_dict[oid]
        except KeyError:
            serial_list = oid_dict[oid] = []
        else:
            assert tid not in serial_list
        if not(serial_list) or tid > serial_list[0]:
            serial_list.insert(0, tid)
        else:
            serial_list.insert(0, tid)
            serial_list.sort(reverse=True)
        invalidated = self._invalidated
        try:
            tid_list = invalidated[oid]
        except KeyError:
            pass
        else:
            try:
                tid_list.remove(tid)
            except ValueError:
                pass
            else:
                if not tid_list:
                    del invalidated[oid]

    def invalidate(self, oid_list, tid):
        """
        Mark object invalidated by given transaction.
        Must be called with increasing TID values (which is standard for
        ZODB).
        """
        invalidated = self._invalidated
        oid_dict = self._oid_dict
        for oid in (x for x in oid_list if x in oid_dict):
            try:
                tid_list = invalidated[oid]
            except KeyError:
                tid_list = invalidated[oid] = []
            assert not tid_list or tid > tid_list[-1], (dump(oid), dump(tid),
                dump(tid_list[-1]))
            tid_list.append(tid)

    def getSerialBefore(self, oid, tid):
        """
        Get the first tid in cache which value is lower that given tid.
        """
        # WARNING: return-intensive to save on indentation
        oid_list = self._oid_dict.get(oid)
        if oid_list is None:
            # Unknown oid
            return None
        for result in oid_list:
            if result < tid:
                # Candidate found
                break
        else:
            # No candidate in cache.
            return None
        # Check if there is a chance that an intermediate revision would
        # exist, while missing from cache.
        try:
            inv_tid_list = self._invalidated[oid]
        except KeyError:
            return result
        # Remember: inv_tid_list is sorted in ascending order.
        for inv_tid in inv_tid_list:
            if tid < inv_tid:
                # We don't care about invalidations past requested TID.
                break
            elif result < inv_tid < tid:
                # An invalidation was received between candidate revision,
                # and before requested TID: there is a matching revision we
                # don't know of, so we cannot answer.
                return None
        return result

    def getLatestSerial(self, oid):
        """
        Get the latest tid for given object.
        """
        result = self._oid_dict.get(oid)
        if result is not None:
            result = result[0]
            try:
                tid_list = self._invalidated[oid]
            except KeyError:
                pass
            else:
                if result < tid_list[-1]:
                    # An invalidation happened from a transaction later than our
                    # most recent view of this object, so we cannot answer.
                    result = None
        return result

    def getSerialList(self, oid):
        """
        Get the list of all serials cache knows about for given object.
        """
        return self._oid_dict.get(oid, [])[:]

class Application(object):
    """The client node application."""

    def __init__(self, master_nodes, name, connector=None, compress=True, **kw):
        # Start polling thread
        self.em = EventManager()
        self.poll_thread = ThreadedPoll(self.em, name=name)
        psThreadedPoll()
        # Internal Attributes common to all thread
        self._db = None
        self.name = name
        self.connector_handler = getConnectorHandler(connector)
        self.dispatcher = Dispatcher(self.poll_thread)
        self.nm = NodeManager()
        self.cp = ConnectionPool(self)
        self.pt = None
        self.master_conn = None
        self.primary_master_node = None
        self.trying_master_node = None

        # load master node list
        for address in parseMasterList(master_nodes):
            self.nm.createMaster(address=address)

        # no self-assigned UUID, primary master will supply us one
        self.uuid = None
        self.mq_cache = MQ()
        self.cache_revision_index = RevisionIndex()
        self.mq_cache.addIndex(self.cache_revision_index)
        self.new_oid_list = []
        self.last_oid = '\0' * 8
        self.storage_event_handler = storage.StorageEventHandler(self)
        self.storage_bootstrap_handler = storage.StorageBootstrapHandler(self)
        self.storage_handler = storage.StorageAnswersHandler(self)
        self.primary_handler = master.PrimaryAnswersHandler(self)
        self.primary_bootstrap_handler = master.PrimaryBootstrapHandler(self)
        self.notifications_handler = master.PrimaryNotificationsHandler( self)
        # Internal attribute distinct between thread
        self.local_var = ThreadContext()
        # Lock definition :
        # _load_lock is used to make loading and storing atomic
        lock = Lock()
        self._load_lock_acquire = lock.acquire
        self._load_lock_release = lock.release
        # _oid_lock is used in order to not call multiple oid
        # generation at the same time
        lock = Lock()
        self._oid_lock_acquire = lock.acquire
        self._oid_lock_release = lock.release
        lock = Lock()
        # _cache_lock is used for the client cache
        self._cache_lock_acquire = lock.acquire
        self._cache_lock_release = lock.release
        lock = Lock()
        # _connecting_to_master_node is used to prevent simultaneous master
        # node connection attemps
        self._connecting_to_master_node_acquire = lock.acquire
        self._connecting_to_master_node_release = lock.release
        # _nm ensure exclusive access to the node manager
        lock = Lock()
        self._nm_acquire = lock.acquire
        self._nm_release = lock.release
        self.compress = compress
        registerLiveDebugger(on_log=self.log)

    def log(self):
        self.em.log()
        self.nm.log()
        if self.pt is not None:
            self.pt.log()

    @profiler_decorator
    def _handlePacket(self, conn, packet, handler=None):
        """
          conn
            The connection which received the packet (forwarded to handler).
          packet
            The packet to handle.
          handler
            The handler to use to handle packet.
            If not given, it will be guessed from connection's not type.
        """
        if handler is None:
            # Guess the handler to use based on the type of node on the
            # connection
            node = self.nm.getByAddress(conn.getAddress())
            if node is None:
                raise ValueError, 'Expecting an answer from a node ' \
                    'which type is not known... Is this right ?'
            if node.isStorage():
                handler = self.storage_handler
            elif node.isMaster():
                handler = self.primary_handler
            else:
                raise ValueError, 'Unknown node type: %r' % (node.__class__, )
        conn.lock()
        try:
            handler.dispatch(conn, packet)
        finally:
            conn.unlock()

    @profiler_decorator
    def _waitAnyMessage(self, block=True):
        """
          Handle all pending packets.
          block
            If True (default), will block until at least one packet was
            received.
        """
        pending = self.dispatcher.pending
        queue = self.local_var.queue
        get = queue.get
        _handlePacket = self._handlePacket
        while pending(queue):
            try:
                conn, packet = get(block)
            except Empty:
                break
            if packet is None or isinstance(packet, ForgottenPacket):
                # connection was closed or some packet was forgotten
                continue
            block = False
            try:
                _handlePacket(conn, packet)
            except ConnectionClosed:
                pass

    @profiler_decorator
    def _waitMessage(self, target_conn, msg_id, handler=None):
        """Wait for a message returned by the dispatcher in queues."""
        get = self.local_var.queue.get
        _handlePacket = self._handlePacket
        while True:
            conn, packet = get(True)
            is_forgotten = isinstance(packet, ForgottenPacket)
            if target_conn is conn:
                # check fake packet
                if packet is None:
                    raise ConnectionClosed
                if msg_id == packet.getId():
                    if is_forgotten:
                        raise ValueError, 'ForgottenPacket for an ' \
                            'explicitely expected packet.'
                    _handlePacket(conn, packet, handler=handler)
                    break
            if not is_forgotten and packet is not None:
                _handlePacket(conn, packet)

    @profiler_decorator
    def _askStorage(self, conn, packet):
        """ Send a request to a storage node and process it's answer """
        msg_id = conn.ask(packet, queue=self.local_var.queue)
        self._waitMessage(conn, msg_id, self.storage_handler)

    @profiler_decorator
    def _askPrimary(self, packet):
        """ Send a request to the primary master and process it's answer """
        conn = self._getMasterConnection()
        msg_id = conn.ask(packet, queue=self.local_var.queue)
        self._waitMessage(conn, msg_id, self.primary_handler)

    @profiler_decorator
    def _getMasterConnection(self):
        """ Connect to the primary master node on demand """
        # acquire the lock to allow only one thread to connect to the primary
        result = self.master_conn
        if result is None:
            self._connecting_to_master_node_acquire()
            try:
                self.new_oid_list = []
                result = self._connectToPrimaryNode()
                self.master_conn = result
            finally:
                self._connecting_to_master_node_release()
        return result

    def _getPartitionTable(self):
        """ Return the partition table manager, reconnect the PMN if needed """
        # this ensure the master connection is established and the partition
        # table is up to date.
        self._getMasterConnection()
        return self.pt

    @profiler_decorator
    def _getCellListForOID(self, oid, readable=False, writable=False):
        """ Return the cells available for the specified OID """
        pt = self._getPartitionTable()
        return pt.getCellListForOID(oid, readable, writable)

    def _getCellListForTID(self, tid, readable=False, writable=False):
        """ Return the cells available for the specified TID """
        pt = self._getPartitionTable()
        return pt.getCellListForTID(tid, readable, writable)

    @profiler_decorator
    def _connectToPrimaryNode(self):
        """
            Lookup for the current primary master node
        """
        neo.logging.debug('connecting to primary master...')
        ready = False
        nm = self.nm
        queue = self.local_var.queue
        while not ready:
            # Get network connection to primary master
            index = 0
            connected = False
            while not connected:
                if self.primary_master_node is not None:
                    # If I know a primary master node, pinpoint it.
                    self.trying_master_node = self.primary_master_node
                    self.primary_master_node = None
                else:
                    # Otherwise, check one by one.
                    master_list = nm.getMasterList()
                    try:
                        self.trying_master_node = master_list[index]
                    except IndexError:
                        time.sleep(1)
                        index = 0
                        self.trying_master_node = master_list[0]
                    index += 1
                # Connect to master
                conn = MTClientConnection(self.em,
                        self.notifications_handler,
                        addr=self.trying_master_node.getAddress(),
                        connector=self.connector_handler(),
                        dispatcher=self.dispatcher)
                # Query for primary master node
                if conn.getConnector() is None:
                    # This happens if a connection could not be established.
                    neo.logging.error('Connection to master node %s failed',
                                  self.trying_master_node)
                    continue
                try:
                    msg_id = conn.ask(Packets.AskPrimary(), queue=queue)
                    self._waitMessage(conn, msg_id,
                            handler=self.primary_bootstrap_handler)
                except ConnectionClosed:
                    continue
                # If we reached the primary master node, mark as connected
                connected = self.primary_master_node is not None and \
                        self.primary_master_node is self.trying_master_node
            neo.logging.info('Connected to %s' % (self.primary_master_node, ))
            try:
                ready = self.identifyToPrimaryNode(conn)
            except ConnectionClosed:
                neo.logging.error('Connection to %s lost',
                    self.trying_master_node)
                self.primary_master_node = None
                continue
        neo.logging.info("Connected and ready")
        return conn

    def identifyToPrimaryNode(self, conn):
        """
            Request identification and required informations to be operational.
            Might raise ConnectionClosed so that the new primary can be
            looked-up again.
        """
        neo.logging.info('Initializing from master')
        queue = self.local_var.queue
        # Identify to primary master and request initial data
        while conn.getUUID() is None:
            p = Packets.RequestIdentification(NodeTypes.CLIENT, self.uuid,
                    None, self.name)
            self._waitMessage(conn, conn.ask(p, queue=queue),
                    handler=self.primary_bootstrap_handler)
            if conn.getUUID() is None:
                # Node identification was refused by master, it is considered
                # as the primary as long as we are connected to it.
                time.sleep(1)
        if self.uuid is not None:
            msg_id = conn.ask(Packets.AskNodeInformation(), queue=queue)
            self._waitMessage(conn, msg_id,
                    handler=self.primary_bootstrap_handler)
            msg_id = conn.ask(Packets.AskPartitionTable(), queue=queue)
            self._waitMessage(conn, msg_id,
                    handler=self.primary_bootstrap_handler)
        return self.uuid is not None and self.pt is not None \
                             and self.pt.operational()

    def registerDB(self, db, limit):
        self._db = db

    def getDB(self):
        return self._db

    @profiler_decorator
    def new_oid(self):
        """Get a new OID."""
        self._oid_lock_acquire()
        try:
            if len(self.new_oid_list) == 0:
                # Get new oid list from master node
                # we manage a list of oid here to prevent
                # from asking too many time new oid one by one
                # from master node
                self._askPrimary(Packets.AskNewOIDs(100))
                if len(self.new_oid_list) <= 0:
                    raise NEOStorageError('new_oid failed')
            self.last_oid = self.new_oid_list.pop(0)
            return self.last_oid
        finally:
            self._oid_lock_release()

    def getStorageSize(self):
        # return the last OID used, this is innacurate
        return int(u64(self.last_oid))

    @profiler_decorator
    def _load(self, oid, serial=None, tid=None):
        """
        Internal method which manage load, loadSerial and loadBefore.
        OID and TID (serial) parameters are expected packed.
        oid
            OID of object to get.
        serial
            If given, the exact serial at which OID is desired.
            tid should be None.
        tid
            If given, the excluded upper bound serial at which OID is desired.
            serial should be None.

        Return value: (3-tuple)
        - Object data (None if object creation was undone).
        - Serial of given data.
        - Next serial at which object exists, or None. Only set when tid
          parameter is not None.

        Exceptions:
            NEOStorageError
                technical problem
            NEOStorageNotFoundError
                object exists but no data satisfies given parameters
            NEOStorageDoesNotExistError
                object doesn't exist
            NEOStorageCreationUndoneError
                object existed, but its creation was undone
        """
        # TODO:
        # - rename parameters (here and in handlers & packet definitions)

        self._load_lock_acquire()
        try:
            # Once per transaction, upon first load, trigger a barrier so we
            # handle all pending invalidations, so the snapshot of the database
            # is as up-to-date as possible.
            if not self.local_var.barrier_done:
                self.invalidationBarrier()
                self.local_var.barrier_done = True
            try:
                result = self._loadFromCache(oid, serial, tid)
            except KeyError:
                pass
            else:
                return result
            data, start_serial, end_serial = self._loadFromStorage(oid, serial,
                tid)
            self._cache_lock_acquire()
            try:
                self.mq_cache[(oid, start_serial)] = data, end_serial
            finally:
                self._cache_lock_release()
            if data == '':
                raise NEOStorageCreationUndoneError(dump(oid))
            return data, start_serial, end_serial
        finally:
            self._load_lock_release()

    @profiler_decorator
    def _loadFromStorage(self, oid, at_tid, before_tid):
        cell_list = self._getCellListForOID(oid, readable=True)
        if len(cell_list) == 0:
            # No cells available, so why are we running ?
            raise NEOStorageError('No storage available for oid %s' % (
                dump(oid), ))

        shuffle(cell_list)
        cell_list.sort(key=self.cp.getCellSortKey)
        self.local_var.asked_object = 0
        packet = Packets.AskObject(oid, at_tid, before_tid)
        for cell in cell_list:
            neo.logging.debug('trying to load %s at %s before %s from %s',
                dump(oid), dump(at_tid), dump(before_tid), dump(cell.getUUID()))
            conn = self.cp.getConnForCell(cell)
            if conn is None:
                continue

            try:
                self._askStorage(conn, packet)
            except ConnectionClosed:
                continue

            # Check data
            noid, tid, next_tid, compression, checksum, data \
                = self.local_var.asked_object
            if noid != oid:
                # Oops, try with next node
                neo.logging.error('got wrong oid %s instead of %s from node ' \
                    '%s', noid, dump(oid), cell.getAddress())
                self.local_var.asked_object = -1
                continue
            elif checksum != makeChecksum(data):
                # Check checksum.
                neo.logging.error('wrong checksum from node %s for oid %s',
                              cell.getAddress(), dump(oid))
                self.local_var.asked_object = -1
                continue
            else:
                # Everything looks alright.
                break

        if self.local_var.asked_object == 0:
            # We didn't got any object from all storage node because of
            # connection error
            raise NEOStorageError('connection failure')

        if self.local_var.asked_object == -1:
            raise NEOStorageError('inconsistent data')

        # Uncompress data
        if compression:
            data = decompress(data)
        return data, tid, next_tid

    @profiler_decorator
    def _loadFromCache(self, oid, at_tid, before_tid):
        """
        Load from local cache, raising KeyError if not found.
        """
        self._cache_lock_acquire()
        try:
            if at_tid is not None:
                tid = at_tid
            elif before_tid is not None:
                tid = self.cache_revision_index.getSerialBefore(oid,
                    before_tid)
            else:
                tid = self.cache_revision_index.getLatestSerial(oid)
            if tid is None:
                raise KeyError
            # Raises KeyError on miss
            data, next_tid = self.mq_cache[(oid, tid)]
            return (data, tid, next_tid)
        finally:
            self._cache_lock_release()

    @profiler_decorator
    def load(self, oid, version=None):
        """Load an object for a given oid."""
        result = self._load(oid)[:2]
        # Start a network barrier, so we get all invalidations *after* we
        # received data. This ensures we get any invalidation message that
        # would have been about the version we loaded.
        # Those invalidations are checked at ZODB level, so it decides if
        # loaded data can be handed to current transaction or if a separate
        # loadBefore call is required.
        # XXX: A better implementation is required to improve performances
        self.invalidationBarrier()
        return result

    @profiler_decorator
    def loadSerial(self, oid, serial):
        """Load an object for a given oid and serial."""
        neo.logging.debug('loading %s at %s', dump(oid), dump(serial))
        return self._load(oid, serial=serial)[0]


    @profiler_decorator
    def loadBefore(self, oid, tid):
        """Load an object for a given oid before tid committed."""
        neo.logging.debug('loading %s before %s', dump(oid), dump(tid))
        return self._load(oid, tid=tid)


    @profiler_decorator
    def tpc_begin(self, transaction, tid=None, status=' '):
        """Begin a new transaction."""
        # First get a transaction, only one is allowed at a time
        if self.local_var.txn is transaction:
            # We already begin the same transaction
            raise StorageTransactionError('Duplicate tpc_begin calls')
        if self.local_var.txn is not None:
            raise NeoException, 'local_var is not clean in tpc_begin'
        # use the given TID or request a new one to the master
        self._askPrimary(Packets.AskBeginTransaction(tid))
        if self.local_var.tid is None:
            raise NEOStorageError('tpc_begin failed')
        assert tid in (None, self.local_var.tid), (tid, self.local_var.tid)
        self.local_var.txn = transaction

    @profiler_decorator
    def store(self, oid, serial, data, version, transaction):
        """Store object."""
        if transaction is not self.local_var.txn:
            raise StorageTransactionError(self, transaction)
        neo.logging.debug('storing oid %s serial %s',
                     dump(oid), dump(serial))
        self._store(oid, serial, data)
        return None

    def _store(self, oid, serial, data, data_serial=None):
        # Find which storage node to use
        cell_list = self._getCellListForOID(oid, writable=True)
        if len(cell_list) == 0:
            raise NEOStorageError
        if data is None:
            # This is some undo: either a no-data object (undoing object
            # creation) or a back-pointer to an earlier revision (going back to
            # an older object revision).
            data = compressed_data = ''
            compression = 0
        else:
            assert data_serial is None
            compression = self.compress
            compressed_data = data
            if self.compress:
                compressed_data = compress(data)
                if len(compressed_data) > len(data):
                    compressed_data = data
                    compression = 0
                else:
                    compression = 1
        checksum = makeChecksum(compressed_data)
        p = Packets.AskStoreObject(oid, serial, compression,
                 checksum, compressed_data, data_serial, self.local_var.tid)
        on_timeout = OnTimeout(self.onStoreTimeout, self.local_var.tid, oid)
        # Store object in tmp cache
        local_var = self.local_var
        data_dict = local_var.data_dict
        if oid not in data_dict:
            local_var.data_list.append(oid)
        data_dict[oid] = data
        # Store data on each node
        self.local_var.object_stored_counter_dict[oid] = {}
        self.local_var.object_serial_dict[oid] = serial
        getConnForCell = self.cp.getConnForCell
        queue = self.local_var.queue
        add_involved_nodes = self.local_var.involved_nodes.add
        for cell in cell_list:
            conn = getConnForCell(cell)
            if conn is None:
                continue
            try:
                conn.ask(p, on_timeout=on_timeout, queue=queue)
                add_involved_nodes(cell.getNode())
            except ConnectionClosed:
                continue

        self._waitAnyMessage(False)

    def onStoreTimeout(self, conn, msg_id, tid, oid):
        # NOTE: this method is called from poll thread, don't use
        # local_var !
        # Stop expecting the timed-out store request.
        queue = self.dispatcher.forget(conn, msg_id)
        # Ask the storage if someone locks the object.
        # Shorten timeout to react earlier to an unresponding storage.
        conn.ask(Packets.AskHasLock(tid, oid), timeout=5, queue=queue)
        return True

    @profiler_decorator
    def _handleConflicts(self, tryToResolveConflict):
        result = []
        append = result.append
        local_var = self.local_var
        # Check for conflicts
        data_dict = local_var.data_dict
        object_serial_dict = local_var.object_serial_dict
        conflict_serial_dict = local_var.conflict_serial_dict
        resolved_conflict_serial_dict = local_var.resolved_conflict_serial_dict
        for oid, conflict_serial_set in conflict_serial_dict.items():
            resolved_serial_set = resolved_conflict_serial_dict.setdefault(
                oid, set())
            conflict_serial = max(conflict_serial_set)
            if resolved_serial_set and conflict_serial <= max(resolved_serial_set):
                # A later serial has already been resolved, skip.
                resolved_serial_set.update(conflict_serial_dict.pop(oid))
                continue
            serial = object_serial_dict[oid]
            data = data_dict[oid]
            tid = local_var.tid
            resolved = False
            if data is not None:
                if conflict_serial <= tid:
                    new_data = tryToResolveConflict(oid, conflict_serial,
                        serial, data)
                    if new_data is not None:
                        neo.logging.info('Conflict resolution succeed for ' \
                            '%r:%r with %r', dump(oid), dump(serial),
                            dump(conflict_serial))
                        # Mark this conflict as resolved
                        resolved_serial_set.update(conflict_serial_dict.pop(
                            oid))
                        # Try to store again
                        self._store(oid, conflict_serial, new_data)
                        append(oid)
                        resolved = True
                    else:
                        neo.logging.info('Conflict resolution failed for ' \
                            '%r:%r with %r', dump(oid), dump(serial),
                            dump(conflict_serial))
                else:
                    neo.logging.info('Conflict reported for %r:%r with ' \
                        'later transaction %r , cannot resolve conflict.',
                        dump(oid), dump(serial), dump(conflict_serial))
            if not resolved:
                # XXX: Is it really required to remove from data_dict ?
                del data_dict[oid]
                local_var.data_list.remove(oid)
                if data is None:
                    exc = ReadConflictError(oid=oid, serials=(conflict_serial,
                        serial))
                else:
                    exc = ConflictError(oid=oid, serials=(tid, serial),
                        data=data)
                raise exc
        return result

    @profiler_decorator
    def waitResponses(self):
        """Wait for all requests to be answered (or their connection to be
        dected as closed)"""
        queue = self.local_var.queue
        pending = self.dispatcher.pending
        _waitAnyMessage = self._waitAnyMessage
        while pending(queue):
            _waitAnyMessage()

    @profiler_decorator
    def waitStoreResponses(self, tryToResolveConflict):
        result = []
        append = result.append
        resolved_oid_set = set()
        update = resolved_oid_set.update
        local_var = self.local_var
        tid = local_var.tid
        _handleConflicts = self._handleConflicts
        while True:
            self.waitResponses()
            conflicts = _handleConflicts(tryToResolveConflict)
            if conflicts:
                update(conflicts)
            else:
                # No more conflict resolutions to do, no more pending store
                # requests
                break

        # Check for never-stored objects, and update result for all others
        for oid, store_dict in \
            local_var.object_stored_counter_dict.iteritems():
            if not store_dict:
                raise NEOStorageError('tpc_store failed')
            elif oid in resolved_oid_set:
                append((oid, ResolvedSerial))
            else:
                append((oid, tid))
        return result

    @profiler_decorator
    def tpc_vote(self, transaction, tryToResolveConflict):
        """Store current transaction."""
        local_var = self.local_var
        if transaction is not local_var.txn:
            raise StorageTransactionError(self, transaction)

        result = self.waitStoreResponses(tryToResolveConflict)

        tid = local_var.tid
        # Store data on each node
        txn_stored_counter = 0
        p = Packets.AskStoreTransaction(tid, str(transaction.user),
            str(transaction.description), dumps(transaction._extension),
            local_var.data_list)
        add_involved_nodes = self.local_var.involved_nodes.add
        for cell in self._getCellListForTID(tid, writable=True):
            neo.logging.debug("voting object %s %s", cell.getAddress(),
                cell.getState())
            conn = self.cp.getConnForCell(cell)
            if conn is None:
                continue

            try:
                self._askStorage(conn, p)
                add_involved_nodes(cell.getNode())
            except ConnectionClosed:
                continue
            txn_stored_counter += 1

        # check at least one storage node accepted
        if txn_stored_counter == 0:
            raise NEOStorageError('tpc_vote failed')
        # Check if master connection is still alive.
        # This is just here to lower the probability of detecting a problem
        # in tpc_finish, as we should do our best to detect problem before
        # tpc_finish.
        self._getMasterConnection()

        local_var.txn_voted = True
        return result

    @profiler_decorator
    def tpc_abort(self, transaction):
        """Abort current transaction."""
        if transaction is not self.local_var.txn:
            return

        tid = self.local_var.tid
        p = Packets.AbortTransaction(tid)
        getConnForNode = self.cp.getConnForNode
        # cancel transaction one all those nodes
        for node in self.local_var.involved_nodes:
            conn = getConnForNode(node)
            if conn is None:
                continue
            try:
                conn.notify(p)
            except:
                neo.logging.error('Exception in tpc_abort while notifying ' \
                    'storage node %r of abortion, ignoring.', conn, exc_info=1)
        self._getMasterConnection().notify(p)

        # Just wait for responses to arrive. If any leads to an exception,
        # log it and continue: we *must* eat all answers to not disturb the
        # next transaction.
        queue = self.local_var.queue
        pending = self.dispatcher.pending
        _waitAnyMessage = self._waitAnyMessage
        while pending(queue):
            try:
                _waitAnyMessage()
            except:
                neo.logging.error('Exception in tpc_abort while handling ' \
                    'pending answers, ignoring.', exc_info=1)

        self.local_var.clear()

    @profiler_decorator
    def tpc_finish(self, transaction, tryToResolveConflict, f=None):
        """Finish current transaction."""
        local_var = self.local_var
        if local_var.txn is not transaction:
            raise StorageTransactionError('tpc_finish called for wrong '
                'transaction')
        if not local_var.txn_voted:
            self.tpc_vote(transaction, tryToResolveConflict)
        self._load_lock_acquire()
        try:
            tid = local_var.tid
            # Call function given by ZODB
            if f is not None:
                f(tid)

            # Call finish on master
            oid_list = local_var.data_list
            p = Packets.AskFinishTransaction(tid, oid_list)
            self._askPrimary(p)

            # Update cache
            self._cache_lock_acquire()
            try:
                mq_cache = self.mq_cache
                update = mq_cache.update
                def updateNextSerial(value):
                    data, next_tid = value
                    assert next_tid is None, (dump(oid), dump(base_tid),
                        dump(next_tid))
                    return (data, tid)
                get_baseTID = local_var.object_serial_dict.get
                for oid, data in local_var.data_dict.iteritems():
                    if data is None:
                        # this is just a remain of
                        # checkCurrentSerialInTransaction call, ignore (no data
                        # was modified).
                        continue
                    # Update ex-latest value in cache
                    base_tid = get_baseTID(oid)
                    try:
                        update((oid, base_tid), updateNextSerial)
                    except KeyError:
                        pass
                    if data == '':
                        self.cache_revision_index.invalidate([oid], tid)
                    else:
                        # Store in cache with no next_tid
                        mq_cache[(oid, tid)] = (data, None)
            finally:
                self._cache_lock_release()
            local_var.clear()
            return tid
        finally:
            self._load_lock_release()

    def undo(self, undone_tid, txn, tryToResolveConflict):
        if txn is not self.local_var.txn:
            raise StorageTransactionError(self, undone_tid)

        # First get transaction information from a storage node.
        cell_list = self._getCellListForTID(undone_tid, readable=True)
        shuffle(cell_list)
        cell_list.sort(key=self.cp.getCellSortKey)
        packet = Packets.AskTransactionInformation(undone_tid)
        getConnForCell = self.cp.getConnForCell
        for cell in cell_list:
            conn = getConnForCell(cell)
            if conn is None:
                continue

            self.local_var.txn_info = 0
            self.local_var.txn_ext = 0
            try:
                self._askStorage(conn, packet)
            except ConnectionClosed:
                continue
            except NEOStorageNotFoundError:
                # Tid not found, try with next node
                neo.logging.warning('Transaction %s was not found on node %s',
                    dump(undone_tid), self.nm.getByAddress(conn.getAddress()))
                continue

            if isinstance(self.local_var.txn_info, dict):
                break
            else:
                raise NEOStorageError('undo failed')
        else:
            raise NEOStorageError('undo failed')

        oid_list = self.local_var.txn_info['oids']

        # Regroup objects per partition, to ask a minimum set of storage.
        partition_oid_dict = {}
        pt = self._getPartitionTable()
        for oid in oid_list:
            partition = pt.getPartition(oid)
            try:
                oid_list = partition_oid_dict[partition]
            except KeyError:
                oid_list = partition_oid_dict[partition] = []
            oid_list.append(oid)

        # Ask storage the undo serial (serial at which object's previous data
        # is)
        getCellList = pt.getCellList
        getCellSortKey = self.cp.getCellSortKey
        queue = self.local_var.queue
        undo_object_tid_dict = self.local_var.undo_object_tid_dict = {}
        for partition, oid_list in partition_oid_dict.iteritems():
            cell_list = getCellList(partition, readable=True)
            shuffle(cell_list)
            cell_list.sort(key=getCellSortKey)
            storage_conn = getConnForCell(cell_list[0])
            storage_conn.ask(Packets.AskObjectUndoSerial(self.local_var.tid,
                undone_tid, oid_list), queue=queue)

        # Wait for all AnswerObjectUndoSerial. We might get OidNotFoundError,
        # meaning that objects in transaction's oid_list do not exist any
        # longer. This is the symptom of a pack, so forbid undoing transaction
        # when it happens, but sill keep waiting for answers.
        failed = False
        while True:
            try:
                self.waitResponses()
            except NEOStorageNotFoundError:
                failed = True
            else:
                break
        if failed:
            raise UndoError('non-undoable transaction')

        # Send undo data to all storage nodes.
        for oid in oid_list:
            current_serial, undo_serial, is_current = undo_object_tid_dict[oid]
            if is_current:
                data = None
            else:
                # Serial being undone is not the latest version for this
                # object. This is an undo conflict, try to resolve it.
                try:
                    # Load the latest version we are supposed to see
                    data = self.loadSerial(oid, current_serial)
                    # Load the version we were undoing to
                    undo_data = self.loadSerial(oid, undo_serial)
                except NEOStorageNotFoundError:
                    raise UndoError('Object not found while resolving undo '
                        'conflict')
                # Resolve conflict
                try:
                    data = tryToResolveConflict(oid, current_serial,
                        undone_tid, undo_data, data)
                except ConflictError:
                    data = None
                if data is None:
                    raise UndoError('Some data were modified by a later ' \
                        'transaction', oid)
                undo_serial = None
            self._store(oid, current_serial, data, undo_serial)

    def _insertMetadata(self, txn_info, extension):
        for k, v in loads(extension).items():
            txn_info[k] = v

    def __undoLog(self, first, last, filter=None, block=0, with_oids=False):
        if last < 0:
            # See FileStorage.py for explanation
            last = first - last

        # First get a list of transactions from all storage nodes.
        # Each storage node will return TIDs only for UP_TO_DATE state and
        # FEEDING state cells
        pt = self._getPartitionTable()
        storage_node_list = pt.getNodeList()

        self.local_var.node_tids = {}
        queue = self.local_var.queue
        for storage_node in storage_node_list:
            conn = self.cp.getConnForNode(storage_node)
            if conn is None:
                continue
            conn.ask(Packets.AskTIDs(first, last, INVALID_PARTITION), queue=queue)

        # Wait for answers from all storages.
        self.waitResponses()

        # Reorder tids
        ordered_tids = set()
        update = ordered_tids.update
        for tid_list in self.local_var.node_tids.itervalues():
            update(tid_list)
        ordered_tids = list(ordered_tids)
        ordered_tids.sort(reverse=True)
        neo.logging.debug("UndoLog tids %s", [dump(x) for x in ordered_tids])
        # For each transaction, get info
        undo_info = []
        append = undo_info.append
        for tid in ordered_tids:
            cell_list = self._getCellListForTID(tid, readable=True)
            shuffle(cell_list)
            cell_list.sort(key=self.cp.getCellSortKey)
            for cell in cell_list:
                conn = self.cp.getConnForCell(cell)
                if conn is not None:
                    self.local_var.txn_info = 0
                    self.local_var.txn_ext = 0
                    try:
                        self._askStorage(conn,
                                Packets.AskTransactionInformation(tid))
                    except ConnectionClosed:
                        continue
                    if isinstance(self.local_var.txn_info, dict):
                        break

            if self.local_var.txn_info in (-1, 0):
                # TID not found at all
                raise NeoException, 'Data inconsistency detected: ' \
                                    'transaction info for TID %r could not ' \
                                    'be found' % (tid, )

            if filter is None or filter(self.local_var.txn_info):
                self.local_var.txn_info.pop('packed')
                if not with_oids:
                    self.local_var.txn_info.pop("oids")
                append(self.local_var.txn_info)
                self._insertMetadata(self.local_var.txn_info,
                        self.local_var.txn_ext)
                if len(undo_info) >= last - first:
                    break
        # Check we return at least one element, otherwise call
        # again but extend offset
        if len(undo_info) == 0 and not block:
            undo_info = self.__undoLog(first=first, last=last*5, filter=filter,
                    block=1, with_oids=with_oids)
        return undo_info

    def undoLog(self, first, last, filter=None, block=0):
        return self.__undoLog(first, last, filter, block)

    def transactionLog(self, first, last):
        return self.__undoLog(first, last, with_oids=True)

    def history(self, oid, version=None, size=1, filter=None):
        # Get history informations for object first
        cell_list = self._getCellListForOID(oid, readable=True)
        shuffle(cell_list)
        cell_list.sort(key=self.cp.getCellSortKey)
        for cell in cell_list:
            # FIXME: we keep overwriting self.local_var.history here, we
            # should aggregate it instead.
            conn = self.cp.getConnForCell(cell)
            if conn is None:
                continue

            self.local_var.history = None
            try:
                self._askStorage(conn, Packets.AskObjectHistory(oid, 0, size))
            except ConnectionClosed:
                continue

            if self.local_var.history[0] != oid:
                # Got history for wrong oid
                raise NEOStorageError('inconsistency in storage: asked oid ' \
                                      '%r, got %r' % (
                                      oid, self.local_var.history[0]))

        if not isinstance(self.local_var.history, tuple):
            raise NEOStorageError('history failed')

        if self.local_var.history[1] == [] or \
            self.local_var.history[1][0][1] == 0:
            # KeyError expected if no history was found
            # XXX: this may requires an error from the storages
            raise KeyError

        # Now that we have object informations, get txn informations
        history_list = []
        for serial, size in self.local_var.history[1]:
            self._getCellListForTID(serial, readable=True)
            shuffle(cell_list)
            cell_list.sort(key=self.cp.getCellSortKey)
            for cell in cell_list:
                conn = self.cp.getConnForCell(cell)
                if conn is None:
                    continue

                # ask transaction information
                self.local_var.txn_info = None
                try:
                    self._askStorage(conn,
                            Packets.AskTransactionInformation(serial))
                except ConnectionClosed:
                    continue
                except NEOStorageNotFoundError:
                    # TID not found
                    continue
                if isinstance(self.local_var.txn_info, dict):
                    break

            # create history dict
            self.local_var.txn_info.pop('id')
            self.local_var.txn_info.pop('oids')
            self.local_var.txn_info.pop('packed')
            self.local_var.txn_info['tid'] = serial
            self.local_var.txn_info['version'] = ''
            self.local_var.txn_info['size'] = size
            if filter is None or filter(self.local_var.txn_info):
                history_list.append(self.local_var.txn_info)
            self._insertMetadata(self.local_var.txn_info,
                    self.local_var.txn_ext)

        return history_list

    @profiler_decorator
    def importFrom(self, source, start, stop, tryToResolveConflict):
        serials = {}
        def updateLastSerial(oid, result):
            if result:
                if isinstance(result, str):
                    assert oid is not None
                    serials[oid] = result
                else:
                    for oid, serial in result:
                        assert isinstance(serial, str), serial
                        serials[oid] = serial
        transaction_iter = source.iterator(start, stop)
        for transaction in transaction_iter:
            self.tpc_begin(transaction, transaction.tid, transaction.status)
            for r in transaction:
                pre = serials.get(r.oid, None)
                # TODO: bypass conflict resolution, locks...
                result = self.store(r.oid, pre, r.data, r.version, transaction)
                updateLastSerial(r.oid, result)
            updateLastSerial(None, self.tpc_vote(transaction,
                        tryToResolveConflict))
            self.tpc_finish(transaction, tryToResolveConflict)
        transaction_iter.close()

    def iterator(self, start=None, stop=None):
        return Iterator(self, start, stop)

    def lastTransaction(self):
        self._askPrimary(Packets.AskLastTransaction())
        return self.local_var.last_transaction

    def abortVersion(self, src, transaction):
        if transaction is not self.local_var.txn:
            raise StorageTransactionError(self, transaction)
        return '', []

    def commitVersion(self, src, dest, transaction):
        if transaction is not self.local_var.txn:
            raise StorageTransactionError(self, transaction)
        return '', []

    def loadEx(self, oid, version):
        data, serial = self.load(oid=oid)
        return data, serial, ''

    def __del__(self):
        """Clear all connection."""
        # Due to bug in ZODB, close is not always called when shutting
        # down zope, so use __del__ to close connections
        for conn in self.em.getConnectionList():
            conn.close()
        self.cp.flush()
        self.master_conn = None
        # Stop polling thread
        neo.logging.debug('Stopping %s', self.poll_thread)
        self.poll_thread.stop()
        psThreadedPoll()
    close = __del__

    def invalidationBarrier(self):
        self._askPrimary(Packets.AskBarrier())

    def sync(self):
        self._waitAnyMessage(False)

    def setNodeReady(self):
        self.local_var.node_ready = True

    def setNodeNotReady(self):
        self.local_var.node_ready = False

    def isNodeReady(self):
        return self.local_var.node_ready

    def setTID(self, value):
        self.local_var.tid = value

    def getTID(self):
        return self.local_var.tid

    def pack(self, t):
        tid = repr(TimeStamp(*time.gmtime(t)[:5] + (t % 60, )))
        if tid == ZERO_TID:
            raise NEOStorageError('Invalid pack time')
        self._askPrimary(Packets.AskPack(tid))
        # XXX: this is only needed to make ZODB unit tests pass.
        # It should not be otherwise required (clients should be free to load
        # old data as long as it is available in cache, event if it was pruned
        # by a pack), so don't bother invalidating on other clients.
        self._cache_lock_acquire()
        try:
            self.mq_cache.clear()
        finally:
            self._cache_lock_release()

    def getLastTID(self, oid):
        return self._load(oid)[1]

    def checkCurrentSerialInTransaction(self, oid, serial, transaction):
        local_var = self.local_var
        if transaction is not local_var.txn:
              raise StorageTransactionError(self, transaction)
        cell_list = self._getCellListForOID(oid, writable=True)
        if len(cell_list) == 0:
            raise NEOStorageError
        p = Packets.AskCheckCurrentSerial(local_var.tid, serial, oid)
        getConnForCell = self.cp.getConnForCell
        queue = local_var.queue
        local_var.object_serial_dict[oid] = serial
        # Placeholders
        local_var.object_stored_counter_dict[oid] = {}
        data_dict = local_var.data_dict
        if oid not in data_dict:
            # Marker value so we don't try to resolve conflicts.
            data_dict[oid] = None
            local_var.data_list.append(oid)
        for cell in cell_list:
            conn = getConnForCell(cell)
            if conn is None:
                continue
            try:
                conn.ask(p, queue=queue)
            except ConnectionClosed:
                continue

        self._waitAnyMessage(False)

