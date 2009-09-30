#
# Copyright (C) 2009  Nexedi SA
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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

import os
import unittest
from neo import logging
from struct import pack, unpack
from mock import Mock
from collections import deque
from neo.tests import NeoTestBase
from neo.storage.app import Application
from neo.storage.handlers.master import MasterOperationHandler
from neo.exception import PrimaryFailure, OperationFailure
from neo.pt import PartitionTable
from neo import protocol
from neo.protocol import *

class StorageMasterHandlerTests(NeoTestBase):

    def checkHandleUnexpectedPacket(self, _call, _msg_type, _listening=True, **kwargs):
        conn = Mock({ 
            "getAddress" : ("127.0.0.1", self.master_port), 
            "isServer": _listening,    
        })
        packet = Packet(msg_type=_msg_type)
        # hook
        self.operation.peerBroken = lambda c: c.peerBrokendCalled()
        self.checkUnexpectedPacketRaised(_call, conn=conn, packet=packet, **kwargs)

    def setUp(self):
        self.prepareDatabase(number=1)
        # create an application object
        config = self.getStorageConfiguration(master_number=1)
        self.app = Application(**config)
        self.app.transaction_dict = {}
        self.app.store_lock_dict = {}
        self.app.load_lock_dict = {}
        self.app.event_queue = deque()
        for address in self.app.master_node_list:
            self.app.nm.createMaster(address=address)
        # handler
        self.operation = MasterOperationHandler(self.app)
        # set pmn
        self.master_uuid = self.getNewUUID()
        pmn = self.app.nm.getMasterList()[0]
        pmn.setUUID(self.master_uuid)
        self.app.primary_master_node = pmn
        self.master_port = 10010

    def tearDown(self):
        NeoTestBase.tearDown(self)


    def test_06_timeoutExpired(self):
        # client connection
        conn = Mock({
            "getUUID": self.master_uuid,
            "getAddress" : ("127.0.0.1", self.master_port),
        })
        self.assertRaises(PrimaryFailure, self.operation.timeoutExpired, conn)
        self.checkNoPacketSent(conn)

    def test_07_connectionClosed2(self):
        # primary has closed the connection
        conn = Mock({
            "getUUID": self.master_uuid,
            "getAddress" : ("127.0.0.1", self.master_port),
        })
        self.assertRaises(PrimaryFailure, self.operation.connectionClosed, conn)
        self.checkNoPacketSent(conn)

    def test_08_peerBroken(self):
        # client connection
        conn = Mock({
            "getUUID": self.master_uuid,
            "getAddress" : ("127.0.0.1", self.master_port),
        })
        self.assertRaises(PrimaryFailure, self.operation.peerBroken, conn)
        self.checkNoPacketSent(conn)

    def test_14_handleNotifyPartitionChanges1(self):
        # old partition change -> do nothing
        app = self.app
        conn = Mock({
            "isServer": False,
            "getAddress" : ("127.0.0.1", self.master_port), 
        })
        app.replicator = Mock({})
        packet = Packet(msg_type=NOTIFY_PARTITION_CHANGES)
        self.app.pt = Mock({'getID': 1})
        count = len(self.app.nm.getList())
        self.operation.handleNotifyPartitionChanges(conn, packet, 0, ())
        self.assertEquals(self.app.pt.getID(), 1)
        self.assertEquals(len(self.app.nm.getList()), count)
        calls = self.app.replicator.mockGetNamedCalls('removePartition')
        self.assertEquals(len(calls), 0)
        calls = self.app.replicator.mockGetNamedCalls('addPartition')
        self.assertEquals(len(calls), 0)

    def test_14_handleNotifyPartitionChanges2(self):
        # cases :
        uuid1, uuid2, uuid3 = [self.getNewUUID() for i in range(3)]
        cells = (
            (0, uuid1, UP_TO_DATE_STATE),
            (1, uuid2, DISCARDED_STATE),
            (2, uuid3, OUT_OF_DATE_STATE),
        )
        # context
        conn = Mock({
            "isServer": False,
            "getAddress" : ("127.0.0.1", self.master_port), 
        })
        packet = Packet(msg_type=NOTIFY_PARTITION_CHANGES)
        app = self.app
        # register nodes
        app.nm.createStorage(uuid=uuid1)
        app.nm.createStorage(uuid=uuid2)
        app.nm.createStorage(uuid=uuid3)
        ptid1, ptid2 = (1, 2)
        self.assertNotEquals(ptid1, ptid2)
        app.pt = PartitionTable(3, 1)
        app.dm = Mock({ })
        app.replicator = Mock({})
        count = len(app.nm.getList())
        self.operation.handleNotifyPartitionChanges(conn, packet, ptid2, cells)
        # ptid set
        self.assertEquals(app.pt.getID(), ptid2)
        # dm call
        calls = self.app.dm.mockGetNamedCalls('changePartitionTable')
        self.assertEquals(len(calls), 1)
        calls[0].checkArgs(ptid2, cells)

    def test_16_handleStopOperation1(self):
        # OperationFailure
        conn = Mock({ 'isServer': False })
        packet = Packet(msg_type=STOP_OPERATION)
        self.assertRaises(OperationFailure, self.operation.handleStopOperation, conn, packet)

    def test_22_handleLockInformation2(self):
        # load transaction informations
        conn = Mock({ 'isServer': False, })
        self.app.dm = Mock({ })
        packet = Packet(msg_type=LOCK_INFORMATION)
        packet.setId(1)
        transaction = Mock({ 'getObjectList': ((0, ), ), })
        self.app.transaction_dict[INVALID_TID] = transaction
        self.operation.handleLockInformation(conn, packet, INVALID_TID)
        self.assertEquals(self.app.load_lock_dict[0], INVALID_TID)
        calls = self.app.dm.mockGetNamedCalls('storeTransaction')
        self.assertEquals(len(calls), 1)
        self.checkNotifyInformationLocked(conn, answered_packet=packet)
        # transaction not in transaction_dict -> KeyError
        transaction = Mock({ 'getObjectList': ((0, ), ), })
        conn = Mock({ 'isServer': False, })
        self.operation.handleLockInformation(conn, packet, '\x01' * 8)
        self.checkNotifyInformationLocked(conn, answered_packet=packet)

    def test_23_handleUnlockInformation2(self):
        # delete transaction informations
        conn = Mock({ 'isServer': False, })
        self.app.dm = Mock({ })
        packet = Packet(msg_type=LOCK_INFORMATION)
        packet.setId(1)
        transaction = Mock({ 'getObjectList': ((0, ), ), })
        self.app.transaction_dict[INVALID_TID] = transaction
        self.app.load_lock_dict[0] = transaction
        self.app.store_lock_dict[0] = transaction
        self.operation.handleUnlockInformation(conn, packet, INVALID_TID)
        self.assertEquals(len(self.app.load_lock_dict), 0)
        self.assertEquals(len(self.app.store_lock_dict), 0)
        self.assertEquals(len(self.app.store_lock_dict), 0)
        calls = self.app.dm.mockGetNamedCalls('finishTransaction')
        self.assertEquals(len(calls), 1)
        calls[0].checkArgs(INVALID_TID)
        # transaction not in transaction_dict -> KeyError
        transaction = Mock({ 'getObjectList': ((0, ), ), })
        conn = Mock({ 'isServer': False, })
        self.operation.handleLockInformation(conn, packet, '\x01' * 8)
        self.checkNotifyInformationLocked(conn, answered_packet=packet)

    def test_30_handleAnswerLastIDs(self):
        # set critical TID on replicator
        conn = Mock()
        packet = Packet(msg_type=ANSWER_LAST_IDS)
        self.app.replicator = Mock()
        self.operation.handleAnswerLastIDs(
            conn=conn,
            packet=packet,
            loid=INVALID_OID,
            ltid=INVALID_TID,
            lptid=INVALID_TID,
        )
        calls = self.app.replicator.mockGetNamedCalls('setCriticalTID')
        self.assertEquals(len(calls), 1)
        calls[0].checkArgs(packet, INVALID_TID)

    def test_31_handleAnswerUnfinishedTransactions(self):
        # set unfinished TID on replicator
        conn = Mock()
        packet = Packet(msg_type=ANSWER_UNFINISHED_TRANSACTIONS)
        self.app.replicator = Mock()
        self.operation.handleAnswerUnfinishedTransactions(
            conn=conn,
            packet=packet,
            tid_list=(INVALID_TID, ),
        )
        calls = self.app.replicator.mockGetNamedCalls('setUnfinishedTIDList')
        self.assertEquals(len(calls), 1)
        calls[0].checkArgs((INVALID_TID, ))

if __name__ == "__main__":
    unittest.main()
