from typing import Dict, Optional, Union
import httpx
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class RouteConfig(BaseModel):
    """Configuration for a specific route"""

    proxy: bool = True
    proxy_url: Optional[str] = None
    verify_ssl: bool = False  # Permite conexiones sin verificación SSL


class TransportConfig(BaseSettings):
    """Main proxy configuration"""

    proxy_url: Optional[str] = Field(
        None, description="Primary proxy URL. Example: socks5://user:pass@proxy:1080 or http://proxy:8080"
    )
    all_proxy: bool = Field(False, description="Enable proxy for all routes by default")
    transport_routes: Dict[str, RouteConfig] = Field(
        default_factory=dict, description="Pattern-based route configuration"
    )

    def get_transport(self, async_http: bool = True) -> Union[httpx.HTTPTransport, httpx.AsyncHTTPTransport]:
        """
        Create a transport object with the appropriate settings.
        """
        transport_cls = httpx.AsyncHTTPTransport if async_http else httpx.HTTPTransport

        # Configuración del transporte sin usar 'proxies' directamente
        transport = transport_cls(
            verify=False,  # Desactiva la verificación SSL
        )

        if self.proxy_url:
            # Si se ha configurado un proxy, se configura
            transport.proxies = self.proxy_url

        return transport

    def get_client(self, async_http: bool = True) -> Union[httpx.Client, httpx.AsyncClient]:
        """
        Create an HTTP client with the appropriate transport.
        """
        client_cls = httpx.AsyncClient if async_http else httpx.Client

        # Obtener el transporte adecuado
        transport = self.get_transport(async_http=async_http)

        # Crear y retornar el cliente con el transporte configurado
        return client_cls(transport=transport)

    class Config:
        env_file = ".env"
        extra = "ignore"


class Settings(BaseSettings):
    api_password: str | None = None  # The password for protecting the API endpoints.
    log_level: str = "INFO"  # The logging level to use.
    transport_config: TransportConfig = Field(default_factory=TransportConfig)  # Configuration for httpx transport.
    enable_streaming_progress: bool = False  # Whether to enable streaming progress tracking.

    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3"  # The user agent to use for HTTP requests.
    )

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()

# Cliente HTTP con SSL deshabilitado
client = settings.transport_config.get_client(async_http=False)
async_client = settings.transport_config.get_client(async_http=True)
