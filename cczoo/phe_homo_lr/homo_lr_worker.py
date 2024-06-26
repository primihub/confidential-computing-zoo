#
# Copyright (c) 2021 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os
import sys
import time
import pickle
import argparse
import grpc
import secrets
import logging
import threading
import numpy as np
import pandas as pd
from concurrent.futures import ThreadPoolExecutor as Executor
import homo_lr_pb2
import homo_lr_pb2_grpc
import multiprocessing as mp
import hetero_attestation_pb2_grpc
from homo_lr_common import verify_party
from homo_lr_common import parse_config
from attestation import HeteroAttestationTransmit
from homo_lr_common import verify_party
from homo_lr_common import parse_config

CPU_COUNTS = os.cpu_count()
PARTITIONS = min(1, CPU_COUNTS)

logging.basicConfig(level=logging.DEBUG,
                    format="[%(asctime)s][%(name)s][%(levelname)s] %(message)s",
                    datefmt='%Y-%m-%d  %H:%M:%S %a')


class AliveServicer(homo_lr_pb2_grpc.HostServicer):
    def Alive(self, request, context):
        return homo_lr_pb2.Empty()


class HomoLRWorker(object):
    def __init__(self, id, ip, epochs, alpha,
                 learning_rate, secure):
        self.id = id
        self.grpc_channel = ip + ':50051'
        self.pool = Executor()
        self.epochs = epochs
        self.alpha = alpha
        self.learning_rate = learning_rate
        self.iter_n = 0
        self.secure = secure
        self.w = None
        if secure:
            self.pub_key = self.get_pubkey()
        else:
            self.pub_key = None

    def fit(self, x, y):
        m = x.shape[0]
        x = np.concatenate((np.ones((m, 1)), x), axis=1)
        n = x.shape[1]
        x = x.T
        if self.secure:
            enc_one = self.pub_key.encrypt(1)
            self.w = np.array([enc_one] * n)
        else:
            self.w = np.ones((n,))
        y = y.reshape(1, -1)
        for i in range(self.epochs):
            self.iter_n = i
            grad = self.compute_gradient(x, y)
            self.updated_model(grad)
            self.aggregate_model()
            if i % 10 == 0:
                acc, loss = self.validate()
                print('iter: {}  acc: {:.3f}  loss: {:.3f}'.format(
                    i, acc, loss), file=sys.stderr, flush=True)

    def compute_gradient(self, x, y):
        m = x.shape[1]
        y_pred = None

        if self.secure:   # Use multi-process
            bs = m // PARTITIONS  # batch size for a partition
            futures = [self.pool.submit(
                np.dot, self.w, x[:, i*bs:(i+1)*bs]) for i in range(PARTITIONS)]
            futures.append(self.pool.submit(
                np.dot, self.w, x[:, PARTITIONS*bs:]))
            result = futures[0].result()
            for i in range(1, len(futures)):
                result = np.concatenate((result, futures[i].result()), axis=0)
            y_pred = self.sigmoid_taylor_expand(result)
        else:
            y_pred = self.sigmoid(np.dot(self.w, x))
        grad = np.dot((y_pred - y), x.T) / m
        return grad

    def sigmoid(self, x):
        return 1 / (1 + np.exp(-x))

    def sigmoid_taylor_expand(self, x):
        return (0.5 + 0.25 * x)

    def updated_model(self, grad):
        learning_rate = self.learning_rate / np.sqrt(1 + self.iter_n)
        grad = grad + self.alpha * self.w
        self.w = self.w - learning_rate * grad

    def validate(self):
        with grpc.insecure_channel(self.grpc_channel) as channel:
            stub = homo_lr_pb2_grpc.HostStub(channel)
            response = stub.Validate(homo_lr_pb2.Empty())
            return response.acc, response.loss

    def get_pubkey(self):
        with grpc.insecure_channel(self.grpc_channel) as channel:
            stub = homo_lr_pb2_grpc.HostStub(channel)
            response = stub.GetPubKey(homo_lr_pb2.KeyRequest(id=self.id))
            key = pickle.loads(response.key)
            return key

    def aggregate_model(self):
        with grpc.insecure_channel(self.grpc_channel) as channel:
            stub = homo_lr_pb2_grpc.HostStub(channel)
            weights_pb = pickle.dumps(self.w)
            response = stub.AggregateModel(homo_lr_pb2.WeightsRequest(
                id=self.id, iter_n=self.iter_n, weights=weights_pb))
            self.w = pickle.loads(response.updated_weights)

    def finish(self):
        self.pool.shutdown()
        with grpc.insecure_channel(self.grpc_channel) as channel:
            stub = homo_lr_pb2_grpc.HostStub(channel)
            stub.Finish(homo_lr_pb2.Empty())


def parse_dataset(dataset):
    data_array = pd.read_csv(dataset).to_numpy()
    x = data_array[:, 2:]
    y = data_array[:, 1].astype('int32')
    return x, y

def run_attestation_service(server, tee_node_addr, worker_id, port):
    servicer = HeteroAttestationTransmit(tee_node_addr)
    hetero_attestation_pb2_grpc.add_TransmitServiceServicer_to_server(
        servicer, server)

    alive_servicer = AliveServicer()
    homo_lr_pb2_grpc.add_HostServicer_to_server(alive_servicer, server)

    server.add_insecure_port('[::]:{}'.format(port))
    server.start()
    server.wait_for_termination()

    logging.info("Attestation service exits.")

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--train-set', required=True,
                        help='CSV format training data')
    parser.add_argument('--host-ip', required=True,
                        help='Parameter server IP for gRPC communication')
    parser.add_argument('--id', type=int, help='Worker ID')
    parser.add_argument('--epochs', type=int, default=100, help='Epochs')
    parser.add_argument('--alpha', type=float, default=0.01,
                        help='Alpha for regularization')
    parser.add_argument('--learning-rate', type=float, default=0.15)
    parser.add_argument('--secure', type=bool,
                        default=False, help='Enable PHE or not')
    parser.add_argument('--config', required=True, help="Party config file path.")
    args = parser.parse_args()

    config = parse_config(args.config)

    server = grpc.server(Executor(max_workers=10))
    attestation_thread = threading.Thread(
        target=run_attestation_service,
        args=(server, config["attestation_service"], args.id, 
              "{}".format(args.id + 60050)))
    attestation_thread.start()

    # Verify parameter server.
    verify_party(config["party_service"]["ps"], 
                 "parameter_server",
                 "attest_from_{}".format(args.id),
                 "gramine_party_{}".format(args.id))

    # Verify another worker.
    if args.id == 1:
        verify_party(config["party_service"]["2"], "party_2",
                     "RA_from_gramine_party_{}".format(args.id),
                     "gramine_party_{}".format(args.id))
    else:
        verify_party(config["party_service"]["1"], "party_1",
                     "RA_from_gramine_party_{}".format(args.id),
                     "gramine_party_{}".format(args.id))

    worker = HomoLRWorker(args.id, args.host_ip, args.epochs,
                          args.alpha, args.learning_rate, args.secure)
    x, y = parse_dataset(args.train_set)
    worker.fit(x, y)
    worker.finish()
    
    server.stop(5)
    attestation_thread.join()
