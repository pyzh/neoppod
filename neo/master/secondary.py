import logging

from neo.protocol import MASTER_NODE_TYPE, \
        RUNNING_STATE, BROKEN_STATE, TEMPORARILY_DOWN_STATE, DOWN_STATE
from neo.master.handler import MasterEventHandler
from neo.connection import ClientConnection
from neo.exception import ElectionFailure, PrimaryFailure
from neo.protocol import Packet, INVALID_UUID
from neo.node import MasterNode

class SecondaryEventHandler(MasterEventHandler):
    """This class deals with events for a secondary master."""

    def connectionClosed(self, conn):
        if isinstance(conn, ClientConnection):
            self.app.primary_master_node.setState(DOWN_STATE)
            raise PrimaryFailure, 'primary master is dead'
        MasterEventHandler.connectionClosed(self, conn)

    def timeoutExpired(self, conn):
        if isinstance(conn, ClientConnection):
            self.app.primary_master_node.setState(DOWN_STATE)
            raise PrimaryFailure, 'primary master is down'
        MasterEventHandler.timeoutExpired(self, conn)

    def peerBroken(self, conn):
        if isinstance(conn, ClientConnection):
            self.app.primary_master_node.setState(DOWN_STATE)
            raise PrimaryFailure, 'primary master is crazy'
        MasterEventHandler.peerBroken(self, conn)

    def packetReceived(self, conn, packet):
        if isinstance(conn, ClientConnection):
            node = self.app.nm.getNodeByServer(conn.getAddress())
            if node.getState() != BROKEN_STATE:
                node.setState(RUNNING_STATE)
        MasterEventHandler.packetReceived(self, conn, packet)

    def handleRequestNodeIdentification(self, conn, packet, node_type,
                                        uuid, ip_address, port, name):
        if isinstance(conn, ClientConnection):
            self.handleUnexpectedPacket(conn, packet)
        else:
            app = self.app
            if name != app.name:
                logging.error('reject an alien cluster')
                conn.addPacket(Packet().protocolError(packet.getId(),
                                                      'invalid cluster name'))
                conn.abort()
                return

            # Add a node only if it is a master node and I do not know it yet.
            if node_type == MASTER_NODE_TYPE:
                addr = (ip_address, port)
                node = app.nm.getNodeByServer(addr)
                if node is None:
                    node = MasterNode(server = addr, uuid = uuid)
                    app.nm.add(node)

                # Trust the UUID sent by the peer.
                node.setUUID(uuid)

            conn.setUUID(uuid)

            p = Packet()
            p.acceptNodeIdentification(packet.getId(), MASTER_NODE_TYPE,
                                       app.uuid, app.server[0], app.server[1],
                                       app.num_partitions, app.num_replicas)
            conn.addPacket(p)
            # Next, the peer should ask a primary master node.
            conn.expectMessage()

    def handleAskPrimaryMaster(self, conn, packet):
        if isinstance(conn, ClientConnection):
            self.handleUnexpectedPacket(conn, packet)
        else:
            uuid = conn.getUUID()
            if uuid is None:
                self.handleUnexpectedPacket(conn, packet)
                return

            app = self.app
            primary_uuid = app.primary_master_node.getUUID()

            known_master_list = []
            for n in app.nm.getMasterNodeList():
                if n.getState() == BROKEN_STATE:
                    continue
                info = n.getServer() + (n.getUUID() or INVALID_UUID,)
                known_master_list.append(info)

            p = Packet()
            p.answerPrimaryMaster(packet.getId(), primary_uuid, known_master_list)
            conn.addPacket(p)

    def handleAnnouncePrimaryMaster(self, conn, packet):
        self.handleUnexpectedPacket(conn, packet)

    def handleReelectPrimaryMaster(self, conn, packet):
        raise ElectionFailure, 'reelection requested'

    def handleNotifyNodeInformation(self, conn, packet, node_list):
        app = self.app
        for node_type, ip_address, port, uuid, state in node_list:
            if node_type != MASTER_NODE_TYPE:
                # No interest.
                continue

            # Register new master nodes.
            addr = (ip_address, port)
            if app.server == addr:
                # This is self.
                continue
            else:
                n = app.nm.getNodeByServer(addr)
                if n is None:
                    n = MasterNode(server = addr)
                    app.nm.add(n)

                if uuid != INVALID_UUID:
                    # If I don't know the UUID yet, believe what the peer
                    # told me at the moment.
                    if n.getUUID() is None:
                        n.setUUID(uuid)
