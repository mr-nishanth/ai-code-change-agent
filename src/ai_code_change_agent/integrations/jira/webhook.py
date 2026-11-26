"""Jira Cloud admin-webhook verifier: `X-Hub-Signature: sha256=<hex>` (HMAC-SHA256, raw body).

Confirmed deployment: Jira Cloud, System Administration -> WebHooks, secret configured.
Only sha256 is accepted; any other method fails closed.
"""

import hashlib
import hmac
import re
from collections.abc import Mapping

from ai_code_change_agent.domain.webhooks.models import RejectionCode
from ai_code_change_agent.domain.webhooks.ports import VerificationResult

SIGNATURE_HEADER = "x-hub-signature"
MIN_SECRET_LENGTH = 16
_SHA256_HEX = re.compile(r"[0-9a-fA-F]{64}")


class JiraCloudSignatureVerifier:
    def __init__(self, secret: str) -> None:
        if len(secret) < MIN_SECRET_LENGTH:
            msg = f"webhook secret must be at least {MIN_SECRET_LENGTH} characters"
            raise ValueError(msg)
        self._key = secret.encode()

    def __repr__(self) -> str:
        return "JiraCloudSignatureVerifier()"

    def verify(self, raw_body: bytes, headers: Mapping[str, str]) -> VerificationResult:
        header = headers.get(SIGNATURE_HEADER)
        if not header:
            return self._fail(RejectionCode.SIGNATURE_MISSING)
        separator = header.find("=")
        if separator <= 0:
            return self._fail(RejectionCode.SIGNATURE_MALFORMED)
        if header[:separator].lower() != "sha256":
            return self._fail(RejectionCode.SIGNATURE_UNSUPPORTED_METHOD)
        provided = header[separator + 1 :]
        if not _SHA256_HEX.fullmatch(provided):
            return self._fail(RejectionCode.SIGNATURE_MALFORMED)
        expected = hmac.new(self._key, raw_body, hashlib.sha256).digest()
        if hmac.compare_digest(expected, bytes.fromhex(provided)):
            return VerificationResult(ok=True)
        return self._fail(RejectionCode.SIGNATURE_MISMATCH)

    @staticmethod
    def _fail(code: RejectionCode) -> VerificationResult:
        return VerificationResult(ok=False, failure=code)
