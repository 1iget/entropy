from twisted.trial.unittest import TestCase

from entropy.hash import getHash
from entropy.errors import UnknownHashAlgorithm


class HashingTests(TestCase):
    """
    Tests for L{entropy.hash}.
    """
    def test_sha256(self):
        """
        Retrieving the sha256 hash function succeeds.
        """
        hash = getHash('sha256')

    def test_invalidHash(self):
        """
        Trying to retrieve an unknown hash function results in an
        L{UnknownHashAlgorithm} exception.
        """
        self.assertRaises(UnknownHashAlgorithm, getHash, '***DOESNOTEXIST***')