"""
Object data store.

This service acts as a cache / access point for a backend object store;
currently Amazon S3 is used as the backend store, but the architecture should
be flexible enough to allow other possibilities. The system is designed to
handle objects in an immutable fashion; once an object is created, it exists in
perpetuity, and the contents will never change.

The service's functionality is two-fold; firstly, it handles requests for
retrieval of objects, servicing them from the local cache, fetching them from a
neighbour cache, or retrieving them from the backend store. Secondly, it
handles requests for storage of a new object; the object is first cached
locally to ensure local view consistency, and then queued for backend storage
in a reliable fashion.
"""
import hashlib
from datetime import timedelta

from zope.interface import implements

from epsilon.extime import Time

from axiom.iaxiom import IScheduler
from axiom.item import Item, transacted
from axiom.attributes import text, path, timestamp, AND, inmemory, reference
from axiom.dependency import dependsOn

from twisted.web import http, error as eweb
from twisted.web.client import getPage
from twisted.python import log
from twisted.python.components import registerAdapter
from twisted.internet.defer import succeed

from nevow.inevow import IResource, IRequest
from nevow.static import File
from nevow.rend import NotFound

from entropy.ientropy import (IContentStore, IContentObject, ISiblingStore,
    IBackendStore, IUploadScheduler)
from entropy.errors import CorruptObject, NonexistentObject, DigestMismatch
from entropy.hash import getHash
from entropy.util import deferred, getPageWithHeaders, MemoryObject



class ImmutableObject(Item):
    """
    An immutable object.

    Immutable objects are addressed by content hash, and consist of the object
    data as a binary blob, and object key/value metadata pairs.
    """
    implements(IContentObject)

    hash = text(allowNone=False)
    contentDigest = text(allowNone=False)
    content = path(allowNone=False)
    contentType = text(allowNone=False)
    created = timestamp(allowNone=False, defaultFactory=lambda: Time())

    @property
    def metadata(self):
        return {}


    @property
    def objectId(self):
        return u'%s:%s' % (self.hash, self.contentDigest)


    def _getDigest(self):
        fp = self.content.open()
        try:
            h = getHash(self.hash)(fp.read())
            return unicode(h.hexdigest(), 'ascii')
        finally:
            fp.close()


    def verify(self):
        digest = self._getDigest()
        if self.contentDigest != digest:
            raise CorruptObject(
                'expected: %r actual: %r' % (self.contentDigest, digest))


    def getContent(self):
        return self.content.getContent()

def objectResource(obj):
    """
    Adapt L{ImmutableObject) to L{IResource}.
    """
    obj.verify()
    res = File(obj.content.path)
    res.type = obj.contentType.encode('ascii')
    res.encoding = None
    return res

registerAdapter(objectResource, ImmutableObject, IResource)



class ContentStore(Item):
    """
    Manager for stored objects.
    """
    implements(IContentStore)
    powerupInterfaces = [IContentStore]

    hash = text(allowNone=False, default=u'sha256')

    @transacted
    def _storeObject(self, content, contentType, metadata={}, created=None):
        """
        Do the actual work of synchronously storing the object.
        """
        if metadata != {}:
            raise NotImplementedError('metadata not yet supported')

        contentDigest = getHash(self.hash)(content).hexdigest()
        contentDigest = unicode(contentDigest, 'ascii')

        if created is None:
            created = Time()

        obj = self.store.findUnique(
            ImmutableObject,
            AND(ImmutableObject.hash == self.hash,
                ImmutableObject.contentDigest == contentDigest),
            default=None)
        if obj is None:
            contentFile = self.store.newFile(
                'objects', 'immutable', '%s:%s' % (self.hash, contentDigest))
            contentFile.write(content)
            contentFile.close()

            obj = ImmutableObject(store=self.store,
                                  contentDigest=contentDigest,
                                  hash=self.hash,
                                  content=contentFile.finalpath,
                                  contentType=contentType,
                                  created=created)
        else:
            obj.contentType = contentType
            obj.created = created

        scheduler = IUploadScheduler(self.store, None)
        for backend in self.store.powerupsFor(IBackendStore):
            scheduler.scheduleUpload(obj.objectId, backend)

        return obj


    def importObject(self, obj):
        """
        Import an object from elsewhere.

        @param obj: the object to import.
        @type obj: ImmutableObject
        """
        return self._storeObject(obj.getContent(),
                                 obj.contentType,
                                 obj.metadata,
                                 obj.created)


    @transacted
    def getSiblingObject(self, objectId):
        """
        Import an object from a sibling store.

        @returns: the local imported object.
        @type obj: ImmutableObject
        """
        siblings = list(self.store.powerupsFor(ISiblingStore))
        siblings.extend(self.store.powerupsFor(IBackendStore))
        siblings = iter(siblings)

        def _eb(f):
            f.trap(NonexistentObject)
            try:
                remoteStore = siblings.next()
            except StopIteration:
                raise NonexistentObject(objectId)

            d = remoteStore.getObject(objectId)
            d.addCallbacks(self.importObject, _eb)
            return d

        return self.getObject(objectId).addErrback(_eb)


    # IContentStore

    @deferred
    def storeObject(self, content, contentType, metadata={}, created=None):
        obj = self._storeObject(content, contentType, metadata, created)
        return obj.objectId


    @deferred
    @transacted
    def getObject(self, objectId):
        hash, contentDigest = objectId.split(u':', 1)
        obj = self.store.findUnique(
            ImmutableObject,
            AND(ImmutableObject.hash == hash,
                ImmutableObject.contentDigest == contentDigest),
            default=None)
        if obj is None:
            raise NonexistentObject(objectId)
        return obj



