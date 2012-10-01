import json
from uuid import uuid1

from twisted.trial.unittest import TestCase

from shannon.util import (tagsToStr, tagsToDict, ShannonEncoder)



class UtilTests(TestCase):
    """
    Tests for L{shannon.util}.
    """
    def test_tagsToStr(self):
        """
        C{tagsToStr} takes a C{dict} and returns a C{str}.
        """
        tags = dict(test1='test', test2='more test')
        self.assertEqual('test1=test, test2=more test', tagsToStr(tags))

        tag = dict(test='test')
        self.assertEqual('test=test', tagsToStr(tag))


    def test_tagsToDict(self):
        """
        C{tagsToDict} takes a C{str} and returns a C{dict}.
        """
        tags = 'test1=value1, test2=value2'
        tagsDict = dict(test1='value1', test2='value2')
        self.assertEqual(tagsDict, tagsToDict(tags))

        tag = 'test1=value1'
        tagDict = dict(test1='value1')
        self.assertEqual(tagDict, tagsToDict(tag))


    def test_ShannonEncoderUUID(self):
        """
        C{ShannonEncoder} handles json.UUID objects.
        """
        id = uuid1()
        result = json.dumps(id, cls=ShannonEncoder)
        self.assertEqual('"%s"' % str(id), result)
    
    def test_ShannonEncoderFailure(self):
        """
        C{ShannonEncoder} handles L{twisted.python.failure.Failure} objects.
        """
        from twisted.python.failure import Failure
        result = json.dumps(Failure(ValueError('testing')), cls=ShannonEncoder)
        self.assertEqual('{"ValueError": "testing"}', result)

