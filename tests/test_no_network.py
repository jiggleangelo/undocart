import json
import os
import socket
import tempfile
import unittest
from unittest.mock import patch

from remedy.batch import run
from remedy.classify import classify
from remedy.sample_data import DEMO_TODAY, RAWS, SAMPLE_CASES, make_sources


class NoNetworkTests(unittest.TestCase):
    def test_classifier_and_batch_do_not_connect(self):
        def blocked(sock, address):
            if not (isinstance(address, tuple) and address[0] == "127.0.0.1"):
                raise AssertionError("outbound network attempted")
        with tempfile.TemporaryDirectory() as folder, patch.object(socket.socket, "connect", blocked):
            classify(SAMPLE_CASES["sample-6"], make_sources(), DEMO_TODAY)
            source, target = os.path.join(folder, "in.jsonl"), os.path.join(folder, "out.csv")
            with open(source, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(RAWS[0]) + "\n")
            run(source, target, DEMO_TODAY)
            self.assertTrue(os.path.getsize(target))


if __name__ == "__main__": unittest.main()
