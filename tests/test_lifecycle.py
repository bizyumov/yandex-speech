import concurrent.futures
import threading
import time
import unittest

import grpc
from yandex.cloud.operation import operation_pb2, operation_service_pb2_grpc

from yandex_speech.grpc_stt import GrpcTransport


class SlowOperations(operation_service_pb2_grpc.OperationServiceServicer):
    def __init__(self):
        self.started = threading.Event()

    def Get(self, request, context):
        self.started.set()
        while context.is_active():
            time.sleep(0.01)
        return operation_pb2.Operation(id=request.operation_id, done=False)


class TransportCancellationTests(unittest.TestCase):
    def setUp(self):
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=3)
        self.server = grpc.server(self.pool)
        self.service = SlowOperations()
        operation_service_pb2_grpc.add_OperationServiceServicer_to_server(self.service, self.server)
        self.port = self.server.add_insecure_port("127.0.0.1:0")
        self.server.start()
        self.transports = []
        self.workers = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for transport in self.transports:
            transport.close()
        self.server.stop(0).wait(2)
        for worker in self.workers:
            worker.join(2)
        self.pool.shutdown(wait=True)

    def transport(self):
        channels = (grpc.insecure_channel(f"127.0.0.1:{self.port}"),
                    grpc.insecure_channel(f"127.0.0.1:{self.port}"))
        transport = GrpcTransport("synthetic-only", _channels=channels)
        self.transports.append(transport)
        return transport

    def worker(self, transport, errors):
        def poll():
            try:
                transport.get_operation("synthetic-operation", 120)
            except Exception as error:
                errors.append(type(error).__name__)
        thread = threading.Thread(target=poll)
        self.workers.append(thread)
        thread.start()
        return thread

    def test_cancel_interrupts_a_blocked_real_grpc_call_within_two_seconds(self):
        transport = self.transport()
        cancel = threading.Event()
        transport.bind_cancel_event(cancel)
        errors = []
        worker = self.worker(transport, errors)
        self.assertTrue(self.service.started.wait(2))
        start = time.monotonic()
        cancel.set()
        worker.join(1.5)
        transport.close()
        self.assertFalse(worker.is_alive())
        self.assertLessEqual(time.monotonic() - start, 2)
        self.assertTrue(errors)

    def test_cancellation_does_not_cancel_another_transport(self):
        first, second = self.transport(), self.transport()
        one, two = threading.Event(), threading.Event()
        first.bind_cancel_event(one)
        second.bind_cancel_event(two)
        a, b = self.worker(first, []), self.worker(second, [])
        self.assertTrue(self.service.started.wait(2))
        one.set()
        a.join(1)
        self.assertFalse(a.is_alive())
        self.assertTrue(b.is_alive())
        two.set()
        b.join(1)
        self.assertFalse(b.is_alive())
