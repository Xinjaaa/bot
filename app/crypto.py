import base64
import hashlib
import os
import socket
import struct
from dataclasses import dataclass

from Crypto.Cipher import AES


BLOCK_SIZE = 32


class WeComCryptoError(Exception):
    pass


def _sha1_signature(token: str, timestamp: str, nonce: str, value: str) -> str:
    payload = "".join(sorted([token, timestamp, nonce, value]))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _pkcs7_pad(data: bytes) -> bytes:
    padding = BLOCK_SIZE - (len(data) % BLOCK_SIZE)
    if padding == 0:
        padding = BLOCK_SIZE
    return data + bytes([padding]) * padding


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        raise WeComCryptoError("empty decrypted payload")
    padding = data[-1]
    if padding < 1 or padding > BLOCK_SIZE:
        raise WeComCryptoError("invalid PKCS#7 padding")
    return data[:-padding]


@dataclass
class WeComCrypto:
    token: str
    encoding_aes_key: str
    receive_id: str

    def __post_init__(self) -> None:
        self.aes_key = base64.b64decode(f"{self.encoding_aes_key}=")
        self.iv = self.aes_key[:16]

    def verify_signature(self, msg_signature: str, timestamp: str, nonce: str, value: str) -> None:
        expected = _sha1_signature(self.token, timestamp, nonce, value)
        if expected != msg_signature:
            raise WeComCryptoError("msg_signature mismatch")

    def decrypt(self, msg_signature: str, timestamp: str, nonce: str, encrypted: str) -> str:
        self.verify_signature(msg_signature, timestamp, nonce, encrypted)

        cipher = AES.new(self.aes_key, AES.MODE_CBC, self.iv)
        decrypted = cipher.decrypt(base64.b64decode(encrypted))
        decrypted = _pkcs7_unpad(decrypted)

        content = decrypted[16:]
        msg_len = socket.ntohl(struct.unpack("I", content[:4])[0])
        msg = content[4 : 4 + msg_len]
        receive_id = content[4 + msg_len :].decode("utf-8")
        if receive_id != self.receive_id:
            raise WeComCryptoError("receive_id mismatch")
        return msg.decode("utf-8")

    def encrypt(self, plaintext: str, nonce: str, timestamp: str) -> str:
        raw = (
            os.urandom(16)
            + struct.pack("I", socket.htonl(len(plaintext.encode("utf-8"))))
            + plaintext.encode("utf-8")
            + self.receive_id.encode("utf-8")
        )
        cipher = AES.new(self.aes_key, AES.MODE_CBC, self.iv)
        encrypted = base64.b64encode(cipher.encrypt(_pkcs7_pad(raw))).decode("utf-8")
        signature = _sha1_signature(self.token, timestamp, nonce, encrypted)
        return (
            "<xml>"
            f"<Encrypt><![CDATA[{encrypted}]]></Encrypt>"
            f"<MsgSignature><![CDATA[{signature}]]></MsgSignature>"
            f"<TimeStamp>{timestamp}</TimeStamp>"
            f"<Nonce><![CDATA[{nonce}]]></Nonce>"
            "</xml>"
        )
