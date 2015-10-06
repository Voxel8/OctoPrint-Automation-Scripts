from concurrent.futures import Future, as_completed
from threading import Thread, Event, Lock

class QueueMessage:

    def __init__(self, future, message_type, args, kwargs = None):
        self.future = future
        self.message_type = message_type
        self.args = args
        self.kwargs = kwargs


class AtomicQueue:

    def __init__(self):
        self.queue = []
        self.queue_lock = Lock()
        self.has_payload_event = Event()

    def append(payload):
        with self.queue_lock:
            self.queue.append(payload)
            self.has_payload_event.set()

    def popleft(self):
        # If the queue is empty, yield to the OS until we have payload.
        if not self.queue:
            self.has_payload_event.wait()

        with self.queue_lock:
            payload = self.queue.popleft()
            # If we've popped off the last payload, clear the event so we can
            # get signaled in the future.  This must be done while holding
            # the queue lock.
            if not self.queue:
                self.has_payload_event.clear()

            return payload

class FutureSerial:
    """
    A proxy for a Serial object that returns Futures and executes `readline()`,
    `write()`, and `close()` in another thread instead of the calling thread.
    """

    def __init__(self):
        self.has_serial_event = Event()
        self._serial = None

        # Since a serial port can be read from and written to simultaneously, we
        # have two separate queues.
        self.read_queue = AtomicQueue()
        self.write_queue = AtomicQueue()

        # These flags are a way to terminate the worker threads.  Set them to
        # True from another thread to make the work loop exit.
        self.done_reading = False
        self.done_writing = False

    @property
    def serial(self):
        return self._serial

    @serial.setter
    def serial(self, serial):
        self._serial = serial
        # Signal to the worker thread that we have a serial object.
        if self._serial:
            self.has_serial_event.set()
        else:
            self.has_serial_event.clear()

    def future_readline(self, *args, **kwargs):
        """
        Returns a future that resolves to the result of readline() on the serial
        object.

        This can safely be called from any number of threads.
        """
        future = Future()
        message = QueueMessage(future, 'readline', args, kwargs)
        self.read_queue.append(message)
        return future

    def future_write(self, data):
        """
        Returns a future that resolves to the result of write() on the serial
        object.

        This can safely be called from any number of threads.
        """
        future = Future()
        message = QueueMessage(future, 'write', [data])
        self.write_queue.append(message)
        return future

    def future_close(self):
        """
        Returns a future that resolves to the result of close() on the serial
        object.

        This can safely be called from any number of threads.
        """
        future = Future()
        message = QueueMessage(future, 'close', [])
        self.write_queue.append(message)
        return future

    def work_off_reads(self):
        """
        Work off the read queue.

        This should only be called from a single thread.
        """
        self._work_off(self.read_queue, 'done_reading')

    def work_off_writes(self):
        """
        Work off the write queue.

        This should only be called from a single thread.
        """
        self._work_off(self.write_queue, 'done_writing')

    def _work_off(self, atomic_queue, done_flag):
        # Wait until we actually have a serial object set.
        self.has_serial_event.wait()

        while not getattr(self, done_flag):
            # Pop work off the queue in FIFO order.
            message = atomic_queue.popleft()

            # Execute the message.
            self._run(message)

    def _run(self, message):
        """
        Actually execute a message
        """
        if message.message_type == 'readline':
            return self._readline(*message.args, **message.kwargs)
        elif message.message_type == 'write':
            return self._write(*message.args)
        elif message.message_type == 'close':
            return self._close(*message.args)
        else:
            raise RuntimeError('Unknown message type: {}'.format(message.message_type))

    def _readline(self, *args, **kwargs):
        return self.serial.single_threaded_readline(*args, **kwargs)

    def _write(self, data):
        return self.serial.single_threaded_write(data)

    def _close(self):
        return self.serial.single_threaded_close()
