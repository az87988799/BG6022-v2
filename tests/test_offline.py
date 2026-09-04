import socket

import pytest
from pytest_socket import SocketBlockedError


def test_pytest_default_blocks_socket_creation() -> None:
    with pytest.raises(SocketBlockedError):
        socket.socket()
