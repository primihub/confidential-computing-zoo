import grpc
from homo_lr_pb2 import TransferRequest
from homo_lr_pb2 import TransferResponse
import homo_lr_pb2_grpc
from homo_lr_pb2_grpc import TransferServiceStub
from threading import Thread
import logging
import time
from concurrent import futures

import sys
import queue

logging.basicConfig(level=logging.DEBUG,
                    format="[%(asctime)s][%(name)s][%(levelname)s] %(message)s",
                    datefmt = '%Y-%m-%d  %H:%M:%S %a')

class NamedQueue:
    def __init__(self, name):
        self.valid = True 
        self.queue = queue.Queue()
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
        if key in self.queue_map:
            logging.warn(f"Another queue has the same key ${key}, duplicate key error.")  
            return TransferResponse(is_ok = False, errmsg = "Duplicate key error.")

        self.queue_map[key] = NamedQueue(key)
        logging.info(f"Init queue for key {key}.")
        return TransferResponse(is_ok = True)

    
    def Fini(self, request, context):
        key = request.key
        if not key in self.queue_map:
            logging.warn(f"No such queue has key {key}, not found error.")
            return TransferResponse(is_ok = False, errmsg = "Not found error.")

        queue = self.queue_map[key]
        queue.Stop()
        del self.queue_map[key]


    def Transfer(self, request, context):
        key = request.key
        if not key in self.queue_map:
            logging.warn(f"No such queue has key {key}, not found error.")
            return TransferResponse(is_ok = False, errmsg = "Not found error.")

        queue = self.queue_map[key]
        queue.Put(request.msg)

        response = TransferResponse(is_ok = True)
        return response


    def GetQueue(self, key):
        if not key in self.queue_map:
            logging.warn(f"The queue that kas key {key} is not inited.")
            return None

        return self.queue_map[key]


class TransferChannel:
    def __init__(self, key, peer):
        self.key = key
        self.service = TransferServiceImpl() 
        self.peer = peer

        channel = grpc.insecure_channel(self.peer)
        client = TransferServiceStub(channel)
        request = TransferRequest(key = self.key)
        response = client.Init(request)
        if response.is_ok is False:
            logging.error(f"Failed to init transfer queue, errmsg ${response.errmsg}.")
            raise RuntimeError(f"Failed to init transfer queue, errmsg ${response.errmsg}.")


    def __del__(self):
        channel = grpc.insecure_channel(self.peer)
        client = TransferServiceStub(channel)
        request = TransferRequest(key = self.key)
        client.Fini(request)


    def Recv(self, retry = 10, timeout = 5):
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
            logging.warn("Receive msg with key {self.key} failed, timeout.")
            return None

        return item

    def Send(self, msg):
        channel = grpc.insecure_channel(self.peer)
        stub = TransferServiceStub(channel)

        request = TransferRequest()
        request.msg = msg
        request.key = self.key

        response = stub.Transfer(request)
        if not response.is_ok:
            logging.error(f"Send msg to {self.peer} failed, errmsg {response.errmsg}.")
            return False
        
        return True


def run_server():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    service = TransferServiceImpl()
    homo_lr_pb2_grpc.add_TransferServiceServicer_to_server(service, server)

    server.add_insecure_port('[::]:50051')
    server.start()
    server.wait_for_termination()


if __name__ == '__main__':
    server_thread = Thread(target = run_server)
    server_thread.start()

    time.sleep(2)

    channel = TransferChannel("test", "localhost:50051")
    msg = "test".encode("utf-8")
    channel.Send(msg)
    print(channel.Recv())
