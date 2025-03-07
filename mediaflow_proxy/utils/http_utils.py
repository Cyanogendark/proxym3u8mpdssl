import logging
import typing
from dataclasses import dataclass
from functools import partial
from urllib import parse
from urllib.parse import urlencode

import anyio
import httpx
import tenacity
from fastapi import Response
from starlette.background import BackgroundTask
from starlette.concurrency import iterate_in_threadpool
from starlette.requests import Request
from starlette.types import Receive, Send, Scope
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from tqdm.asyncio import tqdm as tqdm_asyncio

from mediaflow_proxy.configs import settings
from mediaflow_proxy.const import SUPPORTED_REQUEST_HEADERS
from mediaflow_proxy.utils.crypto_utils import EncryptionHandler

logger = logging.getLogger(__name__)


class DownloadError(Exception):
    def __init__(self, status_code, message):
        self.status_code = status_code
        self.message = message
        super().__init__(message)


def create_httpx_client(follow_redirects: bool = True, timeout: float = 30.0, **kwargs) -> httpx.AsyncClient:
    """Creates an HTTPX client with configured proxy routing and SSL verification disabled"""
    mounts = settings.transport_config.get_mounts()
    client = httpx.AsyncClient(mounts=mounts, follow_redirects=follow_redirects, timeout=timeout, verify=False, **kwargs)
    return client


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type(DownloadError),
)
async def fetch_with_retry(client, method, url, headers, follow_redirects=True, **kwargs):
    """
    Fetches a URL with retry logic.

    Args:
        client (httpx.AsyncClient): The HTTP client to use for the request.
        method (str): The HTTP method to use (e.g., GET, POST).
        url (str): The URL to fetch.
        headers (dict): The headers to include in the request.
        follow_redirects (bool, optional): Whether to follow redirects. Defaults to True.
        **kwargs: Additional arguments to pass to the request.

    Returns:
        httpx.Response: The HTTP response.

    Raises:
        DownloadError: If the request fails after retries.
    """
    try:
        response = await client.request(method, url, headers=headers, follow_redirects=follow_redirects, **kwargs)
        response.raise_for_status()
        return response
    except httpx.TimeoutException:
        logger.warning(f"Timeout while downloading {url}")
        raise DownloadError(409, f"Timeout while downloading {url}")
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error {e.response.status_code} while downloading {url}")
        if e.response.status_code == 404:
            logger.error(f"Segment Resource not found: {url}")
            raise e
        raise DownloadError(e.response.status_code, f"HTTP error {e.response.status_code} while downloading {url}")
    except Exception as e:
        logger.error(f"Error downloading {url}: {e}")
        raise


class Streamer:
    def __init__(self, client):
        """
        Initializes the Streamer with an HTTP client.

        Args:
            client (httpx.AsyncClient): The HTTP client to use for streaming.
        """
        self.client = client
        self.response = None
        self.progress_bar = None
        self.bytes_transferred = 0
        self.start_byte = 0
        self.end_byte = 0
        self.total_size = 0

    async def create_streaming_response(self, url: str, headers: dict):
        """
        Creates and sends a streaming request.

        Args:
            url (str): The URL to stream from.
            headers (dict): The headers to include in the request.
        """
        request = self.client.build_request("GET", url, headers=headers)
        self.response = await self.client.send(request, stream=True, follow_redirects=True)
        self.response.raise_for_status()

    async def stream_content(self) -> typing.AsyncGenerator[bytes, None]:
        """
        Streams the content from the response.
        """
        if not self.response:
            raise RuntimeError("No response available for streaming")

        try:
            self.parse_content_range()

            if settings.enable_streaming_progress:
                with tqdm_asyncio(
                    total=self.total_size,
                    initial=self.start_byte,
                    unit="B",
                    unit_scale=True,
                    unit_divisor=1024,
                    desc="Streaming",
                    ncols=100,
                    mininterval=1,
                ) as self.progress_bar:
                    async for chunk in self.response.aiter_bytes():
                        yield chunk
                        chunk_size = len(chunk)
                        self.bytes_transferred += chunk_size
                        self.progress_bar.set_postfix_str(
                            f"📥 : {self.format_bytes(self.bytes_transferred)}", refresh=False
                        )
                        self.progress_bar.update(chunk_size)
            else:
                async for chunk in self.response.aiter_bytes():
                    yield chunk
                    self.bytes_transferred += len(chunk)

        except httpx.TimeoutException:
            logger.warning("Timeout while streaming")
            raise DownloadError(409, "Timeout while streaming")
        except GeneratorExit:
            logger.info("Streaming session stopped by the user")
        except Exception as e:
            logger.error(f"Error streaming content: {e}")
            raise

    @staticmethod
    def format_bytes(size) -> str:
        power = 2**10
        n = 0
        units = {0: "B", 1: "KB", 2: "MB", 3: "GB", 4: "TB"}
        while size > power:
            size /= power
            n += 1
        return f"{size:.2f} {units[n]}"

    def parse_content_range(self):
        content_range = self.response.headers.get("Content-Range", "")
        if content_range:
            range_info = content_range.split()[-1]
            self.start_byte, self.end_byte, self.total_size = map(int, range_info.replace("/", "-").split("-"))
        else:
            self.start_byte = 0
            self.total_size = int(self.response.headers.get("Content-Length", 0))
            self.end_byte = self.total_size - 1 if self.total_size > 0 else 0

    async def get_text(self, url: str, headers: dict):
        """
        Sends a GET request to a URL and returns the response text.

        Args:
            url (str): The URL to send the GET request to.
            headers (dict): The headers to include in the request.

        Returns:
            str: The response text.
        """
        try:
            self.response = await fetch_with_retry(self.client, "GET", url, headers)
        except tenacity.RetryError as e:
            raise e.last_attempt.result()
        return self.response.text

    async def close(self):
        """
        Closes the HTTP client and response.
        """
        if self.response:
            await self.response.aclose()
        if self.progress_bar:
            self.progress_bar.close()
        await self.client.aclose()


async def download_file_with_retry(url: str, headers: dict):
    """
    Downloads a file with retry logic.

    Args:
        url (str): The URL of the file to download.
        headers (dict): The headers to include in the request.

    Returns:
        bytes: The downloaded file content.

    Raises:
        DownloadError: If the download fails after retries.
    """
    async with create_httpx_client() as client:
        try:
            response = await fetch_with_retry(client, "GET", url, headers)
            return response.content
        except DownloadError as e:
            logger.error(f"Failed to download file: {e}")
            raise e
        except tenacity.RetryError as e:
            raise DownloadError(502, f"Failed to download file: {e.last_attempt.result()}")


async def request_with_retry(method: str, url: str, headers: dict, **kwargs) -> httpx.Response:
    """
    Sends an HTTP request with retry logic.

    Args:
        method (str): The HTTP method to use (e.g., GET, POST).
        url (str): The URL to send the request to.
        headers (dict): The headers to include in the request.
        **kwargs: Additional arguments to pass to the request.

    Returns:
        httpx.Response: The HTTP response.

    Raises:
        DownloadError: If the request fails after retries.
    """
    async with create_httpx_client() as client:
        try:
            response = await fetch_with_retry(client, method, url, headers, **kwargs)
            return response
        except DownloadError as e:
            logger.error(f"Failed to download file: {e}")
            raise
            
