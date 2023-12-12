import json
from pathlib import Path
import tarfile
import tempfile
import time
from typing import Dict, Iterable, List, Optional
import os

import numpy as np
import qibo
import requests

from .config import MalformedResponseError


QRCCLUSTER_IP = os.environ.get("QRCCLUSTER_IP", "login.qrccluster.com")
QRCCLUSTER_PORT = os.environ.get("QRCCLUSTER_PORT", "8010")
RESULTS_BASE_FOLDER = os.environ.get("RESULTS_BASE_FOLDER", "/tmp/qibo_tii_provider")
SECONDS_BETWEEN_CHECKS = os.environ.get("SECONDS_BETWEEN_CHECKS", 2)

BASE_URL = f"http://{QRCCLUSTER_IP}:{QRCCLUSTER_PORT}/"

RESULTS_BASE_FOLDER = Path(RESULTS_BASE_FOLDER)
RESULTS_BASE_FOLDER.mkdir(exist_ok=True)

SECONDS_BETWEEN_CHECKS = SECONDS_BETWEEN_CHECKS

# configure logger
import logging

logging.basicConfig(format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)
logging_level = logging.INFO
logger.setLevel(logging_level)


# TODO: check here that the stream of data
# TODO: split this into two function and unittest the two functionalities:
# - create an archive from a stream iterable
# - extract everything into results_folder
def _write_stream_response_to_folder(stream: Iterable, results_folder: Path):
    """Save the stream to a given folder.

    Internally, save the stream to a temporary archive and extract its contents
    to the target folder.

    :param stream: the iterator containing the response content
    :type stream: Iterable
    :param results_folder: the local path to the results folder
    :type results_folder: Path
    """
    # save archive to tempfile
    with tempfile.NamedTemporaryFile(delete=False) as archive:
        for chunk in stream:
            if chunk:
                archive.write(chunk)
        archive_path = archive.name

    # extract archive content to target directory
    with tarfile.open(archive_path, "r") as archive:
        archive.extractall(results_folder)

    os.remove(archive_path)


def check_response_has_keys(response: requests.models.Response, keys: List[str]):
    """Check that the response body contains certain keys.


    :raises MalformedResponseError: if the server response does not contain all
    the expected keys
    """
    response_keys = set(response.json().keys())
    expected_keys = set(keys)
    missing_keys = expected_keys.difference(response_keys)

    if len(missing_keys):
        raise MalformedResponseError(
            f"The server response is missing the following keys: {' '.join(missing_keys)}"
        )


class TIIProvider:
    """Class to manage the interaction with the QRC cluster."""

    def __init__(self, token: str):
        """
        :param token: the authentication token associated to the webapp user
        :type: str
        """
        self.token = token

        self.check_client_server_qibo_versions()

    def check_client_server_qibo_versions(self):
        """Check that client and server qibo package installed versions match.

        Raise assertion error if the two versions are not the same.
        """
        url = BASE_URL + "qibo_version/"
        response = requests.get(url)
        response.raise_for_status()
        check_response_has_keys(response, ["qibo_version"])
        qibo_server_version = response.json()["qibo_version"]
        qibo_local_version = qibo.__version__

        assert (
            qibo_local_version == qibo_server_version
        ), f"Local Qibo package version does not match the server one, please upgrade: {qibo_local_version} -> {qibo_server_version}"

    def run_circuit(
        self, circuit: qibo.Circuit, nshots: int = 1000, device: str = "sim"
    ) -> Optional[np.ndarray]:
        """Run circuit on the cluster.

        List of available devices:

        - sim
        - iqm5q
        - spinq10q
        - tii1q_b1
        - qw25q_gold
        - tiidc
        - tii2q
        - tii2q1
        - tii2q2
        - tii2q3
        - tii2q4

        :param circuit: the QASM representation of the circuit to run
        :type circuit: Circuit
        :param nshots:
        :type nshots: int
        :param device: the device to run the circuit on. Default device is `sim`
        :type device: str

        :return: the numpy array with the results of the computation. None if
        the job raised an error.
        :rtype: np.ndarray
        """
        # post circuit to server
        logger.info("Post new circuit on the server")
        try:
            self._post_circuit(circuit, nshots, device)
        except:
            logger.error()
            return

        # retrieve results
        logger.info("Job posted on server with pid %s", self.pid)
        logger.info("Check results every %d seconds ...", SECONDS_BETWEEN_CHECKS)
        result = self._get_result()

        return result

    def _post_circuit(
        self, circuit: qibo.Circuit, nshots: int = 100, device: str = "sim"
    ) -> int:
        """

        :return: the post request status code. If the post block raises an
        exception, the exit code is set to -1.
        :rtype: int
        """
        # HTTP request
        url = BASE_URL + "run_circuit/"
        payload = {
            "token": self.token,
            "circuit": circuit.raw,
            "nshots": nshots,
            "device": device,
        }
        response = requests.post(url, json=payload)

        # checks
        response.raise_for_status()
        check_response_has_keys(response, ["pid", "message"])

        # save the response
        response_content = response.json()
        self.pid = response_content["pid"]
        return response_content["message"]

    def _get_result(self) -> Optional[np.ndarray]:
        """Send requests to server checking whether the job is completed.

        This function populates the `TIIProvider.result_folder` and
        `TIIProvider.result_path` attributes.

        :return: the numpy array with the results of the computation. None if
        the job raised an error.
        :rtype: Optional[np.ndarray]
        """
        url = BASE_URL + f"get_result/{self.pid}"
        while True:
            time.sleep(SECONDS_BETWEEN_CHECKS)
            response = requests.get(url)

            if response.content == b"Job still in progress":
                continue

            # create the job results folder
            self.result_folder = RESULTS_BASE_FOLDER / self.pid
            self.result_folder.mkdir(exist_ok=True)

            # Save the stream to disk
            _write_stream_response_to_folder(
                response.iter_content(), self.result_folder
            )

            if response.headers["Job-Status"].lower() == "error":
                logger.info(
                    "Job exited with error, check logs in %s folder",
                    self.result_folder.as_posix(),
                )
                return None

            self.result_path = self.result_folder / "results.npy"
            return qibo.result.load_result(self.result_path)
