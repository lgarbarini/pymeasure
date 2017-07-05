#
# This file is part of the PyMeasure package.
#
# Copyright (c) 2013-2017 PyMeasure Developers
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#

import logging
import time
import traceback
from logging.handlers import QueueHandler
from multiprocessing import Queue

from .listeners import Recorder
from .procedure import Procedure
from .results import Results
from ..log import TopicQueueHandler
from ..process import StoppableProcess

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())

try:
    import zmq
    import cloudpickle
except ImportError:
    zmq = None
    cloudpickle = None
    log.warning("ZMQ and cloudpickle are required for TCP communication")


class Worker(StoppableProcess):
    """ Worker runs the procedure and emits information about
    the procedure and its status over a ZMQ TCP port. In a child
    thread, a Recorder is run to write the results to
    """

    def __init__(self, results, log_queue=None, log_level=logging.INFO, port=None):
        """ Constructs a Worker to perform the Procedure
        defined in the file at the filepath
        """
        super().__init__()

        self.port = port
        if not isinstance(results, Results):
            raise ValueError("Invalid Results object during Worker construction")
        self.results = results
        self.procedure = self.results.procedure
        self.procedures_file = self.procedure.__module__
        self.procedure.check_parameters()
        self.procedure.status = Procedure.QUEUED

        self.recorder = None
        self.recorder_queue = Queue()

        self.monitor_queue = Queue()
        if log_queue is None:
            log_queue = Queue()
        self.log_queue = log_queue
        self.log_level = log_level

        self.context = None
        self.publisher = None

    def join(self, timeout=0):
        try:
            super().join(timeout)
        except (KeyboardInterrupt, SystemExit):
            log.warning("User stopped Worker join prematurely")
            self.stop()
            super().join(0)

    def emit(self, topic, record):
        """ Emits data of some topic over TCP """
        log.debug("Emitting message: %s %s", topic, record)

        try:
            data = cloudpickle.dumps((topic, record))
            self.publisher.send_multipart(data)
        except (NameError, AttributeError):
            pass  # No dumps defined
        if topic == 'results':
            self.recorder.handle(record)
        elif topic == 'status' or topic == 'progress':
            self.monitor_queue.put((topic.decode(), record))

    def handle_abort(self):
        log.exception("User stopped Worker execution prematurely")
        self.update_status(Procedure.ABORTED)

    def handle_error(self):
        log.exception("Worker caught an error on %r", self.procedure)
        traceback_str = traceback.format_exc()
        self.emit('error', traceback_str)
        self.update_status(Procedure.FAILED)

    def update_status(self, status):
        self.procedure.status = status
        self.emit('status', status)

    def shutdown(self):
        self.procedure.shutdown()

        if self.should_stop() and self.procedure.status == Procedure.RUNNING:
            self.update_status(Procedure.ABORTED)
        elif self.procedure.status == Procedure.RUNNING:
            self.update_status(Procedure.FINISHED)
            self.emit('progress', 100.)

        self.recorder.enqueue_sentinel()
        self.monitor_queue.put(None)

    def run(self):
        global log
        log = logging.getLogger()
        log.setLevel(self.log_level)
        log.handlers = []  # Remove all other handlers
        log.addHandler(TopicQueueHandler(self.monitor_queue))
        log.addHandler(QueueHandler(self.log_queue))
        log.info("Worker process started")

        self.recorder = Recorder(self.results, self.recorder_queue)
        self.recorder.start()

        locals()[self.procedures_file] = __import__(self.procedures_file)

        # route Procedure methods & log
        self.procedure.should_stop = self.should_stop
        self.procedure.emit = self.emit

        if self.port is not None:
            try:
                self.context = zmq.Context()
                log.debug("Worker ZMQ Context: %r" % self.context)
                self.publisher = self.context.socket(zmq.PUB)
                self.publisher.bind('tcp://*:%d' % self.port)
                log.info("Worker connected to tcp://*:%d" % self.port)
                time.sleep(0.01)
            except Exception:
                log.exception("couldn't connect to ZMQ context")

        log.info("Worker started running an instance of %r", self.procedure.__class__.__name__)
        self.update_status(Procedure.RUNNING)
        self.emit('progress', 0.)

        try:
            self.procedure.startup()
            self.procedure.execute()
        except (KeyboardInterrupt, SystemExit):
            self.handle_abort()
        except Exception:
            self.handle_error()
        finally:
            self.shutdown()
            self.stop()

    def __repr__(self):
        return "<%s(port=%s,procedure=%s,should_stop=%s)>" % (
            self.__class__.__name__, self.port,
            self.procedure.__class__.__name__,
            self.should_stop()
        )
