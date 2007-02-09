import logging
from random import choice

from neo.protocol import Packet, OUT_OF_DATE_STATE, STORAGE_NODE_TYPE, \
        INVALID_OID, INVALID_TID, RUNNING_STATE
from neo.connection import ClientConnection
from neo.storage.handler import StorageEventHandler

class Partition(object):
    """This class abstracts the state of a partition."""

    def __init__(self, rid):
        self.rid = rid
        self.tid = None

    def getRID(self):
        return self.rid

    def getCriticalTID(self):
        return self.tid

    def setCriticalTID(self, tid):
        self.tid = tid

    def safe(self, pending_tid_list):
        if self.tid is None:
            return False
        for tid in pending_tid_list:
            if self.tid >= tid:
                return False
        return True

class ReplicationEventHandler(StorageEventHandler):
    """This class handles events for replications."""

    def connectionCompleted(self, conn):
        # Nothing to do.
        pass

    def connectionFailed(self, conn):
        logging.error('replication is stopped due to connection failure')
        self.app.replicator.reset()

    def handleAcceptNodeIdentification(self, conn, packet, node_type,
                                       uuid, ip_address, port,
                                       num_partitions, num_replicas):
        # Nothing to do.
        pass


class Replicator(object):
    """This class handles replications of objects and transactions.
    
    Assumptions:

        - Client nodes recognize partition changes reasonably quickly.

        - When an out of date partition is added, next transaction ID
          is given after the change is notified and serialized.

    Procedures:

        - Get the last TID right after a partition is added. This TID
          is called a "critical TID", because this and TIDs before this
          may not be present in this storage node yet. After a critical
          TID, all transactions must exist in this storage node.

        - Check if a primary master node still has pending transactions
          before and at a critical TID. If so, I must wait for them to be
          committed or aborted.

        - In order to copy data, first get the list of TIDs. This is done
          part by part, because the list can be very huge. When getting
          a part of the list, I verify if they are in my database, and
          ask data only for non-existing TIDs. This is performed until
          the check reaches a critical TID.

        - Next, get the list of OIDs. And, for each OID, ask the history,
          namely, a list of serials. This is also done part by part, and
          I ask only non-existing data. """

    def __init__(self, app):
        self.app = app
        self.new_partition_list = self._getOutdatedPartitionList()
        self.partition_list = []
        self.current_partition = None
        self.current_connection = None
        self.critical_tid_dict = {}
        self.waiting_for_unfinished_tids = False
        self.unfinished_tid_list = None
        self.replication_done = True

        # Find a connection to a primary master node.
        uuid = app.primary_master_node.getUUID()
        for conn in app.em.getConnectionList():
            if isinstance(conn, ClientConnection) and conn.getUUID() == uuid:
                self.primary_master_connection = conn
                break

    def reset(self):
        """Reset attributes to restart replicating."""
        self.current_partition = None
        self.current_connection = None
        self.waiting_for_unfinished_tids = False
        self.unfinished_tid_list = None
        self.replication_done = True

    def _getOutdatedPartitionList(self):
        app = self.app
        partition_list = []
        for offset in xrange(app.num_partitions):
            for uuid, state in app.pt.getRow(offset):
                if uuid == app.uuid and state == OUT_OF_DATE_STATE:
                    partition_list.append(Partition(offset))
        return partition_list

    def pending(self):
        """Return whether there is any pending partition."""
        return len(self.partition_list) or len(self.new_partition_list)

    def setCriticalTID(self, packet, tid):
        """This is a callback from OperationEventHandler."""
        msg_id = packet.getId()
        try:
            for partition in self.critical_tid_dict[msg_id]:
                partition.setCriticalTID(tid)
            del self.critical_tid_dict[msg_id]
        except KeyError:
            pass

    def _askCriticalTID(self):
        conn = self.primary_master_connection
        msg_id = conn.getNextId()
        conn.addPacket(Packet().askLastIDs(msg_id))
        conn.expectMessage(msg_id)
        self.critical_tid_dict[msg_id] = self.new_partition_list
        self.partition_list.extend(self.new_partition_list)
        self.new_partition_list = []

    def setUnfinishedTIDList(self, tid_list):
        """This is a callback from OperationEventHandler."""
        self.waiting_for_unfinished_tids = False
        self.unfinished_tid_list = tid_list

    def _askUnfinishedTIDs(self):
        conn = self.primary_master_connection
        msg_id = conn.getNextId()
        conn.addPacket(Packet().askUnfinishedTIDs(msg_id))
        conn.expectMessage(msg_id)
        self.waiting_for_unfinished_tids = True

    def _startReplication(self):
        # Choose a storage node for the source.
        app = self.app
        try:
            cell_list = app.pt.getCellList(self.current_partition, True)
            node_list = [cell.getNode() for cell in cell_list
                            if cell.getNodeState() == RUNNING_STATE]
            node = choice(node_list)
        except:
            # Not operational.
            return

        addr = node.getServer()
        if self.current_connection is not None:
            if self.current_connection.getAddress() == addr:
                # I can reuse the same connection.
                pass
            else:
                self.current_connection.close()
                self.current_connection = None

        if self.current_connection is None:
            handler = ReplicationEventHandler(app)
            self.current_connection = ClientConnection(app.em, handler, 
                                                       addr = addr)
            msg_id = self.current_connection.getNextId()
            p = Packet()
            p.requestNodeIdentification(msg_id, STORAGE_NODE_TYPE, app.uuid,
                                        app.server[0], app.server[1], app.name)
            self.current_connection.addPacket(p)
            self.current_connection.expectMessage(msg_id)

        msg_id = self.current_connection.getNextId()
        p = Packet()
        p.askTIDs(msg_id, 0, 1000, self.current_partition.getRID())
        self.current_connection.addPacket(p)
        self.current_connection.expectMessage(timeout = 300)

        self.replication_done = False

    def act(self):
        # If the new partition list is not empty, I must ask a critical
        # TID to a primary master node.
        if self.new_partition_list:
            self._askCriticalTID()

        if self.current_partition is None:
            # I need to choose something.
            if self.waiting_for_unfinished_tids:
                # Still waiting.
                return
            elif self.unfinished_tid_list is not None:
                # Try to select something.
                for partition in self.partition_list:
                    if partition.safe(self.unfinished_tid_list):
                        self.current_partition = partition
                        self.unfinished_tid_list = None
                        break
                else:
                    # Not yet.
                    self.unfinished_tid_list = None
                    return

                self._startReplication()
            else:
                # Ask pending transactions.
                self._askUnfinishedTIDs()
        else:
            if self.replication_done:
                try:
                    self.partition_list.remove(self.current_partition)
                except ValueError:
                    pass
                self.current_partition = None
