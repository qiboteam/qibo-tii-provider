import tarfile
import tempfile
import time
import typing as T
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import numpy as np
import qibo
import requests

from . import constants
from .config_logging import logger
from .exceptions import JobApiError
from .utils import QiboApiRequest


def convert_str_to_job_status(status: str):
    return next((s for s in QiboJobStatus if s.value == status), None)


class QiboJobStatus(Enum):
    QUEUED = "to_do"
    RUNNING = "in_progress"
    DONE = "success"
    ERROR = "error"


def wait_for_response_to_get_request(
    url: str, seconds_between_checks: T.Optional[int] = None, verbose: bool = False
) -> requests.Response:
    """Wait until the server completes the computation and return the response.

    :param url: the endpoint to make the request
    :type url: str

    :return: the response of the get request
    :rtype: requests.Response
    """
    if seconds_between_checks is None:
        seconds_between_checks = constants.SECONDS_BETWEEN_CHECKS

    while True:
        if verbose:
            logger.info("Check results every %d seconds ...", seconds_between_checks)
        response = QiboApiRequest.get(url, timeout=constants.TIMEOUT)
        job_status = convert_str_to_job_status(response.headers["Job-Status"])
        if job_status in [QiboJobStatus.DONE, QiboJobStatus.ERROR]:
            return response
        time.sleep(seconds_between_checks)


def _write_stream_to_tmp_file(stream: T.Iterable) -> Path:
    """Write chunk of bytes to temporary file.

    The tmp_path should be closed manually.

    :param stream: the stream of bytes chunks to be saved on disk
    :type stream: Iterable

    :return: the name of the tempo

    """
    with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
        for chunk in stream:
            if chunk:
                tmp_file.write(chunk)
        archive_path = tmp_file.name
    return Path(archive_path)


def _extract_archive_to_folder(source_archive: Path, destination_folder: Path):
    with tarfile.open(source_archive, "r:gz") as archive:
        archive.extractall(destination_folder)


def _save_and_unpack_stream_response_to_folder(
    stream: T.Iterable, results_folder: Path
):
    """Save the stream to a given folder.

    Internally, save the stream to a temporary archive and extract its contents
    to the target folder.

    :param stream: the iterator containing the response content
    :type stream: Iterable
    :param results_folder: the local path to the results folder
    :type results_folder: Path
    """
    archive_path = _write_stream_to_tmp_file(stream)

    _extract_archive_to_folder(archive_path, results_folder)

    # clean up temporary file
    archive_path.unlink()


@dataclass
class QiboJobResult:
    pid: str
    success: bool
    result: T.Optional[qibo.result.QuantumState]

    def __str__(self):
        return str(self.result)


class QiboJob:
    def __init__(
        self,
        pid: str,
        base_url: str = constants.BASE_URL,
        circuit: T.Optional[qibo.Circuit] = None,
        nshots: T.Optional[int] = None,
        lab_location: T.Optional[str] = None,
        device: T.Optional[str] = None,
    ):
        self.base_url = base_url
        self.pid = pid
        self.circuit = circuit
        self.nshots = nshots
        self.device = device
        self.lab_location = lab_location

        self._status = None

    def refresh(self):
        """Refreshes job information from server.

        This method does not query the results from server.
        """
        url = self.base_url + f"/job/info/{self.pid}"
        response = requests.get(url)
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError:
            raise JobApiError(response.status_code, response.json()["detail"])

        info = response.json()
        if info is not None:
            self._update_job_info(info)

    def _update_job_info(self, info: T.Dict):
        self.circuit = info.get("circuit")
        self.nshots = info.get("nshots")
        self.lab_location = info["device"].get("lab_location")
        self.device = info["device"].get("device")
        self._status = convert_str_to_job_status(info["status"])

    def status(self) -> QiboJobStatus:
        url = self.base_url + f"/job/status/{self.pid}"
        response = requests.get(url)
        response.raise_for_status()
        status = response.json()["status"]
        self._status = convert_str_to_job_status(status)
        return self._status

    def running(self) -> bool:
        if self._status is None:
            self.refresh()
        return self._status is QiboJobStatus.RUNNING

    def done(self) -> bool:
        if self._status is None:
            self.refresh()
        return self._status is QiboJobStatus.DONE

    def result(self, wait: int = 5, verbose: bool = False) -> QiboJobResult:
        """Send requests to server checking whether the job is completed.

        This function populates the `TIIProvider.results_folder` and
        `TIIProvider.results_path` attributes.

        :return: the numpy array with the results of the computation. None if
        the job raised an error.
        :rtype: T.Optional[np.ndarray]
        """
        # @TODO: here we can use custom logger levels instead of if statement
        url = self.base_url + f"/job/result/{self.pid}/"
        response = wait_for_response_to_get_request(url, wait, verbose)

        # create the job results folder
        self.results_folder = constants.RESULTS_BASE_FOLDER / self.pid
        self.results_folder.mkdir(parents=True, exist_ok=True)

        result = QiboJobResult(pid=self.pid, success=False, result=None)

        # Save the stream to disk
        try:
            _save_and_unpack_stream_response_to_folder(
                response.iter_content(), self.results_folder
            )
        except tarfile.ReadError as err:
            logger.error("Catched tarfile ReadError: %s", err)
            logger.error(
                "The received file is not a valid gzip "
                "archive, the result might have to be inspected manually. Find "
                "the file at `%s`",
                self.results_folder.as_posix(),
            )
            return result

        if response.headers["Job-Status"].lower() == "error":
            logger.info(
                "Job exited with error, check logs in %s folder",
                self.results_folder.as_posix(),
            )
            return result

        self.results_path = self.results_folder / "results.npy"
        result.result = qibo.result.load_result(self.results_path)
        return result
