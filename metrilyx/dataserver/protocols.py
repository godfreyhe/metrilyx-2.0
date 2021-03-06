
import logging
import ujson as json
import time
from datetime import datetime

from twisted.internet import reactor

from autobahn.twisted.websocket import WebSocketServerProtocol
from autobahn.websocket.compress import PerMessageDeflateOffer, PerMessageDeflateOfferAccept

from ..httpclients import AsyncHttpJsonClient, MetrilyxGraphFetcher, checkHttpResponse
from transforms import MetrilyxSerie, MetrilyxAnalyticsSerie

from dataproviders.opentsdb import getPerfDataProvider

from datarequest import GraphRequest

from pprint import pprint

logger = logging.getLogger(__name__)


def writeRequestLogLine(request_obj):
    logger.info("Request type=%s, name='%s', sub-queries=%d, id=%s, start='%s'" %(
        request_obj['graphType'], request_obj['name'], len(request_obj['series']),
        request_obj['_id'], datetime.fromtimestamp(float(request_obj['start']))))


def writeResponseLogLine(graph):
    logger.info("Response type=%s, id=%s, name='%s', start='%s'" %(
                        graph['graphType'], graph['_id'],  graph['name'],
                        datetime.fromtimestamp(float(graph['start']))))


## Enable WebSocket extension "permessage-deflate".
## Function to accept offers from the client ..
def acceptedCompression(offers):
    for offer in offers:
        if isinstance(offer, PerMessageDeflateOffer):
            return PerMessageDeflateOfferAccept(offer)


class BaseGraphServerProtocol(WebSocketServerProtocol):
    '''
        Basic protocol that handles incoming requests.
        This does nothing more than check the request and submit for processing.
        If needed, 'GraphServerProtocol' should be subclassed instead.
    '''
    def onConnect(self, request):
        logger.info("Connection request by %s" %(str(request.peer)))

    def onOpen(self):
        logger.info("Connection opened. extensions: %s" %(
                                        self.websocket_extensions_in_use))
        self.factory.addClient(self)

    def onClose(self, wasClean, code, reason):
        logger.info("Connection closed: wasClean=%s code=%s reason=%s" %(
                                str(wasClean), str(code), str(reason)))
        self.factory.removeClient(self)


    def checkMessage(self, payload, isBinary):
        if not isBinary:
            try:
                return json.loads(payload)
            except Exception, e:
                self.sendMessage(json.dumps({'error': str(e)}))
                logger.error(str(e))
                return {'error': str(e)}
        else:
            self.sendMessage(json.dumps({'error': 'Binary data not support!'}))
            logger.warning("Binary data not supported!")
            return {'error': 'Binary data not support!'}

    def onMessage(self, payload, isBinary):

        request_obj = self.checkMessage(payload, isBinary)
        if not request_obj.get("error"):
            try:
                if request_obj['_id'] == 'annotations':
                    logger.info("Annotation Request: %s" %(str(request_obj)))
                    self.processRequest(request_obj)
                else:
                    writeRequestLogLine(request_obj)

                    graphReq = GraphRequest(request_obj)
                    self.processRequest(graphReq)

            except Exception,e:
                logger.error(str(e) + " " + str(request_obj))
        else:
            logger.error("Invalid request object: %s" %(str(request_obj)))

    def processRequest(self, graphOrAnnoRequest):
        '''
            Implemented by subclasser
        '''
        pass


class GraphServerProtocol(BaseGraphServerProtocol):

    __activeFetchers = {}
    __activeFetchersTimeout = 900
    __expirerDeferred = None

    def __expireActiveFetchers(self):
        logger.info("Starting fetcher expiration...")

        expireTime = time.time() - self.__activeFetchersTimeout
        expired = 0
        for k,v in self.__activeFetchers.items():
            if float(k.split("-")[-1]) <= expireTime:

                v.cancelRequests()
                self.__removeFetcher(k)
                logger.info("Expired fetcher: %s" %(k))
                expired += 1

        logger.info("Expired %d fetchers" %(expired))

        self.__expirerDeferred = reactor.callLater(self.__activeFetchersTimeout, self.__expireActiveFetchers)

    def __removeFetcher(self, key):
        if self.__activeFetchers.has_key(key):
            del self.__activeFetchers[key]
        logger.info("Active fetchers: %d" %(len(self.__activeFetchers.keys())))

    def __addFetcher(self, key, fetcher):
        if self.__activeFetchers.has_key(key):
            logger.warning("Fetcher inprogress: %s" %(key))
        self.__activeFetchers[key] = fetcher

    def processRequest(self, graphRequest):
        self.submitPerfQueries(graphRequest)

    def submitPerfQueries(self, graphRequest):
        mgf = MetrilyxGraphFetcher(self.dataprovider, graphRequest)

        stamp = "%s-%f" %(graphRequest.request['_id'], time.time())

        mgf.addCompleteCallback(self.completeCallback, stamp)
        mgf.addCompleteErrback(self.completeErrback, graphRequest.request, stamp)
        mgf.addPartialResponseCallback(self.partialResponseCallback)
        mgf.addPartialResponseErrback(self.partialResponseErrback, graphRequest.request)

        self.__addFetcher(stamp, mgf)

    def completeErrback(self, error, *cbargs):
        (request, key) = cbargs
        self.__removeFetcher(key)

        if "CancelledError" not in str(error):
            logger.error("%s" %(str(error)))

    def completeCallback(self, *cbargs):
        (graph, key) = cbargs
        self.__removeFetcher(key)

        if graph != None:
            self.sendMessage(json.dumps(graph))
            logger.info("Reponse (secondaries graph) %s '%s' start: %s" %(graph['_id'],
                    graph['name'], datetime.fromtimestamp(float(graph['start']))))


    def partialResponseCallback(self, graph):
        self.sendMessage(json.dumps(graph))
        writeResponseLogLine(graph)

    def partialResponseErrback(self, error, *cbargs):
        (graphMeta,) = cbargs
        if "CancelledError" not in str(error):
            logger.error("%s" %(str(error)))
            errResponse = self.dataprovider.responseErrback(error, graphMeta)
            self.sendMessage(json.dumps(errResponse))

    def onOpen(self):
        logger.info("Connection opened. extensions: %s" %(
                                        self.websocket_extensions_in_use))
        self.factory.addClient(self)
        ## Expire fetchers
        logger.info("Scheduling fetcher expiration...")
        self.__expirerDeferred = reactor.callLater(self.__activeFetchersTimeout, self.__expireActiveFetchers)

    def onClose(self, wasClean, code, reason):
        logger.info("Connection closed: wasClean=%s code=%s reason=%s" % 
                                (str(wasClean), str(code), str(reason)))

        for k,d in self.__activeFetchers.items():
            d.cancelRequests()
            self.__removeFetcher(k)

        self.factory.removeClient(self)

        try:
            self.__expirerDeferred.cancel()
        except Exception:
            pass


def getConfiguredProtocol():
    try:
        class GraphProtocol(GraphServerProtocol):
            dataprovider = getPerfDataProvider()

        return GraphProtocol
        #logger.warning("Protocol: %s" %(str(proto)))
    except Exception,e:
        logger.error("Could not set dataprovider and/or protocol: %s" %(str(e)))
        sys.exit(2)
