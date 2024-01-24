import grpc 
from homo_lr_pb2 import TransferRequest
from homo_lr_pb2 import TransferResponse
import homo_lr_pb2_grpc
import Queue
import logging

logging.basicConfig(level=logging.DEBUG,
                    format="[%(asctime)s][%(name)s][%(levelname)s] %(message)s",
                    datefmt = '%Y-%m-%d  %H:%M:%S %a')

class NamedQueue:
    def __init__(self, name):
        self.valid = True 
        self.queue = Queue.queue()
        self.name = name

    def Put(self, msg):
        self.queue.put(msg)

    def Get(self):
        item = self.queue.get_nowait()
        return item

    def IsStop(self):
        return self.valid

    def Stop(self):
        self.valid = False


class TransferServiceImpl(homo_lr_pb2_grpc.TransferService):
    instance_ = None
    
    def __new__(cls, *args, **kw):
        if cls.instance_ is None:
            cls.instance_ = object.__new__(cls, *args, **kw)
        return cls.instance_

    def __init__(self):
        self.queue_map = {}
    
    def Init(self, request, context):
        key = request.key
        if self.queue_map.has_key(key):
            logging.warn(f"Another queue has the same key ${key}, duplicate key error.")  
            return TransferResponse(is_ok = False, errmsg = "Duplicate key error.")

        self.queue_map[key] = NamedQueue(key)
        return TransferResponse(is_ok = True)

    
    def Fini(self, request, context):
        key = request.key
        if not self.queue_map.has_key(key):
            logging.warn(f"No such queue has key ${key}, not found error.")
            return TransferResponse(is_ok = False, errmsg = "Not found error.")

        queue = self.queue_map[key]
        queue.Stop()
        del self.queue_map[key]


    def GetQueue(self, key):
        if not self.queue_map.has_key(key):
            logging.warn(f"The queue that kas key ${key} is not inited.")
            return None

        return self.queue_map[key]


class TransferChannel:
    def __init__(self, key, peer):
        self.key = key
        self.service = TransferServiceImpl() 
        self.peer = peer

        channel = grpc.insecure_channel(self.peer)
        client = TransferServiceStub()
        request = TransferRequest(key = self.key)
        response = client.Init(request)
        if response.is_ok is False:
            logging.error(f"Failed to init transfer queue, errmsg ${response.errmsg}.")
            raise RuntimeError(f"Failed to init transfer queue, errmsg ${response.errmsg}.")


    def __del__(self):
        channel = grpc.insecure_channel(self.peer)
        client = TransferServiceStub()
        request = TransferRequest(key = self.key)
        client.Fini(request)


    def Recv(self, retry, timeout):
        count = 0
        item = None
        while count < retry:
            queue = self.service.GetQueue(self.key)
            if queue is None:
                time.sleep(timeout)
                count = count + 1
                continue

            item = queue.Get()
            if item is None:
                time.sleep(timeout)
                count = count + 1
                continue

            break
        
        if item is None:
            logging.warn("Receive msg with key ${self.key} failed, timeout.")
            return None

        return item

    def Send(self, msg):
        channel = grpc.insecure_channel(self.peer)
        client = TransferServiceStub()

        request = TransferRequest()
        request.msg = msg
        request.key = self.key()

        response = client.Send(request)
        if not response.is_ok:
            logging.error(f"Send msg to ${self.peer} failed, errmsg ${response.errmsg}.")
            return False
        
        return True
