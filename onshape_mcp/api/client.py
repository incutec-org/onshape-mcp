"""Onshape API client for REST API communication."""

import base64
import httpx
from typing import Any, Dict, Optional
from pydantic import BaseModel
from loguru import logger


class OnshapeCredentials(BaseModel):
    """Onshape API credentials."""

    access_key: str
    secret_key: str
    base_url: str = "https://cad.onshape.com"


class OnshapeClient:
    """Client for interacting with Onshape REST API.

    Use as an async context manager to ensure proper cleanup:
        async with OnshapeClient(credentials) as client:
            result = await client.get("/api/v9/documents")
    """

    def __init__(self, credentials: OnshapeCredentials):
        """Initialize the Onshape client.

        Args:
            credentials: Onshape API credentials (access key and secret key)
        """
        self.credentials = credentials
        self.base_url = credentials.base_url
        self._client: Optional[httpx.AsyncClient] = None
        self._own_client = False

    async def __aenter__(self):
        """Async context manager entry."""
        self._client = httpx.AsyncClient(timeout=30.0)
        self._own_client = True
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit - ensures cleanup."""
        await self.close()
        return False

    def _ensure_client(self):
        """Ensure HTTP client is initialized."""
        if self._client is None:
            # Create client if not using context manager (backwards compatibility)
            self._client = httpx.AsyncClient(timeout=30.0)
            self._own_client = True

    def _get_auth_header(self) -> str:
        """Generate Basic Auth header from credentials.

        Returns:
            Authorization header value
        """
        auth_string = f"{self.credentials.access_key}:{self.credentials.secret_key}"
        encoded = base64.b64encode(auth_string.encode()).decode()
        return f"Basic {encoded}"

    def _sanitize_for_logging(self, data: Any, max_length: int = 200) -> str:
        """Sanitize sensitive data for logging.

        Args:
            data: Data to sanitize
            max_length: Maximum length of output string

        Returns:
            Sanitized string safe for logging
        """
        if isinstance(data, dict):
            sanitized = {}
            for k, v in data.items():
                if k.lower() in {
                    "authorization",
                    "api_key",
                    "secret",
                    "password",
                    "token",
                    "access_key",
                    "secret_key",
                }:
                    sanitized[k] = "***REDACTED***"
                else:
                    sanitized[k] = v
            return str(sanitized)[:max_length]

        result = str(data)
        if len(result) > max_length:
            return result[:max_length] + "... (truncated)"
        return result

    async def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Make a GET request to Onshape API.

        Args:
            path: API endpoint path (e.g., "/api/v9/documents")
            params: Query parameters

        Returns:
            JSON response data
        """
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": self._get_auth_header(),
            "Accept": "application/json;charset=UTF-8; qs=0.09",
        }

        self._ensure_client()
        logger.debug(f"GET {url} with params: {self._sanitize_for_logging(params)}")
        response = await self._client.get(url, params=params, headers=headers)
        response.raise_for_status()
        result = response.json()
        logger.debug(f"GET {url} response: {self._sanitize_for_logging(result, max_length=500)}")
        return result

    async def post(
        self,
        path: str,
        data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Make a POST request to Onshape API.

        Args:
            path: API endpoint path
            data: JSON body data
            params: Query parameters

        Returns:
            JSON response data
        """
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": self._get_auth_header(),
            "Accept": "application/json;charset=UTF-8; qs=0.09",
            "Content-Type": "application/json;charset=UTF-8; qs=0.09",
        }

        self._ensure_client()
        logger.debug(f"POST {url} with params: {self._sanitize_for_logging(params)}")
        logger.debug(f"POST {url} data: {self._sanitize_for_logging(data, max_length=1000)}")
        response = await self._client.post(url, json=data, params=params, headers=headers)

        # Log error details if request failed
        if response.status_code >= 400:
            try:
                error_body = response.json()
                logger.error(
                    f"POST {url} failed with status {response.status_code}: {self._sanitize_for_logging(error_body)}"
                )
            except Exception:
                logger.error(
                    f"POST {url} failed with status {response.status_code}: {response.text[:500]}"
                )

        response.raise_for_status()
        if not response.content:
            logger.debug(f"POST {url} returned empty body (status {response.status_code})")
            return {}
        result = response.json()
        logger.debug(f"POST {url} response: {self._sanitize_for_logging(result, max_length=500)}")
        return result

    async def delete(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Make a DELETE request to Onshape API.

        Args:
            path: API endpoint path
            params: Query parameters

        Returns:
            JSON response data
        """
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": self._get_auth_header(),
            "Accept": "application/json;charset=UTF-8; qs=0.09",
        }

        self._ensure_client()
        response = await self._client.delete(url, params=params, headers=headers)
        response.raise_for_status()
        if not response.content:
            return {}
        return response.json()

    async def request_raw(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Any] = None,
        files: Optional[Any] = None,
        data: Optional[Dict[str, Any]] = None,
        accept: str = "application/json;charset=UTF-8; qs=0.09",
        follow_redirects: bool = True,
    ) -> httpx.Response:
        """Make an arbitrary request and return the raw response.

        This is the transport used by the generic REST tools. Unlike get/post/
        delete it does not assume a JSON response body, so it also serves
        binary downloads and multipart uploads.

        Args:
            method: HTTP method (GET, POST, DELETE, PUT, PATCH)
            path: API endpoint path, already including the /api/vN prefix
            params: Query parameters
            json_body: JSON body, mutually exclusive with files/data
            files: multipart file payload for httpx
            data: multipart form fields, used alongside files
            accept: Accept header value
            follow_redirects: Onshape serves export payloads via a redirect

        Returns:
            The raw httpx response, not raised for status
        """
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": self._get_auth_header(),
            "Accept": accept,
        }
        # Let httpx set the multipart boundary; forcing JSON here breaks uploads.
        if json_body is not None and files is None:
            headers["Content-Type"] = "application/json;charset=UTF-8; qs=0.09"

        self._ensure_client()
        logger.debug(f"{method.upper()} {url} params={self._sanitize_for_logging(params)}")
        response = await self._client.request(
            method.upper(),
            url,
            params=params,
            json=json_body if files is None else None,
            files=files,
            data=data,
            headers=headers,
            follow_redirects=follow_redirects,
        )
        if response.status_code >= 400:
            logger.error(
                f"{method.upper()} {url} failed with status {response.status_code}: "
                f"{response.text[:500]}"
            )
        return response

    async def request_json(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Any] = None,
    ) -> Any:
        """Make an arbitrary request and decode a JSON response."""
        response = await self.request_raw(method, path, params=params, json_body=json_body)
        response.raise_for_status()
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {"_raw_text": response.text[:5000]}

    async def request_binary(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        accept: str = "*/*",
    ) -> bytes:
        """Fetch a binary payload, such as an exported STEP or STL file."""
        response = await self.request_raw(
            method, path, params=params, accept=accept
        )
        response.raise_for_status()
        return response.content

    async def upload_multipart(
        self,
        path: str,
        files: Any,
        data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """POST a multipart/form-data payload, used for file import."""
        response = await self.request_raw(
            "POST", path, params=params, files=files, data=data
        )
        response.raise_for_status()
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {"_raw_text": response.text[:5000]}

    async def close(self):
        """Close the HTTP client and clean up resources."""
        if self._client and self._own_client:
            await self._client.aclose()
            self._client = None