class ObjectCreator(object):
    """
    Resource for storing new objects.

    @ivar contentStore: The {IContentStore} provider to create objects in.
    """
    implements(IResource)

    def __init__(self, contentStore):
        self.contentStore = contentStore


    # IResource
    def renderHTTP(self, ctx):
        req = IRequest(ctx)
        if req.method == 'GET':
            req.setHeader('Content-Type', 'text/plain')
            return 'PUT data here to create an object.'
        elif req.method == 'PUT':
            return self.handlePUT(req)
        else:
            req.setResponseCode(http.NOT_ALLOWED)
            req.setHeader('Content-Type', 'text/plain')
            return 'Method not allowed'


    def handlePUT(self, req):
        data = req.content.read()
        contentType = unicode(
            req.getHeader('Content-Type') or 'application/octet-stream',
            'ascii')

        contentMD5 = req.getHeader('Content-MD5')
        if contentMD5 is not None:
            expectedHash = contentMD5.decode('base64')
            actualHash = hashlib.md5(data).digest()
            if expectedHash != actualHash:
                raise DigestMismatch(expectedHash, actualHash)

        def _cb(objectId):
            req.setHeader('Content-Type', 'text/plain')
            objectId = objectId.encode('ascii')
            return objectId

        d = self.contentStore.storeObject(data, contentType)
        return d.addCallback(_cb)



class ContentResource(Item):
    """
    Resource for accessing the content store.
    """
    implements(IResource)
    powerupInterfaces = [IResource]

    addSlash = inmemory()

    contentStore = dependsOn(ContentStore)

    def getObject(self, name):
        def _notFound(f):
            f.trap(NonexistentObject)
            return None
        return self.contentStore.getSiblingObject(name).addErrback(_notFound)


    def childFactory(self, name):
        """
        Hook up children.

        / is the root, nothing to see her.

        /new is how new objects are stored.

        /<objectId> is where existing objects are retrieved.
        """
        if name == '':
            return self
        elif name == 'new':
            return ObjectCreator(self.contentStore)
        else:
            return self.getObject(name)
        return None


    # IResource
    def renderHTTP(self, ctx):
        """
        Nothing to see here.
        """
        return 'Entropy'


    def locateChild(self, ctx, segments):
        """
        Dispatch to L{childFactory}.
        """
        if len(segments) >= 1:
            res = self.childFactory(segments[0])
            if res is not None:
                return res, segments[1:]
        return NotFound



class RemoteEntropyStore(Item):
    """
    IContentStore implementation for remote Entropy services.
    """
    implements(IContentStore)

    entropyURI = text(allowNone=False,
                      doc="""The URI of the Entropy service in use.""")

    def getURI(self, documentId):
        """
        Construct an Entropy URI for the specified document.
        """
        return self.entropyURI + documentId


    # IContentStore

    def storeObject(self, content, contentType, metadata={}, created=None):
        digest = hashlib.md5(data).digest()
        return getPage((self.entropyURI + 'new').encode('ascii'),
                       method='PUT',
                       postdata=data,
                       headers={'Content-Length': len(data),
                                'Content-Type': contentType,
                                'Content-MD5': b64encode(digest)}
                    ).addCallback(lambda url: unicode(url, 'ascii'))


    def getObject(self, objectId):
        hash, contentDigest = objectId.split(':', 1)

        def _makeContentObject((data, headers)):
            # XXX: Actually get the real creation time
            return MemoryObject(
                content=data,
                hash=hash,
                contentDigest=contentDigest,
                contentType=unicode(headers['content-type'][0], 'ascii'),
                metadata={},
                created=Time())

        def _eb(f):
            f.trap(eweb.Error)
            if f.value.status == '404':
                raise NonexistentObject(objectId)
            return f

        return getPageWithHeaders(self.getURI(objectId)
                    ).addCallbacks(_makeContentObject, _eb)



class _PendingUpload(Item):
    """
    Marker for a pending upload to a backend store.
    """
    objectId = text(allowNone=False)
    backend = reference(allowNone=False) # reftype=IBackendStore
    scheduled = timestamp(
        indexed=True, allowNone=False, defaultFactory=lambda: Time())

    def run(self):
        self.attemptUpload()


    def attemptUpload(self):
        """
        Attempt an upload of an object to a backend store.

        If the upload fails, it will be rescheduled; if it succeeds, this item
        will be deleted.
        """
        def _uploadObject(obj):
            return self.backend.storeObject(
                obj.getContent(),
                obj.contentType,
                obj.metadata,
                obj.created,
                objectId=self.objectId)

        def _reschedule(f):
            log.err(f, 'Error uploading object %r to backend store %r' % (
                self.objectId, self.backend))
            self.scheduled += timedelta(minutes=2)
            self.schedule()
            return f

        d = succeed(None)
        d.addCallback(
            lambda ign: IContentStore(self.store).getObject(self.objectId))
        d.addCallback(_uploadObject)
        d.addCallback(lambda ign: self.deleteFromStore())
        d.addErrback(_reschedule)
        return d


    def schedule(self):
        IScheduler(self.store).schedule(self, self.scheduled)



class UploadScheduler(Item):
    """
    Schedule upload attempts for pending uploads.
    """
    implements(IUploadScheduler)
    powerupInterfaces = [IUploadScheduler]

    dummy = text()

    # IUploadScheduler

    def scheduleUpload(self, objectId, backend):
        upload = _PendingUpload(
            store=self.store,
            objectId=objectId,
            backend=backend)
        upload.schedule()
