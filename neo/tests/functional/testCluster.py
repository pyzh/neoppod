#
# Copyright (C) 2009-2010  Nexedi SA
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

import unittest
import transaction
from persistent import Persistent

from neo.tests.functional import NEOCluster, NEOFunctionalTest

class ClusterTests(NEOFunctionalTest):

    def setUp(self):
        NEOFunctionalTest.setUp(self)
        self.neo = None

    def tearDown(self):
        if self.neo is not None:
            self.neo.stop()

    def testClusterBreaks(self):
        self.neo = NEOCluster(['test_neo1'],
                master_node_count=1, temp_dir=self.getTempDirectory())
        neoctl = self.neo.getNEOCTL()
        self.neo.setupDB()
        self.neo.start()
        self.neo.expectClusterRunning()
        self.neo.expectOudatedCells(number=0)
        self.neo.killStorage()
        self.neo.expectClusterVerifying()

    def testClusterBreaksWithTwoNodes(self):
        self.neo = NEOCluster(['test_neo1', 'test_neo2'],
                 partitions=2, master_node_count=1, replicas=0,
                 temp_dir=self.getTempDirectory())
        neoctl = self.neo.getNEOCTL()
        self.neo.setupDB()
        self.neo.start()
        self.neo.expectClusterRunning()
        self.neo.expectOudatedCells(number=0)
        self.neo.killStorage()
        self.neo.expectClusterVerifying()

    def testClusterDoesntBreakWithTwoNodesOneReplica(self):
        self.neo = NEOCluster(['test_neo1', 'test_neo2'],
                         partitions=2, replicas=1, master_node_count=1,
                         temp_dir=self.getTempDirectory())
        neoctl = self.neo.getNEOCTL()
        self.neo.setupDB()
        self.neo.start()
        self.neo.expectClusterRunning()
        self.neo.expectOudatedCells(number=0)
        self.neo.killStorage()
        self.neo.expectClusterRunning()

    def testElectionWithManyMasters(self):
        MASTER_COUNT = 20
        self.neo = NEOCluster(['test_neo1', 'test_neo2'],
            partitions=10, replicas=0, master_node_count=MASTER_COUNT,
            temp_dir=self.getTempDirectory())
        neoctl = self.neo.getNEOCTL()
        self.neo.start()
        self.neo.expectClusterRunning()
        self.neo.expectAllMasters(MASTER_COUNT)
        self.neo.expectOudatedCells(0)

    def testVerificationCommitUnfinishedTransactions(self):
        """ Verification step should commit unfinished transactions """
        # XXX: this kind of definition should be defined in base test class
        class PObject(Persistent):
            pass
        self.neo = NEOCluster(['test_neo1'], replicas=0,
            temp_dir=self.getTempDirectory())
        neoctl = self.neo.getNEOCTL()
        self.neo.start()
        db, conn = self.neo.getZODBConnection()
        conn.root()[0] = 'ok'
        transaction.commit()
        self.neo.stop()
        # XXX: (obj|trans) become t(obj|trans)
        self.neo.switchTables('test_neo1')
        self.neo.start()
        db, conn = self.neo.getZODBConnection()
        # transaction should be verified and commited
        self.assertEqual(conn.root()[0], 'ok')

    def testLeavingOperationalStateDropClientNodes(self):
        """
            Check that client nodes are dropped where the cluster leaves the
            operational state.
        """
        # start a cluster
        self.neo = NEOCluster(['test_neo1'], replicas=0,
            temp_dir=self.getTempDirectory())
        neoctl = self.neo.getNEOCTL()
        self.neo.start()
        self.neo.expectClusterRunning()
        self.neo.expectOudatedCells(0)
        # connect a client a check it's known
        db, conn = self.neo.getZODBConnection()
        self.assertEqual(len(self.neo.getClientlist()), 1)
        # drop the storage, the cluster is no more operational...
        self.neo.getStorageProcessList()[0].stop()
        self.neo.expectClusterVerifying()
        # ...and the client gets disconnected
        self.assertEqual(len(self.neo.getClientlist()), 0)
        # restart storage so that the cluster is operational again
        self.neo.getStorageProcessList()[0].start()
        self.neo.expectClusterRunning()
        self.neo.expectOudatedCells(0)
        # and reconnect the client, there must be only one known by the admin
        conn.root()['plop'] = 1
        transaction.commit()
        self.assertEqual(len(self.neo.getClientlist()), 1)

def test_suite():
    return unittest.makeSuite(ClusterTests)

if __name__ == "__main__":
    unittest.main(defaultTest="test_suite")

