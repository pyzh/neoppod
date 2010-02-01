#
# Copyright (C) 2006-2009  Nexedi SA
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

from neo import logging
from neo import protocol
from neo.protocol import NodeStates, ErrorCodes, Packets
from neo.protocol import PacketMalformedError, UnexpectedPacketError, \
        BrokenNodeDisallowedError, NotReadyError, ProtocolError


class EventHandler(object):
    """This class handles events."""

    def __init__(self, app):
        self.app = app
        self.packet_dispatch_table = self.__initPacketDispatchTable()
        self.error_dispatch_table = self.__initErrorDispatchTable()

    def _packetMalformed(self, conn, packet, message='', *args):
        """Called when a packet is malformed."""
        args = (conn.getAddress()[0], conn.getAddress()[1], message)
        if packet is None:
            # if decoding fail, there's no packet instance
            logging.error('malformed packet from %s:%d: %s', *args)
        else:
            logging.error('malformed packet %s from %s:%d: %s',
                   packet.getType(), *args)
        response = protocol.protocolError(message)
        if packet is not None:
            conn.answer(response)
        else:
            conn.notify(response)
        conn.abort()
        self.peerBroken(conn)

    def __unexpectedPacket(self, conn, packet, message=None):
        """Handle an unexpected packet."""
        if message is None:
            message = 'unexpected packet type %s in %s' % (packet.getType(),
                    self.__class__.__name__)
        else:
            message = 'unexpected packet: %s in %s' % (message,
                    self.__class__.__name__)
        logging.error(message)
        conn.answer(protocol.protocolError(message))
        conn.abort()
        self.peerBroken(conn)

    def dispatch(self, conn, packet):
        """This is a helper method to handle various packet types."""
        try:
            try:
                method = self.packet_dispatch_table[packet.getType()]
            except KeyError:
                raise UnexpectedPacketError('no handler found')
            args = packet.decode() or ()
            conn.setPeerId(packet.getId())
            method(conn, *args)
        except UnexpectedPacketError, e:
            self.__unexpectedPacket(conn, packet, *e.args)
        except PacketMalformedError, e:
            self._packetMalformed(conn, packet, *e.args)
        except BrokenNodeDisallowedError:
            conn.answer(protocol.brokenNodeDisallowedError('go away'))
            conn.abort()
        except NotReadyError, message:
            if not message.args:
                message = 'Retry Later'
            message = str(message)
            conn.answer(protocol.notReady(message))
            conn.abort()
        except ProtocolError, message:
            message = str(message)
            conn.answer(protocol.protocolError(message))
            conn.abort()

    def checkClusterName(self, name):
        # raise an exception if the fiven name mismatch the current cluster name
        if self.app.name != name:
            logging.error('reject an alien cluster')
            raise protocol.ProtocolError('invalid cluster name')


    # Network level handlers

    def packetReceived(self, conn, packet):
        """Called when a packet is received."""
        self.dispatch(conn, packet)

    def connectionStarted(self, conn):
        """Called when a connection is started."""
        logging.debug('connection started for %s:%d', *(conn.getAddress()))

    def connectionCompleted(self, conn):
        """Called when a connection is completed."""
        logging.debug('connection completed for %s:%d', *(conn.getAddress()))

    def connectionFailed(self, conn):
        """Called when a connection failed."""
        logging.debug('connection failed for %s:%d', *(conn.getAddress()))

    def connectionAccepted(self, conn):
        """Called when a connection is accepted."""
        # A request for a node identification should arrive.
        conn.expectMessage(timeout = 10, additional_timeout = 0)

    def timeoutExpired(self, conn):
        """Called when a timeout event occurs."""
        logging.debug('timeout expired for %s:%d', *(conn.getAddress()))
        self.connectionLost(conn, NodeStates.TEMPORARILY_DOWN)

    def connectionClosed(self, conn):
        """Called when a connection is closed by the peer."""
        logging.debug('connection closed for %s:%d', *(conn.getAddress()))
        self.connectionLost(conn, NodeStates.TEMPORARILY_DOWN)

    def peerBroken(self, conn):
        """Called when a peer is broken."""
        logging.error('%s:%d is broken', *(conn.getAddress()))
        self.connectionLost(conn, NodeStates.BROKEN)

    def connectionLost(self, conn, new_state):
        """ this is a method to override in sub-handlers when there is no need
        to make distinction from the kind event that closed the connection  """
        pass


    # Packet handlers.

    def requestIdentification(self, conn, node_type,
                                        uuid, address, name):
        raise UnexpectedPacketError

    def acceptIdentification(self, conn, node_type,
                       uuid, num_partitions, num_replicas, your_uuid):
        raise UnexpectedPacketError

    def askPrimary(self, conn):
        raise UnexpectedPacketError

    def answerPrimary(self, conn, primary_uuid,
                                  known_master_list):
        raise UnexpectedPacketError

    def announcePrimary(self, con):
        raise UnexpectedPacketError

    def reelectPrimary(self, conn):
        raise UnexpectedPacketError

    def notifyNodeInformation(self, conn, node_list):
        raise UnexpectedPacketError

    def askLastIDs(self, conn):
        raise UnexpectedPacketError

    def answerLastIDs(self, conn, loid, ltid, lptid):
        raise UnexpectedPacketError

    def askPartitionTable(self, conn, offset_list):
        raise UnexpectedPacketError

    def answerPartitionTable(self, conn, ptid, row_list):
        raise UnexpectedPacketError

    def sendPartitionTable(self, conn, ptid, row_list):
        raise UnexpectedPacketError

    def notifyPartitionChanges(self, conn, ptid, cell_list):
        raise UnexpectedPacketError

    def startOperation(self, conn):
        raise UnexpectedPacketError

    def stopOperation(self, conn):
        raise UnexpectedPacketError

    def askUnfinishedTransactions(self, conn):
        raise UnexpectedPacketError

    def answerUnfinishedTransactions(self, conn, tid_list):
        raise UnexpectedPacketError

    def askObjectPresent(self, conn, oid, tid):
        raise UnexpectedPacketError

    def answerObjectPresent(self, conn, oid, tid):
        raise UnexpectedPacketError

    def deleteTransaction(self, conn, tid):
        raise UnexpectedPacketError

    def commitTransaction(self, conn, tid):
        raise UnexpectedPacketError

    def askBeginTransaction(self, conn, tid):
        raise UnexpectedPacketError

    def answerBeginTransaction(self, conn, tid):
        raise UnexpectedPacketError

    def askNewOIDs(self, conn, num_oids):
        raise UnexpectedPacketError

    def answerNewOIDs(self, conn, num_oids):
        raise UnexpectedPacketError

    def finishTransaction(self, conn, oid_list, tid):
        raise UnexpectedPacketError

    def answerTransactionFinished(self, conn, tid):
        raise UnexpectedPacketError

    def lockInformation(self, conn, tid):
        raise UnexpectedPacketError

    def answerInformationLocked(self, conn, tid):
        raise UnexpectedPacketError

    def invalidateObjects(self, conn, oid_list, tid):
        raise UnexpectedPacketError

    def notifyUnlockInformation(self, conn, tid):
        raise UnexpectedPacketError

    def askStoreObject(self, conn, oid, serial,
                             compression, checksum, data, tid):
        raise UnexpectedPacketError

    def answerStoreObject(self, conn, conflicting, oid, serial):
        raise UnexpectedPacketError

    def abortTransaction(self, conn, tid):
        raise UnexpectedPacketError

    def askStoreTransaction(self, conn, tid, user, desc,
                                  ext, oid_list):
        raise UnexpectedPacketError

    def answerStoreTransaction(self, conn, tid):
        raise UnexpectedPacketError

    def askObject(self, conn, oid, serial, tid):
        raise UnexpectedPacketError

    def answerObject(self, conn, oid, serial_start,
                           serial_end, compression, checksum, data):
        raise UnexpectedPacketError

    def askTIDs(self, conn, first, last, partition):
        raise UnexpectedPacketError

    def answerTIDs(self, conn, tid_list):
        raise UnexpectedPacketError

    def askTransactionInformation(self, conn, tid):
        raise UnexpectedPacketError

    def answerTransactionInformation(self, conn, tid,
                                           user, desc, ext, oid_list):
        raise UnexpectedPacketError

    def askObjectHistory(self, conn, oid, first, last):
        raise UnexpectedPacketError

    def answerObjectHistory(self, conn, oid, history_list):
        raise UnexpectedPacketError

    def askOIDs(self, conn, first, last, partition):
        raise UnexpectedPacketError

    def answerOIDs(self, conn, oid_list):
        raise UnexpectedPacketError

    def askPartitionList(self, conn, min_offset, max_offset, uuid):
        raise UnexpectedPacketError

    def answerPartitionList(self, conn, ptid, row_list):
        raise UnexpectedPacketError

    def askNodeList(self, conn, offset_list):
        raise UnexpectedPacketError

    def answerNodeList(self, conn, node_list):
        raise UnexpectedPacketError

    def setNodeState(self, conn, uuid, state, modify_partition_table):
        raise UnexpectedPacketError

    def answerNodeState(self, conn, uuid, state):
        raise UnexpectedPacketError

    def addPendingNodes(self, conn, uuid_list):
        raise UnexpectedPacketError

    def answerNewNodes(self, conn, uuid_list):
        raise UnexpectedPacketError

    def askNodeInformation(self, conn):
        raise UnexpectedPacketError

    def answerNodeInformation(self, conn):
        raise UnexpectedPacketError

    def askClusterState(self, conn):
        raise UnexpectedPacketError

    def answerClusterState(self, conn, state):
        raise UnexpectedPacketError

    def setClusterState(self, conn, state):
        raise UnexpectedPacketError

    def notifyClusterInformation(self, conn, state):
        raise UnexpectedPacketError

    def notifyLastOID(self, conn, oid):
        raise UnexpectedPacketError

    def notifyReplicationDone(self, conn, offset):
        raise UnexpectedPacketError


    # Error packet handlers.

    def error(self, conn, code, message):
        try:
            method = self.error_dispatch_table[code]
            method(conn, message)
        except ValueError:
            raise UnexpectedPacketError(message)

    def notReady(self, conn, message):
        raise UnexpectedPacketError

    def oidNotFound(self, conn, message):
        raise UnexpectedPacketError

    def tidNotFound(self, conn, message):
        raise UnexpectedPacketError

    def protocolError(self, conn, message):
        # the connection should have been closed by the remote peer
        logging.error('protocol error: %s' % (message,))

    def timeoutError(self, conn, message):
        logging.error('timeout error: %s' % (message,))

    def brokenNodeDisallowedError(self, conn, message):
        raise RuntimeError, 'broken node disallowed error: %s' % (message,)

    def ack(self, conn, message):
        logging.debug("no error message : %s" % (message))


    # Fetch tables initialization

    def __initPacketDispatchTable(self):
        d = {}

        d[Packets.Error] = self.error
        d[Packets.RequestIdentification] = self.requestIdentification
        d[Packets.AcceptIdentification] = self.acceptIdentification
        d[Packets.AskPrimary] = self.askPrimary
        d[Packets.AnswerPrimary] = self.answerPrimary
        d[Packets.AnnouncePrimary] = self.announcePrimary
        d[Packets.ReelectPrimary] = self.reelectPrimary
        d[Packets.NotifyNodeInformation] = self.notifyNodeInformation
        d[Packets.AskLastIDs] = self.askLastIDs
        d[Packets.AnswerLastIDs] = self.answerLastIDs
        d[Packets.AskPartitionTable] = self.askPartitionTable
        d[Packets.AnswerPartitionTable] = self.answerPartitionTable
        d[Packets.SendPartitionTable] = self.sendPartitionTable
        d[Packets.NotifyPartitionChanges] = self.notifyPartitionChanges
        d[Packets.StartOperation] = self.startOperation
        d[Packets.StopOperation] = self.stopOperation
        d[Packets.AskUnfinishedTransactions] = self.askUnfinishedTransactions
        d[Packets.AnswerUnfinishedTransactions] = \
            self.answerUnfinishedTransactions
        d[Packets.AskObjectPresent] = self.askObjectPresent
        d[Packets.AnswerObjectPresent] = self.answerObjectPresent
        d[Packets.DeleteTransaction] = self.deleteTransaction
        d[Packets.CommitTransaction] = self.commitTransaction
        d[Packets.AskBeginTransaction] = self.askBeginTransaction
        d[Packets.AnswerBeginTransaction] = self.answerBeginTransaction
        d[Packets.FinishTransaction] = self.finishTransaction
        d[Packets.AnswerTransactionFinished] = self.answerTransactionFinished
        d[Packets.LockInformation] = self.lockInformation
        d[Packets.AnswerInformationLocked] = self.answerInformationLocked
        d[Packets.InvalidateObjects] = self.invalidateObjects
        d[Packets.NotifyUnlockInformation] = self.notifyUnlockInformation
        d[Packets.AskNewOIDs] = self.askNewOIDs
        d[Packets.AnswerNewOIDs] = self.answerNewOIDs
        d[Packets.AskStoreObject] = self.askStoreObject
        d[Packets.AnswerStoreObject] = self.answerStoreObject
        d[Packets.AbortTransaction] = self.abortTransaction
        d[Packets.AskStoreTransaction] = self.askStoreTransaction
        d[Packets.AnswerStoreTransaction] = self.answerStoreTransaction
        d[Packets.AskObject] = self.askObject
        d[Packets.AnswerObject] = self.answerObject
        d[Packets.AskTIDs] = self.askTIDs
        d[Packets.AnswerTIDs] = self.answerTIDs
        d[Packets.AskTransactionInformation] = self.askTransactionInformation
        d[Packets.AnswerTransactionInformation] = \
            self.answerTransactionInformation
        d[Packets.AskObjectHistory] = self.askObjectHistory
        d[Packets.AnswerObjectHistory] = self.answerObjectHistory
        d[Packets.AskOIDs] = self.askOIDs
        d[Packets.AnswerOIDs] = self.answerOIDs
        d[Packets.AskPartitionList] = self.askPartitionList
        d[Packets.AnswerPartitionList] = self.answerPartitionList
        d[Packets.AskNodeList] = self.askNodeList
        d[Packets.AnswerNodeList] = self.answerNodeList
        d[Packets.SetNodeState] = self.setNodeState
        d[Packets.AnswerNodeState] = self.answerNodeState
        d[Packets.SetClusterState] = self.setClusterState
        d[Packets.AddPendingNodes] = self.addPendingNodes
        d[Packets.AnswerNewNodes] = self.answerNewNodes
        d[Packets.AskNodeInformation] = self.askNodeInformation
        d[Packets.AnswerNodeInformation] = self.answerNodeInformation
        d[Packets.AskClusterState] = self.askClusterState
        d[Packets.AnswerClusterState] = self.answerClusterState
        d[Packets.NotifyClusterInformation] = self.notifyClusterInformation
        d[Packets.NotifyLastOID] = self.notifyLastOID
        d[Packets.NotifyReplicationDone] = self.notifyReplicationDone

        return d

    def __initErrorDispatchTable(self):
        d = {}

        d[ErrorCodes.ACK] = self.ack
        d[ErrorCodes.NOT_READY] = self.notReady
        d[ErrorCodes.OID_NOT_FOUND] = self.oidNotFound
        d[ErrorCodes.TID_NOT_FOUND] = self.tidNotFound
        d[ErrorCodes.PROTOCOL_ERROR] = self.protocolError
        d[ErrorCodes.BROKEN_NODE] = self.brokenNodeDisallowedError

        return d

